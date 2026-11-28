"""Unit/ASGI tests for ``/api/tools`` and ``/api/skills`` (in-memory DB store, no live Postgres required)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from yggdrasil.admin import common as admin_common
from yggdrasil.admin import settings_api, tools_api
from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt
from yggdrasil.db.models import Channel, Guild, Provider, SearchProvider, SettingsDoc, TaskRoute
from yggdrasil.runtime import GMOD_SERVERS_KEY, TOOL_SECRETS_KEY, ToolSecretInfo
from yggdrasil.tools.azuriom import AzuriomAuthError
from yggdrasil.tools.provenance import ProvenanceReport, ProvenanceSignal


class _FakeScalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)


class _FakeSession:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    async def get(self, model: type[Any], key: Any) -> Any:
        if model is SettingsDoc:
            return self._store["settings"].get(key)
        if model is Guild:
            return self._store["guilds"].get(key)
        if model is Channel:
            return self._store["channels"].get(key)
        return None

    async def scalars(self, stmt: Any) -> _FakeScalars:
        text = str(stmt)
        if "FROM providers" in text:
            return _FakeScalars(self._store["providers"])
        if "FROM search_providers" in text:
            return _FakeScalars(self._store["search_providers"])
        if "FROM task_routes" in text:
            return _FakeScalars(self._store["routes"])
        return _FakeScalars([])

    async def scalar(self, stmt: Any) -> Any:
        text = str(stmt)
        if "FROM embed_spaces" in text:
            return self._store["embed_id"]
        return None

    def add(self, obj: Any) -> None:
        if isinstance(obj, SettingsDoc):
            self._store["settings"][obj.key] = obj
        else:
            self._store["audits"].append(obj)


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store: dict[str, Any] = {
        "settings": {},
        "guilds": {},
        "channels": {},
        "providers": [
            Provider(
                id=1,
                name="OpenAI Official",
                api_format="openai",
                base_url="https://api.openai.com/v1",
                api_key_enc=encrypt("sk-openai-12345678"),
                headers={},
                enabled=True,
                models=["gpt-5"],
                model_caps={},
            ),
            Provider(
                id=2,
                name="OpenRouter",
                api_format="openai",
                base_url="https://openrouter.ai/api/v1",
                api_key_enc=encrypt("sk-or-99998888"),
                headers={},
                enabled=True,
                models=["openai/gpt-5"],
                model_caps={},
            ),
        ],
        "search_providers": [
            SearchProvider(
                id=1,
                name="Exa",
                kind="exa",
                base_url="https://api.exa.ai",
                api_key_enc=encrypt("exa-key-1234"),
                enabled=True,
                priority=10,
                monthly_limit=1000,
                params={},
            )
        ],
        "routes": [
            TaskRoute(task="chat", provider_id=1, model="gpt-5", params={}, limits={}),
        ],
        "embed_id": 1,
        "audits": [],
    }

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession(store)

    monkeypatch.setattr(tools_api, "get_session", _session)
    monkeypatch.setattr(settings_api, "get_session", _session)
    monkeypatch.setattr(admin_common, "get_session", _session)
    monkeypatch.setattr(APP_CONFIG, "openai_api_key", "")
    return store


async def test_tools_overview_and_per_tool_config_updates(
    client: httpx.AsyncClient, fake_db: dict[str, Any], published: list[str]
) -> None:
    resp = await client.get("/api/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = {i["id"]: i for i in body["items"]}
    assert len(items) == 20
    assert items["check_provenance"]["status"] == "ready"
    assert "OpenAI Official" in items["check_provenance"]["status_label"]
    assert items["check_provenance"]["provider_param_key"] == "provenance_provider"
    assert any(f["key"] == "provenance_timeout_s" for f in items["check_provenance"]["param_fields"])
    assert items["web_search"]["status"] == "ready"
    assert items["generate_image"]["enabled"] is False

    # Update read_url params sparsely
    put_resp = await client.put(
        "/api/tools/read_url",
        json={"params": {"read_url_max_chars": 24000, "browser_fallback": False}, "slow": False},
    )
    assert put_resp.status_code == 200, put_resp.text
    updated = {i["id"]: i for i in put_resp.json()["items"]}
    assert updated["read_url"]["params"]["read_url_max_chars"] == 24000
    assert updated["read_url"]["params"]["browser_fallback"] is False
    assert updated["read_url"]["slow"] is False
    assert fake_db["settings"]["global"].value["tools"]["read_url_max_chars"] == 24000
    assert published

    # Reject unknown param for tool
    bad = await client.put("/api/tools/web_search", json={"params": {"file_max_mb": 50}})
    assert bad.status_code == 422


@pytest.mark.parametrize("enabled_count", [None, 0, 2])
async def test_gmod_queue_tools_diagnostics(
    client: httpx.AsyncClient, fake_db: dict[str, Any], enabled_count: int | None
) -> None:
    if enabled_count is not None:
        servers = [{"id": "disabled", "enabled": False}, *({"id": f"server{i}"} for i in range(enabled_count))]
        fake_db["settings"][GMOD_SERVERS_KEY] = SettingsDoc(key=GMOD_SERVERS_KEY, value={"servers": servers})

    resp = await client.get("/api/tools")
    assert resp.status_code == 200, resp.text
    tools = {item["id"]: item for item in resp.json()["items"] if item["category"] == "gmod"}
    assert set(tools) == {"gmod_queue_lookup", "gmod_queue_list"}
    for item in tools.values():
        assert item["enabled"] is True
        assert item["handlers"] == ["chat"]
        if enabled_count:
            assert item["status"] == "ready"
            assert f"已启用 {enabled_count} 台" in item["status_detail"]
        else:
            assert item["status"] == "unconfigured"
            assert "GMod 通信" in item["status_detail"]


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ({}, "未启用独立凭据"),
        ({"enabled": True}, "缺少网站地址"),
        ({"enabled": True, "base_url": "http://azuriom"}, "缺少 API Token"),
    ],
)
async def test_azuriom_tool_diagnostics(
    client: httpx.AsyncClient, fake_db: dict[str, Any], secret: dict[str, Any], message: str
) -> None:
    fake_db["settings"][TOOL_SECRETS_KEY] = SettingsDoc(key=TOOL_SECRETS_KEY, value={"azuriom_lookup": secret})
    overview = await client.get("/api/tools")
    assert overview.status_code == 200, overview.text
    item = next(i for i in overview.json()["items"] if i["id"] == "azuriom_lookup")
    assert item["category"] == "azuriom"
    assert item["handlers"] == ["chat"]
    assert item["status"] == "unconfigured"
    assert message in item["status_label"]
    probe = await client.post("/api/tools/azuriom/test")
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is False
    assert message in probe.json()["error"]


async def test_azuriom_encrypted_secret_and_online_test(
    client: httpx.AsyncClient, fake_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "bridge-token-9876"
    response = await client.put(
        "/api/tools/azuriom_lookup",
        json={
            "params": {"azuriom_timeout_s": 37},
            "secret": {
                "enabled": True,
                "base_url": "http://azuriom:8080",
                "api_key": token,
                "headers": {"x-test": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    item = next(i for i in response.json()["items"] if i["id"] == "azuriom_lookup")
    assert item["status"] == "ready"
    assert item["params"]["azuriom_timeout_s"] == 37
    assert item["secret"]["api_key_masked"].endswith("9876")
    assert token not in response.text
    saved = fake_db["settings"][TOOL_SECRETS_KEY].value["azuriom_lookup"]["api_key_enc"]
    assert token != saved
    assert decrypt(saved.encode()) == token
    assert token not in str(fake_db["settings"]["global"].value)
    assert all(token not in str(audit.detail) for audit in fake_db["audits"])

    expected = {
        "ok": True,
        "latency_ms": 14,
        "endpoint": "http://azuriom:8080/api/ygg-bridge/v1/health",
        "plugin": "ygg-bridge",
        "version": "1.0.0",
        "game": "steam",
        "kook_accounts_ready": False,
        "kook_account_model": True,
        "error": None,
    }

    async def healthy(*, secret: ToolSecretInfo, timeout_s: float) -> dict[str, Any]:
        assert secret == ToolSecretInfo(True, "http://azuriom:8080", token, {"x-test": "1"})
        assert timeout_s == 37
        return expected

    monkeypatch.setattr(tools_api, "health_check", healthy)
    probe = await client.post("/api/tools/azuriom/test")
    assert probe.status_code == 200, probe.text
    assert probe.json() == expected

    async def unauthorized(**kwargs: Any) -> dict[str, Any]:
        raise AzuriomAuthError("Ygg Bridge 鉴权失败（401）")

    monkeypatch.setattr(tools_api, "health_check", unauthorized)
    probe = await client.post("/api/tools/azuriom/test")
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is False
    assert "鉴权失败" in probe.json()["error"]


async def test_scope_role_defaults_follow_parent_after_sparse_edit(
    client: httpx.AsyncClient, fake_db: dict[str, Any]
) -> None:
    guild = Guild(
        guild_id="g",
        name="服务器",
        settings={
            "access": {"role_rules": {"10": {"azuriom_lookup_others": True}, "30": {"azuriom_lookup_others": True}}}
        },
    )
    category = Channel(
        channel_id="cat",
        guild_id="g",
        is_category=True,
        name="分组",
        settings={"access": {"role_rules": {"10": {"bypass_channel_privacy": True}}}},
    )
    channel = Channel(
        channel_id="ch",
        guild_id="g",
        parent_id="cat",
        is_category=False,
        name="频道",
        settings={"access": {"role_rules": {"20": {"tools_deny": ["web_search"]}}}},
    )
    fake_db["guilds"]["g"] = guild
    fake_db["channels"].update({"cat": category, "ch": channel})
    path = "/api/settings/scopes/channel/ch"
    view = await client.get(path)
    assert view.status_code == 200, view.text
    defaults = view.json()["defaults"]["access"]["role_rules"]
    assert defaults["10"]["azuriom_lookup_others"] is True
    assert defaults["10"]["bypass_channel_privacy"] is True
    assert "20" not in defaults
    assert view.json()["effective"]["access"]["role_rules"]["10"]["azuriom_lookup_others"] is True

    overrides = {
        "access": {"role_rules": {"20": {"tools_deny": ["web_search"]}, "10": {"azuriom_lookup_others": False}}}
    }
    saved = await client.put(path, json=overrides)
    assert saved.status_code == 200, saved.text
    assert channel.settings == overrides
    effective = saved.json()["effective"]["access"]["role_rules"]
    assert effective["10"]["azuriom_lookup_others"] is False
    assert effective["10"]["bypass_channel_privacy"] is True
    assert effective["30"]["azuriom_lookup_others"] is True

    guild.settings = {}
    revoked = await client.get(path)
    assert revoked.status_code == 200, revoked.text
    assert "30" not in revoked.json()["effective"]["access"]["role_rules"]
    assert revoked.json()["defaults"]["access"]["role_rules"]["10"]["azuriom_lookup_others"] is False


async def test_check_provenance_standalone_encrypted_secret_and_online_test(
    client: httpx.AsyncClient, fake_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Configure standalone encrypted secret for check_provenance
    resp = await client.put(
        "/api/tools/check_provenance",
        json={
            "params": {"provenance_provider": "", "provenance_timeout_s": 45, "provenance_max_mb": 20},
            "secret": {
                "enabled": True,
                "base_url": "https://proxy.example.com/v1",
                "api_key": "sk-secret-prov-9876",
                "headers": {"X-Custom": "1"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    prov = next(i for i in resp.json()["items"] if i["id"] == "check_provenance")
    assert prov["status"] == "ready"
    assert prov["status_label"] == "独立凭据"
    assert prov["secret"]["enabled"] is True
    assert prov["secret"]["base_url"] == "https://proxy.example.com/v1"
    assert prov["secret"]["api_key_masked"].endswith("9876")
    assert "sk-secret-prov-9876" not in str(prov)

    # Confirm encrypted at rest in tool.secrets and never leaked into global settings
    raw_secret = fake_db["settings"]["tool.secrets"].value["check_provenance"]
    assert raw_secret["api_key_enc"] != "sk-secret-prov-9876"
    assert decrypt(raw_secret["api_key_enc"].encode()) == "sk-secret-prov-9876"
    assert "sk-secret-prov-9876" not in str(fake_db["settings"]["global"].value)

    # Test online probe endpoint (both 1x1 synthetic probe and custom uploaded base64 payload)
    seen: list[tuple[str, float, int, str]] = []

    async def _fake_check(data: bytes, filename: str, **kw: Any) -> ProvenanceReport:
        seen.append((filename, kw["timeout_s"], kw["max_bytes"], kw["secret"].api_key))
        return ProvenanceReport(
            created_at=1_740_000_000,
            results=(ProvenanceSignal(type="c2pa", outcome="detected", issuer="OpenAI"),),
        )

    monkeypatch.setattr(tools_api, "check_content_provenance", _fake_check)

    probe_resp = await client.post("/api/tools/check_provenance/test", json={})
    assert probe_resp.status_code == 200, probe_resp.text
    probe = probe_resp.json()
    assert probe["ok"] is True
    assert probe["any_detected"] is True
    assert probe["filename"] == "probe-1x1.png"
    assert "C2PA 内容凭证：已检测到" in probe["report"]
    assert seen[-1] == ("probe-1x1.png", 45.0, 20 * 1024 * 1024, "sk-secret-prov-9876")

    custom_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n-fake").decode()
    upload_resp = await client.post(
        "/api/tools/check_provenance/test",
        json={"file_b64": custom_b64, "filename": "check.png", "mime": "image/png"},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    assert upload_resp.json()["filename"] == "check.png"


async def test_skills_builtin_and_custom_crud(
    client: httpx.AsyncClient, fake_db: dict[str, Any], published: list[str]
) -> None:
    # 1. Overview shows built-in skills and empty custom list
    resp = await client.get("/api/skills")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [b["id"] for b in data["builtin"]] == ["kook", "stickers"]
    assert data["custom"] == []

    # 2. Update built-in kook skill params & extra instructions
    upd_b = await client.put(
        "/api/skills/builtin/kook",
        json={"params": {"kook_upload_max_mb": 15, "kook_pinned_shown": 8}, "extra_instructions": "保持简洁。"},
    )
    assert upd_b.status_code == 200, upd_b.text
    kook_item = next(b for b in upd_b.json()["builtin"] if b["id"] == "kook")
    assert kook_item["params"]["kook_upload_max_mb"] == 15
    assert kook_item["extra_instructions"] == "保持简洁。"

    # 3. Create custom skill
    create_resp = await client.post(
        "/api/skills/custom",
        json={
            "id": "server-faq",
            "name": "服务器 FAQ",
            "description": "群友询问服务器规则时加载",
            "instructions": "1. 文明发言\n2. 遵守版规",
            "defer_loading": True,
            "handlers": ["chat"],
            "allow_private": True,
            "enabled": True,
            "default_active": True,
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    assert create_resp.json()["id"] == "server-faq"

    # Duplicate or builtin-colliding or invalid IDs are rejected
    dup = await client.post("/api/skills/custom", json=create_resp.json())
    assert dup.status_code == 409
    clash = await client.post("/api/skills/custom", json={**create_resp.json(), "id": "kook"})
    assert clash.status_code == 409
    bad_id = await client.post("/api/skills/custom", json={**create_resp.json(), "id": "bad:id"})
    assert bad_id.status_code == 422

    # 4. Update custom skill
    edit_resp = await client.put(
        "/api/skills/custom/server-faq",
        json={
            "name": "服务器 FAQ v2",
            "description": "规则与指引",
            "instructions": "更新后的正文",
            "defer_loading": False,
            "handlers": ["chat", "tldr"],
            "allow_private": False,
            "enabled": True,
            "default_active": False,
        },
    )
    assert edit_resp.status_code == 200, edit_resp.text
    assert edit_resp.json()["defer_loading"] is False
    assert edit_resp.json()["default_active"] is False

    # 5. Delete custom skill
    del_resp = await client.delete("/api/skills/custom/server-faq")
    assert del_resp.status_code == 200
    after = await client.get("/api/skills")
    assert after.json()["custom"] == []
    assert len(published) >= 4


async def test_ops_tools_overview_and_role_rules_api(
    client: httpx.AsyncClient, fake_db: dict[str, Any], published: list[str]
) -> None:
    resp = await client.get("/api/tools/ops")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["tools"]) >= 20
    gmod = {item["id"]: item for item in body["tools"] if item["category"] == "gmod"}
    assert gmod.keys() == {
        "gmod_servers",
        "gmod_ping",
        "gmod_queue_lookup",
        "gmod_queue_list",
        "gmod_queue_singlepass",
        "gmod_queue_letin",
    }
    for tool_id, item in gmod.items():
        assert item["global_only"] is False
        assert item["write"] == (tool_id in {"gmod_queue_singlepass", "gmod_queue_letin"})
    assert "/guild/leave" in body["never_allowed"]
    assert body["role_rules"] == {}

    # Reject forbidden high-risk KOOK paths
    bad = await client.put("/api/tools/ops", json={"kook_write_actions": ["/guild/leave"]})
    assert bad.status_code == 422

    # Save global role_rules and trimmed kook_write_actions
    upd = await client.put(
        "/api/tools/ops",
        json={
            "kook_write_actions": ["/message/pin", "/message/unpin"],
            "role_rules": {
                "10086": {
                    "tools": ["status", "kook_api", "decide_review", "gmod_queue_singlepass", "gmod_queue_letin"],
                    "kook_write_actions": ["/message/pin"],
                    "guild_only": True,
                }
            },
        },
    )
    assert upd.status_code == 200, upd.text
    updated = upd.json()
    assert updated["kook_write_actions"] == ["/message/pin", "/message/unpin"]
    assert updated["role_rules"]["10086"]["tools"] == [
        "status",
        "kook_api",
        "decide_review",
        "gmod_queue_singlepass",
        "gmod_queue_letin",
    ]
    assert fake_db["settings"]["global"].value["ops"]["role_rules"]["10086"]["guild_only"] is True
    assert published
