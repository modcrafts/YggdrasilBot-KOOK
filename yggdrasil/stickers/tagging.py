"""Vision tagging of collected stickers (worker): description, text, emotion tags, when to use, and moderation.

Task ``sticker`` (a vision route, e.g. Claude Haiku). One call per sticker covers both tagging and moderation; an
unsafe verdict is recorded as ``unsafe_category`` and disables the sticker only when the global
``moderation.disable_unsafe_stickers`` setting is on. Each sticker gets ``MAX_ATTEMPTS`` tries before it is marked
failed.
"""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from yggdrasil.agent.media import model_image
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Sticker, StickerBlob
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.stickers.collect import NOT_A_STICKER

TASK = "sticker"
BATCH = 30
MAX_ATTEMPTS = 3


class StickerTags(BaseModel):
    is_sticker: bool = Field(
        default=True,
        description=(
            "是否为可在群聊中复用表达情绪的表情包/梗图/反应图；"
            "游戏/软件/网页/代码/报错/聊天记录截图、界面信息图、二维码或普通照片填 false"
        ),
    )
    description: str = Field(description="一句话描述画面")
    ocr_text: str = Field(default="", description="图中文字，原样；没有则为空")
    emotions: list[str] = Field(description="1-4 个情绪或语气标签")
    usage: str = Field(description="群聊里什么时候适合发")
    safe: bool
    unsafe_category: Literal["", "sexual", "gore", "extremism", "personal_data"] = ""


async def tag_pending(limit: int = BATCH) -> dict[str, int]:
    """Tag up to ``limit`` downloaded stickers, most-seen first."""
    if not llm.available(TASK):
        return {"skipped": 1}
    async with get_session() as session:
        items = (
            await session.execute(
                select(Sticker.id, Sticker.kind)
                .where(Sticker.status == "untagged")
                .order_by(Sticker.seen_count.desc(), Sticker.id)
                .limit(limit)
            )
        ).all()
    instructions = render("sticker_tag")
    stats = {"tagged": 0, "rejected": 0, "unsafe": 0, "failed": 0}
    for sticker_id, kind in items:
        async with get_session() as session:
            blob = await session.scalar(select(StickerBlob.data).where(StickerBlob.sticker_id == sticker_id))
        if blob is None:
            continue
        image = await asyncio.to_thread(model_image, blob)
        prompt = (
            f"这是服务器自定义表情，给它打标签。{image.note}"
            if kind == "emoji"
            else (
                "这是频道里发出的单张图片，先判断它是不是真正的表情包/梗图"
                f"（不要把游戏/软件/报错/聊天截图或普通照片当成表情包），再打标签。{image.note}"
            )
        )
        try:
            tags = await llm.run_structured(
                TASK,
                StickerTags,
                [prompt, image.content],
                instructions=instructions,
            )
        except Exception as exc:
            LOGGER.warning("sticker {} tagging failed: {!r}", sticker_id, exc)
            stats["failed"] += await _record_failure(sticker_id, exc)
            continue
        rejected = await _save(sticker_id, kind, tags)
        if rejected:
            stats["rejected"] += 1
        else:
            stats["tagged"] += 1
            stats["unsafe"] += not tags.safe
    return stats


async def _save(sticker_id: int, kind: str, tags: StickerTags) -> bool:
    """Persist tagging output; returns True when a non-sticker image (e.g. screenshot) was rejected."""
    now = datetime.now(UTC)
    if kind == "image" and not tags.is_sticker:
        async with get_session() as session:
            await session.execute(
                update(Sticker)
                .where(Sticker.id == sticker_id)
                .values(
                    status="failed",
                    error=NOT_A_STICKER,
                    enabled=False,
                    description=tags.description.strip(),
                    tagged_at=now,
                )
            )
            # Drop the image blob to save space while keeping the sha256 row so resends won't re-tag it.
            await session.execute(delete(StickerBlob).where(StickerBlob.sticker_id == sticker_id))
        LOGGER.info("sticker {} rejected as non-sticker image: {}", sticker_id, tags.description.strip()[:80])
        return True

    values: dict[str, object] = {
        "status": "ready",
        "error": "",
        "description": tags.description.strip(),
        "ocr_text": tags.ocr_text.strip(),
        "emotions": [e.strip() for e in tags.emotions if e.strip()][:4],
        "usage": tags.usage.strip(),
        "tagged_at": now,
        "unsafe_category": "" if tags.safe else tags.unsafe_category or "unspecified",
    }
    if not tags.safe and snapshot().settings().moderation.disable_unsafe_stickers:
        values["enabled"] = False
    async with get_session() as session:
        await session.execute(update(Sticker).where(Sticker.id == sticker_id).values(**values))
    return False


async def _record_failure(sticker_id: int, exc: Exception) -> int:
    """Count the attempt; returns 1 once the sticker gives up for good."""
    async with get_session() as session:
        attempts = await session.scalar(
            update(Sticker)
            .where(Sticker.id == sticker_id)
            .values(tag_attempts=Sticker.tag_attempts + 1, error=f"tagging: {exc}"[:300])
            .returning(Sticker.tag_attempts)
        )
        if attempts is not None and attempts >= MAX_ATTEMPTS:
            await session.execute(update(Sticker).where(Sticker.id == sticker_id).values(status="failed"))
            return 1
    return 0
