"""NCG queue operations over the GMod bridge."""

import asyncio
import re
from typing import Any

from yggdrasil.gmod import bridge
from yggdrasil.runtime import snapshot

_STEAM64_BASE = 76561197960265728
_MAX_ACCOUNT_ID = 2**32 - 1
_STEAMID2 = re.compile(r"STEAM_[01]:([01]):([0-9]+)", re.IGNORECASE | re.ASCII)


def normalize_steamid(raw: str) -> tuple[str, str]:
    """Return the canonical SteamID2 and SteamID64 for an individual Steam account."""
    value = raw.strip()
    account_id = 0
    if match := _STEAMID2.fullmatch(value):
        number = match[2].lstrip("0")
        if len(number) <= 10:
            account_id = 2 * int(number or "0") + int(match[1])
    elif re.fullmatch(r"[0-9]{17}", value):
        account_id = int(value) - _STEAM64_BASE
    if not 1 <= account_id <= _MAX_ACCOUNT_ID:
        raise ValueError(f"无效的 SteamID / SteamID64：{value or '不能为空'}")
    return f"STEAM_0:{account_id % 2}:{account_id // 2}", str(_STEAM64_BASE + account_id)


async def resolve_server_id(server_id: str | None, *, required_handler: str | None = None) -> tuple[str, str]:
    """Resolve an enabled server by ID, name, or online handler availability."""
    servers = {server.id: server for server in snapshot().gmod_servers.values() if server.enabled}
    if not servers:
        raise bridge.GModServerNotFound("尚未配置已启用的 GMod 服务器")

    requested = (server_id or "").strip()
    options = "、".join(f"{server.id} ({server.name})" for server in servers.values())
    if requested:
        if server := servers.get(requested):
            return server.id, server.name
        folded = requested.casefold()
        matches = [server for server in servers.values() if folded in (server.id.casefold(), server.name.casefold())]
        if len(matches) == 1:
            return matches[0].id, matches[0].name
        if not matches:
            raise bridge.GModServerNotFound(f"找不到已启用的 GMod 服务器「{requested}」。可用服务器：{options}")
        raise bridge.GModError(f"GMod 服务器「{requested}」匹配多台服务器，请指定 server_id。可用服务器：{options}")

    if len(servers) == 1:
        server = next(iter(servers.values()))
        return server.id, server.name

    heartbeats = await asyncio.gather(*(bridge.get_heartbeat(sid) for sid in servers))
    online = [(server, hb) for server, hb in zip(servers.values(), heartbeats, strict=True) if hb and hb.online]
    if required_handler is not None:
        capable = [server for server, hb in online if required_handler in hb.handlers]
        if len(capable) == 1:
            return capable[0].id, capable[0].name
    if len(online) == 1:
        server = online[0][0]
        return server.id, server.name
    raise bridge.GModError(f"无法自动选择 GMod 服务器，请指定 server_id。可用服务器：{options}")


async def _call_queue(server_id: str | None, action: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    sid, name = await resolve_server_id(server_id, required_handler=action)
    try:
        result = await bridge.call(sid, action, args, timeout_s=timeout_s, raise_on_error=True)
    except bridge.GModRemoteError as e:
        if str(e) == f"unknown action: {action}":
            raise bridge.GModRemoteError(
                f"GMod 服务器「{sid}」未注册队列动作，请确认已加载 sv_ply_queue.lua 及 YggBridge 队列扩展"
            ) from e
        raise
    if not isinstance(result.data, dict):
        raise bridge.GModError(f"GMod 服务器「{sid}」返回了无效的队列结果")
    return {**result.data, "server_id": sid, "server_name": name}


async def lookup_queue(steamid: str, server_id: str | None = None, *, timeout_s: float = 8.0) -> dict[str, Any]:
    sid, _ = normalize_steamid(steamid)
    return await _call_queue(server_id, "ncg.queue.lookup", {"steamid": sid}, timeout_s)


async def list_queue(server_id: str | None = None, *, timeout_s: float = 8.0) -> dict[str, Any]:
    data = await _call_queue(server_id, "ncg.queue.list", {}, timeout_s)
    for field in ("entries", "active_tickets"):
        if not isinstance(data.get(field), list):
            data[field] = []
    return data


async def grant_singlepass(
    steamid: str,
    server_id: str | None = None,
    *,
    ttl_s: int | None = None,
    actor: str = "",
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    sid, _ = normalize_steamid(steamid)
    args: dict[str, Any] = {"steamid": sid, "actor": actor}
    if ttl_s is not None:
        if type(ttl_s) is not int or not 30 <= ttl_s <= 3600:
            raise ValueError("SinglePass 的 ttl_s 必须为 30 到 3600 秒之间的整数")
        args["ttl_s"] = ttl_s
    return await _call_queue(server_id, "ncg.queue.singlepass", args, timeout_s)


async def letin_player(
    steamid: str, server_id: str | None = None, *, actor: str = "", timeout_s: float = 8.0
) -> dict[str, Any]:
    sid, _ = normalize_steamid(steamid)
    return await _call_queue(server_id, "ncg.queue.letin", {"steamid": sid, "actor": actor}, timeout_s)
