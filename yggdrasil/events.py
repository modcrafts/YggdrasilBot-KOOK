"""KOOK system events recorded in ``bot_events`` and automatic guild/channel lifecycle policies."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import BOT_EVENTS
from yggdrasil.db.models import BotEvent, Channel, Guild
from yggdrasil.db.session import get_session
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import CHANNEL_CATEGORY, CHANNEL_TEXT, CHANNEL_THREAD, KookError, Observed
from yggdrasil.runtime import Snapshot, load, publish_change, snapshot

AUTO = "auto"
_SUPPORTED_CHANNEL_TYPES = frozenset({CHANNEL_TEXT, CHANNEL_THREAD})


class EventError(Exception):
    pass


def _normalize_parent(parent_id: Any, *, is_category: bool) -> str | None:
    if is_category:
        return None
    raw = str(parent_id or "").strip()
    return None if (not raw or raw == "0") else raw


def _is_supported_channel(is_category: bool, channel_type: int) -> bool:
    return is_category or channel_type in _SUPPORTED_CHANNEL_TYPES


async def upsert_channel_record(
    channel_id: str,
    *,
    guild_id: str,
    name: str,
    topic: str = "",
    position: int = 0,
    is_category: bool = False,
    parent_id: str | None = None,
    channel_type: int = CHANNEL_TEXT,
) -> bool:
    """Insert or update a KOOK channel/category/forum in DB for an already-registered guild.

    New grouped text/forum channels default to ``enabled=None`` (inherit their parent category's switch),
    matching manual KOOK sync semantics; new categories and ungrouped channels default to
    ``enabled=False``. Existing rows preserve their ``enabled`` and ``settings`` overrides.
    Returns True if a DB write occurred.
    """
    parent = _normalize_parent(parent_id, is_category=is_category)
    ctype = CHANNEL_CATEGORY if is_category else channel_type
    async with get_session() as s:
        if await s.get(Guild, guild_id) is None:
            return False
        row = await s.get(Channel, channel_id)
        if row is None:
            s.add(
                Channel(
                    channel_id=channel_id,
                    guild_id=guild_id,
                    parent_id=parent,
                    name=name,
                    topic=topic,
                    position=position,
                    is_category=is_category,
                    channel_type=ctype,
                    enabled=False if (is_category or parent is None) else None,
                    settings={},
                )
            )
            return True
        if (
            row.guild_id == guild_id
            and row.parent_id == parent
            and row.name == name
            and row.topic == topic
            and row.position == position
            and row.is_category == is_category
            and row.channel_type == ctype
        ):
            return False
        row.guild_id = guild_id
        row.parent_id = parent
        row.name = name
        row.topic = topic
        row.position = position
        row.is_category = is_category
        row.channel_type = ctype
        return True


async def delete_channel_record(channel_id: str) -> bool:
    """Remove a deleted KOOK channel/category from the ``channels`` config table (keeps message history & memory)."""
    async with get_session() as s:
        # If a category was deleted, detach any remaining child pointers first.
        await s.execute(update(Channel).where(Channel.parent_id == channel_id).values(parent_id=None))
        res = await s.execute(delete(Channel).where(Channel.channel_id == channel_id))
        return bool(res.rowcount)  # type: ignore[attr-defined]


async def _ensure_parent_category(kook: Kook, guild_id: str, parent_id: str | None) -> bool:
    if not parent_id or parent_id in snapshot().channels:
        return False
    try:
        cat = await kook.channel(parent_id)
    except KookError as e:
        LOGGER.debug("fetching parent category {} failed: {}", parent_id, e)
        return False
    return await upsert_channel_record(
        cat.channel_id,
        guild_id=cat.guild_id or guild_id,
        name=cat.name,
        topic=cat.topic,
        position=cat.level,
        is_category=True,
        parent_id=None,
        channel_type=CHANNEL_CATEGORY,
    )


async def _upsert_from_event_body(kook: Kook, body: dict[str, Any], *, fallback_guild_id: str = "") -> bool:
    cid = str(body.get("id") or body.get("channel_id") or "").strip()
    gid = str(body.get("guild_id") or fallback_guild_id or "").strip()
    if not cid or not gid:
        return False
    is_category = bool(body.get("is_category")) or int(body.get("type") or 0) == CHANNEL_CATEGORY
    ch_type = int(body.get("type") or (CHANNEL_CATEGORY if is_category else CHANNEL_TEXT))
    if not _is_supported_channel(is_category, ch_type):
        return False
    parent_id = _normalize_parent(body.get("parent_id"), is_category=is_category)
    changed_parent = await _ensure_parent_category(kook, gid, parent_id)
    changed = await upsert_channel_record(
        cid,
        guild_id=gid,
        name=str(body.get("name") or ""),
        topic=str(body.get("topic") or ""),
        position=int(body.get("level") or 0),
        is_category=is_category,
        parent_id=parent_id,
        channel_type=CHANNEL_CATEGORY if is_category else ch_type,
    )
    return changed or changed_parent


async def sync_channel(kook: Kook, channel_id: str, *, guild_id: str | None = None) -> Snapshot:
    """Fetch one unknown KOOK channel via REST, persist it, and reload the runtime snapshot."""
    ch = await kook.channel(channel_id)
    if not _is_supported_channel(ch.is_category, ch.type):
        return snapshot()
    gid = ch.guild_id or guild_id or ""
    if not gid:
        return snapshot()
    parent_id = _normalize_parent(ch.parent_id, is_category=ch.is_category)
    changed_parent = await _ensure_parent_category(kook, gid, parent_id)
    changed = await upsert_channel_record(
        ch.channel_id,
        guild_id=gid,
        name=ch.name,
        topic=ch.topic,
        position=ch.level,
        is_category=ch.is_category,
        parent_id=parent_id,
        channel_type=CHANNEL_CATEGORY if ch.is_category else ch.type,
    )
    if changed or changed_parent:
        snap = await load()
        LOGGER.info(
            "auto-synced KOOK channel #{} ({}) type={} in guild {} (parent={}, enabled={})",
            ch.name or ch.channel_id,
            ch.channel_id,
            ch.type,
            gid,
            parent_id,
            snap.channel_enabled(ch.channel_id),
        )
        await publish_change(f"channel {ch.channel_id} auto-synced")
        return snap
    return snapshot()


async def sync_registered_guilds(kook: Kook, *, guild_ids: list[str] | None = None) -> int:
    """Refresh categories, text channels, and forum channels for guilds already in DB."""
    snap = snapshot()
    targets = [gid for gid in (guild_ids or list(snap.guilds)) if gid in snap.guilds]
    if not targets:
        return 0
    touched = 0
    for gid in targets:
        try:
            chs = await kook.channels(gid)
        except KookError as e:
            LOGGER.warning("auto-syncing channels for guild {} failed: {}", gid, e)
            continue
        async with get_session() as s:
            if await s.get(Guild, gid) is None:
                continue
            existing = {
                c.channel_id: c for c in (await s.scalars(select(Channel).where(Channel.guild_id == gid))).all()
            }
            live_ids: set[str] = set()
            for ch in chs:
                if not _is_supported_channel(ch.is_category, ch.type):
                    continue
                live_ids.add(ch.channel_id)
                parent_id = _normalize_parent(ch.parent_id, is_category=ch.is_category)
                ctype = CHANNEL_CATEGORY if ch.is_category else ch.type
                crow = existing.get(ch.channel_id)
                if crow is None:
                    s.add(
                        Channel(
                            channel_id=ch.channel_id,
                            guild_id=gid,
                            parent_id=parent_id,
                            name=ch.name,
                            topic=ch.topic,
                            position=ch.level,
                            is_category=ch.is_category,
                            channel_type=ctype,
                            enabled=False if (ch.is_category or parent_id is None) else None,
                            settings={},
                        )
                    )
                    touched += 1
                elif (
                    crow.parent_id != parent_id
                    or crow.name != ch.name
                    or crow.topic != ch.topic
                    or crow.position != ch.level
                    or crow.is_category != ch.is_category
                    or crow.channel_type != ctype
                ):
                    crow.parent_id = parent_id
                    crow.name = ch.name
                    crow.topic = ch.topic
                    crow.position = ch.level
                    crow.is_category = ch.is_category
                    crow.channel_type = ctype
                    touched += 1
            for cid, crow in existing.items():
                if cid not in live_ids:
                    await s.delete(crow)
                    touched += 1
    if touched:
        await load()
        await publish_change(f"guilds auto-synced ({touched} channels)")
        LOGGER.info("auto-synced {} channel changes across {} guilds", touched, len(targets))
    return touched


def kept(o: Observed, self_id: str) -> bool:
    """Keep bot/guild/channel/role/emoji lifecycle events always; member events only for known guilds."""
    if o.kind in (
        "kook.self_joined_guild",
        "kook.self_exited_guild",
        "kook.deleted_guild",
        "kook.updated_guild",
        "kook.deleted_channel",
        "kook.added_channel",
        "kook.updated_channel",
        "kook.updated_server_type",
        "kook.sort_channel",
        "kook.batch_added_channel",
        "kook.batch_updated_channel",
        "kook.batch_deleted_channel",
        "kook.added_role",
        "kook.updated_role",
        "kook.deleted_role",
        "kook.added_emoji",
        "kook.updated_emoji",
        "kook.deleted_emoji",
    ):
        return True
    if o.user_id == self_id:
        return True
    if o.guild_id and (g := snapshot().guilds.get(o.guild_id)) is not None and g.enabled:
        return True
    return o.channel_id is not None and snapshot().channel_enabled(o.channel_id)


async def handle(kook: Kook, o: Observed) -> None:
    try:
        if not kept(o, kook.self_id):
            return
        async with get_session() as s:
            row = BotEvent(
                kind=o.kind,
                guild_id=o.guild_id,
                channel_id=o.channel_id,
                user_id=o.user_id,
                operator_id=o.operator_id,
                data=dict(o.data),
            )
            s.add(row)
            await s.flush()
            event_id = row.id
        BOT_EVENTS.add(1, {"kind": o.kind})
        LOGGER.info("bot event E{}: {} guild={} channel={}", event_id, o.kind, o.guild_id, o.channel_id)
        await _apply_policy(kook, event_id, o)
    except Exception:
        LOGGER.exception("handling KOOK bot event failed")


async def _apply_policy(kook: Kook, event_id: int, o: Observed) -> None:
    from yggdrasil.moderation.review import notify_admins

    snap = snapshot()
    cfg = snap.settings().events
    if (
        o.kind in ("kook.added_channel", "kook.updated_channel", "kook.updated_server_type")
        and o.channel_id
        and o.guild_id
    ):
        body = {**o.data, "id": o.data.get("id") or o.channel_id, "guild_id": o.data.get("guild_id") or o.guild_id}
        if await _upsert_from_event_body(kook, body, fallback_guild_id=o.guild_id):
            await load()
            await publish_change(f"channel {o.channel_id} {o.kind}")
    elif o.kind == "kook.batch_added_channel":
        items = [x for x in (o.data.get("items") or []) if isinstance(x, dict)]
        changed_any = False
        for item in items:
            if await _upsert_from_event_body(kook, item, fallback_guild_id=o.guild_id or ""):
                changed_any = True
        if changed_any:
            await load()
            await publish_change(f"channels batch added ({len(items)})")
    elif o.kind == "kook.batch_updated_channel":
        candidates: list[dict[str, Any]] = []
        if isinstance(o.data.get("added_channel"), dict):
            candidates.append(o.data["added_channel"])
        for item in o.data.get("updated_channel") or []:
            if isinstance(item, dict):
                candidates.append(item)
        changed_any = False
        for item in candidates:
            if await _upsert_from_event_body(kook, item, fallback_guild_id=o.guild_id or ""):
                changed_any = True
        if changed_any:
            await load()
            await publish_change(f"channels batch updated ({len(candidates)})")
    elif o.kind == "kook.batch_deleted_channel":
        if cfg.disable_channel_when_deleted:
            items = [x for x in (o.data.get("items") or []) if isinstance(x, dict)]
            deleted_any = False
            for item in items:
                cid = str(item.get("id") or item.get("channel_id") or "").strip()
                if cid and cid in snap.channels and await delete_channel_record(cid):
                    deleted_any = True
            if deleted_any:
                await load()
                await publish_change(f"channels batch deleted ({len(items)})")
    elif o.kind == "kook.sort_channel" and o.guild_id and o.guild_id in snap.guilds:
        await sync_registered_guilds(kook, guild_ids=[o.guild_id])
    elif o.kind in ("kook.added_role", "kook.updated_role", "kook.deleted_role") and o.guild_id:
        kook.invalidate_guild_meta(o.guild_id)
    elif o.kind in ("kook.added_emoji", "kook.updated_emoji", "kook.deleted_emoji") and o.guild_id:
        kook.invalidate_guild_emojis(o.guild_id)
    elif o.kind == "kook.updated_guild" and o.guild_id and o.guild_id in snap.guilds:
        name = str(o.data.get("name") or "").strip()
        icon = str(o.data.get("icon") or "").strip()
        if name or icon:
            async with get_session() as s:
                g = await s.get(Guild, o.guild_id)
                if g is not None:
                    if name:
                        g.name = name
                    if icon:
                        g.icon = icon
            await load()
            await publish_change(f"guild {o.guild_id} updated")
    elif o.kind == "kook.deleted_channel" and o.channel_id:
        if cfg.disable_channel_when_deleted and o.channel_id in snap.channels:
            if await delete_channel_record(o.channel_id):
                await load()
                await publish_change(f"channel {o.channel_id} deleted")
    elif o.kind in ("kook.self_exited_guild", "kook.deleted_guild") and o.guild_id:
        if cfg.disable_guild_when_exited:
            async with get_session() as s:
                await s.execute(update(Channel).where(Channel.guild_id == o.guild_id).values(enabled=False))
            await load()
            await publish_change(f"guild {o.guild_id} exited")
        if cfg.notify_guild_changes:
            gname = snap.guild_name(o.guild_id) or o.guild_id
            await notify_admins(kook, f"E{event_id} bot 已退出/离开 KOOK 服务器 {gname}（{o.guild_id}）。")
    elif o.kind == "kook.self_joined_guild" and o.guild_id:
        if cfg.notify_guild_changes:
            await notify_admins(
                kook,
                f"E{event_id} bot 加入了新 KOOK 服务器 {o.guild_id}，可在管理后台点击「从 KOOK 同步」配置分组与频道。",
            )


async def sweep() -> dict[str, int]:
    cfg = snapshot().settings().events
    now = datetime.now(UTC)
    async with get_session() as s:
        deleted = await s.execute(delete(BotEvent).where(BotEvent.at < now - timedelta(days=cfg.retention_days)))
    return {"expired": 0, "deleted": deleted.rowcount or 0}  # type: ignore[attr-defined]
