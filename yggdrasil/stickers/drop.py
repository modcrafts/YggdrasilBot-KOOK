"""Occasional sticker drops in KOOK channels (``Settings.sticker_drop``).

Usage popularity is shared per guild; drop cadence and activity checks are isolated per channel.
Same-guild custom emojis can be sent natively as KMarkdown ``(emj)name(emj)[id]``; images and cross-guild emojis
are uploaded via ``/asset/create`` and sent as KOOK image messages.
"""

import asyncio
import json
import math
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import exists, func, select

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Sticker, StickerBlob, StickerDrop, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.kv import redis_client
from yggdrasil.llm import registry
from yggdrasil.memory.common import LOCAL_TZ, format_line, people_for
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.runtime import snapshot

TASK = "sticker_pick"
OUTBOX = "ygg:sticker_drop:outbox"
REPEAT_WINDOW = timedelta(days=7)
REPEAT_PENALTY = 0.2
MAX_AGE_S = 120
POLL_S = 2
LINE_MAX_CHARS = 80


def sent_key(channel_id: str) -> str:
    return f"ygg:sticker_drop:sent:{channel_id}"


def mark_key(channel_id: str) -> str:
    return f"ygg:sticker_drop:mark:{channel_id}"


@dataclass(frozen=True, slots=True)
class Candidate:
    id: int
    uses: int
    description: str
    usage: str
    emotions: list[str]
    ocr_text: str

    def criterion(self) -> str:
        parts = [self.description[:40], f"适合：{self.usage[:50]}"]
        if self.emotions:
            parts.append(f"情绪：{'、'.join(self.emotions[:4])}")
        if self.ocr_text:
            parts.append(f"图中文字：{self.ocr_text[:20]}")
        return "；".join(parts)


@dataclass(frozen=True, slots=True)
class Outcome:
    outcome: str
    fit: float | None = None
    sticker_id: int | None = None


def weight(uses: int, recent_sends: int) -> float:
    return (1 + math.log1p(uses)) * REPEAT_PENALTY**recent_sends


def sample[T](items: Sequence[T], weights: Sequence[float], k: int, rng: random.Random) -> list[T]:
    keyed = sorted(((rng.random() ** (1 / w), i) for i, w in enumerate(weights) if w > 0), reverse=True)
    return [items[i] for _, i in keyed[:k]]


def _local_midnight(now: datetime) -> datetime:
    return now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


async def _recent_messages(channel_id: str, limit: int) -> list[MessageView]:
    async with get_session() as session:
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .where(Message.channel_id == channel_id, Message.recalled.is_(False))
            .order_by(Message.id.desc())
            .limit(limit)
        )
        return [MessageView(*r) for r in reversed(rows.all())]


async def _new_messages(channel_id: str, after_id: int, limit: int, since: datetime) -> int:
    async with get_session() as session:
        ids = (
            select(Message.id)
            .where(
                Message.channel_id == channel_id,
                Message.sent_at > since,
                Message.recalled.is_(False),
                Message.is_self.is_(False),
                Message.id > after_id,
            )
            .limit(limit)
        )
        return await session.scalar(select(func.count()).select_from(ids.subquery())) or 0


async def _candidates(guild_id: str, *, guild_only: bool) -> list[Candidate]:
    query = select(Sticker.id).where(
        Sticker.status == "ready",
        Sticker.enabled.is_(True),
        exists().where(StickerBlob.sticker_id == Sticker.id),
    )
    if guild_only:
        query = query.join(
            StickerUsage, (StickerUsage.sticker_id == Sticker.id) & (StickerUsage.guild_id == guild_id)
        ).add_columns(StickerUsage.count)
    else:
        query = query.add_columns(Sticker.seen_count)
    query = query.add_columns(Sticker.description, Sticker.usage, Sticker.emotions, Sticker.ocr_text)
    async with get_session() as session:
        return [Candidate(*r) for r in (await session.execute(query)).all()]


def questions(picks: Sequence[Candidate]) -> dict[str, dict[str, Any]]:
    return {
        "fit": {
            "type": "noul",
            "instructions": "最后几条消息是不是适合用表情包接话的时刻？",
            "criteria": {
                "true": "最后几条是玩梗、吐槽、感叹、调侃、自嘲、发表情或明显的情绪反应，接一张表情很自然",
                "false": "最后几条在讲技术细节、提问、贴链接、讨论具体事情，或者 bot 刚回答完，插一张表情会打断话题",
            },
        },
        "pick": {
            "type": "choice",
            "instructions": "哪张表情包最适合接最近这几条聊天？",
            "criteria": {f"s{c.id}": c.criterion() for c in picks},
        },
    }


