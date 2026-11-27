"""Sending KOOK messages (adaptive KMarkdown / CardMessage, images, files, reactions) and recording self messages."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from yggdrasil.agent.media import media_type
from yggdrasil.agent.schema import BotReply
from yggdrasil.core.log import LOGGER
from yggdrasil.delivery.render import UploadedPicture, render_reply
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.kook import segments as seg
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import KookError
from yggdrasil.kook.segments import Segment
from yggdrasil.settings import InteractionSettings
from yggdrasil.transcript.notes import Blob, blob_id
from yggdrasil.transcript.store import store_blobs

type Picture = tuple[bytes, str]  # (image bytes, caption)


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
