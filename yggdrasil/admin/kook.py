"""KOOK Bot connection status, token management (encrypted in DB), live guild listing, and one-click sync."""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, unavailable
from yggdrasil.bot import state as bot_state
from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt
from yggdrasil.db.models import Channel, Guild, SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.kook import client as kook_client
from yggdrasil.kook import gateway as kook_gateway
from yggdrasil.kook.model import CHANNEL_CATEGORY, CHANNEL_TEXT, CHANNEL_THREAD, KookError
from yggdrasil.runtime import snapshot

router = APIRouter(prefix="/kook", tags=["kook"])

KOOK_DOC_KEY = kook_gateway.KEY


class KookConnectionOut(BaseModel):
    configured: bool
    source: Literal["db", "env", "none"]
    token_preview: str
    connected: bool
    bot_id: str | None
    bot_name: str | None
    connected_at: datetime | None
    last_heartbeat: datetime | None
    guild_count: int
    enabled_channels: int


class KookConnectionIn(BaseModel):
    bot_token: str = Field(min_length=8, description="KOOK 开发者后台机器人的 Bot Token")


class LiveChannelOut(BaseModel):
    channel_id: str
    parent_id: str | None
    name: str
    topic: str
    position: int
    is_category: bool
    type: int


class LiveGuildOut(BaseModel):
    guild_id: str
    name: str
    icon: str
    channels: list[LiveChannelOut]


class SyncOut(BaseModel):
    guilds: int
    categories: int
    channels: int
    forums: int = 0


class GuildRoleOut(BaseModel):
    role_id: str
    name: str
    permissions: int


def _mask(token: str) -> str:
    t = token.strip()
    if len(t) <= 8:
        return "***" if t else ""
    return f"{t[:4]}…{t[-4:]}"


async def _token_info() -> tuple[Literal["db", "env", "none"], str]:
    async with get_session() as s:
        doc = await s.get(SettingsDoc, KOOK_DOC_KEY)
        if doc and isinstance(doc.value, dict) and doc.value.get("token_enc"):
            try:
                enc = doc.value["token_enc"]
                raw = decrypt(enc.encode() if isinstance(enc, str) else enc)
                if raw.strip():
                    return "db", raw.strip()
            except Exception:
                return "db", ""
    if APP_CONFIG.kook_bot_token and APP_CONFIG.kook_bot_token.strip():
        return "env", APP_CONFIG.kook_bot_token.strip()
    return "none", ""


def _notify_reload() -> None:
    if (gw := kook_gateway.gateway()) is not None:
        gw.trigger_reload()


@router.get("/connection")
async def get_kook_connection() -> KookConnectionOut:
    source, token = await _token_info()
    live = kook_client.connected()
    snap = snapshot()
    bot_id = live.self_id if live is not None else (next(iter(bot_state.connected), None))
    bot_name: str | None = None
    if live is not None:
        ident = await bot_state.identity(live)
        bot_name = ident.nickname
    conn_ts = bot_state.connected.get(bot_id) if bot_id else None
    hb_ts = bot_state.last_heartbeat.get(bot_id) if bot_id else None
    enabled_ch = sum(1 for c in snap.channels.values() if not c.is_category and snap.channel_enabled(c.channel_id))
    return KookConnectionOut(
        configured=source != "none",
        source=source,
        token_preview=_mask(token),
        connected=live is not None,
        bot_id=bot_id,
        bot_name=bot_name,
        connected_at=datetime.fromtimestamp(conn_ts, UTC) if conn_ts else None,
        last_heartbeat=datetime.fromtimestamp(hb_ts, UTC) if hb_ts else None,
        guild_count=len(snap.guilds),
        enabled_channels=enabled_ch,
    )


@router.put("/connection")
async def put_kook_connection(body: KookConnectionIn, actor: Actor) -> KookConnectionOut:
    token = body.bot_token.strip()
    enc = encrypt(token).decode()
    async with change(actor, "kook_connection", "update", KOOK_DOC_KEY) as c:
        doc = await c.session.get(SettingsDoc, KOOK_DOC_KEY)
        if doc is None:
            c.session.add(SettingsDoc(key=KOOK_DOC_KEY, value={"token_enc": enc}))
        else:
            doc.value = {"token_enc": enc}
        c.detail = {"preview": _mask(token)}
    _notify_reload()
    return await get_kook_connection()


@router.delete("/connection")
async def delete_kook_connection(actor: Actor) -> Ok:
    async with change(actor, "kook_connection", "delete", KOOK_DOC_KEY) as c:
        doc = await c.session.get(SettingsDoc, KOOK_DOC_KEY)
        if doc is not None:
            await c.session.delete(doc)
    _notify_reload()
    return Ok()


