"""Parse KOOK Gateway events and REST message payloads into domain models and ``kook.segments``."""

import json
import re
from typing import Any

from yggdrasil.kook import segments as seg
from yggdrasil.kook.model import (
    ForumPost,
    ForumThread,
    Incoming,
    Message,
    Observed,
    Role,
    Sender,
    ThreadCategory,
    ThreadMedia,
    ThreadTag,
    emoji_origin_guild,
)
from yggdrasil.kook.segments import Segment

# KOOK message types (https://developer.kookapp.cn/doc/event/message)
MSG_TEXT = 1
MSG_IMAGE = 2
MSG_VIDEO = 3
MSG_FILE = 4
MSG_AUDIO = 8
MSG_KMARKDOWN = 9
MSG_CARD = 10
MSG_SYSTEM = 255

# KMarkdown inline tokens:
# (met)<user_id|all|here>(met), (rol)<role_id>(rol), (chn)<channel_id>(chn), (emj)<name>(emj)[<id>]
_KMD_TOKEN = re.compile(
    r"\(met\)(?P<met>[^()]+)\(met\)"
    r"|\(rol\)(?P<rol>\d+)\(rol\)"
    r"|\(chn\)(?P<chn>[^()]+)\(chn\)"
    r"|\(emj\)(?P<emj_name>[^()]*)\(emj\)\[(?P<emj_id>[^\]]+)\]"
)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_KOOK_MSG_LINK_RE = re.compile(
    r"^https?://(?:www\.)?kookapp\.cn/(?:direct/anchor|app/channels)/([^/?#]+)/([^/?#]+)/([^/?#]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_KOOK_ANCHOR_LINK_RE = re.compile(
    r"^https?://(?:www\.)?kookapp\.cn/(?:direct/anchor|app/channels)"
    r"/([^/?#]+)/([^/?#]+)/([^/?#]+)(?:/([^/?#]+))?/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def parse_kook_message_link(url: str) -> tuple[str, str, str] | None:
    """Parse a 3-segment KOOK message anchor URL into ``(guild_id, channel_id, kook_msg_id)``."""
    m = _KOOK_MSG_LINK_RE.match(url.strip())
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)


def parse_kook_anchor_link(url: str) -> tuple[str, str, str, str | None] | None:
    """Parse a KOOK message, forum thread, or forum post reply anchor URL.

    Returns ``(guild_id, channel_id, target_id, post_id)``, where ``post_id`` is non-None for 4-segment
    forum reply links (``.../guild/channel/thread_id/post_id``).
    """
    m = _KOOK_ANCHOR_LINK_RE.match(url.strip())
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def _quote_msg_id(quote: dict[str, Any]) -> str:
    """Extract the canonical KOOK message UUID from a ``quote`` object.

    KOOK Gateway's ``extra.quote`` often puts the message UUID in ``rong_id`` while ``id`` is a short
    quote-record id; REST docs and newer payloads put the UUID in ``id``. Prefer whichever field is a
    UUID (checking ``rong_id`` first when both are UUIDs), falling back to ``id`` then ``rong_id``.
    """
    rong_id = str(quote.get("rong_id") or "").strip()
    qid = str(quote.get("id") or "").strip()
    if rong_id and _UUID_RE.match(rong_id):
        return rong_id
    if qid and _UUID_RE.match(qid):
        return qid
    return qid or rong_id


# Admin permission bit in KOOK guild role bitmask (bit 0 = Administrator).
_PERM_ADMIN = 1 << 0

# System event types worth recording in bot_events (including undocumented batch channel, role, and emoji events).
_OBSERVED_EVENTS = frozenset(
    {
        "joined_guild",
        "exited_guild",
        "updated_guild_member",
        "added_channel",
        "updated_channel",
        "updated_server_type",
        "sort_channel",
        "deleted_channel",
        "batch_added_channel",
        "batch_updated_channel",
        "batch_deleted_channel",
        "added_role",
        "updated_role",
        "deleted_role",
        "added_emoji",
        "updated_emoji",
        "deleted_emoji",
        "self_joined_guild",
        "self_exited_guild",
        "updated_guild",
        "deleted_guild",
        "guild_member_online",
        "guild_member_offline",
        "pinned_message",
        "unpinned_message",
    }
)


