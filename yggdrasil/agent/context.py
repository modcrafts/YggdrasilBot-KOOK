"""Assemble the user prompt for chat / tldr runs from the incoming KOOK message, channel context, and guild memory."""

import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import ahocorasick
from sqlalchemy import select

from yggdrasil.agent.deps import Handles
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message
from yggdrasil.db.session import get_session
from yggdrasil.ingest.normalize import URL_RE, normalize
from yggdrasil.ingest.writer import WRITER
from yggdrasil.kook.model import Incoming
from yggdrasil.people import People, resolve
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import Settings
from yggdrasil.transcript.schema import ChatContextDisplay, ChatMessage
from yggdrasil.triggers.index import Resolved

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from yggdrasil.memory.search import MessageView


@dataclass(slots=True)
class TargetMessage:
    kook_message_id: str
    user_id: str
    sender_name: str
    text: str
    segments: list[dict[str, Any]]
    is_self: bool
    sent_at: datetime | None = None
    channel_id: str | None = None

    def snapshot(self) -> ChatMessage:
        return ChatMessage(
            kook_message_id=self.kook_message_id,
            channel_id=self.channel_id,
            user_id=self.user_id,
            sender_name=self.sender_name,
            sent_at=self.sent_at,
            segments=self.segments,
            is_self=self.is_self,
        )


@dataclass
class BuiltContext:
    prompt: str
    target: TargetMessage
    trigger: TargetMessage
    handles: Handles
    request_text: str
    mentioned: list[str] = field(default_factory=list)
    nonce: str = ""
    display: ChatContextDisplay | None = None
    focus: list[str] = field(default_factory=list)


class _Blocks:
    def __init__(self, nonce: str | None = None) -> None:
        self.nonce = nonce or secrets.token_hex(3)
        self.parts: list[str] = []
        self.tags: list[str] = []

    def add(self, tag: str, body: str, attrs: str = "") -> None:
        self.tags.append(tag)
        name = f"{tag}-{self.nonce}"
        body = body.replace(self.nonce, "*" * len(self.nonce))
        self.parts.append(f"<{name}{attrs}>\n{body}\n</{name}>")

    def note(self, text: str) -> None:
        self.parts.append(text)


