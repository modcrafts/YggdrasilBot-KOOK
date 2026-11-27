"""Read-only GMod queue tools for ordinary chat."""

import json

from pydantic_ai import RunContext

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool
from yggdrasil.gmod import GModError, list_queue, lookup_queue
from yggdrasil.tools.errors import ToolError


@chat_tool("gmod_queue_lookup")
async def gmod_queue_lookup(ctx: RunContext[Deps], steamid: str, server_id: str | None = None) -> str:
    """按 SteamID 或 SteamID64 查询玩家的 GMod 排队位次、进服状态及通行证。可指定游戏服 ID 或名称。"""
    try:
        result = await lookup_queue(steamid, server_id)
    except (ValueError, GModError) as e:
        raise ToolError(str(e)) from e
    return json.dumps(result, ensure_ascii=False)


@chat_tool("gmod_queue_list")
async def gmod_queue_list(ctx: RunContext[Deps], server_id: str | None = None) -> str:
    """查询指定 GMod 游戏服的完整队列、容量和有效通行证。server_id 可填游戏服 ID 或名称。"""
    try:
        result = await list_queue(server_id)
    except (ValueError, GModError) as e:
        raise ToolError(str(e)) from e
    return json.dumps(result, ensure_ascii=False)
