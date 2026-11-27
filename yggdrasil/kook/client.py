"""The KOOK client interface: everything the bot does on KOOK.

``LiveKook`` wraps :class:`KookRest` with lightweight guild-role and emoji caches so every caller works against a
clean, easily mockable interface (``FakeKook`` in tests).
"""

import time
from abc import ABC, abstractmethod
from typing import Any

from yggdrasil.core.log import LOGGER
from yggdrasil.kook.model import (
    Channel,
    ForumPost,
    ForumThread,
    Guild,
    GuildEmoji,
    GuildRole,
    KookError,
    Login,
    Member,
    Message,
    ThreadCategory,
)
from yggdrasil.kook.parser import (
    admin_roles_from_list,
    parse_forum_post,
    parse_forum_thread,
    parse_rest_message,
    parse_thread_category,
)
from yggdrasil.kook.rest import KookRest

_CACHE_TTL_S = 300.0


class Kook(ABC):
    @property
    @abstractmethod
    def self_id(self) -> str: ...

    # --- identity / guild / channel / member info ---------------------------------------------------------------

    @abstractmethod
    async def login(self) -> Login: ...

    @abstractmethod
    async def guilds(self) -> list[Guild]: ...

    @abstractmethod
    async def guild(self, guild_id: str) -> Guild: ...

    @abstractmethod
    async def channels(self, guild_id: str) -> list[Channel]: ...

    @abstractmethod
    async def channel(self, channel_id: str) -> Channel: ...

    @abstractmethod
    async def member(self, guild_id: str, user_id: str) -> Member: ...

    @abstractmethod
    async def guild_emojis(self, guild_id: str) -> list[GuildEmoji]: ...

    async def guild_roles(self, guild_id: str) -> list[GuildRole]:
        """Guild roles (default empty for lightweight test doubles)."""
        return []

    def invalidate_guild_meta(self, guild_id: str) -> None:
        """Drop cached guild owner/role metadata so the next lookup refreshes from KOOK."""

    def invalidate_guild_emojis(self, guild_id: str) -> None:
        """Drop cached guild custom emojis so the next lookup refreshes from KOOK."""

    async def guild_members(
        self,
        guild_id: str,
        *,
        channel_id: str | None = None,
        search: str | None = None,
        role_id: int | None = None,
    ) -> list[Member]:
        """Search or list guild members (default empty for lightweight test doubles)."""
        return []

    async def update_message(
        self, *, channel_id: str | None, msg_id: str, content: str, quote: str | None = None
    ) -> None:
        """Edit an existing message sent by the bot."""
        raise KookError("update_message not implemented")

    # --- forum / thread channels (read-only) --------------------------------------------------------------------

    async def thread_categories(self, channel_id: str) -> list[ThreadCategory]:
        """List forum categories inside a thread channel."""
        return []

    async def forum_threads(
        self,
        channel_id: str,
        *,
        guild_id: str | None = None,
        category_id: str | None = None,
        sort: int = 1,
        time_ms: int | None = None,
        limit: int = 20,
    ) -> list[ForumThread]:
        """List threads in a forum channel (summaries; call :meth:`forum_thread` for full content)."""
        return []

    async def forum_thread(self, channel_id: str, thread_id: str, *, guild_id: str | None = None) -> ForumThread:
        """Fetch full thread details (including main post content)."""
        raise KookError(f"forum thread not found: {thread_id}")

    async def forum_posts(
        self,
        channel_id: str,
        thread_id: str,
        *,
        post_id: str | None = None,
        order: int = 1,
        page: int = 1,
        limit: int = 20,
    ) -> list[ForumPost]:
        """List top-level comments (``post_id=None``) or nested floor replies in a forum thread."""
        return []

    @abstractmethod
    async def pinned_messages(self, channel_id: str, *, limit: int = 20) -> list[Message]: ...

    # --- messages & reactions -----------------------------------------------------------------------------------

    @abstractmethod
    async def send_kmarkdown(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        content: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def send_card(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        card_json: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def send_image_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def send_file_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def upload_asset(self, data: bytes, filename: str) -> str: ...

    @abstractmethod
    async def message(
        self, *, channel_id: str | None, msg_id: str, guild_id: str | None = None, chat_code: str | None = None
    ) -> Message: ...

    @abstractmethod
    async def history(
        self, channel_id: str, *, guild_id: str | None = None, before: str | None = None, limit: int = 50
    ) -> list[Message]:
        """A page of channel history, oldest first."""

    @abstractmethod
    async def react(self, *, channel_id: str | None, msg_id: str, emoji: str) -> None: ...

    @abstractmethod
    async def unreact(self, *, channel_id: str | None, msg_id: str, emoji: str, user_id: str | None = None) -> None: ...

    @abstractmethod
    async def recall(self, *, channel_id: str | None, msg_id: str) -> None: ...

    @abstractmethod
    async def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Raw KOOK API v3 call (used by the ops agent's ``kook_api``)."""


class LiveKook(Kook):
    """Production KOOK client backed by :class:`KookRest`."""

    def __init__(self, rest: KookRest, login: Login) -> None:
        self.rest = rest
        self._login = login
        # guild_id -> (fetched_at, owner_id, admin_role_ids, role_map)
        self._guild_meta: dict[str, tuple[float, str, set[int], dict[int, GuildRole]]] = {}
        # guild_id -> (fetched_at, {emoji_id: GuildEmoji})
        self._emoji_cache: dict[str, tuple[float, dict[str, GuildEmoji]]] = {}

    @property
    def self_id(self) -> str:
        return self._login.user_id

    async def login(self) -> Login:
        return self._login

    def invalidate_guild_meta(self, guild_id: str) -> None:
        self._guild_meta.pop(guild_id, None)

    def invalidate_guild_emojis(self, guild_id: str) -> None:
        self._emoji_cache.pop(guild_id, None)

    @staticmethod
    def _parse_role(r: dict[str, Any]) -> GuildRole:
        return GuildRole(
            role_id=int(r["role_id"]),
            name=str(r.get("name") or ""),
            permissions=int(r.get("permissions") or 0),
            color=int(r.get("color") or 0),
            position=int(r.get("position") or 0),
            hoist=bool(r.get("hoist")),
            mentionable=bool(r.get("mentionable")),
            op_permissions=int(r.get("op_permissions") or 0),
        )

    @staticmethod
    def _parse_guild(g: dict[str, Any], fallback_id: str = "") -> Guild:
        return Guild(
            guild_id=str(g.get("id") or fallback_id),
            name=str(g.get("name") or ""),
            topic=str(g.get("topic") or ""),
            icon=str(g.get("icon") or ""),
            owner_id=str(g.get("user_id") or ""),
            default_channel_id=str(g.get("default_channel_id") or ""),
            welcome_channel_id=str(g.get("welcome_channel_id") or ""),
            region=str(g.get("region") or ""),
            enable_open=bool(g.get("enable_open")),
            open_id=str(g.get("open_id") or ""),
            notify_type=int(g.get("notify_type") or 0),
        )

    @staticmethod
    def _parse_channel(c: dict[str, Any], *, fallback_id: str = "", fallback_guild_id: str = "") -> Channel:
        is_cat = bool(c.get("is_category")) or int(c.get("type") or 0) == 0
        parent = str(c.get("parent_id") or "") or None
        if parent == "0":
            parent = None
        perm_sync = c.get("permission_sync")
        return Channel(
            channel_id=str(c.get("id") or fallback_id),
            guild_id=str(c.get("guild_id") or fallback_guild_id),
            name=str(c.get("name") or ""),
            topic=str(c.get("topic") or ""),
            type=int(c.get("type") or 0),
            is_category=is_cat,
            parent_id=None if is_cat else parent,
            level=int(c.get("level") or 0),
            creator_id=str(c.get("user_id") or ""),
            slow_mode=int(c.get("slow_mode") or 0),
            slow_mode_reply=int(c.get("slow_mode_reply") or 0),
            default_layout=int(c.get("default_layout") or 0),
            sort_order=int(c.get("sort_order") or 0),
            permission_sync=bool(perm_sync) if perm_sync is not None else None,
        )

    @staticmethod
    def _parse_member(
        u: dict[str, Any],
        guild_id: str,
        *,
        fallback_uid: str = "",
        owner_id: str = "",
        admin_ids: set[int] | None = None,
        role_map: dict[int, GuildRole] | None = None,
    ) -> Member:
        uid = str(u.get("id") or fallback_uid)
        raw_roles = [int(r) for r in (u.get("roles") or []) if str(r).isdigit()]
        is_master = bool(u.get("is_master")) or (bool(owner_id) and uid == owner_id)
        role = "owner" if is_master else ("admin" if admin_ids and any(r in admin_ids for r in raw_roles) else "member")
        rm = role_map or {}
        role_names = tuple(rm[r].name for r in raw_roles if r in rm and rm[r].name)
        joined = int(u.get("joined_at") or 0)
        active = int(u.get("active_time") or 0)
        tag_info = u.get("tag_info")
        tag_text = str(tag_info.get("text") or "") if isinstance(tag_info, dict) else ""
        mob = u.get("mobile_verified")
        return Member(
            guild_id=guild_id,
            user_id=uid,
            username=str(u.get("username") or ""),
            nickname=str(u.get("nickname") or ""),
            role=role,
            roles=tuple(raw_roles),
            role_names=role_names,
            is_bot=bool(u.get("bot")),
            avatar=str(u.get("avatar") or ""),
            joined_at=(joined // 1000 if joined > 10_000_000_000 else joined) or None,
            active_time=(active // 1000 if active > 10_000_000_000 else active) or None,
            identify_num=str(u.get("identify_num") or ""),
            online=bool(u.get("online")),
            os=str(u.get("os") or ""),
            status=int(u.get("status") or 0),
            is_vip=bool(u.get("is_vip")),
            vip_avatar=str(u.get("vip_avatar") or ""),
            is_sys=bool(u.get("is_sys")),
            is_master=is_master,
            banner=str(u.get("banner") or ""),
            tag_text=tag_text,
            wealth_level=int(u.get("wealth_level") or 0),
            mobile_verified=bool(mob) if mob is not None else None,
        )

    async def _meta(self, guild_id: str) -> tuple[str, set[int], dict[int, GuildRole]]:
        cached = self._guild_meta.get(guild_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1], cached[2], cached[3]
        owner_id = ""
        raw_roles: list[dict[str, Any]] = []
        role_map: dict[int, GuildRole] = {}
        try:
            g = await self.rest.guild_view(guild_id)
            owner_id = str(g.get("user_id") or "")
            # /guild/view already embeds roles[] in KOOK API v3; reuse to avoid an extra round-trip when present.
            if isinstance(g.get("roles"), list) and g["roles"]:
                raw_roles = [r for r in g["roles"] if isinstance(r, dict)]
        except KookError as e:
            LOGGER.debug("guild_view {} failed while caching meta: {}", guild_id, e)
        if not raw_roles:
            try:
                raw_roles = await self.rest.guild_role_list(guild_id)
            except KookError as e:
                LOGGER.debug("guild_role_list {} failed while caching meta: {}", guild_id, e)
        admin_ids = admin_roles_from_list(raw_roles)
        for r in raw_roles:
            if r.get("role_id") is not None:
                role = self._parse_role(r)
                role_map[role.role_id] = role
        self._guild_meta[guild_id] = (now, owner_id, admin_ids, role_map)
        return owner_id, admin_ids, role_map

    async def guild_role_info(self, guild_id: str) -> tuple[str, set[int]]:
        """Cached ``(owner_id, admin_role_ids)`` for event parsing."""
        owner_id, admin_ids, _ = await self._meta(guild_id)
        return owner_id, admin_ids

    async def guild_roles(self, guild_id: str) -> list[GuildRole]:
        """Cached KOOK roles for ``guild_id`` (sorted by role_id)."""
        _, _, role_map = await self._meta(guild_id)
        return sorted(role_map.values(), key=lambda r: r.role_id)

    async def guilds(self) -> list[Guild]:
        rows = await self.rest.guild_list()
        return [self._parse_guild(g) for g in rows if g.get("id")]

    async def guild(self, guild_id: str) -> Guild:
        g = await self.rest.guild_view(guild_id)
        return self._parse_guild(g, fallback_id=guild_id)

    async def channels(self, guild_id: str) -> list[Channel]:
        rows = await self.rest.channel_list(guild_id)
        return [self._parse_channel(c, fallback_guild_id=guild_id) for c in rows if c.get("id")]

    async def channel(self, channel_id: str) -> Channel:
        c = await self.rest.channel_view(channel_id)
        return self._parse_channel(c, fallback_id=channel_id)

    async def member(self, guild_id: str, user_id: str) -> Member:
        u = await self.rest.user_view(user_id, guild_id=guild_id)
        owner_id, admin_ids, role_map = await self._meta(guild_id)
        return self._parse_member(
            u, guild_id, fallback_uid=user_id, owner_id=owner_id, admin_ids=admin_ids, role_map=role_map
        )

    async def guild_members(
        self,
        guild_id: str,
        *,
        channel_id: str | None = None,
        search: str | None = None,
        role_id: int | None = None,
    ) -> list[Member]:
        rows = await self.rest.guild_user_list(guild_id, channel_id=channel_id, search=search, role_id=role_id)
        owner_id, admin_ids, role_map = await self._meta(guild_id)
        return [
            self._parse_member(u, guild_id, owner_id=owner_id, admin_ids=admin_ids, role_map=role_map)
            for u in rows
            if u.get("id")
        ]

    async def update_message(
        self, *, channel_id: str | None, msg_id: str, content: str, quote: str | None = None
    ) -> None:
        if channel_id is not None:
            await self.rest.message_update(msg_id, content, quote=quote)
        else:
            await self.rest.direct_message_update(msg_id, content, quote=quote)

    async def thread_categories(self, channel_id: str) -> list[ThreadCategory]:
        rows = await self.rest.thread_category_list(channel_id)
        out: list[ThreadCategory] = []
        for r in rows:
            cat = parse_thread_category(r)
            if cat is not None:
                out.append(cat)
        return out

    async def forum_threads(
        self,
        channel_id: str,
        *,
        guild_id: str | None = None,
        category_id: str | None = None,
        sort: int = 1,
        time_ms: int | None = None,
        limit: int = 20,
    ) -> list[ForumThread]:
        rows = await self.rest.thread_list(
            channel_id, category_id=category_id, sort=sort, time_ms=time_ms, page_size=limit
        )
        owner_id, admin_ids = await self.guild_role_info(guild_id) if guild_id else (None, None)
        return [
            parse_forum_thread(
                r, channel_id=channel_id, guild_id=guild_id or "", owner_id=owner_id, admin_role_ids=admin_ids
            )
            for r in rows
            if r.get("id")
        ]

    async def forum_thread(self, channel_id: str, thread_id: str, *, guild_id: str | None = None) -> ForumThread:
        raw = await self.rest.thread_view(channel_id, thread_id)
        owner_id, admin_ids = await self.guild_role_info(guild_id) if guild_id else (None, None)
        return parse_forum_thread(
            raw, channel_id=channel_id, guild_id=guild_id or "", owner_id=owner_id, admin_role_ids=admin_ids
        )

    async def forum_posts(
        self,
        channel_id: str,
        thread_id: str,
        *,
        post_id: str | None = None,
        order: int = 1,
        page: int = 1,
        limit: int = 20,
    ) -> list[ForumPost]:
        rows = await self.rest.thread_post_list(
            channel_id, thread_id, post_id=post_id, order=order, page=page, page_size=limit
        )
        return [parse_forum_post(r, thread_id=thread_id) for r in rows if r.get("id")]

    async def guild_emojis(self, guild_id: str) -> list[GuildEmoji]:
        cached = self._emoji_cache.get(guild_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return list(cached[1].values())
        rows = await self.rest.guild_emoji_list(guild_id)
        by_id: dict[str, GuildEmoji] = {}
        for e in rows:
            eid = str(e.get("id") or "")
            if eid:
                by_id[eid] = GuildEmoji(
                    id=eid,
                    name=str(e.get("name") or ""),
                    emoji_type=int(e.get("type") or 0),
                    guild_id=guild_id,
                )
        self._emoji_cache[guild_id] = (now, by_id)
        return list(by_id.values())

    async def emoji_url(self, guild_id: str, emoji_id: str) -> str:
        """Resolve a guild custom emoji URL (animated GIF vs static PNG)."""
        cached = self._emoji_cache.get(guild_id)
        if cached is None or time.monotonic() - cached[0] >= _CACHE_TTL_S:
            try:
                await self.guild_emojis(guild_id)
            except KookError:
                pass
            cached = self._emoji_cache.get(guild_id)
        if cached and emoji_id in cached[1]:
            return cached[1][emoji_id].url
        return GuildEmoji(id=emoji_id, name="", emoji_type=0, guild_id=guild_id).url

    async def pinned_messages(self, channel_id: str, *, limit: int = 20) -> list[Message]:
        rows = await self.rest.message_list(channel_id, pin=1, page_size=min(limit, 50))
        return [parse_rest_message(r, channel_id=channel_id, guild_id=None) for r in rows[:limit]]

    async def send_kmarkdown(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        content: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=content, msg_type=9, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=content, msg_type=9, quote=quote, chat_code=chat_code
        )

    async def send_card(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        card_json: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=card_json, msg_type=10, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=card_json, msg_type=10, quote=quote, chat_code=chat_code
        )

    async def send_image_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=url, msg_type=2, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=url, msg_type=2, quote=quote, chat_code=chat_code
        )

    async def send_file_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=url, msg_type=4, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=url, msg_type=4, quote=quote, chat_code=chat_code
        )

    async def upload_asset(self, data: bytes, filename: str) -> str:
        return await self.rest.asset_create(data, filename)

    async def message(
        self, *, channel_id: str | None, msg_id: str, guild_id: str | None = None, chat_code: str | None = None
    ) -> Message:
        if channel_id is not None:
            raw = await self.rest.message_view(msg_id)
        elif chat_code:
            raw = await self.rest.direct_message_view(msg_id, chat_code)
        else:
            raise KookError("direct message lookup requires chat_code")
        return parse_rest_message(raw, channel_id=channel_id, guild_id=guild_id)

    async def history(
        self, channel_id: str, *, guild_id: str | None = None, before: str | None = None, limit: int = 50
    ) -> list[Message]:
        rows = await self.rest.message_list(channel_id, msg_id=before, flag="before", page_size=limit)
        msgs = [parse_rest_message(r, channel_id=channel_id, guild_id=guild_id) for r in rows]
        msgs.sort(key=lambda m: m.time)
        return msgs

    async def react(self, *, channel_id: str | None, msg_id: str, emoji: str) -> None:
        if channel_id is not None:
            await self.rest.message_add_reaction(msg_id, emoji)
        else:
            await self.rest.direct_message_add_reaction(msg_id, emoji)

    async def unreact(self, *, channel_id: str | None, msg_id: str, emoji: str, user_id: str | None = None) -> None:
        if channel_id is not None:
            await self.rest.message_delete_reaction(msg_id, emoji, user_id=user_id)
        else:
            await self.rest.direct_message_delete_reaction(msg_id, emoji, user_id=user_id)

    async def recall(self, *, channel_id: str | None, msg_id: str) -> None:
        if channel_id is not None:
            await self.rest.message_delete(msg_id)
        else:
            await self.rest.direct_message_delete(msg_id)

    async def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        m = method.upper()
        if m == "GET":
            return await self.rest.request(m, path, params=params)
        return await self.rest.request(m, path, json=params)


# --- process-wide active connection -----------------------------------------------------------------------------

_active: Kook | None = None


def set_connected(kook: Kook | None) -> None:
    global _active
    _active = kook


def connected() -> Kook | None:
    return _active
