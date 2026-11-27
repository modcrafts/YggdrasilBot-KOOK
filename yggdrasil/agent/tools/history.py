"""Guild-shared memory and channel history tools:
``search_history``, ``get_messages_around``, ``recent_chat``, ``lookup_glossary``, ``get_profile``.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from yggdrasil.agent.context import chat_line, chat_tail, reply_targets
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool, people_for
from yggdrasil.runtime import snapshot
from yggdrasil.tools.errors import ToolError

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView

GLOSSARY_LIST_MAX = 200


async def _chat_lines(ctx: RunContext[Deps], channel_id: str, msgs: "Sequence[MessageView]") -> list[str]:
    people = await people_for(ctx, [m.user_id for m in msgs], [m.text for m in msgs])
    replies = await reply_targets(channel_id, msgs)
    return [chat_line(m, people, ctx.deps.handles, replies) for m in msgs]


def _ch_tag(channel_id: str | None) -> str:
    if not channel_id:
        return ""
    name = snapshot().channel_name(channel_id)
    return f"[#{name or channel_id}] "


@chat_tool("search_history")
async def search_history(
    ctx: RunContext[Deps],
    query: str,
    days: int | None = None,
    user_id: str | None = None,
    current_channel_only: bool = False,
) -> str:
    """检索本服务器（默认跨同服务器所有共享频道）的历史聊天记录、话题和每日摘要（关键词+语义）。
    命中其他频道的内容会标出 [#频道名]，回答时可直接指出消息来自哪个频道。
    days 限定最近几天，user_id 限定发言人，current_channel_only=True 仅搜当前频道。"""
    from yggdrasil.memory.search import accessible_channels, find_by_msg_id, message_segments
    from yggdrasil.memory.search import search_history as _search

    if not ctx.deps.guild_id:
        raise ToolError("私聊里不能检索服务器历史")
    since = datetime.now().astimezone() - timedelta(days=days) if days else None
    asked = (
        await find_by_msg_id(ctx.deps.channel_id, ctx.deps.trigger_message_id)
        if ctx.deps.channel_id and ctx.deps.trigger_message_id
        else None
    )
    hits = await _search(
        ctx.deps.guild_id,
        query,
        channel_id=ctx.deps.channel_id,
        current_channel_only=current_channel_only,
        role_ids=ctx.deps.role_ids,
        since=since,
        until=asked.sent_at if asked else None,
        user_id=user_id.strip() if user_id else None,
        top_k=ctx.deps.settings.tools.history_top_k,
    )
    if not hits:
        return "没有找到相关记录"
    ctx.deps.notes.messages(ctx.tool_call_id, [h.msg_id for h in hits if h.msg_id])
    people = await people_for(ctx, [h.user_id for h in hits if h.user_id], [h.text for h in hits])
    allowed = accessible_channels(
        ctx.deps.guild_id,
        ctx.deps.channel_id,
        current_channel_only=current_channel_only,
        role_ids=ctx.deps.role_ids,
    )
    segments = await message_segments(ctx.deps.guild_id, [h.msg_id for h in hits if h.msg_id], allowed_channels=allowed)
    out = []
    for h in hits:
        when = h.when.astimezone().strftime("%Y-%m-%d %H:%M") if h.when else ""
        ref = f"#{h.msg_id}" if h.msg_id else f"{h.kind}:{h.ref_id}"
        ch_prefix = _ch_tag(h.channel_id)
        who = f"{people.who(h.user_id, h.sender_name)}: " if h.user_id else ""
        keys = ctx.deps.handles.register(segments.get(h.msg_id) or []) if h.msg_id else ""
        body = f"{who}{people.mentions(h.text)}{keys}"
        out.append(f"[{when}] {ch_prefix}({ref})" + (f"\n{body}" if h.kind == "chunk" else f" {body}"))
    return "\n".join(out)


@chat_tool("get_messages_around")
async def get_messages_around(ctx: RunContext[Deps], message_id: int, before: int = 10, after: int = 10) -> str:
    """取某条历史消息（#id）前后的上下文；支持同服务器其他共享频道的消息，并在首行注明来源频道。"""
    from yggdrasil.memory.search import accessible_channels, messages_around

    if not ctx.deps.guild_id:
        raise ToolError("私聊里不能检索服务器历史")
    allowed = accessible_channels(ctx.deps.guild_id, ctx.deps.channel_id, role_ids=ctx.deps.role_ids)
    max_around = ctx.deps.settings.tools.messages_around_max
    source_ch, msgs = await messages_around(
        ctx.deps.guild_id, message_id, min(before, max_around), min(after, max_around), allowed_channels=allowed
    )
    if not source_ch or not msgs:
        return "无"
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in msgs])
    lines = await _chat_lines(ctx, source_ch, msgs)
    header = (
        f"来源频道：#{snapshot().channel_name(source_ch) or source_ch}\n" if source_ch != ctx.deps.channel_id else ""
    )
    return header + "\n".join(lines)