def _timestamp_s(raw: Any) -> int:
    try:
        ts = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    # KOOK timestamps are in milliseconds (13 digits).
    return ts // 1000 if ts > 10_000_000_000 else ts


def _sender_role(
    author: dict[str, Any],
    *,
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
) -> Role:
    uid = str(author.get("id") or "")
    if author.get("is_master") or (owner_id and uid == owner_id):
        return "owner"
    roles = author.get("roles") or []
    if admin_role_ids and any(int(r) in admin_role_ids for r in roles if str(r).isdigit()):
        return "admin"
    return "member"


def _parse_role_ids(author: dict[str, Any]) -> tuple[int, ...]:
    raw = author.get("roles")
    if not isinstance(raw, list):
        return ()
    out: list[int] = []
    for r in raw:
        if isinstance(r, int) and not isinstance(r, bool):
            out.append(r)
        elif isinstance(r, str) and r.isdigit():
            out.append(int(r))
    return tuple(out)


def parse_sender(
    author: dict[str, Any] | None,
    *,
    fallback_id: str = "",
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
) -> Sender:
    if not isinstance(author, dict):
        return Sender(user_id=fallback_id, name="")
    uid = str(author.get("id") or fallback_id)
    username = str(author.get("username") or "")
    nickname = str(author.get("nickname") or "")
    name = (nickname or username).strip()
    tag_info = author.get("tag_info")
    tag_text = str(tag_info.get("text") or "") if isinstance(tag_info, dict) else ""
    return Sender(
        user_id=uid,
        name=name,
        username=username,
        role=_sender_role(author, owner_id=owner_id, admin_role_ids=admin_role_ids),
        role_ids=_parse_role_ids(author),
        is_bot=bool(author.get("bot")),
        avatar=str(author.get("avatar") or ""),
        identify_num=str(author.get("identify_num") or ""),
        online=bool(author.get("online")),
        tag_text=tag_text,
        is_sys=bool(author.get("is_sys")),
    )


def _extract_card(raw_content: str) -> tuple[list[dict[str, Any]], str, list[Segment]]:
    """Extract card modules, readable plain text, and any embedded image/file segments from a CardMessage JSON."""
    try:
        cards = json.loads(raw_content)
    except (TypeError, ValueError):
        return [], raw_content.strip(), []
    if not isinstance(cards, list):
        return [], raw_content.strip(), []

    all_modules: list[dict[str, Any]] = []
    texts: list[str] = []
    attachments: list[Segment] = []

    def walk_element(el: Any) -> None:
        if not isinstance(el, dict):
            return
        et = el.get("type")
        if et in ("plain-text", "kmarkdown") and el.get("content"):
            texts.append(str(el["content"]))
        elif et == "image" and el.get("src"):
            attachments.append(seg.image(str(el["src"]), name=str(el.get("title") or "")))
        elif et == "paragraph" and isinstance(el.get("fields"), list):
            for field in el["fields"]:
                walk_element(field)
        elif et == "button" and isinstance(el.get("text"), dict):
            walk_element(el["text"])

    for card in cards:
        if not isinstance(card, dict):
            continue
        modules = card.get("modules") or []
        if not isinstance(modules, list):
            continue
        for mod in modules:
            if not isinstance(mod, dict):
                continue
            all_modules.append(mod)
            mt = mod.get("type")
            if mt in ("header", "section"):
                walk_element(mod.get("text"))
                walk_element(mod.get("accessory"))
            elif mt in ("container", "image-group", "context", "action-group"):
                for el in mod.get("elements") or []:
                    walk_element(el)
            elif mt == "file" and mod.get("src"):
                attachments.append(
                    seg.file(
                        str(mod["src"]),
                        file_name=str(mod.get("title") or "file"),
                        file_size=int(mod.get("size") or 0),
                    )
                )
            elif mt in ("audio", "video") and mod.get("src"):
                attachments.append(
                    {
                        "type": mt,
                        "data": {"url": str(mod["src"]), "name": str(mod.get("title") or ""), "duration": 0},
                    }
                )
            elif mt == "countdown":
                texts.append("[倒计时]")
            elif mt == "invite" and mod.get("code"):
                texts.append(f"[邀请:{mod['code']}]")
    return all_modules, "\n".join(t.strip() for t in texts if t.strip()), attachments


