"""Model-facing views of queue snapshots shared by chat and ops."""

import json
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import RunContext

from yggdrasil.gmod import GModError, list_queue, lookup_queue
from yggdrasil.tools.errors import ToolError

_PAGE_SIZE = 20


def _fields(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def gmod_queue_lookup(
    ctx: RunContext[Any], steamid: str, server_id: str | None = None, view: Literal["summary", "full"] = "summary"
) -> str:
    """按 SteamID 或 SteamID64 查玩家。summary 查位次、已等待秒数、进服状态和通行证；full 查优先级等完整细节。

    server_id 可填游戏服 ID 或名称。
    """
    try:
        data = await lookup_queue(steamid, server_id)
    except (ValueError, GModError) as e:
        raise ToolError(str(e)) from e
    if view == "full":
        return _json(data)
    result = _fields(
        data, "server_id", "server_name", "nick", "in_queue", "in_server", "in_real_slot", "awaiting_spawn"
    )
    entry = data.get("entry")
    if isinstance(entry, dict):
        result.update(_fields(entry, "position", "wait_s"))
        if not result.get("nick") and entry.get("nick"):
            result["nick"] = entry["nick"]
    result["ticket"] = data.get("ticket")
    return _json(result)


async def gmod_queue_list(
    ctx: RunContext[Any],
    server_id: str | None = None,
    view: Literal["summary", "roster", "tickets", "full"] = "summary",
    page: Annotated[int, Field(ge=1)] = 1,
) -> str:
    """查 GMod 队列。summary 看人数与容量；roster 看排队名单；tickets 看有效通行证；full 取所有原始详情。

    server_id 可填游戏服 ID 或名称。roster 和 tickets 每页 20 条，按需传 next_page 续查。
    """
    if page < 1 or (page != 1 and view not in ("roster", "tickets")):
        raise ToolError("页码从 1 开始，仅 roster 和 tickets 支持翻页")
    try:
        data = await list_queue(server_id)
    except (ValueError, GModError) as e:
        raise ToolError(str(e)) from e
    if view == "full":
        return _json(data)
    result = _fields(data, "server_id", "server_name")
    if view == "summary":
        summary = data.get("summary") or {}
        result.update(_fields(summary, "players", "max_slots", "total_queued", "priority_count", "regular_count"))
        result.setdefault("total_queued", len(data["entries"]))
        result["active_ticket_count"] = len(data["active_tickets"])
        return _json(result)

    if view == "roster":
        records = data["entries"]
        columns = ("position", "nick", "tier", "wait_s")
    else:
        records = data["active_tickets"]
        columns = ("steamid64", "kind", "remaining_s")
    start = (page - 1) * _PAGE_SIZE
    end = start + _PAGE_SIZE
    rows = [[record.get(key) for key in columns] for record in records[start:end]]
    result.update(page=page, total=len(records), columns=columns, rows=rows)
    if end < len(records):
        result["next_page"] = page + 1
    return _json(result)