def _resolve_target_channel(
    guild_id: str, current_channel_id: str, channel: str | None, *, role_ids: Sequence[str] = ()
) -> str:
    from yggdrasil.memory.search import accessible_channels

    if not channel or not channel.strip():
        return current_channel_id
    wanted = channel.strip().lstrip("#")
    allowed = set(accessible_channels(guild_id, current_channel_id, role_ids=role_ids))
    snap = snapshot()
    if wanted in allowed:
        return wanted
    for cid in allowed:
        if snap.channel_name(cid).lower() == wanted.lower():
            return cid
    available = "、".join(f"#{snap.channel_name(cid) or cid}" for cid in sorted(allowed))
    raise ToolError(f"找不到同服务器可见频道「{channel}」；可用频道：{available}")


@chat_tool("recent_chat")
async def recent_chat(ctx: RunContext[Deps], minutes: int = 30, channel: str | None = None) -> str:
    """取当前频道（或传入 channel 查看同服务器其他共享频道，如 channel='开发'）最近的聊天记录。"""
    from yggdrasil.memory.search import recent_messages

    if not ctx.deps.channel_id or not ctx.deps.guild_id:
        raise ToolError("私聊里没有频道聊天记录")
    target_ch = _resolve_target_channel(ctx.deps.guild_id, ctx.deps.channel_id, channel, role_ids=ctx.deps.role_ids)
    cfg = ctx.deps.settings.context
    msgs = await recent_messages(target_ch, limit=cfg.recent_messages, minutes=min(max(minutes, 1), 24 * 60))
    if not msgs:
        return "这段时间没有聊天记录"
    lines = await _chat_lines(ctx, target_ch, msgs)
    first, text = chat_tail(lines, cfg.max_context_chars)
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in msgs[first:]])
    header = (
        f"来源频道：#{snapshot().channel_name(target_ch) or target_ch}\n" if target_ch != ctx.deps.channel_id else ""
    )
    return header + text


@chat_tool("lookup_glossary")
async def lookup_glossary(ctx: RunContext[Deps], term: str) -> str:
    """查本服务器共享的黑话/梗词典（传空字符串列出全部词条）。"""
    from yggdrasil.memory.search import glossary_terms
    from yggdrasil.memory.search import lookup_glossary as _lookup

    if not ctx.deps.guild_id:
        raise ToolError("私聊里没有服务器词典")
    if not term.strip():
        entries = await glossary_terms(ctx.deps.guild_id)
        names = sorted((f"{t.term}（{'、'.join(t.aliases)}）" if t.aliases else t.term for t in entries), key=str.lower)
        if not names:
            return "本服务器词典还是空的"
        shown = names[: ctx.deps.settings.tools.glossary_list_max]
        more = f"\n（共 {len(names)} 条，只列出前 {len(shown)} 条）" if len(names) > len(shown) else ""
        return f"本服务器词典的词条（查释义时传入词条名）：\n{'、'.join(shown)}{more}"
    terms = await _lookup(ctx.deps.guild_id, term)
    if not terms:
        return f"词典里没有「{term}」"
    return "\n".join(f"{t.term}（别名：{'、'.join(t.aliases) or '无'}）：{t.definition}" for t in terms)


@chat_tool("get_profile")
async def get_profile(ctx: RunContext[Deps], user_id: str) -> str:
    """查本服务器成员画像（跨同服务器频道同步的昵称、兴趣、特点和重要事实）。user_id 是成员 ID。"""
    from yggdrasil.memory.search import get_profile as _get

    if not ctx.deps.guild_id:
        raise ToolError("私聊里没有成员画像")
    uid = user_id.strip()
    p = await _get(ctx.deps.guild_id, uid)
    if p is None:
        return "没有这个人的画像（可能选择了不被记录）"
    who = (await people_for(ctx, [uid])).who(p.user_id, p.display_name)
    out = f"{who}：{p.summary}\n兴趣：{'、'.join(p.interests)}\n特点：{'、'.join(p.traits)}"
    if p.facts:
        out += "\n事实：\n" + "\n".join(f"- {f}" for f in p.facts)
    return out