def parse_card_or_kmarkdown(content: str, *, guild_id: str = "") -> list[Segment]:
    """Parse ``content`` as a KOOK CardMessage (including ``invisible`` cards) when valid JSON, else KMarkdown."""
    if content.lstrip().startswith("["):
        modules, plain, card_atts = _extract_card(content)
        if modules:
            return [seg.card(modules, plain), *card_atts]
    return parse_kmarkdown(content, guild_id=guild_id)


def parse_kmarkdown(
    content: str,
    *,
    mention_names: dict[str, str] | None = None,
    role_names: dict[str, str] | None = None,
    channel_names: dict[str, str] | None = None,
    guild_id: str = "",
) -> list[Segment]:
    """Turn a KOOK KMarkdown string into segments (mentions, emojis, channel refs, text)."""
    mention_names = mention_names or {}
    role_names = role_names or {}
    channel_names = channel_names or {}
    out: list[Segment] = []
    pos = 0
    for m in _KMD_TOKEN.finditer(content):
        if m.start() > pos:
            chunk = content[pos : m.start()]
            if chunk:
                out.append(seg.text(chunk))
        pos = m.end()
        if (met := m.group("met")) is not None:
            if met == "all":
                out.append(seg.mention_all())
            elif met == "here":
                out.append(seg.mention_here())
            else:
                out.append(seg.mention(met, mention_names.get(met, "")))
        elif (rol := m.group("rol")) is not None:
            out.append(seg.mention_role(rol, role_names.get(rol, "")))
        elif (chn := m.group("chn")) is not None:
            cname = channel_names.get(chn)
            out.append(seg.text(f"#{cname}" if cname else f"#{chn}"))
        elif (emj_id := m.group("emj_id")) is not None:
            emj_name = m.group("emj_name") or ""
            origin_gid = emoji_origin_guild(emj_id) or guild_id
            out.append(seg.emoji(emj_id, emj_name, guild_id=origin_gid))
    if pos < len(content):
        tail = content[pos:]
        if tail:
            out.append(seg.text(tail))
    return out


