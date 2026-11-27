"""Adaptive KOOK reply rendering: concise KMarkdown vs structured CardMessage.

Chooses the most readable presentation for humans automatically:
- Short, single-section answers without sources or multiple pictures go out as lightweight KMarkdown (type=9).
- Long, multi-section, titled, sourced, multi-image, TL;DR or report answers go out as crisp KOOK CardMessages
  (type=10) with headers, well-chunked KMarkdown sections, full-width image containers, clickable source links,
  and an understated gray context footer for metadata.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from yggdrasil.agent.schema import BotReply
from yggdrasil.settings import InteractionSettings

META_TITLE = "⚙ 元数据"

# KOOK hard limits per CardMessage (leave comfortable margin).
MAX_CARDS_PER_MSG = 5
MAX_MODULES_PER_MSG = 45
MAX_CARD_JSON_BYTES = 45_000
MAX_HEADER_CHARS = 96
MAX_KMD_MODULE_CHARS = 4500

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[*+]\s+", re.MULTILINE)
_SLOT_RE = re.compile(r"\x00(\d+)\x00")
_MD_MSG_ANCHOR_RE = re.compile(r"\[([^\]]+)\]\(#(?:msg:)?(\d+)\)")
_BARE_MSG_ANCHOR_RE = re.compile(r"(?<![A-Za-z0-9_#/(\[])#msg:(\d+)\b")
_SOURCE_MSG_ANCHOR_RE = re.compile(r"^#(?:msg:)?(\d+)$")
_MD_THREAD_ANCHOR_RE = re.compile(r"\[([^\]]+)\]\(#thread:(\d+)(?:/(\d+))?\)")
_BARE_THREAD_ANCHOR_RE = re.compile(r"(?<![A-Za-z0-9_#/(\[])#thread:(\d+)(?:/(\d+))?\b")
_SOURCE_THREAD_ANCHOR_RE = re.compile(r"^#thread:(\d+)(?:/(\d+))?$")
_KOOK_ANCHOR_PREFIX = "https://www.kookapp.cn/direct/anchor/"


def kook_anchor_url(guild_id: str, channel_id: str, target_id: str, post_id: str | None = None) -> str:
    """Build a bare KOOK native anchor URL (message, forum thread, or forum post reply)."""
    base = f"{_KOOK_ANCHOR_PREFIX}{guild_id}/{channel_id}/{target_id}"
    return f"{base}/{post_id}" if post_id else base


def extract_message_anchor_ids(reply: BotReply) -> set[int]:
    """Collect DB message IDs referenced via ``#msg:<id>`` or ``[text](#<id>)`` in ``reply``."""
    ids: set[int] = set()
    for section in reply.sections:
        text = _FENCE_RE.sub("", section.content)
        for m in _MD_MSG_ANCHOR_RE.finditer(text):
            ids.add(int(m.group(2)))
        for m in _BARE_MSG_ANCHOR_RE.finditer(text):
            ids.add(int(m.group(1)))
    for src in reply.sources:
        if src.url and (m := _SOURCE_MSG_ANCHOR_RE.match(src.url.strip())):
            ids.add(int(m.group(1)))
        for m in _MD_MSG_ANCHOR_RE.finditer(src.label):
            ids.add(int(m.group(2)))
        for m in _BARE_MSG_ANCHOR_RE.finditer(src.label):
            ids.add(int(m.group(1)))
    return ids


def extract_thread_anchor_ids(reply: BotReply) -> set[str]:
    """Collect KOOK forum ``thread_id`` values referenced via ``#thread:<tid>[/<pid>]`` in ``reply``."""
    ids: set[str] = set()
    for section in reply.sections:
        text = _FENCE_RE.sub("", section.content)
        for m in _MD_THREAD_ANCHOR_RE.finditer(text):
            ids.add(m.group(2))
        for m in _BARE_THREAD_ANCHOR_RE.finditer(text):
            ids.add(m.group(1))
    for src in reply.sources:
        if src.url and (m := _SOURCE_THREAD_ANCHOR_RE.match(src.url.strip())):
            ids.add(m.group(1))
        for m in _MD_THREAD_ANCHOR_RE.finditer(src.label):
            ids.add(m.group(2))
        for m in _BARE_THREAD_ANCHOR_RE.finditer(src.label):
            ids.add(m.group(1))
    return ids


