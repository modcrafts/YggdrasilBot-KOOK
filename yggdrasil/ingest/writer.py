"""Batched persistence of KOOK channel messages plus recall marking."""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert

from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import MESSAGES_INGESTED
from yggdrasil.db.models import Message, UserAlias
from yggdrasil.db.partitions import ensure_month
from yggdrasil.db.session import get_session
from yggdrasil.ingest.normalize import normalize
from yggdrasil.runtime import snapshot
from yggdrasil.stickers.collect import COLLECTOR, PendingFetch, Sighting, UsageKey, record, sightings


@dataclass(slots=True)
class IncomingMessage:
    channel_id: str
    guild_id: str
    user_id: str
    kook_message_id: str
    sent_at: datetime
    sender_name: str
    segments: list[dict[str, Any]]
    is_self: bool = False


_NAME_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_sender_name(name: str) -> str:
    return _NAME_CONTROL.sub("", name).strip()


def strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    return value


def _kind(segments: list[dict[str, Any]]) -> str:
    types = {s.get("type") for s in segments}
    for kind in ("card", "file", "image", "emoji", "audio", "video"):
        if kind in types:
            return kind
    return "text"


class IngestWriter:
    def __init__(self, flush_interval: float = 1.0, max_batch: int = 200) -> None:
        self._queue: list[IncomingMessage] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._flush_interval = flush_interval
        self._max_batch = max_batch

    def submit(self, msg: IncomingMessage) -> None:
        self._queue.append(msg)
        MESSAGES_INGESTED.add(1, {"channel": msg.channel_id, "kind": _kind(msg.segments)})
        if len(self._queue) >= self._max_batch:
            self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("ingest flush failed; {} messages kept for retry", len(self._queue))

    async def flush(self) -> int:
        async with self._lock:
            if not self._queue:
                return 0
            batch, self._queue = self._queue, []
            try:
                await self._write(batch)
            except BaseException:
                self._queue[:0] = batch
                raise
            return len(batch)

    async def _write(self, batch: list[IncomingMessage]) -> None:
        snap = snapshot()
        rows: list[dict[str, Any]] = []
        aliases: dict[tuple[str, str, str], datetime] = {}
        sticker_seen: dict[tuple[str, str], Sighting] = {}
        sticker_usage = Counter[UsageKey]()
        image_candidates: list[tuple[str, str, str]] = []  # (url, name, guild_id)

        for m in batch:
            segments = strip_nul(m.segments)
            norm = normalize(segments)
            name = clean_sender_name(m.sender_name)
            guild_id = m.guild_id or snap.guild_of(m.channel_id) or ""
            rows.append(
                {
                    "sent_at": m.sent_at,
                    "channel_id": m.channel_id,
                    "guild_id": guild_id,
                    "user_id": m.user_id,
                    "kook_message_id": m.kook_message_id,
                    "reply_to_msg_id": norm.reply_to,
                    "sender_name": name,
                    "plain_text": norm.text,
                    "segments": segments,
                    "is_self": m.is_self,
                }
            )
            if name and not m.is_self and guild_id:
                aliases[(guild_id, m.user_id, name)] = m.sent_at
            if (
                not m.is_self
                and guild_id
                and snap.settings(channel_id=m.channel_id, guild_id=guild_id).features.sticker_collect
            ):
                found, img_cands = sightings(segments, guild_id=guild_id)
                for s in found:
                    sticker_seen[s.kind, s.ref] = s
                    sticker_usage[s.kind, s.ref, guild_id] += 1
                image_candidates.extend(img_cands)

        pending: list[PendingFetch] = []
        async with get_session() as session:
            conn = await session.connection()
            for month in {r["sent_at"].replace(day=1, hour=0, minute=0, second=0, microsecond=0) for r in rows}:
                await ensure_month(conn, month)
            await session.execute(insert(Message), rows)
            if aliases:
                stmt = insert(UserAlias).values(
                    [{"guild_id": g, "user_id": u, "name": n, "last_seen": t} for (g, u, n), t in aliases.items()]
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["guild_id", "user_id", "name"], set_={"last_seen": stmt.excluded.last_seen}
                    )
                )
            if sticker_seen:
                pending = await record(session, sticker_seen, sticker_usage)
        COLLECTOR.schedule(pending)
        COLLECTOR.schedule_images(image_candidates)

    async def mark_recalled(self, channel_id: str, kook_message_id: str) -> None:
        await self.flush()
        async with get_session() as session:
            result = await session.execute(
                update(Message)
                .where(
                    Message.channel_id == channel_id,
                    Message.kook_message_id == kook_message_id,
                    Message.sent_at > func.now() - text("interval '30 days'"),
                )
                .values(recalled=True, recalled_at=datetime.now(UTC))
                .returning(Message.id)
            )
            ids = [r[0] for r in result]
            if ids:
                await session.execute(
                    text(
                        "UPDATE topics SET dirty = true WHERE id IN "
                        "(SELECT topic_id FROM topic_messages WHERE message_id = ANY(:ids))"
                    ),
                    {"ids": ids},
                )

    async def update_message_content(
        self, channel_id: str, kook_message_id: str, content: str, *, guild_id: str = ""
    ) -> None:
        """Update stored text & segments when KOOK pushes an ``updated_message`` edit event.

        Handles both KMarkdown edits and CardMessage (including ``invisible`` card) edits so raw Card JSON
        never leaks into ``plain_text``.
        """
        from yggdrasil.kook.parser import parse_card_or_kmarkdown

        await self.flush()
        segs = strip_nul(parse_card_or_kmarkdown(content, guild_id=guild_id))
        norm = normalize(segs)
        async with get_session() as session:
            result = await session.execute(
                update(Message)
                .where(
                    Message.channel_id == channel_id,
                    Message.kook_message_id == kook_message_id,
                    Message.recalled.is_(False),
                    Message.sent_at > func.now() - text("interval '30 days'"),
                )
                .values(plain_text=norm.text, segments=segs)
                .returning(Message.id)
            )
            ids = [r[0] for r in result]
            if ids:
                await session.execute(
                    text(
                        "UPDATE topics SET dirty = true WHERE id IN "
                        "(SELECT topic_id FROM topic_messages WHERE message_id = ANY(:ids))"
                    ),
                    {"ids": ids},
                )


WRITER = IngestWriter()