@router.get("/guilds")
async def list_kook_live_guilds() -> list[LiveGuildOut]:
    live = kook_client.connected()
    if live is None:
        raise unavailable("KOOK 机器人尚未连接")
    try:
        guilds = await live.guilds()
        out: list[LiveGuildOut] = []
        for g in guilds:
            chs = await live.channels(g.guild_id)
            out.append(
                LiveGuildOut(
                    guild_id=g.guild_id,
                    name=g.name,
                    icon=g.icon,
                    channels=[
                        LiveChannelOut(
                            channel_id=c.channel_id,
                            parent_id=c.parent_id,
                            name=c.name,
                            topic=c.topic,
                            position=c.level,
                            is_category=c.is_category,
                            type=c.type,
                        )
                        for c in chs
                    ],
                )
            )
        return out
    except KookError as e:
        raise conflict(f"拉取 KOOK 服务器列表失败：{e}") from e


@router.get("/guilds/{guild_id}/roles")
async def list_kook_guild_roles(guild_id: str) -> list[GuildRoleOut]:
    """Live KOOK guild roles (cached by LiveKook) for picking role IDs in access control."""
    live = kook_client.connected()
    if live is None:
        raise unavailable("KOOK 机器人尚未连接")
    try:
        roles = await live.guild_roles(guild_id)
    except KookError as e:
        raise conflict(f"拉取服务器角色失败：{e}") from e
    return [
        GuildRoleOut(role_id=str(r.role_id), name=r.name or str(r.role_id), permissions=r.permissions) for r in roles
    ]


@router.post("/sync")
async def sync_kook_structure(actor: Actor) -> SyncOut:
    """Pull Guild -> Category -> Text Channel structure from KOOK into DB without clobbering overrides."""
    live = kook_client.connected()
    if live is None:
        raise unavailable("KOOK 机器人尚未连接，请先配置 Bot Token 并等待上线")
    try:
        guilds = await live.guilds()
        guild_channels: dict[str, list[Any]] = {}
        for g in guilds:
            guild_channels[g.guild_id] = await live.channels(g.guild_id)
    except KookError as e:
        raise conflict(f"从 KOOK 同步失败：{e}") from e

    n_guilds = n_cats = n_chs = n_forums = 0
    async with change(actor, "kook", "sync", "all") as c:
        existing_guilds = {g.guild_id: g for g in (await c.session.scalars(select(Guild))).all()}
        existing_channels = {ch.channel_id: ch for ch in (await c.session.scalars(select(Channel))).all()}
        for g in guilds:
            n_guilds += 1
            row = existing_guilds.get(g.guild_id)
            if row is None:
                c.session.add(Guild(guild_id=g.guild_id, name=g.name, icon=g.icon, enabled=True, settings={}))
            else:
                row.name = g.name
                row.icon = g.icon
            await c.session.flush()
            live_ids: set[str] = set()
            for ch in guild_channels.get(g.guild_id, []):
                # Keep categories (is_category=True / type=0), text channels (type=1), and forum channels (type=4).
                if not ch.is_category and ch.type not in (CHANNEL_TEXT, CHANNEL_THREAD):
                    continue
                live_ids.add(ch.channel_id)
                ctype = CHANNEL_CATEGORY if ch.is_category else ch.type
                if ch.is_category:
                    n_cats += 1
                elif ctype == CHANNEL_THREAD:
                    n_forums += 1
                else:
                    n_chs += 1
                crow = existing_channels.get(ch.channel_id)
                parent_id = None if ch.is_category else (ch.parent_id or None)
                if crow is None:
                    c.session.add(
                        Channel(
                            channel_id=ch.channel_id,
                            guild_id=g.guild_id,
                            parent_id=parent_id,
                            name=ch.name,
                            topic=ch.topic,
                            position=ch.level,
                            is_category=ch.is_category,
                            channel_type=ctype,
                            # Categories default disabled; grouped text/forum channels inherit (None).
                            enabled=False if (ch.is_category or parent_id is None) else None,
                            settings={},
                        )
                    )
                else:
                    crow.guild_id = g.guild_id
                    crow.parent_id = parent_id
                    crow.name = ch.name
                    crow.topic = ch.topic
                    crow.position = ch.level
                    crow.is_category = ch.is_category
                    crow.channel_type = ctype
            for cid, crow in existing_channels.items():
                if crow.guild_id == g.guild_id and cid not in live_ids:
                    await c.session.delete(crow)
        c.detail = {"guilds": n_guilds, "categories": n_cats, "channels": n_chs, "forums": n_forums}
    return SyncOut(guilds=n_guilds, categories=n_cats, channels=n_chs, forums=n_forums)
