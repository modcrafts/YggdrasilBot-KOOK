"""Model-facing views of queue snapshots shared by chat and ops."""

import json
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import RunContext

from yggdrasil.gmod import GModError, list_queue, lookup_queue, normalize_steamid
from yggdrasil.tools.azuriom import AzuriomError
from yggdrasil.tools.errors import ToolError

_PAGE_SIZE = 20
_ENTRY_FIELDS = (
    "position",
    "tier_position",
    "tier",
    "priority",
    "steamid",
    "steamid64",
    "nick",
    "wait_s",
    "queue_seq",
    "pending_letin",
)
_TICKET_FIELDS = ("kind", "remaining_s", "rejoin_reason")
_SUMMARY_FIELDS = (
    "version",
    "players",
    "engine_occupancy",
    "playable_occupancy",
    "reservations",
    "effective_used",
    "max_slots",
    "engine_max_players",
    "max_queue_size",
    "loading",
    "max_loading",
    "next_is_priority",
    "priority_count",
    "regular_count",
    "reserved_waiting",
    "effective_waiting",
    "total_queued",
    "drain_halted",
    "drain_halt_reason",
    "autorefresh_active",
)


def _fields(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _is_ops(ctx: RunContext[Any]) -> bool:
    # The ops registry imports this module. Check the trusted invocation context at call time.
    from yggdrasil.ops.tools import OpsDeps

    return isinstance(ctx.deps, OpsDeps)


async def _caller_steamid64(ctx: RunContext[Any]) -> str | None:
    deps = ctx.deps
    try:
        _, steamid64 = await deps.caller_binding.lookup(deps.user_id, timeout_s=deps.settings.tools.azuriom_timeout_s)
        return steamid64
    except (AzuriomError, ValueError):
        return None


def _owned(record: dict[str, Any], owner: str | None) -> bool:
    ids = [record[key] for key in ("steamid64", "steamid") if key in record]
    if not owner or not ids:
        return False
    try:
        return all(normalize_steamid(str(uid))[1] == owner for uid in ids)
    except ValueError:
        return False


def _entry_view(entry: dict[str, Any], owner: str | None) -> dict[str, Any]:
    result = _fields(entry, *_ENTRY_FIELDS)
    if _owned(entry, owner):
        result.update(_fields(entry, "has_ticket", "ticket_kind", "ticket_remaining_s"))
    return result


async def gmod_queue_lookup(
    ctx: RunContext[Any], steamid: str, server_id: str | None = None, view: Literal["summary", "full"] = "summary"
) -> str:
    """按 SteamID 或 SteamID64 查玩家。summary 查位次、等候时间、进服状态；full 查详情。

    server_id 可填游戏服 ID 或名称。普通聊天仅能查本人通行证，缺少 ticket 表示状态不可见。OPS 可查任意玩家。
    """
    try:
        data = await lookup_queue(steamid, server_id)
    except (ValueError, GModError) as e:
        raise ToolError(str(e)) from e
    ops = _is_ops(ctx)
    if ops and view == "full":
        return _json(data)
    owner = None if ops else await _caller_steamid64(ctx)
    show_ticket = ops or (normalize_steamid(steamid)[1] == owner and _owned(data, owner))
    result = _fields(
        data, "server_id", "server_name", "nick", "in_queue", "in_server", "in_real_slot", "awaiting_spawn"
    )
    entry = data.get("entry")
    if view == "full":
        result.update(
            _fields(
                data,
                "steamid",
                "steamid64",
                "is_bypass",
                "is_priority",
                "pending_letin",
                "autorefresh_returnee",
                "server_time",
            )
        )
        if isinstance(data.get("summary"), dict):
            result["summary"] = _fields(data["summary"], *_SUMMARY_FIELDS)
        if isinstance(entry, dict):
            result["entry"] = _entry_view(entry, owner if show_ticket else None)
    elif isinstance(entry, dict):
        result.update(_fields(entry, "position", "wait_s"))
        if not result.get("nick") and entry.get("nick"):
            result["nick"] = entry["nick"]
    if show_ticket:
        ticket = data.get("ticket")
        if isinstance(ticket, dict):
            result["ticket"] = _fields(ticket, *_TICKET_FIELDS)
        else:
            result["ticket"] = None
    return _json(result)


async def gmod_queue_list(
    ctx: RunContext[Any],
    server_id: str | None = None,
    view: Literal["summary", "roster", "tickets", "full"] = "summary",
    page: Annotated[int, Field(ge=1)] = 1,
) -> str:
    """查 GMod 队列。summary 看人数与容量；roster 看排队名单；tickets 看有效通行证；full 看队列详情。

    server_id 可填游戏服 ID 或名称。每页 20 条，按需传 next_page 续查。普通聊天仅能查本人通行证，OPS 可查所有人。
    """
    if page < 1 or (page != 1 and view not in ("roster", "tickets")):
        raise ToolError("页码从 1 开始，仅 roster 和 tickets 支持翻页")
    ops = _is_ops(ctx)
    owner = await _caller_steamid64(ctx) if not ops and view in ("tickets", "full") else None
    if view == "tickets" and not ops and owner is None:
        raise ToolError("无法确认调用者本人的 Steam 绑定，请检查账号绑定或稍后重试")
    try:
        data = await list_queue(server_id)
    except (ValueError, GModError) as e:
        raise ToolError(str(e)) from e
    if ops and view == "full":
        return _json(data)
    result = _fields(data, "server_id", "server_name")
    if view == "summary":
        summary = data.get("summary") or {}
        result.update(_fields(summary, "players", "max_slots", "total_queued", "priority_count", "regular_count"))
        result.setdefault("total_queued", len(data["entries"]))
        if ops:
            result["active_ticket_count"] = len(data["active_tickets"])
        return _json(result)

    if view == "roster":
        records = data["entries"]
        columns = ("position", "nick", "tier", "wait_s")
    else:
        records = [
            _fields(ticket, "steamid", "steamid64", *_TICKET_FIELDS)
            for ticket in data["active_tickets"]
            if isinstance(ticket, dict) and (ops or _owned(ticket, owner))
        ]
        if view == "full":
            result.update(_fields(data, "server_time"))
            if isinstance(data.get("summary"), dict):
                result["summary"] = _fields(data["summary"], *_SUMMARY_FIELDS)
            result["entries"] = [_entry_view(entry, owner) for entry in data["entries"] if isinstance(entry, dict)]
            if owner is not None:
                result["active_tickets"] = records
            return _json(result)
        columns = ("steamid64", "kind", "remaining_s")
    start = (page - 1) * _PAGE_SIZE
    end = start + _PAGE_SIZE
    rows = [[record.get(key) for key in columns] for record in records[start:end]]
    result.update(page=page, total=len(records), columns=columns, rows=rows)
    if end < len(records):
        result["next_page"] = page + 1
    return _json(result)
