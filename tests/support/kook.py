"""In-memory ``Kook`` client double and event builders for tests."""

from dataclasses import dataclass, field
from typing import Any

from yggdrasil.kook import segments as seg
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import (
    Channel,
    ForumPost,
    ForumThread,
    Guild,
    GuildEmoji,
    Incoming,
    KookError,
    Login,
    Member,
    Message,
    Sender,
    ThreadCategory,
)


@dataclass
class SentMessage:
    kind: str  # kmarkdown | card | image | file
    channel_id: str | None
    user_id: str
    content: str
    quote: str | None
    chat_code: str | None
    msg_id: str


@dataclass
class FakeKook(Kook):
    bot_id: str = "bot-1"
    bot_name: str = "Yggdrasil"
    guild_list: list[Guild] = field(default_factory=list)
    channel_map: dict[str, list[Channel]] = field(default_factory=dict)
    member_map: dict[tuple[str, str], Member] = field(default_factory=dict)
    emoji_map: dict[str, list[GuildEmoji]] = field(default_factory=dict)
    pinned_map: dict[str, list[Message]] = field(default_factory=dict)
    message_map: dict[str, Message] = field(default_factory=dict)
    history_map: dict[str, list[Message]] = field(default_factory=dict)
    sent: list[SentMessage] = field(default_factory=list)
    uploaded: list[tuple[str, bytes, str]] = field(default_factory=list)
    reactions: list[tuple[str | None, str, str]] = field(default_factory=list)
    unreactions: list[tuple[str | None, str, str, str | None]] = field(default_factory=list)
    recalled: list[tuple[str | None, str]] = field(default_factory=list)
    raw_calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    raw_responses: dict[tuple[str, str], Any] = field(default_factory=dict)
    thread_category_map: dict[str, list[ThreadCategory]] = field(default_factory=dict)
    forum_thread_map: dict[str, list[ForumThread]] = field(default_factory=dict)
    forum_post_map: dict[tuple[str, str | None], list[ForumPost]] = field(default_factory=dict)
    invalidated_meta: list[str] = field(default_factory=list)
    invalidated_emojis: list[str] = field(default_factory=list)
    _seq: int = 0

    @property
    def self_id(self) -> str:
        return self.bot_id

    def _next_id(self) -> str:
        self._seq += 1
        return f"msg-{self._seq}"

    async def login(self) -> Login:
        return Login(user_id=self.bot_id, username=self.bot_name)

    async def guilds(self) -> list[Guild]:
        return list(self.guild_list)

    async def guild(self, guild_id: str) -> Guild:
        for g in self.guild_list:
            if g.guild_id == guild_id:
                return g
        raise KookError(f"guild not found: {guild_id}")

    async def channels(self, guild_id: str) -> list[Channel]:
        return list(self.channel_map.get(guild_id, ()))

    async def channel(self, channel_id: str) -> Channel:
        for chs in self.channel_map.values():
            for c in chs:
                if c.channel_id == channel_id:
                    return c
        raise KookError(f"channel not found: {channel_id}")

    async def member(self, guild_id: str, user_id: str) -> Member:
        m = self.member_map.get((guild_id, user_id))
        if m is None:
            raise KookError(f"member not found: {guild_id}/{user_id}")
        return m

    async def guild_emojis(self, guild_id: str) -> list[GuildEmoji]:
        return list(self.emoji_map.get(guild_id, ()))

    async def pinned_messages(self, channel_id: str, *, limit: int = 20) -> list[Message]:
        return list(self.pinned_map.get(channel_id, ()))[:limit]

    async def send_kmarkdown(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        content: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage("kmarkdown", channel_id, user_id, content, quote, chat_code, mid))
        return mid

    async def send_card(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        card_json: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage("card", channel_id, user_id, card_json, quote, chat_code, mid))
        return mid

    async def send_image_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage("image", channel_id, user_id, url, quote, chat_code, mid))
        return mid

    async def send_file_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage("file", channel_id, user_id, url, quote, chat_code, mid))
        return mid

    async def upload_asset(self, data: bytes, filename: str) -> str:
        url = f"https://img.kookapp.cn/assets/test-{len(self.uploaded) + 1}/{filename}"
        self.uploaded.append((filename, data, url))
        return url

    async def message(
        self, *, channel_id: str | None, msg_id: str, guild_id: str | None = None, chat_code: str | None = None
    ) -> Message:
        m = self.message_map.get(msg_id)
        if m is None:
            raise KookError(f"message not found: {msg_id}")
        return m

    async def history(
        self, channel_id: str, *, guild_id: str | None = None, before: str | None = None, limit: int = 50
    ) -> list[Message]:
        return list(self.history_map.get(channel_id, ()))[-limit:]

    async def react(self, *, channel_id: str | None, msg_id: str, emoji: str) -> None:
        self.reactions.append((channel_id, msg_id, emoji))

    async def unreact(self, *, channel_id: str | None, msg_id: str, emoji: str, user_id: str | None = None) -> None:
        self.unreactions.append((channel_id, msg_id, emoji, user_id))

    async def recall(self, *, channel_id: str | None, msg_id: str) -> None:
        self.recalled.append((channel_id, msg_id))

    async def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        key = (method.upper(), path)
        self.raw_calls.append((method.upper(), path, params))
        if key in self.raw_responses:
            return self.raw_responses[key]
        return {}

    def invalidate_guild_meta(self, guild_id: str) -> None:
        self.invalidated_meta.append(guild_id)

    def invalidate_guild_emojis(self, guild_id: str) -> None:
        self.invalidated_emojis.append(guild_id)

    async def thread_categories(self, channel_id: str) -> list[ThreadCategory]:
        return list(self.thread_category_map.get(channel_id, ()))

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
        rows = list(self.forum_thread_map.get(channel_id, ()))
        if category_id:
            rows = [t for t in rows if t.category is not None and t.category.id == category_id]
        return rows[:limit]

    async def forum_thread(self, channel_id: str, thread_id: str, *, guild_id: str | None = None) -> ForumThread:
        for t in self.forum_thread_map.get(channel_id, ()):
            if t.thread_id == thread_id:
                return t
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
        return list(self.forum_post_map.get((thread_id, post_id), ()))[:limit]


def make_sender(
    user_id: str = "u-1",
    *,
    username: str = "alice",
    nickname: str = "",
    role: Any = "member",
    is_bot: bool = False,
) -> Sender:
    return Sender(user_id=user_id, name=nickname or username, username=username, role=role, is_bot=is_bot)


def make_incoming(
    text: str = "hello",
    *,
    message_id: str = "m-1",
    channel_id: str | None = "c-1",
    guild_id: str | None = "g-1",
    user_id: str = "u-1",
    username: str = "alice",
    nickname: str = "",
    role: str = "member",
    is_bot: bool = False,
    self_id: str = "bot-1",
    to_me: bool = False,
    reply: Message | None = None,
    segments: list[dict[str, Any]] | None = None,
    time_s: int = 1_700_000_000,
    channel_name: str = "general",
    chat_code: str | None = None,
) -> Incoming:
    sender = make_sender(user_id, username=username, nickname=nickname, role=role, is_bot=is_bot)
    segs = segments if segments is not None else ([seg.text(text)] if text else [])
    return Incoming(
        message_id=message_id,
        channel_id=channel_id,
        guild_id=guild_id if channel_id is not None else None,
        sender=sender,
        time=time_s,
        segments=segs,
        self_id=self_id,
        text=text,
        to_me=to_me,
        reply=reply,
        channel_name=channel_name,
        chat_code=chat_code,
    )