def _ts(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%m-%d %H:%M") if dt else ""


def trigger_message(incoming: Incoming, self_id: str) -> TargetMessage:
    return TargetMessage(
        kook_message_id=incoming.message_id,
        channel_id=incoming.channel_id,
        user_id=incoming.user_id,
        sender_name=incoming.sender.name.strip(),
        text=normalize(incoming.segments).text,
        segments=incoming.segments,
        is_self=incoming.user_id == self_id,
        sent_at=datetime.fromtimestamp(incoming.time).astimezone(),
    )


async def _own_segments(channel_id: str, kook_message_id: str) -> list[dict[str, Any]] | None:
    async with get_session() as session:
        return await session.scalar(
            select(Message.segments)
            .where(
                Message.channel_id == channel_id,
                Message.kook_message_id == kook_message_id,
                Message.is_self,
                Message.sent_at > datetime.now(UTC) - timedelta(days=30),
            )
            .order_by(Message.sent_at.desc())
            .limit(1)
        )


async def target_message(incoming: Incoming, resolved: Resolved, self_id: str) -> TargetMessage:
    if resolved.target in ("quoted_or_self", "quoted") and incoming.reply is not None:
        r = incoming.reply
        segs = r.segments
        is_self = r.sender.user_id == self_id
        if is_self and incoming.channel_id is not None:
            await WRITER.flush()
            segs = await _own_segments(incoming.channel_id, r.message_id) or segs
        return TargetMessage(
            kook_message_id=r.message_id,
            channel_id=r.channel_id,
            user_id=r.sender.user_id,
            sender_name=r.sender.name.strip(),
            text=normalize(segs).text,
            segments=segs,
            is_self=is_self,
            sent_at=datetime.fromtimestamp(r.time).astimezone(),
        )
    return trigger_message(incoming, self_id)


# --- glossary matching (guild-shared) ---------------------------------------------------------------------------

_automata: dict[str, tuple[float, Any]] = {}
_GLOSSARY_TTL = 300


async def _glossary_hits(guild_id: str, texts: list[str]) -> list[Any]:
    from yggdrasil.memory.search import glossary_terms

    cached = _automata.get(guild_id)
    if cached is None or time.monotonic() - cached[0] > _GLOSSARY_TTL:
        terms = await glossary_terms(guild_id)
        auto = ahocorasick.Automaton()
        for t in terms:
            for word in [t.term, *t.aliases]:
                if word and len(word) >= 2:
                    auto.add_word(word.lower(), t)
        if len(auto):
            auto.make_automaton()
        cached = (time.monotonic(), auto)
        _automata[guild_id] = cached
    auto = cached[1]
    if not len(auto):
        return []
    found: dict[str, Any] = {}
    for text in texts:
        for _, term in auto.iter(text.lower()):
            found.setdefault(term.term, term)
    return list(found.values())[:12]


# --- assembly ---------------------------------------------------------------------------------------------------

_QUOTE_LEVELS = 3


def chat_line(m: "MessageView", people: People, handles: Handles, replies: "Mapping[str, MessageView]") -> str:
    who = people.who(m.user_id, m.sender_name, is_self=m.is_self)
    if m.reply_to_msg_id is not None:
        target = replies.get(m.reply_to_msg_id)
        if target is not None:
            who += f" 回复 #{target.id} {people.who(target.user_id, target.sender_name, is_self=target.is_self)}"
        else:
            who += " 回复一条更早的消息"
    return f"[{_ts(m.sent_at)}] #{m.id} {who}: {people.mentions(m.text)}{handles.register(m.segments or [])}"


async def reply_targets(channel_id: str | None, msgs: "Sequence[MessageView]") -> "dict[str, MessageView]":
    from yggdrasil.memory import search

    wanted = {m.reply_to_msg_id for m in msgs if m.reply_to_msg_id is not None}
    if not channel_id or not wanted:
        return {}
    found = {m.kook_message_id: m for m in msgs if m.kook_message_id in wanted}
    if missing := wanted - found.keys():
        try:
            found = {**await search.find_by_msg_ids(channel_id, missing), **found}
        except Exception:
            LOGGER.exception("reply target lookup failed")
    return found


def chat_tail(lines: "Sequence[str]", budget: int) -> tuple[int, str]:
    first, size = len(lines) - 1, len(lines[-1])
    while first > 0 and size + 1 + len(lines[first - 1]) <= budget:
        first -= 1
        size += 1 + len(lines[first])
    return first, "\n".join(lines[first:])[:budget]


async def _quoted_row(channel_id: str | None, target: TargetMessage) -> "MessageView | None":
    from yggdrasil.memory import search

    if not channel_id:
        return None
    try:
        return await search.find_by_msg_id(channel_id, target.kook_message_id)
    except Exception:
        LOGGER.exception("quoted message lookup failed")
        return None


async def _reply_chain(channel_id: str, reply_to: str | None) -> list["MessageView"]:
    from yggdrasil.memory import search

    chain: list[MessageView] = []
    while reply_to is not None and len(chain) < _QUOTE_LEVELS - 1:
        try:
            m = await search.find_by_msg_id(channel_id, reply_to)
        except Exception:
            LOGGER.exception("reply chain lookup failed")
            break
        if m is None or any(c.id == m.id for c in chain):
            break
        chain.append(m)
        reply_to = m.reply_to_msg_id
    return chain[::-1]


async def build_context(
    incoming: Incoming,
    resolved: Resolved,
    settings: Settings,
    *,
    self_id: str,
    bot_name: str,
    request_text: str,
    workspace: bool = False,
    recall: bool | None = None,
) -> BuiltContext:
    from yggdrasil.memory import search

    snap = snapshot()
    handles = Handles()
    trigger = trigger_message(incoming, self_id)
    target = await target_message(incoming, resolved, self_id)
    channel_id = incoming.channel_id
    guild_id = incoming.guild_id or (snap.guild_of(channel_id) if channel_id else None)
    cfg = settings.context
    blocks = _Blocks()
    background = resolved.handler != "tldr"

    quoted = target.kook_message_id != trigger.kook_message_id
    trigger_norm = normalize(trigger.segments)
    mentioned = trigger_norm.mentions
    bare_tldr = (
        resolved.handler == "tldr"
        and not quoted
        and not request_text.strip()
        and not trigger_norm.attachments
        and not trigger_norm.urls
    )
    if channel_id is not None:
        await WRITER.flush()
    recent: list[search.MessageView] = []
    if channel_id is not None and (background or bare_tldr):
        try:
            recent = await search.recent_messages(channel_id, limit=cfg.recent_messages, minutes=cfg.recent_minutes)
        except Exception:
            LOGGER.exception("recent_messages failed")
    quoted_row = await _quoted_row(channel_id, target) if quoted else None
    chain: list[search.MessageView] = []
    if quoted and channel_id is not None:
        reply_to = quoted_row.reply_to_msg_id if quoted_row else normalize(target.segments).reply_to
        chain = await _reply_chain(channel_id, reply_to)
    replies = await reply_targets(channel_id, [*chain, *recent])
    try:
        people = await resolve(
            guild_id,
            {
                trigger.user_id,
                target.user_id,
                *mentioned,
                *normalize(target.segments).mentions,
                *(m.user_id for m in chain),
                *(u for m in chain for u in normalize(m.segments or []).mentions),
            },
            bot_name=bot_name,
            self_id=self_id,
            texts=[request_text, *(m.text for m in recent), *(m.text for m in chain)],
        )
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))

    trigger.text = normalize(trigger.segments, people.mention).text
    target.text = trigger.text if target is trigger else normalize(target.segments, people.mention).text
    request_text = people.mentions(request_text)

    chain_lines = [chat_line(m, people, handles, replies) for m in chain]
    target_suffix = handles.register(target.segments, urls=True) if quoted else ""
    trigger_suffix = handles.register(trigger.segments, urls=True)
    focus = list(handles.items)

    recent_ids: list[int] = []
    if recent:
        lines = [chat_line(m, people, handles, replies) for m in recent]
        first, text = chat_tail(lines, cfg.max_context_chars)
        recent_ids = [m.id for m in recent[first:]]
        blocks.add("最近聊天", text)

    if chain_lines:
        blocks.add("被引用消息回复的消息", "\n".join(chain_lines))
    if quoted:
        number = f' 编号="#{quoted_row.id}"' if quoted_row else ""
        blocks.add(
            "被引用的消息",
            f"{target.text}{target_suffix}",
            f' 发送者="{people.who(target.user_id, target.sender_name, is_self=target.is_self)}" '
            f'时间="{_ts(target.sent_at)}"{number}',
        )

    if guild_id is not None:
        others = {trigger.user_id, target.user_id, *mentioned} - {self_id}
        if cfg.include_profiles and background and others:
            try:
                profiles = list((await search.profiles(guild_id, sorted(others))).values())
            except Exception:
                LOGGER.exception("profiles lookup failed")
                profiles = []
            if profiles:
                lines = [f"- {people.who(p.user_id, p.display_name)}：{p.summary}" for p in profiles if p.summary]
                if lines:
                    blocks.add("相关成员画像", "\n".join(lines))
        if cfg.include_glossary:
            try:
                hits = await _glossary_hits(guild_id, [trigger.text, target.text, request_text])
            except Exception:
                LOGGER.exception("glossary lookup failed")
                hits = []
            if hits:
                blocks.add("服务器黑话", "\n".join(f"- {t.term}：{t.definition}" for t in hits))
        if cfg.include_open_topics and background:
            try:
                topics = await search.open_topics(
                    guild_id,
                    channel_id=channel_id,
                    limit=5,
                    role_ids=tuple(str(r) for r in incoming.sender.role_ids),
                )
            except Exception:
                LOGGER.exception("open_topics failed")
                topics = []
            if topics:
                topic_lines = []
                for t in topics:
                    ch_tag = (
                        f"[#{snap.channel_name(t.channel_id) or t.channel_id}] "
                        if channel_id and t.channel_id != channel_id
                        else ""
                    )
                    topic_lines.append(f"- {ch_tag}{t.title}：{t.summary}")
                blocks.add("进行中的话题", "\n".join(topic_lines))
    if channel_id is not None and workspace:
        await _add_workspace_listing(blocks, channel_id)

    sender = f' 发送者="{people.who(trigger.user_id, trigger.sender_name)}"'
    link_focus = ""
    if resolved.handler == "tldr" and not quoted and (links := URL_RE.findall(request_text)):
        rest = " ".join(URL_RE.sub(" ", request_text).split()).lstrip("，。！？、：；,.!?:; ")
        link_focus = rest if any(c.isalnum() for c in rest) else ""
    focused = resolved.handler == "tldr" and ((quoted and bool(request_text)) or bool(link_focus))
    if link_focus:
        blocks.add("请求", f"{' '.join(links)}{trigger_suffix}", sender)
        blocks.add("关注点", link_focus, sender)
    elif focused:
        blocks.add("关注点", f"{request_text}{trigger_suffix}", sender)
    else:
        blocks.add("请求", f"{request_text or trigger.text}{trigger_suffix}", sender)
    if recall is not None:
        blocks.note(render("recall_note", allowed=recall))
    if bare_tldr and recent_ids:
        blocks.note(
            "这条 tldr 没有引用消息：目标是「最近聊天」里请求之前的内容。请求前刚有人发了链接、文件、"
            "卡片、长消息或图片时就总结那一条；没有的话概括最近大家在聊什么。"
        )
    if resolved.handler == "tldr":
        ia = settings.interaction
        blocks.note(
            f"篇幅：只回答关注点，正文不超过 {ia.tldr_focus_max_chars} 字。"
            if focused
            else f"篇幅：正文不超过 {ia.tldr_max_chars} 字，挑最重要的说，不要罗列所有细节。"
        )
    display = ChatContextDisplay(
        recent=recent_ids,
        quoted=target.snapshot() if quoted else None,
        trigger=trigger.snapshot(),
        blocks=blocks.tags,
        chain=[
            ChatMessage(
                id=m.id,
                kook_message_id=m.kook_message_id,
                channel_id=m.channel_id,
                user_id=m.user_id,
                sender_name=m.sender_name,
                sent_at=m.sent_at,
                segments=m.segments or [],
                is_self=m.is_self,
            )
            for m in chain
        ],
    )
    return BuiltContext(
        "\n\n".join(blocks.parts),
        target,
        trigger,
        handles,
        request_text,
        mentioned,
        nonce=blocks.nonce,
        display=display,
        focus=focus,
    )


