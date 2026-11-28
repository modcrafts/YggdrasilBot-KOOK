"""Azuriom account binding lookup for ordinary chat."""

import json

from pydantic_ai import RunContext

from yggdrasil.access import can_lookup_other_identities
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool
from yggdrasil.runtime import snapshot
from yggdrasil.tools.azuriom import AzuriomError, lookup_by_kook, lookup_by_steam, normalize_kook_user_id
from yggdrasil.tools.errors import ToolError


@chat_tool("azuriom_lookup")
async def azuriom_lookup(ctx: RunContext[Deps], kook_user_id: str | None = None, steamid: str | None = None) -> str:
    """查询 Azuriom 网站账号、KOOK 与 Steam 的绑定关系。

    kook_user_id 可填 KOOK 用户 ID 或提及，steamid 可填 SteamID64 或 SteamID2（STEAM_0/1:X:Y）。
    两个参数只能选一个，均留空时查询发起者本人。普通成员仅可查本人 KOOK 绑定；
    只有获授权的 KOOK 角色或超级管理员可查他人或按 SteamID 反查。
    """
    deps = ctx.deps
    allow_others = can_lookup_other_identities(deps.settings, deps.user_id, deps.role_ids, snapshot().superusers)
    kook_user_id = (kook_user_id or "").strip()
    steamid = (steamid or "").strip()
    if kook_user_id and steamid:
        raise ToolError("请只传入 kook_user_id 或 steamid 之一，不要同时传入")

    timeout = deps.settings.tools.azuriom_timeout_s
    try:
        if steamid:
            if not allow_others:
                raise ToolError(
                    "权限不足：出于隐私保护，普通成员不允许通过 SteamID 反查 KOOK 账号。"
                    "如需查看你自己的绑定信息，请不传参数直接查询本人绑定。"
                )
            result = await lookup_by_steam(steamid, timeout_s=timeout)
        else:
            target = normalize_kook_user_id(kook_user_id) if kook_user_id else deps.user_id
            if target != deps.user_id and not allow_others:
                raise ToolError(
                    "权限不足：出于隐私保护，普通成员只能查询自己本人的 Azuriom / Steam 绑定信息，不能查询他人。"
                )
            result = await lookup_by_kook(target, timeout_s=timeout)
    except (ValueError, AzuriomError) as e:
        raise ToolError(str(e)) from e
    return json.dumps(result, ensure_ascii=False)
