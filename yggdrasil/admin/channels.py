"""KOOK Guild -> Category -> Channel management tree and channel history backfill."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found, unavailable
from yggdrasil.admin.settings_api import global_doc, validate_layers
from yggdrasil.db.models import Channel, Guild, KookThread
from yggdrasil.db.session import get_session
from yggdrasil.forum import list_cached_threads, sync_forum_channel
from yggdrasil.kook import client as kook_client
from yggdrasil.kook.model import CHANNEL_CATEGORY, CHANNEL_TEXT, CHANNEL_THREAD, KookError

router = APIRouter(prefix="/channels", tags=["channels"])


class GuildOut(BaseModel):
    guild_id: str
    name: str
    icon: str
    enabled: bool
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class GuildIn(BaseModel):
    name: str = ""
    icon: str = ""
    enabled: bool = True
    settings: dict[str, Any] | None = Field(None, description="省略则保留原有覆盖")


class ChannelOut(BaseModel):
    channel_id: str
    guild_id: str
    parent_id: str | None
    name: str
    topic: str
    position: int
    is_category: bool
    channel_type: int = CHANNEL_TEXT
    enabled: bool | None
    effective_enabled: bool
    settings: dict[str, Any]
    backfilled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChannelIn(BaseModel):
    guild_id: str
    parent_id: str | None = None
    name: str = ""
    topic: str = ""
    position: int = 0
    is_category: bool = False
    channel_type: int | None = Field(None, description="频道类型：0 分组 / 1 文字频道 / 4 帖子频道（省略时保持原值）")
    enabled: bool | None = Field(
        None,
        description="启用开关：true 启用 / false 停用 / null 继承所属分组（分组或无分组频道为 null 时视为停用）",
    )
    settings: dict[str, Any] | None = Field(None, description="省略则保留原有覆盖")


class ForumThreadOut(BaseModel):
    thread_id: str
    channel_id: str
    guild_id: str
    category_id: str | None
    category_name: str
    title: str
    author_id: str
    author_name: str
    preview_text: str
    tags: list[str]
    post_count: int
    collect_num: int
    created_at: datetime
    latest_active_time: datetime
    synced_at: datetime


class ForumSyncOut(BaseModel):
    channel_id: str
    synced: int


class CategoryNode(BaseModel):
    category: ChannelOut
    channels: list[ChannelOut]


class GuildTreeOut(BaseModel):
    guild: GuildOut
    categories: list[CategoryNode]
    ungrouped: list[ChannelOut]


def _guild_out(g: Guild) -> GuildOut:
    return GuildOut(
        guild_id=g.guild_id,
        name=g.name,
        icon=g.icon,
        enabled=g.enabled,
        settings=dict(g.settings),
        created_at=g.created_at,
        updated_at=g.updated_at,
    )


def _effective_enabled(c: Channel, guilds: dict[str, Guild], channels: dict[str, Channel]) -> bool:
    g = guilds.get(c.guild_id)
    if g is None or not g.enabled:
        return False
    if c.is_category:
        return bool(c.enabled)
    if c.enabled is not None:
        return c.enabled
    if c.parent_id and (parent := channels.get(c.parent_id)) is not None:
        return bool(parent.enabled)
    return False


def _channel_out(c: Channel, guilds: dict[str, Guild], channels: dict[str, Channel]) -> ChannelOut:
    return ChannelOut(
        channel_id=c.channel_id,
        guild_id=c.guild_id,
        parent_id=c.parent_id,
        name=c.name,
        topic=c.topic,
        position=c.position,
        is_category=c.is_category,
        channel_type=CHANNEL_CATEGORY if c.is_category else c.channel_type,
        enabled=c.enabled,
        effective_enabled=_effective_enabled(c, guilds, channels),
        settings=dict(c.settings),
        backfilled_at=c.backfilled_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _thread_out(t: KookThread) -> ForumThreadOut:
    return ForumThreadOut(
        thread_id=t.thread_id,
        channel_id=t.channel_id,
        guild_id=t.guild_id,
        category_id=t.category_id,
        category_name=t.category_name,
        title=t.title,
        author_id=t.author_id,
        author_name=t.author_name,
        preview_text=t.preview_text,
        tags=list(t.tags or ()),
        post_count=t.post_count,
        collect_num=t.collect_num,
        created_at=t.created_at,
        latest_active_time=t.latest_active_time,
        synced_at=t.synced_at,
    )


@router.get("/guilds")
async def list_guilds() -> list[GuildOut]:
    async with get_session() as s:
        rows = (await s.scalars(select(Guild).order_by(Guild.name, Guild.guild_id))).all()
    return [_guild_out(g) for g in rows]


@router.get("")
async def list_channels(guild_id: str | None = None, include_categories: bool = False) -> list[ChannelOut]:
    async with get_session() as s:
        guilds = {g.guild_id: g for g in (await s.scalars(select(Guild))).all()}
        q = select(Channel).order_by(Channel.guild_id, Channel.position, Channel.channel_id)
        if guild_id:
            q = q.where(Channel.guild_id == guild_id)
        rows = (await s.scalars(q)).all()
    by_id = {c.channel_id: c for c in rows}
    return [_channel_out(c, guilds, by_id) for c in rows if include_categories or not c.is_category]


@router.get("/tree")
async def get_channel_tree() -> list[GuildTreeOut]:
    """Collapsible 3-level tree: Guild -> Category -> Channel."""
    async with get_session() as s:
        guild_rows = (await s.scalars(select(Guild).order_by(Guild.name, Guild.guild_id))).all()
        ch_rows = (await s.scalars(select(Channel).order_by(Channel.position, Channel.name, Channel.channel_id))).all()
    guilds = {g.guild_id: g for g in guild_rows}
    by_id = {c.channel_id: c for c in ch_rows}

    out: list[GuildTreeOut] = []
    for g in guild_rows:
        g_chs = [c for c in ch_rows if c.guild_id == g.guild_id]
        cats = [c for c in g_chs if c.is_category]
        cat_ids = {c.channel_id for c in cats}
        texts = [c for c in g_chs if not c.is_category]
        by_parent: dict[str, list[ChannelOut]] = {cid: [] for cid in cat_ids}
        ungrouped: list[ChannelOut] = []
        for t in texts:
            item = _channel_out(t, guilds, by_id)
            if t.parent_id and t.parent_id in by_parent:
                by_parent[t.parent_id].append(item)
            else:
                ungrouped.append(item)
        nodes = [
            CategoryNode(category=_channel_out(cat, guilds, by_id), channels=by_parent.get(cat.channel_id, []))
            for cat in cats
        ]
        out.append(GuildTreeOut(guild=_guild_out(g), categories=nodes, ungrouped=ungrouped))
    return out


@router.put("/guilds/{guild_id}")
async def upsert_guild(guild_id: str, body: GuildIn, actor: Actor) -> GuildOut:
    if body.settings is not None:
        validate_layers(await global_doc(), body.settings, check=body.settings)
    async with change(actor, "guild", "upsert", guild_id) as c:
        g = await c.session.get(Guild, guild_id)
        if g is None:
            g = Guild(guild_id=guild_id, settings={})
            c.session.add(g)
        c.detail = {"before_enabled": g.enabled, "enabled": body.enabled, "name": body.name}
        g.name = body.name.strip()
        if body.icon:
            g.icon = body.icon.strip()
        g.enabled = body.enabled
        if body.settings is not None:
            g.settings = body.settings
            c.detail["settings"] = body.settings
        await c.session.flush()
        await c.session.refresh(g)
    return _guild_out(g)


@router.delete("/guilds/{guild_id}")
async def delete_guild(guild_id: str, actor: Actor) -> Ok:
    async with change(actor, "guild", "delete", guild_id) as c:
        g = await c.session.get(Guild, guild_id)
        if g is None:
            raise not_found(f"服务器 {guild_id}")
        c.detail = {"name": g.name}
        await c.session.delete(g)
    return Ok()


@router.put("/{channel_id}")
async def upsert_channel(channel_id: str, body: ChannelIn, actor: Actor) -> ChannelOut:
    async with change(actor, "channel", "upsert", channel_id) as c:
        g = await c.session.get(Guild, body.guild_id)
        if g is None:
            g = Guild(guild_id=body.guild_id, name=body.guild_id, enabled=True, settings={})
            c.session.add(g)
            await c.session.flush()
        parent = await c.session.get(Channel, body.parent_id) if body.parent_id else None
        if body.settings is not None:
            layers = [await global_doc(), dict(g.settings)]
            if parent is not None:
                layers.append(dict(parent.settings))
            layers.append(body.settings)
            validate_layers(*layers, check=body.settings)

        row = await c.session.get(Channel, channel_id)
        if row is None:
            row = Channel(channel_id=channel_id, guild_id=body.guild_id, settings={})
            c.session.add(row)
        c.detail = {
            "guild_id": body.guild_id,
            "before_enabled": row.enabled,
            "enabled": body.enabled,
            "name": body.name,
            "is_category": body.is_category,
        }
        row.guild_id = body.guild_id
        row.parent_id = None if body.is_category else (body.parent_id or None)
        row.name = body.name.strip()
        row.topic = body.topic.strip()
        row.position = body.position
        row.is_category = body.is_category
        if body.is_category:
            row.channel_type = CHANNEL_CATEGORY
        elif body.channel_type is not None:
            row.channel_type = body.channel_type
        row.enabled = body.enabled
        if body.settings is not None:
            row.settings = body.settings
            c.detail["settings"] = body.settings
        await c.session.flush()
        await c.session.refresh(row)
        guilds = {g.guild_id: g}
        by_id = {row.channel_id: row}
        if parent is not None:
            by_id[parent.channel_id] = parent
        out = _channel_out(row, guilds, by_id)
    return out


@router.delete("/{channel_id}")
async def delete_channel(channel_id: str, actor: Actor) -> Ok:
    async with change(actor, "channel", "delete", channel_id) as c:
        row = await c.session.get(Channel, channel_id)
        if row is None:
            raise not_found(f"频道 {channel_id}")
        c.detail = {"name": row.name, "guild_id": row.guild_id}
        await c.session.delete(row)
    return Ok()


@router.post("/{channel_id}/backfill")
async def backfill_channel(channel_id: str, actor: Actor) -> Ok:
    try:
        from yggdrasil.backfill import request_backfill
    except ImportError as e:
        raise unavailable(f"回填模块不可用: {e}") from e
    async with get_session() as s:
        ch = await s.get(Channel, channel_id)
        if ch is None or ch.is_category or ch.channel_type == CHANNEL_THREAD:
            raise not_found(f"文字频道 {channel_id}")
    if not request_backfill(channel_id):
        raise conflict("这个频道的回填正在进行中")
    async with change(actor, "channel", "backfill", channel_id):
        pass
    return Ok()


@router.get("/{channel_id}/threads")
async def list_forum_channel_threads(channel_id: str, limit: int = 50) -> list[ForumThreadOut]:
    """Return cached threads of a KOOK forum channel (``channel_type = 4``)."""
    async with get_session() as s:
        ch = await s.get(Channel, channel_id)
        if ch is None or ch.is_category or ch.channel_type != CHANNEL_THREAD:
            raise not_found(f"帖子频道 {channel_id}")
    rows = await list_cached_threads(channel_id, limit=limit)
    return [_thread_out(r) for r in rows]


@router.post("/{channel_id}/sync-threads")
async def sync_forum_channel_threads(channel_id: str, actor: Actor, limit: int = 30) -> ForumSyncOut:
    """Pull latest threads of a KOOK forum channel into the ``kook_threads`` cache table."""
    live = kook_client.connected()
    if live is None:
        raise unavailable("KOOK 机器人尚未连接")
    async with get_session() as s:
        ch = await s.get(Channel, channel_id)
        if ch is None or ch.is_category or ch.channel_type != CHANNEL_THREAD:
            raise not_found(f"帖子频道 {channel_id}")
        guild_id = ch.guild_id
    try:
        threads = await sync_forum_channel(live, channel_id, guild_id=guild_id, limit=limit)
    except KookError as e:
        raise conflict(f"同步帖子列表失败：{e}") from e
    async with change(actor, "channel", "sync_threads", channel_id) as c:
        c.detail = {"synced": len(threads)}
    return ForumSyncOut(channel_id=channel_id, synced=len(threads))
