"""Turn ``kook.segments`` segments into placeholder text (the memory/LLM view of a message).

Non-text content is never expanded at ingest time: images, files, videos, audio and cards become short placeholders
(or extracted card plain text), and the raw segments remain in ``messages.segments`` so tools can load attachments on
demand.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

URL_RE = re.compile(r"https?://[^\s\u3000，。！？、）」』\])>\"']+(?<![.,;:!?])")
_MEDIA_LABELS = {"audio": "语音", "video": "视频"}


@dataclass(slots=True)
class Attachment:
    kind: str  # image | file | video | audio | card | emoji
    data: dict[str, Any]


@dataclass(slots=True)
class Normalized:
    text: str
    reply_to: str | None = None
    mentions: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def normalize(segments: list[dict[str, Any]], mention: Callable[[str], str] | None = None) -> Normalized:
    """Render KOOK segments into searchable plain text plus structured metadata.

    ``mention`` renders a mention target for a model (``名字(user_id)``); without it the raw ``@<user_id>`` or
    ``@<name>`` is kept for ``messages.plain_text``.
    """
    out = Normalized(text="")
    parts: list[str] = []
    rendered_at = False
    for seg in segments:
        after_at, rendered_at = rendered_at, False
        kind = seg.get("type", "")
        data = seg.get("data") or {}
        match kind:
            case "text":
                text = str(data.get("text", ""))
                if after_at and text[:1].isspace():
                    text = text[1:]
                parts.append(text)
            case "kmarkdown":
                parts.append(str(data.get("content", "")))
            case "mention":
                uid = str(data.get("user_id") or "").strip()
                if uid:
                    out.mentions.append(uid)
                if uid and mention is not None:
                    parts.append(f"@{mention(uid)} ")
                    rendered_at = True
                    continue
                # Store @<uid> in plain_text so people.mentions() can resolve updated aliases later.
                parts.append(f"@{uid} " if uid else f"@{str(data.get('name') or '').lstrip('@')} ")
            case "mention_all":
                parts.append("@全体成员 ")
            case "mention_here":
                parts.append("@在线成员 ")
            case "mention_role":
                name = str(data.get("name") or data.get("role_id") or "角色")
                parts.append(f"@{name} ")
            case "reply":
                mid = str(data.get("message_id") or "").strip()
                if mid:
                    out.reply_to = mid
            case "emoji":
                name = str(data.get("name") or "表情").strip()
                parts.append(f"[表情:{name}]")
                out.attachments.append(Attachment("emoji", data))
            case "image":
                name = str(data.get("name") or "").strip()
                label = (
                    f"[表情包:{name}]"
                    if data.get("sticker") and name
                    else ("[表情包]" if data.get("sticker") else "[图片]")
                )
                parts.append(label)
                out.attachments.append(Attachment("image", data))
            case "audio" | "video":
                parts.append(f"[{_MEDIA_LABELS[kind]}]")
                out.attachments.append(Attachment(kind, data))
            case "file":
                parts.append(f"[文件:{data.get('file_name') or '未命名'}]")
                out.attachments.append(Attachment("file", data))
            case "card":
                plain = str(data.get("plain_text") or "").strip()
                parts.append(plain if plain else "[卡片]")
                # Only register a card#N handle when the card carries an explicit target URL;
                # otherwise its plain_text is already expanded inline above.
                if data.get("url"):
                    out.attachments.append(Attachment("card", data))
            case _:
                parts.append(f"[{kind}]")
    out.text = "".join(parts).strip()
    text_sources = (
        [str(s.get("data", {}).get("text", "")) for s in segments if s.get("type") == "text"]
        + [str(s.get("data", {}).get("content", "")) for s in segments if s.get("type") == "kmarkdown"]
        + [str(s.get("data", {}).get("plain_text", "")) for s in segments if s.get("type") == "card"]
    )
    for url in URL_RE.findall(" ".join(text_sources)):
        if url not in out.urls:
            out.urls.append(url)
    return out