@dataclass(frozen=True, slots=True)
class UploadedPicture:
    url: str
    caption: str = ""
    blob: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedPlan:
    kind: Literal["kmarkdown", "card"]
    kmarkdown: str = ""
    card_batches: tuple[list[dict[str, Any]], ...] = ()
    pictures: tuple[UploadedPicture, ...] = ()
    plain_text: str = ""


def to_kmarkdown(
    text: str,
    *,
    channel_map: Mapping[str, str] | None = None,
    message_anchor_map: Mapping[int, str] | None = None,
    thread_channel_map: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    """Convert standard Markdown to clean KOOK KMarkdown without breaking code blocks.

    - ``channel_map`` (``channel_name -> channel_id``): upgrades ``#channel_name`` to ``(chn)<id>(chn)``.
    - ``message_anchor_map`` (``db_msg_id -> bare KOOK anchor URL``): upgrades ``#msg:<id>`` (and Markdown
      ``[label](#msg:<id>)``) to bare ``https://www.kookapp.cn/direct/anchor/{g}/{c}/{uuid}`` so KOOK renders
      its native jump chip.
    - ``thread_channel_map`` (``thread_id -> (guild_id, channel_id)``): upgrades ``#thread:<tid>`` and
      ``#thread:<tid>/<pid>`` to bare KOOK forum thread/reply anchor URLs.
    """
    stashed: list[str] = []

    def stash(m: re.Match[str]) -> str:
        stashed.append(m.group(0))
        return f"\x00{len(stashed) - 1}\x00"

    norm_channels: dict[str, str] = {}
    if channel_map:
        for raw_name, cid in channel_map.items():
            clean_name = raw_name.strip().lstrip("#")
            if clean_name and cid:
                norm_channels[clean_name] = cid

    def _resolve_thread_url(tid: str, pid: str | None) -> str | None:
        if not thread_channel_map or tid not in thread_channel_map:
            return None
        gid, cid = thread_channel_map[tid]
        return kook_anchor_url(gid, cid, tid, pid)

    def stash_inline(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        # Models often wrap `#频道名`, `#msg:123`, or `#thread:...` in inline backticks (copying prompt examples).
        # Unwrap exact known refs so KOOK renders native chips while keeping real inline code intact.
        if inner.startswith("#"):
            if message_anchor_map and (mm := _SOURCE_MSG_ANCHOR_RE.match(inner)) and inner.startswith("#msg:"):
                if url := message_anchor_map.get(int(mm.group(1))):
                    return url
            if thread_channel_map and (tm := _SOURCE_THREAD_ANCHOR_RE.match(inner)):
                if url := _resolve_thread_url(tm.group(1), tm.group(2)):
                    return url
            if norm_channels:
                wanted = inner[1:].strip()
                for cname, cid in norm_channels.items():
                    if cname.lower() == wanted.lower():
                        return f"(chn){cid}(chn)"
        return stash(m)

    cleaned = text.replace("\x00", "")
    cleaned = _FENCE_RE.sub(stash, cleaned)
    cleaned = _INLINE_CODE_RE.sub(stash_inline, cleaned)
    # KOOK KMarkdown renders bold headings much better than raw `#` lines inside cards/messages.
    cleaned = _HEADING_RE.sub(lambda m: f"**{m.group(1).strip()}**", cleaned)
    cleaned = _BULLET_RE.sub(r"\1- ", cleaned)

    if norm_channels:
        # Longest channel name first so prefixes don't shadow longer sibling channel names.
        for name in sorted(norm_channels, key=len, reverse=True):
            cid = norm_channels[name]
            # Avoid matching URL fragments (/#foo) or ASCII identifiers (foo#bar), while allowing Chinese prose
            # immediately around `#频道名` (e.g. `在#闲聊里` -> `在(chn)123(chn)里`).
            tail_guard = r"(?![A-Za-z0-9_-])" if re.match(r"[A-Za-z0-9_-]", name[-1]) else ""
            pattern = re.compile(rf"(?<![A-Za-z0-9_#/])#{re.escape(name)}{tail_guard}", re.IGNORECASE)
            cleaned = pattern.sub(f"(chn){cid}(chn)", cleaned)

    if message_anchor_map is not None:

        def _replace_md_anchor(m: re.Match[str]) -> str:
            label, mid = m.group(1), int(m.group(2))
            url = message_anchor_map.get(mid)
            return f"[{label}]({url})" if url else label

        def _replace_bare_anchor(m: re.Match[str]) -> str:
            mid = int(m.group(1))
            url = message_anchor_map.get(mid)
            return url if url else f"#{mid}"

        cleaned = _MD_MSG_ANCHOR_RE.sub(_replace_md_anchor, cleaned)
        cleaned = _BARE_MSG_ANCHOR_RE.sub(_replace_bare_anchor, cleaned)

    if thread_channel_map is not None:

        def _replace_md_thread(m: re.Match[str]) -> str:
            label, tid, pid = m.group(1), m.group(2), m.group(3)
            url = _resolve_thread_url(tid, pid)
            return f"[{label}]({url})" if url else label

        def _replace_bare_thread(m: re.Match[str]) -> str:
            tid, pid = m.group(1), m.group(2)
            url = _resolve_thread_url(tid, pid)
            return url if url else m.group(0)

        cleaned = _MD_THREAD_ANCHOR_RE.sub(_replace_md_thread, cleaned)
        cleaned = _BARE_THREAD_ANCHOR_RE.sub(_replace_bare_thread, cleaned)

    return _SLOT_RE.sub(lambda m: stashed[int(m.group(1))], cleaned).strip()


def split_kmarkdown(text: str, limit: int) -> list[str]:
    """Split KMarkdown on paragraph/line boundaries within ``limit`` chars, keeping code fences balanced."""
    limit = min(max(limit, 200), MAX_KMD_MODULE_CHARS)
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    buf = ""
    in_fence = False
    fence_lang = ""

    def flush() -> None:
        nonlocal buf
        piece = buf.strip()
        if not piece:
            buf = ""
            return
        if in_fence:
            piece += "\n```"
        chunks.append(piece)
        buf = f"```{fence_lang}\n" if in_fence else ""

    for line in text.splitlines():
        stripped = line.lstrip()
        is_fence_line = stripped.startswith("```")
        extra_close = 4 if in_fence else 0
        if buf and len(buf) + len(line) + 1 + extra_close > limit:
            flush()
        while len(line) + extra_close > limit:
            take = limit - extra_close - (len(buf) + 1 if buf else 0)
            if take < 40:
                flush()
                take = limit - extra_close - (len(buf) + 1 if buf else 0)
            buf = f"{buf}\n{line[:take]}" if buf else line[:take]
            line = line[take:]
            flush()
        buf = f"{buf}\n{line}" if buf else line
        if is_fence_line:
            if not in_fence:
                in_fence = True
                fence_lang = stripped[3:].strip()
            else:
                in_fence = False
                fence_lang = ""

    if buf.strip() and buf.strip() != f"```{fence_lang}".strip():
        piece = buf.strip()
        if in_fence:
            piece += "\n```"
        chunks.append(piece)
    return [c for c in chunks if c]


# Modules accepted inside KOOK's ``theme="invisible"`` borderless card (no file/audio/video/countdown/invite/accessory).
_INVISIBLE_ALLOWED_MODULES = frozenset(
    {"header", "section", "divider", "context", "action-group", "container", "image-group"}
)


def _sanitize_invisible_modules(modules: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every module complies with KOOK's ``invisible`` card restrictions."""
    out: list[dict[str, Any]] = []
    for mod in modules:
        mt = mod.get("type")
        if mt not in _INVISIBLE_ALLOWED_MODULES:
            continue
        if mt == "section" and "accessory" in mod:
            cleaned = {k: v for k, v in mod.items() if k not in ("accessory", "mode")}
            out.append(cleaned)
        else:
            out.append(mod)
    return out


def choose_format(
    reply: BotReply,
    cfg: InteractionSettings,
    *,
    picture_count: int = 0,
    force_card: bool = False,
) -> Literal["kmarkdown", "card"]:
    """Pick the most human-readable KOOK output format for ``reply``."""
    if cfg.reply_style in ("card", "invisible"):
        return "card"
    if cfg.reply_style == "kmarkdown":
        return "kmarkdown"

    # Adaptive mode (default).
    if force_card or reply.style == "card":
        return "card"
    full = reply.full_text()
    has_structure = (
        bool(reply.title and reply.title.strip())
        or len(reply.sections) > 1
        or any(bool(s.heading and s.heading.strip()) for s in reply.sections)
        or bool(reply.sources)
    )
    if reply.style == "text" and not has_structure and picture_count <= 1 and len(full) <= cfg.adaptive_card_chars:
        return "kmarkdown"
    if has_structure or picture_count >= 2 or len(full) > cfg.adaptive_card_chars:
        return "card"
    # Single short message with at most 1 picture (and very short text) feels most natural as lightweight KMarkdown.
    if picture_count == 1 and len(full) > 160:
        return "card"
    return "kmarkdown"


def _sources_kmarkdown(
    reply: BotReply,
    *,
    channel_map: Mapping[str, str] | None = None,
    message_anchor_map: Mapping[int, str] | None = None,
    thread_channel_map: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    lines = ["**参考来源**"]
    for s in reply.sources[:10]:
        label = s.label.strip().replace("[", "(").replace("]", ")")
        url = (s.url or "").strip()
        if url and (m := _SOURCE_MSG_ANCHOR_RE.match(url)):
            url = (message_anchor_map or {}).get(int(m.group(1)), "")
        elif url and (tm := _SOURCE_THREAD_ANCHOR_RE.match(url)):
            tid, pid = tm.group(1), tm.group(2)
            if thread_channel_map and tid in thread_channel_map:
                gid, cid = thread_channel_map[tid]
                url = kook_anchor_url(gid, cid, tid, pid)
            else:
                url = ""
        if url and url.startswith(_KOOK_ANCHOR_PREFIX):
            # Keep KOOK anchor URLs bare so KOOK natively renders them instead of Markdown link text.
            rendered_label = to_kmarkdown(
                label,
                channel_map=channel_map,
                message_anchor_map=message_anchor_map,
                thread_channel_map=thread_channel_map,
            )
            lines.append(f"- {rendered_label} {url}" if rendered_label else f"- {url}")
        elif url:
            lines.append(f"- [{label}]({url})")
        else:
            rendered_label = to_kmarkdown(
                label,
                channel_map=channel_map,
                message_anchor_map=message_anchor_map,
                thread_channel_map=thread_channel_map,
            )
            lines.append(f"- {rendered_label}")
    return "\n".join(lines)


def _pack_card_batches(modules: list[dict[str, Any]], *, theme: str = "primary") -> tuple[list[dict[str, Any]], ...]:
    """Pack ``modules`` into one or more KOOK CardMessage payloads respecting module and byte limits."""
    if theme == "invisible":
        modules = _sanitize_invisible_modules(modules)
    if not modules:
        return ()
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    def commit() -> None:
        nonlocal current
        if current:
            batches.append([{"type": "card", "theme": theme, "size": "lg", "modules": current}])
            current = []

    for mod in modules:
        candidate = [*current, mod]
        if len(candidate) > MAX_MODULES_PER_MSG:
            commit()
            current = [mod]
            continue
        encoded = json.dumps([{"type": "card", "theme": theme, "size": "lg", "modules": candidate}], ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_CARD_JSON_BYTES and current:
            commit()
            current = [mod]
        else:
            current = candidate
    commit()
    return tuple(batches)


def build_card_batches(
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    *,
    images: Mapping[str, UploadedPicture] | None = None,
    attached: Sequence[UploadedPicture] = (),
    channel_map: Mapping[str, str] | None = None,
    message_anchor_map: Mapping[int, str] | None = None,
    thread_channel_map: Mapping[str, tuple[str, str]] | None = None,
    theme: str = "primary",
) -> tuple[list[dict[str, Any]], ...]:
    """Build KOOK CardMessage JSON payloads for ``reply``."""
    images = images or {}
    sections = list(reply.sections)
    if len(sections) > cfg.max_sections:
        keep, extra = sections[: cfg.max_sections - 1], sections[cfg.max_sections - 1 :]
        merged_content = "\n\n".join((f"**{s.heading}**\n{s.content}" if s.heading else s.content) for s in extra)
        embedded = [k for s in extra for k in s.images]
        sections = [
            *keep,
            extra[0].model_copy(update={"heading": extra[0].heading, "content": merged_content, "images": embedded}),
        ]

    modules: list[dict[str, Any]] = []
    if reply.title and reply.title.strip():
        modules.append(
            {"type": "header", "text": {"type": "plain-text", "content": reply.title.strip()[:MAX_HEADER_CHARS]}}
        )

    for idx, section in enumerate(sections):
        if idx > 0 and section.heading:
            modules.append({"type": "divider"})
        body = section.content.strip()
        if section.heading and section.heading.strip():
            body = f"**{section.heading.strip()}**\n{body}" if body else f"**{section.heading.strip()}**"
        kmd = to_kmarkdown(
            body,
            channel_map=channel_map,
            message_anchor_map=message_anchor_map,
            thread_channel_map=thread_channel_map,
        )
        for chunk in split_kmarkdown(kmd, cfg.section_max_chars):
            modules.append({"type": "section", "text": {"type": "kmarkdown", "content": chunk}})
        for key in section.images:
            if pic := images.get(key):
                modules.extend(_picture_modules(pic))

    for pic in attached:
        modules.extend(_picture_modules(pic))

    if reply.sources:
        modules.append({"type": "divider"})
        modules.append(
            {
                "type": "section",
                "text": {
                    "type": "kmarkdown",
                    "content": _sources_kmarkdown(
                        reply,
                        channel_map=channel_map,
                        message_anchor_map=message_anchor_map,
                        thread_channel_map=thread_channel_map,
                    ),
                },
            }
        )

    if meta_text and meta_text.strip():
        modules.append({"type": "divider"})
        modules.append(
            {
                "type": "context",
                "elements": [{"type": "plain-text", "content": meta_text.strip()[:1000]}],
            }
        )

    return _pack_card_batches(modules, theme=theme)


def _picture_modules(pic: UploadedPicture) -> list[dict[str, Any]]:
    mods: list[dict[str, Any]] = [{"type": "container", "elements": [{"type": "image", "src": pic.url}]}]
    if pic.caption and pic.caption.strip():
        mods.append({"type": "context", "elements": [{"type": "plain-text", "content": pic.caption.strip()[:200]}]})
    return mods


def render_reply(
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    *,
    images: Mapping[str, UploadedPicture] | None = None,
    attached: Sequence[UploadedPicture] = (),
    channel_map: Mapping[str, str] | None = None,
    message_anchor_map: Mapping[int, str] | None = None,
    thread_channel_map: Mapping[str, tuple[str, str]] | None = None,
    force_card: bool = False,
    theme: str = "primary",
) -> RenderedPlan:
    """Adaptively produce either a lightweight KMarkdown plan or structured CardMessage batches."""
    images = images or {}
    ordered_pics = [images[k] for s in reply.sections for k in s.images if k in images] + list(attached)
    fmt = choose_format(reply, cfg, picture_count=len(ordered_pics), force_card=force_card)
    plain = reply.full_text()
    if reply.title and reply.title.strip():
        plain = f"{reply.title.strip()}\n{plain}" if plain else reply.title.strip()

    if fmt == "kmarkdown":
        kmd = to_kmarkdown(
            reply.full_text(),
            channel_map=channel_map,
            message_anchor_map=message_anchor_map,
            thread_channel_map=thread_channel_map,
        )
        if reply.sources:
            src_md = _sources_kmarkdown(
                reply,
                channel_map=channel_map,
                message_anchor_map=message_anchor_map,
                thread_channel_map=thread_channel_map,
            )
            kmd = f"{kmd}\n\n{src_md}" if kmd else src_md
        return RenderedPlan(kind="kmarkdown", kmarkdown=kmd, pictures=tuple(ordered_pics), plain_text=plain)

    batches = build_card_batches(
        reply,
        meta_text,
        cfg,
        images=images,
        attached=attached,
        channel_map=channel_map,
        message_anchor_map=message_anchor_map,
        thread_channel_map=thread_channel_map,
        theme=theme,
    )
    return RenderedPlan(kind="card", card_batches=batches, pictures=tuple(ordered_pics), plain_text=plain)


def build_notice_card(
    *,
    title: str,
    sections: Sequence[str],
    footer: str | None = None,
    theme: str = "warning",
) -> str:
    """Build a single KOOK CardMessage JSON string for admin review / alert notices."""
    modules: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain-text", "content": title[:MAX_HEADER_CHARS]}}
    ]
    for i, text in enumerate(sections):
        if i > 0:
            modules.append({"type": "divider"})
        for chunk in split_kmarkdown(text, 1500):
            modules.append({"type": "section", "text": {"type": "kmarkdown", "content": chunk}})
    if footer:
        modules.append({"type": "divider"})
        modules.append({"type": "context", "elements": [{"type": "plain-text", "content": footer[:500]}]})
    return json.dumps([{"type": "card", "theme": theme, "size": "lg", "modules": modules}], ensure_ascii=False)
