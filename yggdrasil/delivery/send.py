"""Sending KOOK messages (adaptive KMarkdown / CardMessage, images, files, reactions) and recording self messages."""

import asyncio
import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from yggdrasil.agent.media import media_type
from yggdrasil.agent.schema import BotReply
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import KookThread, Message
from yggdrasil.db.session import get_session
from yggdrasil.delivery.render import (
    UploadedPicture,
    extract_message_anchor_ids,
    extract_thread_anchor_ids,
    kook_anchor_url,
    render_reply,
)
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.kook import segments as seg
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import KookError
from yggdrasil.kook.segments import Segment
from yggdrasil.settings import InteractionSettings
from yggdrasil.transcript.notes import Blob, blob_id
from yggdrasil.transcript.store import store_blobs

type Picture = tuple[bytes, str]  # (image bytes, caption)


async def resolve_message_anchors(
    guild_id: str | None,
    channel_id: str | None,
    reply: BotReply,
    *,
    role_ids: Collection[str] = (),
) -> dict[int, str]:
    """Resolve ``#msg:<id>`` / ``[label](#<id>)`` anchors in ``reply`` to KOOK message jump URLs.

    Strictly enforces guild membership, channel privacy (``accessible_channels``), and non-recalled status so
    the model can only link to messages visible in the current scope.
    """
    if not guild_id:
        return {}
    wanted = extract_message_anchor_ids(reply)
    if not wanted:
        return {}
    from yggdrasil.memory.search import accessible_channels

    allowed = accessible_channels(guild_id, channel_id, role_ids=role_ids)
    if not allowed:
        return {}
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Message.id, Message.guild_id, Message.channel_id, Message.kook_message_id).where(
                        Message.id.in_(wanted),
                        Message.guild_id == guild_id,
                        Message.channel_id.in_(allowed),
                        Message.recalled.is_(False),
                    )
                )
            ).all()
    except Exception:
        LOGGER.debug("resolving KOOK message anchors skipped", exc_info=True)
        return {}
    return {
        int(mid): kook_anchor_url(gid, cid, kook_mid) for mid, gid, cid, kook_mid in rows if gid and cid and kook_mid
    }


async def resolve_thread_anchors(
    guild_id: str | None,
    channel_id: str | None,
    reply: BotReply,
    *,
    known_channels: Mapping[str, str] | None = None,
    role_ids: Collection[str] = (),
) -> dict[str, tuple[str, str]]:
    """Resolve ``#thread:<tid>[/<pid>]`` anchors in ``reply`` to ``{thread_id: (guild_id, channel_id)}``.

    Uses per-run ``known_channels`` (from ``list_forum_threads`` / ``read_forum_thread``) first and falls back to
    ``kook_threads`` cache, strictly restricted to ``accessible_forum_channels``.
    """
    if not guild_id:
        return {}
    wanted = extract_thread_anchor_ids(reply)
    if not wanted:
        return {}
    from yggdrasil.forum import accessible_forum_channels

    allowed_ids = {c.channel_id for c in accessible_forum_channels(guild_id, channel_id, role_ids=role_ids)}
    if not allowed_ids:
        return {}
    out: dict[str, tuple[str, str]] = {}
    missing: set[str] = set()
    for tid in wanted:
        cid = (known_channels or {}).get(tid)
        if cid and cid in allowed_ids:
            out[tid] = (guild_id, cid)
        else:
            missing.add(tid)
    if missing:
        try:
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(KookThread.thread_id, KookThread.guild_id, KookThread.channel_id).where(
                            KookThread.thread_id.in_(missing),
                            KookThread.guild_id == guild_id,
                            KookThread.channel_id.in_(allowed_ids),
                        )
                    )
                ).all()
            for tid, gid, cid in rows:
                if gid and cid:
                    out[str(tid)] = (str(gid), str(cid))
        except Exception:
            LOGGER.debug("resolving KOOK thread anchors skipped", exc_info=True)
    return out


