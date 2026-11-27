"""Persist transcripts and their images."""

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Invocation, Message, Transcript, TranscriptBlob
from yggdrasil.db.session import get_session
from yggdrasil.people import names_in_guilds
from yggdrasil.transcript.capture import TranscriptRecorder, entries, live_stats
from yggdrasil.transcript.notes import Blob, TraceNotes
from yggdrasil.transcript.schema import VERSION, ChatMessage, Entry


def dump(items: list[Entry]) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json", exclude_none=True) for e in items]


def msg_from_row(m: Message) -> ChatMessage:
    return ChatMessage(
        id=m.id,
        kook_message_id=m.kook_message_id,
        channel_id=m.channel_id,
        user_id=m.user_id,
        sender_name=m.sender_name,
        sent_at=m.sent_at,
        segments=list(m.segments),
        is_self=m.is_self,
        recalled=m.recalled,
    )


async def snapshot_messages(ids: set[int]) -> dict[str, ChatMessage]:
    if not ids:
        return {}
    async with get_session() as session:
        rows = (await session.scalars(select(Message).where(Message.id.in_(ids)))).all()
    return {str(m.id): msg_from_row(m) for m in rows}


def _mentioned(segments: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for seg in segments:
        uid = (seg.get("data") or {}).get("user_id") if seg.get("type") == "mention" else None
        if uid:
            out.add(str(uid))
    return out


async def _names(guild_id: str | None, messages: list[ChatMessage]) -> dict[str, str]:
    if not guild_id:
        return {}
    uids = {m.user_id for m in messages} | {u for m in messages for u in _mentioned(m.segments)}
    names = await names_in_guilds((guild_id, u) for u in uids)
    return {u: n for (_, u), n in names.items()}


async def store_blobs(blobs: dict[str, Blob]) -> None:
    if not blobs:
        return
    rows = [{"sha256": sha, "mime": b.mime, "size": len(b.data), "data": b.data} for sha, b in blobs.items()]
    async with get_session() as session:
        await session.execute(pg_insert(TranscriptBlob).values(rows).on_conflict_do_nothing(index_elements=["sha256"]))


async def build_context(
    notes: TraceNotes, channel_id: str | None, guild_id: str | None, items: list[Entry]
) -> dict[str, Any]:
    context: dict[str, Any] = {"version": VERSION, "channel_id": channel_id, "guild_id": guild_id}
    if notes.bot:
        context["bot"] = {"user_id": notes.bot[0], "name": notes.bot[1]}
    if not notes.full:
        return context
    ids = set(notes.message_ids)
    if notes.context:
        ids.update(notes.context.recent)
    messages = await snapshot_messages(ids)
    shown = list(messages.values())
    if notes.context:
        shown += [m for m in (notes.context.trigger, notes.context.quoted) if m is not None]
    context["messages"] = {k: m.model_dump(mode="json") for k, m in messages.items()}
    names = await _names(guild_id, shown)
    if notes.bot:
        names[notes.bot[0]] = notes.bot[1]
    context["names"] = names
    context["image_keys"] = notes.image_keys
    return context


async def save_invocation(
    invocation_id: UUID,
    recorder: TranscriptRecorder,
    notes: TraceNotes,
    channel_id: str | None,
    guild_id: str | None = None,
    *,
    live: bool = False,
) -> None:
    try:
        items = entries(recorder, notes, live=live)
        context = await build_context(notes, channel_id, guild_id, items)
        await store_blobs(notes.blobs)
        payload = dump(items)
        values = {
            "invocation_id": invocation_id,
            "source": "pydantic-ai",
            "content": "full" if notes.full else "metadata",
            "entries": payload,
            "context": context,
            "size_bytes": len(json.dumps(payload, ensure_ascii=False)) + len(json.dumps(context, ensure_ascii=False)),
        }
        stmt = pg_insert(Transcript).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["invocation_id"],
            set_={k: stmt.excluded[k] for k in ("entries", "context", "size_bytes", "content")}
            | {"updated_at": func.now()},
        )
        async with get_session() as session:
            await session.execute(stmt)
    except Exception:
        LOGGER.exception("saving the transcript of invocation {} failed", invocation_id)


async def update_invocation_progress(invocation_id: UUID, recorder: TranscriptRecorder) -> None:
    """Refresh live models, tokens, and tool counts on a still-``running`` invocation row."""
    models, tokens_in, tokens_out, tokens_cached, tool_calls = live_stats(recorder)
    with contextlib.suppress(Exception):
        async with get_session() as session:
            await session.execute(
                update(Invocation)
                .where(Invocation.id == invocation_id, Invocation.status == "running")
                .values(
                    models=models,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    tokens_cached=tokens_cached,
                    tool_calls=tool_calls,
                )
            )


class LiveTranscriptFlusher:
    """Coalesced background flusher that persists mid-run transcript & stats snapshots while an invocation runs."""

    def __init__(
        self,
        get_invocation_id: Callable[[], UUID | None],
        recorder: TranscriptRecorder,
        notes: TraceNotes,
        channel_id: str | None,
        guild_id: str | None = None,
        *,
        debounce_s: float = 0.25,
    ) -> None:
        self._get_invocation_id = get_invocation_id
        self._recorder = recorder
        self._notes = notes
        self._channel_id = channel_id
        self._guild_id = guild_id
        self._debounce_s = debounce_s
        self._dirty = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    def trigger(self) -> None:
        if self._closed:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._task = loop.create_task(self._loop(), name="live-transcript-flush")

    async def _loop(self) -> None:
        while not self._closed and self._dirty:
            if self._debounce_s > 0:
                await asyncio.sleep(self._debounce_s)
            if self._closed:
                break
            self._dirty = False
            inv_id = self._get_invocation_id()
            if inv_id is None or not (self._recorder.history or self._recorder.tools):
                continue
            await save_invocation(inv_id, self._recorder, self._notes, self._channel_id, self._guild_id, live=True)
            await update_invocation_progress(inv_id, self._recorder)

    async def close(self) -> None:
        """Stop scheduling new flushes and wait for any in-flight flush before the final authoritative save."""
        self._closed = True
        self._dirty = False
        task = self._task
        if task is not None and not task.done():
            with contextlib.suppress(BaseException):
                await task


async def append_job(
    job_id: str, items: list[Entry], blobs: dict[str, Blob], *, full: bool, source: str = "omp"
) -> None:
    if not items:
        return
    try:
        await store_blobs(blobs)
        payload = dump(items)
        size = len(json.dumps(payload, ensure_ascii=False))
        stmt = pg_insert(Transcript).values(
            job_id=job_id,
            source=source,
            content="full" if full else "metadata",
            entries=payload,
            context={"version": VERSION},
            size_bytes=size,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["job_id"],
            set_={
                "entries": Transcript.entries.op("||")(stmt.excluded.entries),
                "size_bytes": Transcript.size_bytes + size,
                "updated_at": func.now(),
            },
        )
        async with get_session() as session:
            await session.execute(stmt)
    except Exception:
        LOGGER.exception("appending to the transcript of job {} failed", job_id)


async def for_invocation(invocation_id: UUID) -> Transcript | None:
    async with get_session() as session:
        return await session.scalar(select(Transcript).where(Transcript.invocation_id == invocation_id))


async def for_job(job_id: str) -> Transcript | None:
    async with get_session() as session:
        return await session.scalar(select(Transcript).where(Transcript.job_id == job_id))


async def blob(sha256: str) -> TranscriptBlob | None:
    async with get_session() as session:
        return await session.get(TranscriptBlob, sha256)
