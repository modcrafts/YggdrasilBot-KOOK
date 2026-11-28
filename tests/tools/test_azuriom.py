"""Ygg Bridge response contract and chat/ops authorization."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from tests.support.kook import FakeKook
from yggdrasil.agent import bridge
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.tools import ALL_TOOLS
from yggdrasil.agent.tools import azuriom as chat_tools
from yggdrasil.agent.tools import gmod as queue_tools
from yggdrasil.ops import tools as ops
from yggdrasil.runtime import ChannelInfo, Snapshot, ToolSecretInfo
from yggdrasil.settings import RoleAccessRule, Settings
from yggdrasil.tools import azuriom, gmod_queue

_PREFIX = "/api/ygg-bridge/v1"
_BASE = "http://azuriom:8080"
_FIRST = ("STEAM_0:0:23739475", "76561198007744678", "[U:1:47478950]")
_SECOND = ("STEAM_0:0:26039975", "76561198012345678", "[U:1:52079950]")
_TIMESTAMP = "2026-11-28T15:30:00+08:00"
_HEALTH = {
    "ok": True,
    "plugin": "ygg-bridge",
    "version": "1.0.0",
    "game": "steam",
    "kook_accounts_ready": True,
    "kook_account_model": True,
    "server_time": _TIMESTAMP,
}
_KOOK = {"linked": True, "user_id": "123456", "username": "用户", "linked_at": _TIMESTAMP}
_USER = {
    "id": 42,
    "name": "站点用户",
    "role": {"id": 2, "name": "玩家", "is_admin": False},
    "is_banned": False,
    "created_at": _TIMESTAMP,
}
_SECRET = ToolSecretInfo(
    enabled=True,
    base_url=f"{_BASE}{_PREFIX}/",
    api_key="bridge-test-token",
    headers={"aCcEpT": "text/html", "authorization": "other", "X-Custom": "extra"},
)


@pytest.fixture
def mock_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], list[httpx.Request]]:
    real_client = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        def client(**kwargs: Any) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(record), **kwargs)

        monkeypatch.setattr(azuriom.httpx, "AsyncClient", client)
        return requests

    return install


@pytest.mark.parametrize("expected", [_FIRST, _SECOND])
def test_normalize_steam_identity(expected: tuple[str, str, str]) -> None:
    steamid2, steamid64, _ = expected
    for value in (steamid2, steamid2.replace("STEAM_0", "STEAM_1"), steamid64):
        assert azuriom.normalize_steam_identity(f" {value} ") == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("STEAM_1:1:2147483647", ("STEAM_0:1:2147483647", "76561202255233023", "[U:1:4294967295]")),
        ("76561197960265729", ("STEAM_0:1:0", "76561197960265729", "[U:1:1]")),
    ],
)
def test_normalize_steam_boundaries(raw: str, expected: tuple[str, str, str]) -> None:
    assert azuriom.normalize_steam_identity(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "STEAM_2:0:123",
        "STEAM_0:2:123",
        "STEAM_0:1:-1",
        "76561202255233024",
        "7656119796026572",
        "abc",
        "76561197960265728",
        "STEAM_0:0:0",
    ],
)
def test_normalize_invalid_steam_identity(raw: str) -> None:
    with pytest.raises(ValueError, match="SteamID64"):
        azuriom.normalize_steam_identity(raw)


@pytest.mark.parametrize("raw", [_FIRST[2], _FIRST[2].strip("[]"), _SECOND[2], _SECOND[2].strip("[]")])
async def test_steamid3_rejected_locally(
    raw: str, snapshot: Callable[..., Snapshot], mock_bridge: Callable[..., list[httpx.Request]]
) -> None:
    snapshot(tool_secrets={"azuriom_lookup": _SECRET})
    requests = mock_bridge(lambda req: httpx.Response(422, json={"ok": False, "error": "invalid_steam_id"}))
    with pytest.raises(ValueError, match="SteamID64"):
        await azuriom.lookup_by_steam(raw)
    assert requests == []


@pytest.mark.parametrize("raw", ["123456", "  (met)123456(met)  "])
def test_normalize_kook_user_id(raw: str) -> None:
    assert azuriom.normalize_kook_user_id(raw) == "123456"
    assert azuriom.normalize_kook_user_id("9" * 32) == "9" * 32


@pytest.mark.parametrize("raw", ["", "１２３", "9" * 33, "(met)123", "1/2", "-123"])
def test_normalize_invalid_kook_user_id(raw: str) -> None:
    with pytest.raises(ValueError, match="KOOK 用户 ID"):
        azuriom.normalize_kook_user_id(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" https://example.com/// ", "https://example.com"),
        (f"{_BASE}{_PREFIX}/", _BASE),
        ("http://172.18.0.1:8080", "http://172.18.0.1:8080"),
        (f"http://[::1]:8080{_PREFIX}", "http://[::1]:8080"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert azuriom.normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://example.com", "https:///", "https://bad host", "http://example.com:bogus"])
def test_normalize_invalid_base_url(raw: str) -> None:
    with pytest.raises(azuriom.AzuriomConfigError, match="网站地址无效"):
        azuriom.normalize_base_url(raw)


async def test_health(
    mock_bridge: Callable[..., list[httpx.Request]],
) -> None:
    requests = mock_bridge(lambda req: httpx.Response(200, json=_HEALTH))
    result = await azuriom.health_check(secret=_SECRET, timeout_s=18)
    endpoint = f"{_BASE}{_PREFIX}/health"
    latency_ms = result.pop("latency_ms")
    assert isinstance(latency_ms, int)
    assert latency_ms >= 0
    assert result == {**_HEALTH, "endpoint": endpoint}
    (request,) = requests
    assert request.method == "GET"
    assert str(request.url) == endpoint
    assert request.headers.get_list("Authorization") == ["Bearer bridge-test-token"]
    assert request.headers.get_list("Accept") == ["application/json"]
    assert request.headers["X-Custom"] == "extra"
    assert request.extensions["timeout"]["read"] == 18


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        (None, "未启用独立凭据"),
        (ToolSecretInfo(), "未启用独立凭据"),
        (ToolSecretInfo(enabled=True, api_key="token"), "缺少网站地址"),
        (ToolSecretInfo(enabled=True, base_url=_BASE, api_key=" "), "缺少 API Token"),
    ],
)
async def test_secret_required(
    secret: ToolSecretInfo | None,
    message: str,
    snapshot: Callable[..., Snapshot],
    mock_bridge: Callable[..., list[httpx.Request]],
) -> None:
    snapshot()
    requests = mock_bridge(lambda req: httpx.Response(200, json=_HEALTH))
    with pytest.raises(azuriom.AzuriomConfigError, match=message):
        await azuriom.health_check(secret=secret)
    assert requests == []


@pytest.mark.parametrize(
    ("status", "code", "error", "message"),
    [
        (401, "unauthorized", azuriom.AzuriomAuthError, "鉴权失败"),
        (403, "plugin_disabled", azuriom.AzuriomAuthError, "插件未启用"),
        (403, "forbidden_ip", azuriom.AzuriomAuthError, "IP 白名单"),
        (422, "invalid_kook_user_id", azuriom.AzuriomError, "KOOK 用户 ID 无效"),
        (422, "invalid_steam_id", azuriom.AzuriomError, "SteamID 无效"),
        (502, "", azuriom.AzuriomError, "502"),
        (200, "", azuriom.AzuriomError, "无效的 JSON"),
    ],
)
async def test_bridge_http_errors(
    status: int,
    code: str,
    error: type[azuriom.AzuriomError],
    message: str,
    mock_bridge: Callable[..., list[httpx.Request]],
) -> None:
    mock_bridge(lambda req: httpx.Response(status, json={"ok": False, "error": code, "message": _SECRET.api_key}))
    with pytest.raises(error, match=message) as raised:
        await azuriom.health_check(secret=_SECRET)
    assert "bridge-test-token" not in str(raised.value)


def _steam_fields(identity: tuple[str, str, str]) -> dict[str, Any]:
    steamid2, steamid64, steamid3 = identity
    return {"steamid64": steamid64, "steamid2": steamid2, "steamid3": steamid3}


@pytest.mark.parametrize(
    ("by_steam", "found", "both_linked"),
    [
        (False, True, True),
        (False, False, False),
        (False, True, False),
        (True, True, True),
        (True, True, False),
        (True, False, False),
    ],
)
async def test_identity_response_contract(
    by_steam: bool,
    found: bool,
    both_linked: bool,
    snapshot: Callable[..., Snapshot],
    mock_bridge: Callable[..., list[httpx.Request]],
) -> None:
    snapshot(tool_secrets={"azuriom_lookup": _SECRET})
    if by_steam:
        query = {"type": "steam", "input": _SECOND[1], **_steam_fields(_SECOND)}
        path = f"/identity/by-steam/{_SECOND[1]}"
    else:
        query = {"type": "kook", "value": "123456"}
        path = "/identity/by-kook/123456"
    payload: dict[str, Any] = {"ok": True, "found": found, "query": query}
    if found:
        payload.update(
            user=_USER,
            kook=_KOOK
            if both_linked or not by_steam
            else {
                "linked": False,
                "user_id": None,
                "username": None,
                "linked_at": None,
            },
            steam={"linked": True, **_steam_fields(_SECOND if by_steam else _FIRST)}
            if both_linked or by_steam
            else {
                "linked": False,
                "steamid64": None,
                "steamid2": None,
                "steamid3": None,
            },
        )
    else:
        payload.update(reason="steam_not_found" if by_steam else "kook_not_linked", kook=None, user=None, steam=None)
    requests = mock_bridge(lambda req: httpx.Response(200, json=payload))

    if by_steam:
        result = await azuriom.lookup_by_steam(_SECOND[0])
    else:
        result = await azuriom.lookup_by_kook("(met)123456(met)")
    assert result == payload
    assert [req.url.path for req in requests] == [_PREFIX + path]


@pytest.mark.parametrize("grant", ["member", "role", "superuser"])
async def test_chat_lookup_permission(
    grant: str, snapshot: Callable[..., Snapshot], mock_bridge: Callable[..., list[httpx.Request]]
) -> None:
    snapshot(tool_secrets={"azuriom_lookup": _SECRET}, superusers=frozenset({"123456"}) if grant == "superuser" else ())
    settings = Settings()
    settings.tools.azuriom_timeout_s = 17
    settings.access.role_rules["allowed"] = RoleAccessRule(azuriom_lookup_others=True)
    settings.access.role_rules["other"] = RoleAccessRule(bypass_channel_privacy=True)
    roles = ("other", "allowed") if grant == "role" else ("other",)
    deps = Deps(FakeKook(), "ch-1", "g-1", "123456", settings, Handles(), "chat", role_ids=roles)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    requests = mock_bridge(lambda req: httpx.Response(200, json={"ok": True, "found": False}))

    for args in ({}, {"kook_user_id": "123456"}, {"kook_user_id": "(met)123456(met)"}):
        out = await chat_tools.azuriom_lookup(ctx, **args)
        assert json.loads(str(out))["ok"] is True
    assert [req.url.path for req in requests] == [f"{_PREFIX}/identity/by-kook/123456"] * 3
    assert all(req.extensions["timeout"]["read"] == 17 for req in requests)
    requests.clear()

    assert "请只传入" in str(await chat_tools.azuriom_lookup(ctx, kook_user_id="654321", steamid=_FIRST[0]))
    assert requests == []
    by_kook = await chat_tools.azuriom_lookup(ctx, kook_user_id="(met)654321(met)")
    by_steam = await chat_tools.azuriom_lookup(ctx, steamid=_FIRST[1])
    if grant == "member":
        assert "只能查询自己本人" in str(by_kook)
        assert "不允许通过 SteamID" in str(by_steam)
        assert requests == []
    else:
        assert json.loads(str(by_kook))["ok"] is True
        assert json.loads(str(by_steam))["ok"] is True
        assert [req.url.path for req in requests] == [
            f"{_PREFIX}/identity/by-kook/654321",
            f"{_PREFIX}/identity/by-steam/{_FIRST[1]}",
        ]

    assert ALL_TOOLS["azuriom_lookup"] is chat_tools.azuriom_lookup
    assert "azuriom_lookup" not in bridge.TOOLS


async def test_self_binding_reused_by_queue_only_within_invocation(
    snapshot: Callable[..., Snapshot], mock_bridge: Callable[..., list[httpx.Request]], monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot(tool_secrets={"azuriom_lookup": _SECRET}, superusers=frozenset({"123456"}))

    def new_message() -> RunContext[Deps]:
        deps = Deps(FakeKook(), "ch-1", "g-1", "123456", Settings(), Handles(), "chat")
        return RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    linked = True

    def respond(req: httpx.Request) -> httpx.Response:
        user_id = req.url.path.rsplit("/", 1)[1]
        steam = _FIRST if user_id == "123456" else _SECOND
        return httpx.Response(
            200,
            json={
                "ok": True,
                "found": linked,
                "kook": {**_KOOK, "user_id": user_id},
                "steam": {"linked": linked, **_steam_fields(steam)},
            },
        )

    async def queue(server_id: str | None) -> dict[str, Any]:
        return {
            "active_tickets": [
                {"steamid64": ident[1], "kind": "pass", "remaining_s": 12} for ident in (_FIRST, _SECOND)
            ]
        }

    requests = mock_bridge(respond)
    monkeypatch.setattr(gmod_queue, "list_queue", queue)
    ctx = new_message()
    await chat_tools.azuriom_lookup(ctx, kook_user_id="654321")
    await chat_tools.azuriom_lookup(ctx, kook_user_id="(met)123456(met)")
    tickets = await queue_tools.gmod_queue_list(ctx, view="tickets")
    assert json.loads(str(tickets))["rows"] == [[_FIRST[1], "pass", 12]]
    assert [req.url.path for req in requests] == [f"{_PREFIX}/identity/by-kook/{uid}" for uid in ("654321", "123456")]

    linked = False
    tickets = await queue_tools.gmod_queue_list(new_message(), view="tickets")
    assert "无法确认调用者" in str(tickets)
    assert len(requests) == 3
    assert requests[-1].url.path == f"{_PREFIX}/identity/by-kook/123456"


async def test_concurrent_self_lookups_share_one_verified_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    payload = {"found": True, "kook": _KOOK, "steam": {"linked": True, **_steam_fields(_FIRST)}}

    async def lookup(caller_id: str, *, timeout_s: float) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return payload

    monkeypatch.setattr(azuriom, "lookup_by_kook", lookup)
    cache = azuriom.CallerBindingCache()
    results = await asyncio.gather(*(cache.lookup("123456", timeout_s=10) for _ in range(2)))
    assert calls == 1
    assert results == [(payload, _FIRST[1])] * 2


async def test_ops_lookup_and_health(
    snapshot: Callable[..., Snapshot], mock_bridge: Callable[..., list[httpx.Request]]
) -> None:
    snapshot(
        tool_secrets={"azuriom_lookup": _SECRET},
        global_settings_doc={"tools": {"azuriom_timeout_s": 15}},
        channels={
            "ch-1": ChannelInfo("ch-1", "g-1", None, False, 0, "general", True, {"tools": {"azuriom_timeout_s": 21}}, 1)
        },
    )
    grant = ops.OpsGrant(False, frozenset({"azuriom_lookup", "azuriom_health"}), frozenset(), True)
    deps = ops.OpsDeps(FakeKook(), "123456", "ch-1", "g-1", grant=grant)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/health"):
            return httpx.Response(200, json=_HEALTH)
        return httpx.Response(200, json={"ok": True, "found": True})

    requests = mock_bridge(respond)
    assert "必须且只能" in await ops.azuriom_lookup(ctx)
    assert "必须且只能" in await ops.azuriom_lookup(ctx, kook_user_id="654321", steamid=_FIRST[0])
    assert requests == []

    assert json.loads(await ops.azuriom_lookup(ctx, kook_user_id="654321"))["found"] is True
    assert json.loads(await ops.azuriom_lookup(ctx, steamid=_SECOND[0]))["found"] is True
    health = json.loads(await ops.azuriom_health(ctx))
    assert health["version"] == "1.0.0"
    assert health["kook_accounts_ready"] is True
    assert health["latency_ms"] >= 0
    assert health["endpoint"] == f"{_BASE}{_PREFIX}/health"
    assert [req.url.path for req in requests] == [
        f"{_PREFIX}/identity/by-kook/654321",
        f"{_PREFIX}/identity/by-steam/{_SECOND[1]}",
        f"{_PREFIX}/health",
    ]
    assert all(req.extensions["timeout"]["read"] == 21 for req in requests)
    assert deps.actions == []