def record_self(
    channel_id: str,
    guild_id: str,
    self_id: str,
    nickname: str,
    message_id: str,
    segments: list[Segment],
) -> None:
    """Log a message the bot sent so history and continuation sessions can read it back."""
    WRITER.submit(
        IncomingMessage(
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=self_id,
            kook_message_id=message_id,
            sent_at=datetime.now(UTC),
            sender_name=nickname,
            segments=segments,
            is_self=True,
        )
    )


async def react(kook: Kook, channel_id: str | None, message_id: str, emoji: str) -> bool:
    """Add an emoji reaction to a channel or direct message; failures are logged, never raised."""
    if not emoji or not message_id:
        return False
    try:
        await kook.react(channel_id=channel_id, msg_id=message_id, emoji=emoji)
        return True
    except Exception as e:
        LOGGER.warning("KOOK reaction {} on {} failed: {}", emoji, message_id, e)
        return False


async def unreact(kook: Kook, channel_id: str | None, message_id: str, emoji: str) -> bool:
    """Remove the bot's own emoji reaction from a channel or direct message; failures are logged, never raised."""
    if not emoji or not message_id:
        return False
    try:
        await kook.unreact(channel_id=channel_id, msg_id=message_id, emoji=emoji)
        return True
    except Exception as e:
        LOGGER.warning("KOOK unreact {} on {} failed: {}", emoji, message_id, e)
        return False


async def send_kmarkdown(
    kook: Kook,
    *,
    channel_id: str | None,
    guild_id: str | None,
    user_id: str,
    reply_to: str | None,
    content: str,
    self_id: str,
    nickname: str,
    chat_code: str | None = None,
) -> str:
    msg_id = await kook.send_kmarkdown(
        channel_id=channel_id, user_id=user_id, content=content, quote=reply_to, chat_code=chat_code
    )
    if channel_id is not None:
        segs = ([seg.reply(reply_to)] if reply_to else []) + [seg.kmarkdown(content)]
        record_self(channel_id, guild_id or "", self_id, nickname, msg_id, segs)
    return msg_id


async def _upload_picture(kook: Kook, data: bytes, caption: str, index: int) -> UploadedPicture:
    mime = media_type(data)
    ext = mime.removeprefix("image/").replace("jpeg", "jpg")
    sha = blob_id(data)
    url = await kook.upload_asset(data, f"image-{index}.{ext}")
    try:
        await store_blobs({sha: Blob(data, mime)})
    except Exception as e:
        LOGGER.warning("storing sent picture blob failed: {}", e)
    return UploadedPicture(url=url, caption=caption, blob=sha)


async def send_image(
    kook: Kook,
    *,
    channel_id: str | None,
    guild_id: str | None,
    user_id: str,
    reply_to: str | None,
    images: list[bytes],
    text: str = "",
    self_id: str,
    nickname: str,
    sticker: bool = False,
    chat_code: str | None = None,
) -> str | None:
    """Send optional KMarkdown text followed by one or more images (uploaded via ``/asset/create``)."""
    first_id: str | None = None
    if text.strip():
        first_id = await send_kmarkdown(
            kook,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            reply_to=reply_to,
            content=text,
            self_id=self_id,
            nickname=nickname,
            chat_code=chat_code,
        )
    for idx, data in enumerate(images, 1):
        uploaded = await _upload_picture(kook, data, "", idx)
        quote = reply_to if first_id is None else None
        mid = await kook.send_image_url(
            channel_id=channel_id, user_id=user_id, url=uploaded.url, quote=quote, chat_code=chat_code
        )
        if first_id is None:
            first_id = mid
        if channel_id is not None:
            segs = ([seg.reply(quote)] if quote else []) + [
                seg.image(uploaded.url, sticker=sticker, blob=uploaded.blob)
            ]
            record_self(channel_id, guild_id or "", self_id, nickname, mid, segs)
    return first_id