async def maybe_drop(channel_id: str, *, now: datetime | None = None, rng: random.Random | None = None) -> Outcome:
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    snap = snapshot()
    guild_id = snap.guild_of(channel_id) or ""
    settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    cfg = settings.sticker_drop
    if not cfg.enabled or cfg.daily_max == 0:
        return Outcome("off")
    if TASK not in snap.routes:
        return Outcome("no_route")
    r = redis_client()
    key = sent_key(channel_id)
    await r.zremrangebyscore(key, "-inf", (now - REPEAT_WINDOW).timestamp())
    sent = cast("list[tuple[str, float]]", await r.zrange(key, 0, -1, withscores=True))
    if sum(at >= _local_midnight(now).timestamp() for _, at in sent) >= cfg.daily_max:
        return Outcome("capped")
    if sent and now.timestamp() - max(at for _, at in sent) < cfg.min_gap_minutes * 60:
        return Outcome("gap")
    mark = int(await r.get(mark_key(channel_id)) or 0)
    since = now - timedelta(days=1)
    if await _new_messages(channel_id, mark, cfg.min_new_messages, since) < cfg.min_new_messages:
        return Outcome("quiet")
    recent = await _recent_messages(channel_id, cfg.context_messages)
    if not recent or now - recent[-1].sent_at > timedelta(minutes=cfg.active_within_minutes):
        return Outcome("idle")
    candidates = await _candidates(guild_id, guild_only=cfg.guild_only)
    if not candidates:
        return Outcome("no_stickers")
    repeats: dict[int, int] = {}
    for member, _ in sent:
        sid = int(member.split(":", 1)[0])
        repeats[sid] = repeats.get(sid, 0) + 1
    picks = sample(candidates, [weight(c.uses, repeats.get(c.id, 0)) for c in candidates], cfg.candidates, rng)
    bot_name = settings.persona.name
    people = await people_for(guild_id, recent, bot_name=bot_name)
    state = {
        "频道最近的消息": "\n".join(format_line(v, people)[:LINE_MAX_CHARS] for v in recent),
        "发表情的人": f"{bot_name}（频道里的 bot，消息里标着「(你)」）",
    }
    result = await registry.decide(TASK, state, questions(picks))
    await r.set(mark_key(channel_id), recent[-1].id)
    pick = result.answers.get("pick") or {}
    picked = {f"s{c.id}": c.id for c in picks}.get(str(pick.get("choice")))
    outcome = _judge(channel_id, result.answers, picked, cfg.min_fit, cfg.chance, rng)
    confidence = pick.get("confidence")
    async with get_session() as session:
        session.add(
            StickerDrop(
                at=now,
                channel_id=channel_id,
                guild_id=guild_id,
                outcome=outcome.outcome,
                fit=outcome.fit,
                sticker_id=picked,
                confidence=float(confidence) if isinstance(confidence, int | float) else None,
                candidates=[c.id for c in picks],
                last_message_id=recent[-1].id,
                model=result.model,
                input_tokens=result.input_tokens,
                cost=result.cost,
            )
        )
    if outcome.outcome == "sent" and outcome.sticker_id is not None:
        sid = outcome.sticker_id
        await r.zadd(key, {f"{sid}:{int(now.timestamp() * 1000)}": now.timestamp()})
        await r.lpush(
            OUTBOX,
            json.dumps({"channel_id": channel_id, "guild_id": guild_id, "sticker_id": sid, "at": now.timestamp()}),
        )
    return outcome


def _judge(
    channel_id: str,
    answers: dict[str, dict[str, Any]],
    picked: int | None,
    min_fit: float,
    chance: float,
    rng: random.Random,
) -> Outcome:
    fit_answer, pick_answer = answers.get("fit") or {}, answers.get("pick") or {}
    if fit_answer.get("type") != "noul" or pick_answer.get("type") != "choice":
        LOGGER.info("sticker drop in {}: the model refused ({}, {})", channel_id, fit_answer, pick_answer)
        return Outcome("refused")
    fit = float(fit_answer["noul"])
    if fit < min_fit:
        return Outcome("unfit", fit)
    if rng.random() >= chance:
        return Outcome("dice", fit)
    if picked is None:
        LOGGER.warning(
            "sticker drop in {}: the model picked {}, not a candidate", channel_id, pick_answer.get("choice")
        )
        return Outcome("refused", fit)
    return Outcome("sent", fit, picked)


async def deliver_forever() -> None:
    from yggdrasil.kook import client as kook_client

    while True:
        if kook_client.connected() is None:
            await asyncio.sleep(2)
            continue
        try:
            item = await redis_client().brpop([OUTBOX], timeout=POLL_S)
            if item is not None:
                await _deliver(json.loads(item[1]))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("sticker drop delivery failed")
            await asyncio.sleep(5)


async def _deliver(pick: dict[str, Any]) -> None:
    from yggdrasil.bot.state import identity
    from yggdrasil.delivery.send import send_image, send_kmarkdown
    from yggdrasil.kook import client as kook_client
    from yggdrasil.kook.model import emoji_origin_guild

    channel_id, sticker_id = str(pick["channel_id"]), int(pick["sticker_id"])
    guild_id = str(pick.get("guild_id") or snapshot().guild_of(channel_id) or "")
    if time.time() - float(pick["at"]) > MAX_AGE_S:
        LOGGER.info("sticker drop in {}: sticker {} is stale; skipped", channel_id, sticker_id)
        return
    kook = kook_client.connected()
    if kook is None or not snapshot().channel_enabled(channel_id):
        return
    async with get_session() as session:
        sticker = await session.get(Sticker, sticker_id)
        data = await session.scalar(select(StickerBlob.data).where(StickerBlob.sticker_id == sticker_id))
    if sticker is None:
        return
    ident = await identity(kook)
    # Same-guild KOOK custom emoji can be sent natively as KMarkdown!
    origin_gid = emoji_origin_guild(sticker.emoji_id) or sticker.guild_id
    if sticker.kind == "emoji" and sticker.emoji_id and origin_gid == guild_id:
        name = sticker.summary or "emoji"
        await send_kmarkdown(
            kook,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id="",
            reply_to=None,
            content=f"(emj){name}(emj)[{sticker.emoji_id}]",
            self_id=ident.self_id,
            nickname=ident.nickname,
        )
        LOGGER.info("sticker drop in {}: sent native KOOK emoji {}", channel_id, sticker_id)
        return
    if data is None:
        return
    await send_image(
        kook,
        channel_id=channel_id,
        guild_id=guild_id,
        user_id="",
        reply_to=None,
        images=[data],
        text="",
        self_id=ident.self_id,
        nickname=ident.nickname,
        sticker=True,
    )
    LOGGER.info("sticker drop in {}: sent sticker image {}", channel_id, sticker_id)
