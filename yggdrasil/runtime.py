"""Hot-reloadable runtime configuration with 4-level scope inheritance.

Scope chain: ``global`` -> ``guild`` -> ``category`` -> ``channel``.
Readers call :func:`snapshot` (cheap, immutable). Writers update Postgres and call :func:`publish_change`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.crypto import decrypt
from yggdrasil.db.models import (
    Admin,
    Channel,
    CommandTrigger,
    EmbedSpace,
    Guild,
    Prompt,
    PromptVersion,
    Provider,
    SearchProvider,
    SettingsDoc,
    TaskRoute,
)
from yggdrasil.db.session import get_session
from yggdrasil.kv import redis_client
from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.settings import Settings, merge_settings

CHANNEL = "ygg:config"


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    id: int
    name: str
    base_url: str
    api_key: str | None
    headers: dict[str, str]
    enabled: bool
    models: tuple[str, ...]
    api_format: str = "openai"
    caps: dict[str, ModelCaps] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Route:
    task: str
    provider_id: int
    model: str
    params: dict[str, Any]
    fallback_provider_id: int | None
    fallback_model: str | None
    limits: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GuildInfo:
    guild_id: str
    name: str
    icon: str
    settings_doc: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    channel_id: str
    guild_id: str
    parent_id: str | None
    is_category: bool
    position: int
    name: str
    enabled: bool | None  # None = inherit from parent category (default False if uncategorized)
    settings_doc: dict[str, Any]
    channel_type: int = 1  # 0=category, 1=text, 4=thread/forum

    @property
    def is_forum(self) -> bool:
        return not self.is_category and self.channel_type == 4


@dataclass(frozen=True, slots=True)
class TriggerRule:
    id: int
    handler: str
    scope: str  # global | guild | category | channel | private
    scope_id: str | None
    match_type: str
    patterns: tuple[str, ...]
    arg_template: str | None
    require_mention: bool
    target: str
    priority: int
    cooldown_s: int
    allowed_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EmbedSpaceInfo:
    id: int
    provider_id: int
    model: str
    dim: int


@dataclass(frozen=True, slots=True)
class SearchProviderInfo:
    id: int
    name: str
    kind: str
    base_url: str
    api_key: str | None
    enabled: bool
    priority: int
    monthly_limit: int | None
    params: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    version: int = 0
    providers: dict[int, ProviderInfo] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    guilds: dict[str, GuildInfo] = field(default_factory=dict)
    channels: dict[str, ChannelInfo] = field(default_factory=dict)
    global_settings_doc: dict[str, Any] = field(default_factory=dict)
    triggers: tuple[TriggerRule, ...] = ()
    prompts: dict[str, str] = field(default_factory=dict)
    superusers: frozenset[str] = frozenset()
    admin_emails: frozenset[str] = frozenset()
    embed_space: EmbedSpaceInfo | None = None
    search_providers: tuple[SearchProviderInfo, ...] = ()
    _settings_cache: dict[tuple[str | None, str | None, str | None], Settings] = field(default_factory=dict, repr=False)

    def guild_of(self, channel_id: str | None) -> str | None:
        if not channel_id:
            return None
        ch = self.channels.get(channel_id)
        return ch.guild_id if ch is not None else None

    def category_of(self, channel_id: str | None) -> str | None:
        if not channel_id:
            return None
        ch = self.channels.get(channel_id)
        if ch is None:
            return None
        if ch.is_category:
            return ch.channel_id
        return ch.parent_id

    def channel_name(self, channel_id: str | None) -> str:
        if not channel_id:
            return ""
        ch = self.channels.get(channel_id)
        return ch.name if ch is not None else ""

    def guild_name(self, guild_id: str | None) -> str:
        if not guild_id:
            return ""
        g = self.guilds.get(guild_id)
        return g.name if g is not None else ""

    def channel_enabled(self, channel_id: str | None) -> bool:
        """Resolve tri-state ``enabled``: guild gate -> channel override -> parent category override -> False."""
        if not channel_id:
            return False
        ch = self.channels.get(channel_id)
        if ch is None:
            return False
        guild = self.guilds.get(ch.guild_id)
        if guild is not None and not guild.enabled:
            return False
        if ch.enabled is not None:
            return ch.enabled
        if not ch.is_category and ch.parent_id:
            cat = self.channels.get(ch.parent_id)
            if cat is not None and cat.enabled is not None:
                return cat.enabled
        return False

    def guild_channels(
        self, guild_id: str, *, enabled_only: bool = True, include_forums: bool = False
    ) -> list[ChannelInfo]:
        out = [
            c
            for c in self.channels.values()
            if not c.is_category
            and (include_forums or not c.is_forum)
            and c.guild_id == guild_id
            and (not enabled_only or self.channel_enabled(c.channel_id))
        ]
        out.sort(key=lambda c: (c.position, c.channel_id))
        return out

    def guild_forum_channels(self, guild_id: str, *, enabled_only: bool = True) -> list[ChannelInfo]:
        out = [
            c
            for c in self.channels.values()
            if c.is_forum and c.guild_id == guild_id and (not enabled_only or self.channel_enabled(c.channel_id))
        ]
        out.sort(key=lambda c: (c.position, c.channel_id))
        return out

    def shared_channel_ids(self, guild_id: str) -> list[str]:
        """Enabled text channels in ``guild_id`` that participate in guild-shared memory."""
        return [
            c.channel_id
            for c in self.guild_channels(guild_id, enabled_only=True)
            if self.settings(channel_id=c.channel_id, guild_id=guild_id).memory.enabled
            and self.settings(channel_id=c.channel_id, guild_id=guild_id).memory.share_in_guild
        ]

    def settings(
        self,
        channel_id: str | None = None,
        *,
        guild_id: str | None = None,
        category_id: str | None = None,
    ) -> Settings:
        """Effective settings along ``global -> guild -> category -> channel``."""
        ch = self.channels.get(channel_id) if channel_id else None
        if ch is not None:
            if ch.is_category:
                category_id = ch.channel_id
                ch = None
            else:
                category_id = category_id or ch.parent_id
            guild_id = guild_id or self.channels[channel_id].guild_id if channel_id in self.channels else guild_id
        if category_id and not guild_id and category_id in self.channels:
            guild_id = self.channels[category_id].guild_id

        key = (guild_id, category_id, ch.channel_id if ch else None)
        cached = self._settings_cache.get(key)
        if cached is None:
            guild_doc = self.guilds[guild_id].settings_doc if guild_id and guild_id in self.guilds else None
            cat_doc = self.channels[category_id].settings_doc if category_id and category_id in self.channels else None
            ch_doc = ch.settings_doc if ch is not None else None
            cached = merge_settings(self.global_settings_doc, guild_doc, cat_doc, ch_doc)
            self._settings_cache[key] = cached
        return cached


_snapshot = Snapshot()
_listeners: list[Callable[[Snapshot], Awaitable[None] | None]] = []


def snapshot() -> Snapshot:
    return _snapshot


def on_reload(fn: Callable[[Snapshot], Awaitable[None] | None]) -> None:
    _listeners.append(fn)


async def load() -> Snapshot:
    global _snapshot
    async with get_session() as session:
        providers = {}
        for p in (await session.scalars(select(Provider))).all():
            providers[p.id] = ProviderInfo(
                p.id,
                p.name,
                p.base_url.rstrip("/"),
                _decrypt_key(p.api_key_enc, p.name),
                dict(p.headers),
                p.enabled,
                tuple(p.models or ()),
                p.api_format,
                {m: ModelCaps.model_validate(c) for m, c in (p.model_caps or {}).items()},
            )
        search_providers = tuple(
            SearchProviderInfo(
                s.id,
                s.name,
                s.kind,
                s.base_url.rstrip("/"),
                _decrypt_key(s.api_key_enc, s.name),
                s.enabled,
                s.priority,
                s.monthly_limit,
                dict(s.params),
            )
            for s in (
                await session.scalars(select(SearchProvider).order_by(SearchProvider.priority, SearchProvider.id))
            ).all()
        )
        routes = {
            r.task: Route(
                r.task, r.provider_id, r.model, dict(r.params), r.fallback_provider_id, r.fallback_model, dict(r.limits)
            )
            for r in (await session.scalars(select(TaskRoute))).all()
        }
        guilds = {
            g.guild_id: GuildInfo(g.guild_id, g.name, g.icon, dict(g.settings), g.enabled)
            for g in (await session.scalars(select(Guild))).all()
        }
        channels = {
            c.channel_id: ChannelInfo(
                c.channel_id,
                c.guild_id,
                c.parent_id,
                c.is_category,
                c.position,
                c.name,
                c.enabled,
                dict(c.settings),
                c.channel_type,
            )
            for c in (await session.scalars(select(Channel))).all()
        }
        doc = await session.get(SettingsDoc, "global")
        triggers = tuple(
            TriggerRule(
                t.id,
                t.handler,
                t.scope,
                t.scope_id,
                t.match_type,
                tuple(t.patterns),
                t.arg_template,
                t.require_mention,
                t.target,
                t.priority,
                t.cooldown_s,
                tuple(t.allowed_roles),
            )
            for t in (
                await session.scalars(select(CommandTrigger).where(CommandTrigger.enabled).order_by(CommandTrigger.id))
            ).all()
        )
        prompt_rows = await session.execute(
            select(Prompt.key, PromptVersion.body).join(PromptVersion, PromptVersion.id == Prompt.active_version_id)
        )
        prompts = dict(prompt_rows.all())
        admins = (await session.scalars(select(Admin))).all()
        space = await session.scalar(select(EmbedSpace).where(EmbedSpace.active))

    superusers = {a.value.strip() for a in admins if a.kind == "kook" and a.value.strip()} | {
        str(u).strip() for u in APP_CONFIG.bootstrap_superusers if str(u).strip()
    }
    emails = {a.value.lower() for a in admins if a.kind == "email"} | {
        e.lower() for e in APP_CONFIG.bootstrap_admin_emails
    }
    _snapshot = Snapshot(
        version=_snapshot.version + 1,
        providers=providers,
        routes=routes,
        guilds=guilds,
        channels=channels,
        global_settings_doc=dict(doc.value) if doc else {},
        triggers=triggers,
        prompts=prompts,
        superusers=frozenset(superusers),
        admin_emails=frozenset(emails),
        embed_space=EmbedSpaceInfo(space.id, space.provider_id, space.model, space.dim) if space else None,
        search_providers=search_providers,
    )
    for fn in _listeners:
        result = fn(_snapshot)
        if asyncio.iscoroutine(result):
            await result
    LOGGER.info(
        "runtime config v{} loaded: {} providers, {} routes, {} guilds, {} channels, {} triggers",
        _snapshot.version,
        len(providers),
        len(routes),
        len(guilds),
        len(channels),
        len(triggers),
    )
    return _snapshot


def _decrypt_key(enc: bytes | None, owner: str) -> str | None:
    if not enc:
        return None
    try:
        return decrypt(enc)
    except Exception:
        LOGGER.error("cannot decrypt api key of {} (YGG_SECRET_KEY changed?)", owner)
        return None


async def publish_change(reason: str) -> None:
    await redis_client().publish(CHANNEL, reason)


async def listen_forever() -> None:
    delay = 1.0
    while True:
        try:
            pubsub = aioredis.Redis.from_pool(redis_client().connection_pool).pubsub()
            await pubsub.subscribe(CHANNEL)
            delay = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                LOGGER.info("config change: {}", message.get("data"))
                try:
                    await load()
                except Exception:
                    LOGGER.exception("config reload failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("config listener error; retrying in {}s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