async def send_reply(
    kook: Kook,
    *,
    channel_id: str | None,
    guild_id: str | None,
    user_id: str,
    reply_to: str | None,
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    self_id: str,
    nickname: str,
    images: Mapping[str, Picture] | None = None,
    attached: Sequence[Picture] = (),
    channel_map: Mapping[str, str] | None = None,
    message_anchor_map: Mapping[int, str] | None = None,
    thread_channel_map: Mapping[str, tuple[str, str]] | None = None,
    force_card: bool = False,
    theme: str = "primary",
    chat_code: str | None = None,
) -> str | None:
    """Adaptively render and send ``reply`` as KMarkdown or KOOK CardMessage(s); returns the first sent msg_id."""
    images = images or {}
    # Upload referenced & attached pictures concurrently to KOOK CDN.
    keys = [k for s in reply.sections for k in s.images if k in images]
    unique_keys = list(dict.fromkeys(keys))
    upload_coros = [_upload_picture(kook, images[k][0], images[k][1], i + 1) for i, k in enumerate(unique_keys)]
    offset = len(unique_keys)
    upload_coros += [_upload_picture(kook, p[0], p[1], offset + i + 1) for i, p in enumerate(attached)]
    uploaded_all = await asyncio.gather(*upload_coros) if upload_coros else []
    uploaded_by_key = dict(zip(unique_keys, uploaded_all[:offset], strict=True))
    uploaded_attached = uploaded_all[offset:]

    plan = render_reply(
        reply,
        meta_text,
        cfg,
        images=uploaded_by_key,
        attached=uploaded_attached,
        channel_map=channel_map,
        message_anchor_map=message_anchor_map,
        thread_channel_map=thread_channel_map,
        force_card=force_card,
        theme=theme,
    )

    if plan.kind == "kmarkdown":
        first_id: str | None = None
        if plan.kmarkdown.strip():
            first_id = await send_kmarkdown(
                kook,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
                reply_to=reply_to,
                content=plan.kmarkdown,
                self_id=self_id,
                nickname=nickname,
                chat_code=chat_code,
            )
        for pic in plan.pictures:
            quote = reply_to if first_id is None else None
            mid = await kook.send_image_url(
                channel_id=channel_id, user_id=user_id, url=pic.url, quote=quote, chat_code=chat_code
            )
            if first_id is None:
                first_id = mid
            if channel_id is not None:
                segs = ([seg.reply(quote)] if quote else []) + [seg.image(pic.url, blob=pic.blob)]
                record_self(channel_id, guild_id or "", self_id, nickname, mid, segs)
        return first_id

    # CardMessage delivery (one or more batches).
    first_card_id: str | None = None
    for idx, batch in enumerate(plan.card_batches):
        quote = reply_to if idx == 0 else None
        card_json = json.dumps(batch, ensure_ascii=False)
        try:
            mid = await kook.send_card(
                channel_id=channel_id, user_id=user_id, card_json=card_json, quote=quote, chat_code=chat_code
            )
        except KookError as e:
            # If KOOK rejects an invisible card due to module restrictions (e.g. code 40000), fall back to primary card.
            if theme == "invisible" and e.code == 40000:
                LOGGER.warning("KOOK rejected invisible card ({}); retrying as primary card", e)
                fallback_batch = [{**card, "theme": "primary"} for card in batch]
                mid = await kook.send_card(
                    channel_id=channel_id,
                    user_id=user_id,
                    card_json=json.dumps(fallback_batch, ensure_ascii=False),
                    quote=quote,
                    chat_code=chat_code,
                )
            else:
                raise
        if first_card_id is None:
            first_card_id = mid
        if channel_id is not None:
            modules = [m for card in batch for m in card.get("modules", [])]
            segs: list[Segment] = ([seg.reply(quote)] if quote else []) + [
                seg.card(modules, plan.plain_text if idx == 0 else "")
            ]
            if idx == 0:
                for pic in plan.pictures:
                    segs.append(seg.image(pic.url, name=pic.caption, blob=pic.blob))
            record_self(channel_id, guild_id or "", self_id, nickname, mid, segs)
    return first_card_id
