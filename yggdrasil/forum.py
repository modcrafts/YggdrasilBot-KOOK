"""KOOK forum/thread channel (``channel_type = 4``) privacy resolution, caching, and sync helpers."""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from yggdrasil.access import can_bypass_channel_privacy
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Channel, KookThread
from yggdrasil.db.session import get_session
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import ForumThread
from yggdrasil.runtime import ChannelInfo, snapshot


def _clean_ids(values: Sequence[str]) -> set[str]:
    return {v.strip() for v in values if v and v.strip()}


def _matches_scope(channel_id: str, parent_id: str | None, scopes: set[str]) -> bool:
    return channel_id in scopes or (parent_id is not None and parent_id in scopes)


def accessible_forum_channels(
    guild_id: str,
    caller_channel_id: str | None,
    *,
    role_ids: Collection[str] = (),
) -> list[ChannelInfo]:
    """Enabled KOOK forum channels (``type=4``) in ``guild_id`` readable from ``caller_channel_id``.

    Applies the same 4-level one-way privacy rules as text channels (``share_in_guild``,
    ``readable_by_scopes``, ``readable_by_roles``, ``cross_channel_read``, ``cross_channel_allow``,
    ``cross_channel_deny``, and ``access.role_rules[role_id].bypass_channel_privacy``).
    """
    snap = snapshot()
    candidates = snap.guild_forum_channels(guild_id, enabled_only=True)
    if not candidates:
        return []

    caller_settings = snap.settings(channel_id=caller_channel_id, guild_id=guild_id)
    caller_mem = caller_settings.memory
    holder_roles = {str(r).strip() for r in role_ids if str(r).strip()}

    if can_bypass_channel_privacy(caller_settings, holder_roles):
        return candidates

    if caller_channel_id and not caller_mem.cross_channel_read:
        return [c for c in candidates if c.channel_id == caller_channel_id]

    caller_scopes: set[str] = set()
    if caller_channel_id:
        caller_scopes.add(caller_channel_id)
        caller_info = snap.channels.get(caller_channel_id)
        if caller_info is not None and caller_info.parent_id:
            caller_scopes.add(caller_info.parent_id)

    allow_scopes = _clean_ids(caller_mem.cross_channel_allow)
    deny_scopes = _clean_ids(caller_mem.cross_channel_deny)

    visible: list[ChannelInfo] = []
    for target in candidates:
        if target.channel_id == caller_channel_id:
            visible.append(target)
            continue
        if deny_scopes and _matches_scope(target.channel_id, target.parent_id, deny_scopes):
            continue
        if allow_scopes and not _matches_scope(target.channel_id, target.parent_id, allow_scopes):
            continue
        t_mem = snap.settings(channel_id=target.channel_id, guild_id=guild_id).memory
        if t_mem.share_in_guild:
            visible.append(target)
            continue
        readable_scopes = _clean_ids(t_mem.readable_by_scopes)
        readable_roles = _clean_ids(t_mem.readable_by_roles)
        if (caller_scopes & readable_scopes) or (holder_roles & readable_roles):
            visible.append(target)
    return visible


def _dt(ts: int, *, fallback: datetime) -> datetime:
    if ts <= 0:
        return fallback
    return datetime.fromtimestamp(ts, UTC)


def _thread_row(t: ForumThread, *, now: datetime) -> dict[str, Any]:
    return {
        "thread_id": t.thread_id,
        "channel_id": t.channel_id,
        "guild_id": t.guild_id,
        "category_id": t.category.id if t.category else None,
        "category_name": t.category.name if t.category else "",
        "post_id": t.post_id,
        "title": t.title,
        "author_id": t.author.user_id,
        "author_name": t.author.name,
        "preview_text": t.preview_content,
        "content_text": t.content_text if t.content is not None else "",
        "tags": [tag.name for tag in t.tags if tag.name],
        "medias": [{"type": m.type, "src": m.src, "title": m.title, "cover": m.cover} for m in t.medias],
        "post_count": t.post_count,
        "collect_num": t.collect_num,
        "created_at": _dt(t.create_time, fallback=now),
        "latest_active_time": _dt(t.latest_active_time or t.create_time, fallback=now),
        "content_deleted": t.content_deleted,
        "synced_at": now,
    }


async def cache_threads(threads: Sequence[ForumThread]) -> int:
    """Upsert fetched KOOK forum thread summaries/details into ``kook_threads``.

    Preserves existing non-empty ``content_text`` when upserting summary rows from ``/thread/list``
    (which always omit full ``content``).
    """
    valid = [t for t in threads if t.thread_id and t.channel_id]
    if not valid:
        return 0
    now = datetime.now(UTC)
    rows = [_thread_row(t, now=now) for t in valid]
    async with get_session() as session:
        stmt = insert(KookThread).values(rows)
        # Preserve cached full content_text if the incoming row only came from /thread/list (empty content_text).
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["thread_id"],
                set_={
                    "channel_id": stmt.excluded.channel_id,
                    "guild_id": stmt.excluded.guild_id,
                    "category_id": stmt.excluded.category_id,
                    "category_name": stmt.excluded.category_name,
                    "post_id": stmt.excluded.post_id,
                    "title": stmt.excluded.title,
                    "author_id": stmt.excluded.author_id,
                    "author_name": stmt.excluded.author_name,
                    "preview_text": stmt.excluded.preview_text,
                    "content_text": func.coalesce(func.nullif(stmt.excluded.content_text, ""), KookThread.content_text),
                    "tags": stmt.excluded.tags,
                    "medias": stmt.excluded.medias,
                    "post_count": stmt.excluded.post_count,
                    "collect_num": stmt.excluded.collect_num,
                    "created_at": stmt.excluded.created_at,
                    "latest_active_time": stmt.excluded.latest_active_time,
                    "content_deleted": stmt.excluded.content_deleted,
                    "synced_at": stmt.excluded.synced_at,
                },
            )
        )
    return len(valid)


async def sync_forum_channel(
    kook: Kook,
    channel_id: str,
    *,
    guild_id: str | None = None,
    limit: int = 30,
) -> list[ForumThread]:
    """Fetch recent threads of a forum channel from KOOK and persist them to ``kook_threads``."""
    gid = guild_id or snapshot().guild_of(channel_id) or ""
    threads = await kook.forum_threads(channel_id, guild_id=gid, sort=1, limit=min(max(limit, 1), 100))
    try:
        await cache_threads(threads)
        async with get_session() as session:
            await session.execute(
                update(Channel).where(Channel.channel_id == channel_id).values(backfilled_at=datetime.now(UTC))
            )
    except Exception:
        LOGGER.debug("caching forum threads for channel {} skipped", channel_id, exc_info=True)
    return threads


async def list_cached_threads(channel_id: str, *, limit: int = 50) -> list[KookThread]:
    """Return cached threads for a forum channel ordered by ``latest_active_time DESC``."""
    async with get_session() as session:
        rows = (
            await session.scalars(
                select(KookThread)
                .where(KookThread.channel_id == channel_id)
                .order_by(KookThread.latest_active_time.desc())
                .limit(min(max(limit, 1), 200))
            )
        ).all()
    return list(rows)
