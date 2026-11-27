"""Shared helpers for memory pipelines: local time, transcript lines, watermarks, curation weights, scope selectors."""

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, ColumnExpressionArgument, Float, cast, extract, func
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.db.models import PipelineState, UserProfileFact
from yggdrasil.people import People, resolve
from yggdrasil.runtime import Snapshot

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DECAY_FLOOR = 0.5
UNSCORED_FACT_WEIGHT = 3.0


def local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def format_line(
    m: "MessageView",
    people: People,
    *,
    with_id: bool = False,
    channel_tag: str = "",
) -> str:
    """``[MM-DD HH:MM] 名字(uid): 内容``, optionally prefixed with ``#<id>`` and/or ``[#频道]``."""
    who = people.who(m.user_id, m.sender_name, is_self=m.is_self)
    prefix = f"[#{channel_tag}] " if channel_tag else ""
    line = f"[{local(m.sent_at):%m-%d %H:%M}] {prefix}{who}: {people.mentions(m.text)}"
    return f"#{m.id} {line}" if with_id else line


async def people_for(
    guild_id: str, views: "Iterable[MessageView]", *, bot_name: str, user_ids: Iterable[str] = ()
) -> People:
    """Names for rendering ``views`` in ``guild_id``: senders, @mentions and ``user_ids``, in one lookup."""
    views = list(views)
    self_id = next((v.user_id for v in views if v.is_self), None)
    return await resolve(
        guild_id,
        [*(v.user_id for v in views), *user_ids],
        bot_name=bot_name,
        self_id=self_id,
        texts=[v.text for v in views],
    )


def memory_channels(snap: Snapshot) -> list[str]:
    """Enabled text channels whose effective settings keep memory enabled (for chunking & topic segmentation)."""
    return sorted(
        c.channel_id
        for c in snap.channels.values()
        if not c.is_category
        and not c.is_forum
        and snap.channel_enabled(c.channel_id)
        and snap.settings(channel_id=c.channel_id, guild_id=c.guild_id).memory.enabled
    )


def memory_guilds(snap: Snapshot) -> list[str]:
    """Guilds that have at least one enabled memory-sharing channel (for profiles, glossary, digests, curation)."""
    guilds: set[str] = set()
    for cid in memory_channels(snap):
        ch = snap.channels[cid]
        if ch.guild_id and snap.settings(channel_id=cid, guild_id=ch.guild_id).memory.share_in_guild:
            guilds.add(ch.guild_id)
    return sorted(guilds)


def topic_weight(importance: int, msg_count: int, participants: int) -> float:
    return round(importance * (1 + 0.1 * math.log1p(msg_count) + 0.1 * math.log1p(participants)), 2)


def fact_weight(importance: int, sources: int) -> float:
    return round(importance * (1 + 0.1 * math.log(max(sources, 1))), 2)


def decay(age: timedelta, half_life_days: float) -> float:
    days = max(age.total_seconds(), 0.0) / 86400
    return DECAY_FLOOR + (1 - DECAY_FLOOR) * 0.5 ** (days / half_life_days)


def sql_decay(at: ColumnExpressionArgument[datetime], half_life_days: float) -> ColumnElement[float]:
    days = func.greatest(cast(extract("epoch", func.now() - at), Float) / 86400, 0.0)
    return DECAY_FLOOR + (1 - DECAY_FLOOR) * func.power(0.5, days / half_life_days)


def fact_rank(half_life_days: float) -> ColumnElement[float]:
    return func.coalesce(UserProfileFact.weight, UNSCORED_FACT_WEIGHT) * sql_decay(
        UserProfileFact.created_at, half_life_days
    )


async def pipeline_state(session: AsyncSession, pipeline: str, scope_id: str) -> PipelineState:
    """Watermark row for ``(pipeline, scope_id)``; created (pending flush) when missing."""
    st = await session.get(PipelineState, (pipeline, scope_id))
    if st is None:
        st = PipelineState(pipeline=pipeline, scope_id=scope_id, last_msg_id=0, state={})
        session.add(st)
    return st