async def _add_workspace_listing(blocks: _Blocks, channel_id: str) -> None:
    from yggdrasil.sandbox import manager

    try:
        listing = await manager().listing(channel_id)
    except Exception:
        LOGGER.exception("workspace listing failed")
        return
    if listing is not None:
        blocks.add("工作区", listing or "本频道还没有工作区。")


async def build_followup_context(
    incoming: Incoming,
    settings: Settings,
    *,
    self_id: str,
    bot_name: str,
    request_text: str,
    handles: Handles,
    nonce: str,
    answer_msg_id: str,
    seen_message_id: int | None,
    since: datetime,
    now: str,
    recall: bool,
    workspace: bool = False,
) -> BuiltContext:
    from yggdrasil.memory import search

    channel_id = incoming.channel_id
    if channel_id is None:
        raise ValueError("a follow-up continues an answer in a channel")
    guild_id = incoming.guild_id or snapshot().guild_of(channel_id)
    cfg = settings.context
    blocks = _Blocks(nonce)
    trigger = trigger_message(incoming, self_id)
    await WRITER.flush()
    try:
        newer = await search.messages_after(channel_id, seen_message_id, since)
    except Exception:
        LOGGER.exception("messages_after failed")
        newer = []
    upto = next((i for i, m in enumerate(newer) if m.kook_message_id == trigger.kook_message_id), len(newer))
    between = [m for m in newer[:upto] if not (m.is_self and m.kook_message_id == answer_msg_id)]
    mentioned = normalize(trigger.segments).mentions
    try:
        people = await resolve(
            guild_id,
            {trigger.user_id, *mentioned},
            bot_name=bot_name,
            self_id=self_id,
            texts=[request_text, *(m.text for m in between)],
        )
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))
    trigger.text = normalize(trigger.segments, people.mention).text
    request_text = people.mentions(request_text)
    known = set(handles.items)
    trigger_suffix = handles.register(trigger.segments, urls=True)
    focus = [key for key in handles.items if key not in known]

    blocks.note(
        f"现在是 {now}（北京时间），以这个时间为准。这条请求回复的是你上面的回答：直接接着上面的对话回应它，"
        "不必沿用上一次的任务格式，上面查过、说过的不用重来。"
        + ("「中间的新增对话」是那之后本频道里说的话，只是背景。" if between else "")
    )
    shown: list[int] = []
    if between:
        replies = await reply_targets(channel_id, between)
        lines = [chat_line(m, people, handles, replies) for m in between]
        first, text = chat_tail(lines, cfg.max_context_chars)
        shown = [m.id for m in between[first:]]
        blocks.add("中间的新增对话", text)
    if cfg.include_glossary and guild_id:
        try:
            hits = await _glossary_hits(guild_id, [trigger.text, request_text])
        except Exception:
            LOGGER.exception("glossary lookup failed")
            hits = []
        if hits:
            blocks.add("服务器黑话", "\n".join(f"- {t.term}：{t.definition}" for t in hits))
    if workspace:
        await _add_workspace_listing(blocks, channel_id)
    sender = f' 发送者="{people.who(trigger.user_id, trigger.sender_name)}"'
    blocks.add("请求", f"{request_text or trigger.text}{trigger_suffix}", sender)
    blocks.note(render("recall_note", allowed=recall))
    display = ChatContextDisplay(recent=shown, trigger=trigger.snapshot(), blocks=blocks.tags)
    return BuiltContext(
        "\n\n".join(blocks.parts),
        trigger,
        trigger,
        handles,
        request_text,
        mentioned,
        nonce=blocks.nonce,
        display=display,
        focus=focus,
    )