def _mention_maps(extra: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    kmd = extra.get("kmarkdown") or {}
    mention_names: dict[str, str] = {}
    role_names: dict[str, str] = {}
    extra_channels: dict[str, str] = {}
    for p in kmd.get("mention_part") or []:
        if isinstance(p, dict) and p.get("id"):
            mention_names[str(p["id"])] = str(p.get("username") or p.get("full_name") or "")
    for p in kmd.get("mention_role_part") or []:
        if isinstance(p, dict) and p.get("role_id") is not None:
            role_names[str(p["role_id"])] = str(p.get("name") or "")
    for p in kmd.get("mention_channel_part") or []:
        if isinstance(p, dict) and p.get("id"):
            extra_channels[str(p["id"])] = str(p.get("name") or "")
    return mention_names, role_names, extra_channels


def _build_segments(
    msg_type: int,
    content: str,
    *,
    extra: dict[str, Any],
    guild_id: str = "",
    channel_names: dict[str, str] | None = None,
    quote_id: str | None = None,
) -> list[Segment]:
    segs: list[Segment] = []
    if quote_id:
        segs.append(seg.reply(quote_id))
    raw_att = extra.get("attachments")
    att: dict[str, Any] = raw_att if isinstance(raw_att, dict) else {}

    if msg_type == MSG_IMAGE:
        url = str(att.get("url") or content).strip()
        if url:
            segs.append(seg.image(url, name=str(att.get("name") or "")))
    elif msg_type == MSG_FILE:
        url = str(att.get("url") or content).strip()
        if url:
            segs.append(
                seg.file(
                    url,
                    file_name=str(att.get("name") or "file"),
                    file_size=int(att.get("size") or 0),
                )
            )
    elif msg_type in (MSG_VIDEO, MSG_AUDIO):
        url = str(att.get("url") or content).strip()
        kind = "video" if msg_type == MSG_VIDEO else "audio"
        if url:
            segs.append(
                {
                    "type": kind,
                    "data": {
                        "url": url,
                        "name": str(att.get("name") or ""),
                        "duration": int(att.get("duration") or 0),
                    },
                }
            )
    elif msg_type == MSG_CARD:
        modules, plain, card_atts = _extract_card(content)
        segs.append(seg.card(modules, plain))
        segs.extend(card_atts)
    elif msg_type == MSG_KMARKDOWN:
        mention_names, role_names, extra_channels = _mention_maps(extra)
        merged_channels = {**extra_channels, **(channel_names or {})}
        segs.extend(
            parse_kmarkdown(
                content,
                mention_names=mention_names,
                role_names=role_names,
                channel_names=merged_channels,
                guild_id=guild_id,
            )
        )
    else:
        if content:
            segs.append(seg.text(content))
    return segs


def _plain_addressed_text(segments: list[Segment], self_id: str) -> str:
    """Plain text addressed to the bot: strip reply segments and leading/trailing @bot mentions."""
    # Remove reply segments first.
    body = [s for s in segments if s.get("type") != "reply"]
    # Strip leading whitespace / @bot mentions.
    while body:
        s = body[0]
        if s["type"] == "mention" and s["data"].get("user_id") == self_id:
            body.pop(0)
            continue
        if s["type"] == "text" and not str(s["data"].get("text", "")).strip():
            body.pop(0)
            continue
        break
    # Strip trailing whitespace / @bot mentions.
    while body:
        s = body[-1]
        if s["type"] == "mention" and s["data"].get("user_id") == self_id:
            body.pop()
            continue
        if s["type"] == "text" and not str(s["data"].get("text", "")).strip():
            body.pop()
            continue
        break
    parts: list[str] = []
    for s in body:
        st = s.get("type")
        data = s.get("data") or {}
        if st == "text":
            parts.append(str(data.get("text", "")))
        elif st == "kmarkdown":
            parts.append(str(data.get("content", "")))
        elif st == "card":
            parts.append(str(data.get("plain_text", "")))
        elif st == "mention":
            uid = str(data.get("user_id") or "")
            if uid != self_id:
                name = str(data.get("name") or uid)
                parts.append(f"@{name} ")
    return "".join(parts).strip()


def parse_quote(
    quote: dict[str, Any] | None,
    *,
    channel_id: str | None,
    guild_id: str | None,
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
    channel_names: dict[str, str] | None = None,
) -> Message | None:
    if not isinstance(quote, dict) or not (msg_id := _quote_msg_id(quote)):
        return None
    qtype = int(quote.get("type") or MSG_TEXT)
    content = str(quote.get("content") or "")
    sender = parse_sender(quote.get("author"), owner_id=owner_id, admin_role_ids=admin_role_ids)
    extra = {
        "attachments": quote.get("attachments"),
        "kmarkdown": quote.get("kmarkdown") or quote.get("mention_info") or {},
    }
    segs = _build_segments(qtype, content, extra=extra, guild_id=guild_id or "", channel_names=channel_names)
    return Message(
        message_id=msg_id,
        channel_id=channel_id,
        guild_id=guild_id,
        sender=sender,
        time=_timestamp_s(quote.get("create_at")),
        segments=segs,
    )


def parse_rest_message(
    raw: dict[str, Any],
    *,
    channel_id: str | None,
    guild_id: str | None,
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
    channel_names: dict[str, str] | None = None,
) -> Message:
    """Parse a message object returned by ``/message/list`` or ``/message/view``."""
    msg_id = str(raw.get("id") or "")
    msg_type = int(raw.get("type") or MSG_TEXT)
    content = str(raw.get("content") or "")
    quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else None
    quote_id = _quote_msg_id(quote) or None if quote else None
    # REST message objects place mention_info & attachments at top level (unlike gateway events which nest in extra).
    extra = {
        "attachments": raw.get("attachments"),
        "kmarkdown": raw.get("mention_info") or {},
    }
    sender = parse_sender(raw.get("author"), owner_id=owner_id, admin_role_ids=admin_role_ids)
    segs = _build_segments(
        msg_type,
        content,
        extra=extra,
        guild_id=guild_id or "",
        channel_names=channel_names,
        quote_id=quote_id,
    )
    updated_raw = raw.get("updated_at")
    updated_at = _timestamp_s(updated_raw) if updated_raw else None
    embeds = [e for e in (raw.get("embeds") or []) if isinstance(e, dict)]
    reactions = [r for r in (raw.get("reactions") or []) if isinstance(r, dict)]
    return Message(
        message_id=msg_id,
        channel_id=channel_id,
        guild_id=guild_id,
        sender=sender,
        time=_timestamp_s(raw.get("create_at")),
        segments=segs,
        updated_at=updated_at or None,
        embeds=embeds,
        reactions=reactions,
    )


def parse_recalled(event: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(channel_id, msg_id)`` if ``event`` is a channel message deletion event, else None."""
    if int(event.get("type") or 0) != MSG_SYSTEM:
        return None
    extra = event.get("extra") or {}
    if not isinstance(extra, dict) or extra.get("type") != "deleted_message":
        return None
    body = extra.get("body") or {}
    if not isinstance(body, dict):
        return None
    channel_id = str(body.get("channel_id") or "")
    msg_id = str(body.get("msg_id") or "")
    if not channel_id or not msg_id:
        return None
    return channel_id, msg_id


def parse_updated_message(event: dict[str, Any]) -> tuple[str, str, str, int] | None:
    """Return ``(channel_id, msg_id, content, updated_at_s)`` for a channel ``updated_message`` event, else None."""
    if int(event.get("type") or 0) != MSG_SYSTEM:
        return None
    extra = event.get("extra") or {}
    if not isinstance(extra, dict) or extra.get("type") != "updated_message":
        return None
    body = extra.get("body") or {}
    if not isinstance(body, dict):
        return None
    channel_id = str(body.get("channel_id") or "")
    msg_id = str(body.get("msg_id") or "")
    if not channel_id or not msg_id:
        return None
    content = str(body.get("content") or "")
    updated_at = _timestamp_s(body.get("updated_at") or event.get("msg_timestamp"))
    return channel_id, msg_id, content, updated_at


def parse_observed(event: dict[str, Any]) -> Observed | None:
    """Return an ``Observed`` record for non-message KOOK system events worth tracking, else None."""
    if int(event.get("type") or 0) != MSG_SYSTEM:
        return None
    extra = event.get("extra") or {}
    if not isinstance(extra, dict):
        return None
    etype = str(extra.get("type") or "")
    if etype not in _OBSERVED_EVENTS:
        return None
    raw_body = extra.get("body")
    if isinstance(raw_body, list):
        items = [x for x in raw_body if isinstance(x, dict)]
        body: dict[str, Any] = {"items": items}
        first_gid = next((str(x.get("guild_id") or "") for x in items if x.get("guild_id")), "")
        guild_id = str(event.get("target_id") or first_gid or "") or None
        channel_id = None
        user_id = None
        operator_id = None
    else:
        body = dict(raw_body) if isinstance(raw_body, dict) else {}
        guild_id = str(body.get("guild_id") or event.get("target_id") or "") or None
        channel_id = (
            str(
                body.get("channel_id")
                or (body.get("id") if "channel" in etype and not etype.startswith("batch_") else "")
                or ""
            )
            or None
        )
        user_id = str(body.get("user_id") or "") or None
        operator_id = str(body.get("operator_id") or "") or None
    return Observed(
        kind=f"kook.{etype}",
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        operator_id=operator_id,
        data=body,
    )


# --- forum / thread parsing -------------------------------------------------------------------------------------


def parse_thread_category(raw: dict[str, Any] | None) -> ThreadCategory | None:
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip()
    if not cid:
        return None
    return ThreadCategory(
        id=cid,
        name=str(raw.get("name") or ""),
        allow=int(raw.get("allow") or 0),
        deny=int(raw.get("deny") or 0),
    )


def _extract_thread_content(raw_content: str | None, *, fallback: str = "") -> tuple[str, tuple[Segment, ...]]:
    """Decode KOOK thread/post CardMessage JSON into readable plain text and media segments."""
    if not raw_content:
        return fallback.strip(), ()
    modules, plain, atts = _extract_card(raw_content)
    segs: list[Segment] = []
    if modules:
        segs.append(seg.card(modules, plain))
        segs.extend(atts)
        return plain or fallback.strip(), tuple(segs)
    text = raw_content.strip()
    if text:
        segs.append(seg.text(text))
    return text or fallback.strip(), tuple(segs)


def parse_forum_thread(
    raw: dict[str, Any],
    *,
    channel_id: str,
    guild_id: str = "",
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
) -> ForumThread:
    """Parse a thread object from ``/thread/list`` or ``/thread/view``."""
    raw_content = raw.get("content")
    content_str = str(raw_content) if raw_content is not None else None
    preview = str(raw.get("preview_content") or "")
    content_text, content_segments = _extract_thread_content(content_str, fallback=preview)
    tags = tuple(
        ThreadTag(id=int(t.get("id") or 0), name=str(t.get("name") or ""), icon=str(t.get("icon") or ""))
        for t in (raw.get("tags") or [])
        if isinstance(t, dict)
    )
    medias = tuple(
        ThreadMedia(
            type=int(m.get("type") or 0),
            src=str(m.get("src") or ""),
            title=str(m.get("title") or ""),
            cover=str(m.get("cover") or ""),
        )
        for m in (raw.get("medias") or [])
        if isinstance(m, dict) and m.get("src")
    )
    return ForumThread(
        thread_id=str(raw.get("id") or ""),
        channel_id=str(raw.get("channel_id") or channel_id),
        guild_id=str(raw.get("guild_id") or guild_id),
        post_id=str(raw.get("post_id") or ""),
        title=str(raw.get("title") or ""),
        cover=str(raw.get("cover") or ""),
        status=int(raw.get("status") or 0),
        preview_content=preview,
        content=content_str,
        content_text=content_text,
        content_segments=content_segments,
        author=parse_sender(raw.get("user"), owner_id=owner_id, admin_role_ids=admin_role_ids),
        category=parse_thread_category(raw.get("category")),
        tags=tags,
        medias=medias,
        create_time=_timestamp_s(raw.get("create_time")),
        latest_active_time=_timestamp_s(raw.get("latest_active_time") or raw.get("create_time")),
        post_count=int(raw.get("post_count") or 0),
        collect_num=int(raw.get("collect_num") or 0),
        is_updated=bool(raw.get("is_updated")),
        content_deleted=bool(raw.get("content_deleted")),
    )


def parse_forum_post(
    raw: dict[str, Any],
    *,
    thread_id: str = "",
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
) -> ForumPost:
    """Parse a comment or nested floor reply from ``/thread/post``."""
    tid = str(raw.get("thread_id") or thread_id)
    content = str(raw.get("content") or "")
    content_text, content_segments = _extract_thread_content(content)
    nested = tuple(
        parse_forum_post(r, thread_id=tid, owner_id=owner_id, admin_role_ids=admin_role_ids)
        for r in (raw.get("replies") or [])
        if isinstance(r, dict)
    )
    return ForumPost(
        post_id=str(raw.get("id") or ""),
        thread_id=tid,
        author=parse_sender(raw.get("user"), owner_id=owner_id, admin_role_ids=admin_role_ids),
        content=content,
        content_text=content_text,
        content_segments=content_segments,
        reply_id=str(raw.get("reply_id") or ""),
        belong_to_post_id=str(raw.get("belong_to_post_id") or ""),
        create_time=_timestamp_s(raw.get("create_time")),
        is_updated=bool(raw.get("is_updated")),
        replies=nested,
    )


def parse_incoming(
    event: dict[str, Any],
    *,
    self_id: str,
    owner_id: str | None = None,
    admin_role_ids: set[int] | None = None,
    channel_names: dict[str, str] | None = None,
) -> Incoming | None:
    """Parse a KOOK Gateway ``s=0`` event dict ``d`` into ``Incoming`` (None for non-message events)."""
    msg_type = int(event.get("type") or 0)
    if msg_type == MSG_SYSTEM or msg_type <= 0:
        return None
    channel_type = str(event.get("channel_type") or "")
    if channel_type not in ("GROUP", "PERSON"):
        return None

    raw_extra = event.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    is_group = channel_type == "GROUP"
    channel_id = str(event.get("target_id") or "") if is_group else None
    guild_id = str(extra.get("guild_id") or "") if is_group else None
    chat_code = str(extra.get("code") or "") or None if not is_group else None
    msg_id = str(event.get("msg_id") or "")
    author_id = str(event.get("author_id") or "")
    if not msg_id or not author_id:
        return None

    sender = parse_sender(
        extra.get("author"),
        fallback_id=author_id,
        owner_id=owner_id,
        admin_role_ids=admin_role_ids,
    )
    quote = parse_quote(
        extra.get("quote"),
        channel_id=channel_id,
        guild_id=guild_id,
        owner_id=owner_id,
        admin_role_ids=admin_role_ids,
        channel_names=channel_names,
    )
    content = str(event.get("content") or "")
    segs = _build_segments(
        msg_type,
        content,
        extra=extra,
        guild_id=guild_id or "",
        channel_names=channel_names,
        quote_id=quote.message_id if quote else None,
    )

    mentions = {str(u) for u in (extra.get("mention") or [])}
    mentions |= {str(s["data"].get("user_id")) for s in segs if s.get("type") == "mention"}
    replied_to_bot = quote is not None and quote.sender.user_id == self_id
    to_me = (not is_group) or (self_id in mentions) or replied_to_bot

    return Incoming(
        message_id=msg_id,
        channel_id=channel_id,
        guild_id=guild_id,
        sender=sender,
        time=_timestamp_s(event.get("msg_timestamp")),
        segments=segs,
        self_id=self_id,
        text=_plain_addressed_text(segs, self_id),
        to_me=to_me,
        reply=quote,
        channel_name=str(extra.get("channel_name") or ""),
        chat_code=chat_code,
    )


def admin_roles_from_list(roles: list[dict[str, Any]]) -> set[int]:
    """Role IDs in a guild that carry the Administrator permission bit."""
    out: set[int] = set()
    for r in roles:
        rid = r.get("role_id")
        perms = int(r.get("permissions") or 0)
        if rid is not None and (perms & _PERM_ADMIN):
            out.add(int(rid))
    return out
