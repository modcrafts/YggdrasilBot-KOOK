"""NCG queue RPC contract, chat tools, and ops permissions and audit."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, get_args

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.tools import ALL_TOOLS, tool_names, tools_for
from yggdrasil.agent.tools import gmod as chat_tools
from yggdrasil.agent.tools.base import ToolResult
from yggdrasil.db.models import ConfigAudit
from yggdrasil.gmod import GModError, GModServerNotFound, bridge, normalize_steamid, resolve_server_id
from yggdrasil.gmod.server import build_gmod_app
from yggdrasil.ops import tools as ops
from yggdrasil.runtime import GModServerInfo, Snapshot
from yggdrasil.settings import OpsToolName, Settings, ToolName
from yggdrasil.tools import gmod_queue

SID = "STEAM_0:0:146595760"
SID64 = "76561198253457248"
MAIN = GModServerInfo("main", "主服", token="test-token")
SURF = GModServerInfo("surf", "Surf Server")
OFF = GModServerInfo("off", "停用服", enabled=False)

ENTRY = {
    "steamid": SID,
    "steamid64": SID64,
    "nick": "RAY",
    "position": 2,
    "tier": "regular",
    "tier_position": 1,
    "priority": False,
    "wait_s": 135,
    "queue_seq": 42,
    "pending_letin": False,
    "has_ticket": False,
}
SUMMARY = {"players": 78, "max_slots": 80, "total_queued": 2}
TICKET = {"kind": "admin_singlepass", "remaining_s": 285}


@pytest.fixture
def linked_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    async def binding(kook_id: str, *, timeout_s: float) -> dict[str, Any]:
        assert kook_id == "123"
        return {
            "found": True,
            "kook": {"linked": True, "user_id": kook_id},
            "steam": {"linked": True, "steamid64": SID64},
        }

    monkeypatch.setattr(gmod_queue, "lookup_by_kook", binding)


@pytest.mark.parametrize(
    ("raw", "sid", "sid64"),
    [
        (SID64, SID, SID64),
        (" steam_1:0:0146595760 ", SID, SID64),
        ("steam_0:0:146595760", SID, SID64),
        ("76561197960265729", "STEAM_0:1:0", "76561197960265729"),
        ("STEAM_1:1:0", "STEAM_0:1:0", "76561197960265729"),
        ("76561202255233023", "STEAM_0:1:2147483647", "76561202255233023"),
        ("STEAM_0:1:2147483647", "STEAM_0:1:2147483647", "76561202255233023"),
    ],
)
def test_normalize_steamid(raw: str, sid: str, sid64: str) -> None:
    assert normalize_steamid(raw) == (sid, sid64)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "  ",
        "STEAM_0:0:0",
        "STEAM_1:0:000",
        "STEAM_2:1:1",
        "STEAM_0:2:1",
        "STEAM_0:1:-1",
        "STEAM_0:0:2147483648",
        "STEAM_0:1:" + "9" * 5000,
        "STEAM_0:1:１",
        "ſTEAM_0:1:1",
        "STEAM_ID_PENDING",
        "76561197960265728",
        "76561202255233024",
        "7656119796026572",
        "+76561197960265729",
    ],
)
def test_normalize_steamid_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match="无效的 SteamID / SteamID64"):
        normalize_steamid(raw)


async def test_resolve_server_id(snapshot: Callable[..., Snapshot]) -> None:
    snapshot(gmod_servers={OFF.id: OFF})
    with pytest.raises(GModServerNotFound, match="尚未配置已启用"):
        await resolve_server_id(None)

    snapshot(gmod_servers={s.id: s for s in (MAIN, OFF)})
    assert await resolve_server_id("") == ("main", "主服")
    with pytest.raises(GModServerNotFound, match=r"可用服务器：main \(主服\)"):
        await resolve_server_id("off")

    shadow = GModServerInfo("other", "MAIN")
    snapshot(gmod_servers={s.id: s for s in (MAIN, SURF, OFF, shadow)})
    assert await resolve_server_id("main") == ("main", "主服")
    assert await resolve_server_id(" surf server ") == ("surf", "Surf Server")
    assert await resolve_server_id("SURF") == ("surf", "Surf Server")
    with pytest.raises(GModError, match=r"匹配多台服务器.*server_id"):
        await resolve_server_id("MAIN")

    snapshot(gmod_servers={s.id: s for s in (MAIN, SURF, OFF)})
    lookup = "ncg.queue.lookup"
    await bridge.record_heartbeat(SURF.id, handlers=[lookup])
    assert await resolve_server_id(None, required_handler="ncg.queue.list") == ("surf", "Surf Server")

    await bridge.record_heartbeat(MAIN.id, handlers=["ncg.queue.list"])
    await bridge.record_heartbeat(OFF.id, handlers=[lookup])
    assert await resolve_server_id(None, required_handler=lookup) == ("surf", "Surf Server")
    assert await resolve_server_id(None, required_handler="ncg.queue.list") == ("main", "主服")

    await bridge.record_heartbeat(MAIN.id, handlers=[lookup])
    with pytest.raises(GModError, match=r"请指定 server_id.*main \(主服\).*surf \(Surf Server\)"):
        await resolve_server_id(None, required_handler=lookup)


@pytest.fixture
async def game_server(snapshot: Callable[..., Snapshot]) -> AsyncIterator[httpx.AsyncClient]:
    snapshot(gmod_servers={MAIN.id: MAIN})
    await bridge.record_heartbeat(MAIN.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_gmod_app()),
        base_url="http://gmod",
        headers={"Authorization": f"Bearer {MAIN.token}"},
        timeout=None,
    ) as client:
        yield client


async def run_rpc(
    client: httpx.AsyncClient,
    tool_result: Awaitable[ToolResult],
    action: str,
    args: dict[str, Any],
    *,
    data: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    async def reply() -> None:
        poll = await client.post("/bridge/gmod/poll", json={"server_id": MAIN.id, "wait_s": 1, "handlers": [action]})
        assert poll.status_code == 200, poll.text
        (job,) = poll.json()["jobs"]
        assert job["action"] == action
        assert job["args"] == args
        result = await client.post(
            "/bridge/gmod/result",
            json={"server_id": MAIN.id, "job_id": job["job_id"], "ok": error is None, "data": data, "error": error},
        )
        assert result.status_code == 200, result.text

    async def invoke() -> str:
        result = await tool_result
        assert isinstance(result, str)
        return result

    async with asyncio.TaskGroup() as group:
        response = group.create_task(invoke())
        group.create_task(reply())
    return response.result()


@pytest.fixture
def chat_ctx() -> RunContext[Deps]:
    return RunContext(
        deps=Deps(
            kook=None,  # pyright: ignore[reportArgumentType]
            channel_id="channel",
            guild_id="guild",
            user_id="123",
            settings=Settings(),
            handles=Handles(),
            task="chat",
        ),
        model=TestModel(),
        usage=RunUsage(),
    )


@pytest.fixture
def ops_ctx() -> RunContext[ops.OpsDeps]:
    return RunContext(
        deps=ops.OpsDeps(kook=None, user_id="123", channel_id="channel"),  # pyright: ignore[reportArgumentType]
        model=TestModel(),
        usage=RunUsage(),
    )


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[ConfigAudit]:
    records: list[ConfigAudit] = []

    class Session:
        def add(self, audit: ConfigAudit) -> None:
            records.append(audit)

    @asynccontextmanager
    async def session() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(ops, "get_session", session)
    return records


@pytest.mark.parametrize("in_queue", [True, False])
@pytest.mark.usefixtures("linked_caller")
async def test_chat_queue_lookup(game_server: httpx.AsyncClient, chat_ctx: RunContext[Deps], in_queue: bool) -> None:
    payload = {
        "steamid": SID,
        "steamid64": SID64,
        "in_queue": in_queue,
        "in_server": not in_queue,
        "summary": SUMMARY,
        **({"entry": ENTRY} if in_queue else {"ticket": TICKET}),
    }
    result = await run_rpc(
        game_server,
        chat_tools.gmod_queue_lookup(chat_ctx, SID64, view="full"),
        "ncg.queue.lookup",
        {"steamid": SID},
        data=payload,
    )
    assert json.loads(result) == {"ticket": None, **payload, "server_id": MAIN.id, "server_name": MAIN.name}


@pytest.mark.parametrize(
    ("entries", "tickets"),
    [([ENTRY], [{"steamid": SID, "steamid64": SID64, **TICKET}]), ({}, None), ([], {})],
)
@pytest.mark.usefixtures("linked_caller")
async def test_chat_queue_list(
    game_server: httpx.AsyncClient, chat_ctx: RunContext[Deps], entries: Any, tickets: Any
) -> None:
    payload = {"entries": entries, "active_tickets": tickets, "summary": SUMMARY, "server_time": 1764258000}
    result = await run_rpc(
        game_server, chat_tools.gmod_queue_list(chat_ctx, MAIN.name, view="full"), "ncg.queue.list", {}, data=payload
    )
    assert json.loads(result) == {
        **payload,
        "entries": entries if isinstance(entries, list) else [],
        "active_tickets": tickets if isinstance(tickets, list) else [],
        "server_id": MAIN.id,
        "server_name": MAIN.name,
    }


async def test_chat_queue_errors(game_server: httpx.AsyncClient, chat_ctx: RunContext[Deps]) -> None:
    result = await chat_tools.gmod_queue_lookup(chat_ctx, "STEAM_0:0:0")
    assert isinstance(result, str)
    assert result.startswith("[工具错误] 无效的 SteamID / SteamID64")
    error = "NCG 队列模块（sv_ply_queue.lua）未加载"
    assert await run_rpc(game_server, chat_tools.gmod_queue_list(chat_ctx), "ncg.queue.list", {}, error=error) == (
        f"[工具错误] {error}"
    )
    unsupported = await run_rpc(
        game_server, chat_tools.gmod_queue_list(chat_ctx), "ncg.queue.list", {}, error="unknown action: ncg.queue.list"
    )
    assert (
        unsupported
        == "[工具错误] GMod 服务器「main」未注册队列动作，请确认已加载 sv_ply_queue.lua 及 YggBridge 队列扩展"
    )


def test_queue_tool_permissions() -> None:
    read = {"gmod_queue_lookup", "gmod_queue_list"}
    write = {"gmod_queue_singlepass", "gmod_queue_letin"}
    assert read <= ALL_TOOLS.keys()
    assert read <= set(get_args(ToolName))
    assert write.isdisjoint(ALL_TOOLS)
    assert write.isdisjoint(get_args(ToolName))
    assert read <= set(Settings().tools.enabled)
    assert set(tool_names(sorted(read | write), "chat")) == read
    assert tool_names(sorted(read | write), "tldr") == []

    specs = {s.id: s for s in ops.OPS_TOOL_SPECS}
    for name in read | write:
        assert name in get_args(OpsToolName)
        assert specs[name].write == (name in write)
        assert specs[name].category == "gmod"
        assert not specs[name].global_only

    grant = ops.OpsGrant(False, frozenset(read | {"gmod_queue_letin"}), frozenset(), True)
    deps = ops.OpsDeps(kook=None, user_id="123", channel_id="channel", guild_id="guild", grant=grant)  # pyright: ignore[reportArgumentType]
    ops_tools = ops.ops_tools(deps)
    assert {tool.name for tool in ops_tools} == read | {"gmod_queue_letin"}
    for tool in [*ops_tools, *tools_for(sorted(read))]:
        if tool.name in read:
            props = tool.tool_def.parameters_json_schema["properties"]
            assert props["view"]["default"] == "summary"
            if tool.name == "gmod_queue_list":
                assert set(props["view"]["enum"]) == {"summary", "roster", "tickets", "full"}
                assert props["page"]["minimum"] == 1


async def test_ops_read_tools(game_server: httpx.AsyncClient, ops_ctx: RunContext[ops.OpsDeps]) -> None:
    payload = {
        "steamid": SID,
        "in_queue": True,
        "in_server": False,
        "entry": ENTRY,
        "ticket": TICKET,
        "summary": SUMMARY,
    }
    result = await run_rpc(
        game_server, ops.gmod_queue_lookup(ops_ctx, SID), "ncg.queue.lookup", {"steamid": SID}, data=payload
    )
    assert json.loads(result) == {
        "server_id": MAIN.id,
        "server_name": MAIN.name,
        "in_queue": True,
        "in_server": False,
        "nick": "RAY",
        "position": 2,
        "wait_s": 135,
        "ticket": TICKET,
    }
    result = await run_rpc(
        game_server,
        ops.gmod_queue_list(ops_ctx),
        "ncg.queue.list",
        {},
        data={"summary": SUMMARY, "entries": [ENTRY], "active_tickets": {}},
    )
    assert json.loads(result) == {"server_id": MAIN.id, "server_name": MAIN.name, **SUMMARY, "active_ticket_count": 0}
    assert ops_ctx.deps.actions == []


@pytest.mark.usefixtures("linked_caller")
async def test_chat_queue_pages(game_server: httpx.AsyncClient, chat_ctx: RunContext[Deps]) -> None:
    entries = [{**ENTRY, "position": n} for n in range(1, 22)]
    data = {"entries": entries, "active_tickets": [{"steamid64": SID64, **TICKET}]}

    async def query(call: Awaitable[ToolResult]) -> dict[str, Any]:
        return json.loads(await run_rpc(game_server, call, "ncg.queue.list", {}, data=data))

    summary = await query(chat_tools.gmod_queue_list(chat_ctx))
    assert summary == {"server_id": MAIN.id, "server_name": MAIN.name, "total_queued": 21}
    first = await query(chat_tools.gmod_queue_list(chat_ctx, view="roster"))
    assert first["columns"] == ["position", "nick", "tier", "wait_s"]
    assert first["rows"] == [[n, "RAY", "regular", 135] for n in range(1, 21)]
    last = await query(chat_tools.gmod_queue_list(chat_ctx, view="roster", page=first["next_page"]))
    assert (last["page"], last["total"], last["rows"]) == (2, 21, [[21, "RAY", "regular", 135]])
    assert "next_page" not in last
    tickets = await query(chat_tools.gmod_queue_list(chat_ctx, view="tickets"))
    assert tickets["columns"] == ["steamid64", "kind", "remaining_s"]
    assert tickets["rows"] == [[SID64, "admin_singlepass", 285]]
    invalid = await chat_tools.gmod_queue_list(chat_ctx, view="full", page=2)
    assert isinstance(invalid, str)
    assert invalid.startswith("[工具错误]")


@pytest.mark.usefixtures("linked_caller")
async def test_ticket_visibility_across_players(
    game_server: httpx.AsyncClient, chat_ctx: RunContext[Deps], ops_ctx: RunContext[ops.OpsDeps]
) -> None:
    other, other64 = normalize_steamid("STEAM_0:1:123")
    hidden = {"kind": "hidden-pass", "remaining_s": 123}
    entry = {
        **ENTRY,
        "steamid": other,
        "steamid64": other64,
        "has_ticket": True,
        "ticket_kind": "hidden-pass",
        "ticket_remaining_s": 123,
    }
    player = {"steamid": other, "steamid64": other64, "ticket": hidden, "entry": entry}
    for tool in (
        chat_tools.gmod_queue_lookup(chat_ctx, other, view="summary"),
        chat_tools.gmod_queue_lookup(chat_ctx, other64, view="full"),
    ):
        raw = await run_rpc(game_server, tool, "ncg.queue.lookup", {"steamid": other}, data=player)
        assert "ticket" not in raw
        result = json.loads(raw)
        assert result.get("entry", result)["position"] == ENTRY["position"]

    private = {"steamid64": other64, **hidden}
    own = {"steamid64": SID64, **TICKET}
    data = {"entries": [entry, ENTRY], "active_tickets": [private] * 20 + [own]}
    for tool in (
        chat_tools.gmod_queue_list(chat_ctx, view="full"),
        chat_tools.gmod_queue_list(chat_ctx, view="tickets"),
    ):
        raw = await run_rpc(game_server, tool, "ncg.queue.list", {}, data=data)
        assert "hidden-pass" not in raw
        result = json.loads(raw)
        if "entries" in result:
            assert result["active_tickets"] == [own]
            assert "has_ticket" not in result["entries"][0]
            assert "ticket_remaining_s" not in result["entries"][0]
        else:
            assert result["rows"] == [[SID64, TICKET["kind"], TICKET["remaining_s"]]]
            assert result["total"] == 1
            assert "next_page" not in result

    for tool in (ops.gmod_queue_lookup(ops_ctx, other64), ops.gmod_queue_lookup(ops_ctx, other64, view="full")):
        result = json.loads(await run_rpc(game_server, tool, "ncg.queue.lookup", {"steamid": other}, data=player))
        assert result["ticket"] == hidden

    raw = await run_rpc(game_server, ops.gmod_queue_list(ops_ctx, view="full"), "ncg.queue.list", {}, data=data)
    assert json.loads(raw)["entries"] == data["entries"]
    assert json.loads(raw)["active_tickets"] == data["active_tickets"]
    raw = await run_rpc(game_server, ops.gmod_queue_list(ops_ctx, view="tickets"), "ncg.queue.list", {}, data=data)
    assert json.loads(raw)["rows"] == [[other64, hidden["kind"], hidden["remaining_s"]]] * 20
    assert json.loads(raw)["total"] == 21


async def test_unverified_caller_cannot_read_tickets(
    game_server: httpx.AsyncClient, chat_ctx: RunContext[Deps]
) -> None:
    # No Azuriom credentials: skip all private fields, including a negative status.
    result = await chat_tools.gmod_queue_list(chat_ctx, view="tickets")
    assert "无法确认调用者" in str(result)
    data = {"steamid": SID, "entry": ENTRY, "ticket": TICKET}
    raw = await run_rpc(
        game_server,
        chat_tools.gmod_queue_lookup(chat_ctx, SID, view="full"),
        "ncg.queue.lookup",
        {"steamid": SID},
        data=data,
    )
    assert "ticket" not in raw


@pytest.mark.parametrize(
    ("in_queue", "letin_queued", "ttl_s"),
    [(True, True, None), (False, False, None), (True, True, 30), (False, False, 3600), (True, False, None)],
)
async def test_ops_singlepass(
    game_server: httpx.AsyncClient,
    ops_ctx: RunContext[ops.OpsDeps],
    audits: list[ConfigAudit],
    in_queue: bool,
    letin_queued: bool,
    ttl_s: int | None,
) -> None:
    args: dict[str, Any] = {"steamid": SID, "actor": "kook:123"}
    if ttl_s is not None:
        args["ttl_s"] = ttl_s
    ttl = ttl_s if ttl_s is not None else 300
    payload = {
        "steamid": SID,
        "steamid64": SID64,
        "ttl_s": ttl,
        "was_in_queue": in_queue,
        "queued_manual_letin": letin_queued,
        "engine_full": False,
        "drain_halted": False,
    }
    result = await run_rpc(
        game_server, ops.gmod_queue_singlepass(ops_ctx, SID64, ttl_s=ttl_s), "ncg.queue.singlepass", args, data=payload
    )
    assert f"发放 SinglePass，有效期 {ttl} 秒" in result
    assert ("登记了强制放行请求" in result) == letin_queued
    assert ("强制放行请求登记失败" in result) == (in_queue and not letin_queued)
    assert ops_ctx.deps.actions == [result]
    (audit,) = audits
    assert (audit.actor, audit.entity, audit.entity_id, audit.action) == (
        "kook:123",
        "gmod_queue",
        f"main:{SID}",
        "singlepass",
    )
    assert audit.detail["ttl_s"] == ttl
    assert audit.detail["queued_manual_letin"] is letin_queued


@pytest.mark.parametrize("ttl_s", [29, 3601, True])
async def test_ops_singlepass_invalid_ttl(
    ops_ctx: RunContext[ops.OpsDeps], audits: list[ConfigAudit], ttl_s: int
) -> None:
    result = await ops.gmod_queue_singlepass(ops_ctx, SID, ttl_s=ttl_s)
    assert result.startswith("[失败] SinglePass 的 ttl_s 必须为 30 到 3600 秒之间的整数")
    assert ops_ctx.deps.actions == audits == []


@pytest.mark.parametrize("already_pending", [False, True])
async def test_ops_letin(
    game_server: httpx.AsyncClient,
    ops_ctx: RunContext[ops.OpsDeps],
    audits: list[ConfigAudit],
    already_pending: bool,
) -> None:
    payload = {"steamid": SID, "steamid64": SID64, "already_pending_letin": already_pending, "engine_full": True}
    result = await run_rpc(
        game_server,
        ops.gmod_queue_letin(ops_ctx, SID64, MAIN.name),
        "ncg.queue.letin",
        {"steamid": SID, "actor": "kook:123"},
        data=payload,
    )
    assert ("已存在强制放行请求" if already_pending else "已登记强制放行请求") in result
    assert "等待引擎空位" in result
    assert ops_ctx.deps.actions == [result]
    (audit,) = audits
    assert (audit.actor, audit.entity, audit.entity_id, audit.action) == (
        "kook:123",
        "gmod_queue",
        f"main:{SID}",
        "letin",
    )
    assert audit.detail["already_pending_letin"] == already_pending


@pytest.mark.parametrize(
    "error",
    [
        f"玩家不在当前排队队列中，无法直接放行（如需提前授予一次性通行证，请使用 singlepass）：{SID}",
        f"玩家不在排队队列中（该玩家当前已在服务器内游玩）：{SID}",
        f"玩家不在排队队列中（该玩家已在加载进入服务器中）：{SID}",
        "队列晋升泵当前已暂停（test），请先在控制台执行 ncg_queue_resume",
        f"加入强制放行队列失败：{SID}",
    ],
)
async def test_ops_letin_propagates_remote_failure(
    game_server: httpx.AsyncClient, ops_ctx: RunContext[ops.OpsDeps], audits: list[ConfigAudit], error: str
) -> None:
    result = await run_rpc(
        game_server,
        ops.gmod_queue_letin(ops_ctx, SID),
        "ncg.queue.letin",
        {"steamid": SID, "actor": "kook:123"},
        error=error,
    )
    assert result == f"[失败] {error}"
    assert ops_ctx.deps.actions == audits == []
