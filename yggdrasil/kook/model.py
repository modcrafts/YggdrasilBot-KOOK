"""KOOK domain models: messages, incoming events, guilds, channels, members, notices and errors."""

from dataclasses import dataclass, field
from typing import Any, Literal

from yggdrasil.kook.segments import Segment

Role = Literal["owner", "admin", "member"]

# KOOK channel types (from KOOK REST/Gateway protocol).
CHANNEL_CATEGORY = 0
CHANNEL_TEXT = 1
CHANNEL_VOICE = 2
CHANNEL_THREAD = 4


@dataclass(frozen=True, slots=True)
class Sender:
    user_id: str
    name: str  # guild nickname if set, else username ("" when unknown)
    username: str = ""
    role: Role = "member"
    role_ids: tuple[int, ...] = ()  # KOOK guild role IDs from extra.author.roles
    is_bot: bool = False
    avatar: str = ""
    identify_num: str = ""
    online: bool = False
    tag_text: str = ""
    is_sys: bool = False


@dataclass(slots=True)
class Message:
    """A KOOK message in a text channel (``channel_id`` + ``guild_id``) or a direct message (``channel_id`` None)."""

    message_id: str  # KOOK UUID msg_id
    channel_id: str | None
    guild_id: str | None
    sender: Sender
    time: int  # unix seconds
    segments: list[Segment]
    updated_at: int | None = None  # unix seconds if edited
    embeds: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Incoming(Message):
    """An incoming message event parsed for routing and pipeline execution."""

    self_id: str = ""
    text: str = ""  # plain text addressed to the bot (leading/trailing @bot stripped)
    to_me: bool = False  # @bot anywhere, reply to bot, or direct message
    reply: Message | None = None  # quoted message if present
    channel_name: str = ""
    chat_code: str | None = None  # KOOK direct-message session code

    @property
    def user_id(self) -> str:
        return self.sender.user_id


@dataclass(frozen=True, slots=True)
class Login:
    user_id: str
    username: str
    identify_num: str = ""
    avatar: str = ""

    @property
    def nickname(self) -> str:
        return self.username


@dataclass(frozen=True, slots=True)
class Guild:
    guild_id: str
    name: str
    topic: str = ""
    icon: str = ""
    owner_id: str = ""
    default_channel_id: str = ""
    welcome_channel_id: str = ""
    region: str = ""
    enable_open: bool = False
    open_id: str = ""
    notify_type: int = 0


@dataclass(frozen=True, slots=True)
class Channel:
    channel_id: str
    guild_id: str
    name: str
    topic: str = ""
    type: int = CHANNEL_TEXT  # 0=category, 1=text, 2=voice, 4=thread/forum
    is_category: bool = False
    parent_id: str | None = None
    level: int = 0  # KOOK position/sort order
    creator_id: str = ""
    slow_mode: int = 0
    slow_mode_reply: int = 0
    default_layout: int = 0
    sort_order: int = 0
    permission_sync: bool | None = None

    @property
    def is_thread(self) -> bool:
        return not self.is_category and self.type == CHANNEL_THREAD


@dataclass(frozen=True, slots=True)
class GuildRole:
    role_id: int
    name: str
    permissions: int = 0
    color: int = 0
    position: int = 0
    hoist: bool = False
    mentionable: bool = False
    op_permissions: int = 0


@dataclass(frozen=True, slots=True)
class Member:
    guild_id: str
    user_id: str
    username: str
    nickname: str = ""
    role: Role = "member"
    roles: tuple[int, ...] = ()
    role_names: tuple[str, ...] = ()
    is_bot: bool = False
    avatar: str = ""
    joined_at: int | None = None  # epoch seconds
    active_time: int | None = None  # epoch seconds
    identify_num: str = ""
    online: bool = False
    os: str = ""
    status: int = 0
    is_vip: bool = False
    vip_avatar: str = ""
    is_sys: bool = False
    is_master: bool = False
    banner: str = ""
    tag_text: str = ""
    wealth_level: int = 0
    mobile_verified: bool | None = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.username


@dataclass(frozen=True, slots=True)
class ThreadCategory:
    id: str
    name: str
    allow: int = 0
    deny: int = 0


@dataclass(frozen=True, slots=True)
class ThreadTag:
    id: int
    name: str
    icon: str = ""


@dataclass(frozen=True, slots=True)
class ThreadMedia:
    type: int  # 1=image, 2=video, 3=audio, etc.
    src: str
    title: str = ""
    cover: str = ""


@dataclass(frozen=True, slots=True)
class ForumThread:
    """A KOOK forum thread (inside a ``type=4`` thread channel)."""

    thread_id: str
    channel_id: str
    guild_id: str
    title: str
    author: Sender
    post_id: str = ""
    cover: str = ""
    status: int = 0
    preview_content: str = ""
    content: str | None = None  # raw CardMessage JSON; None when fetched via /thread/list
    content_text: str = ""  # extracted readable text from CardMessage content (or preview)
    content_segments: tuple[Segment, ...] = ()
    category: ThreadCategory | None = None
    tags: tuple[ThreadTag, ...] = ()
    medias: tuple[ThreadMedia, ...] = ()
    create_time: int = 0  # unix seconds
    latest_active_time: int = 0  # unix seconds
    post_count: int = 0
    collect_num: int = 0
    is_updated: bool = False
    content_deleted: bool = False


@dataclass(frozen=True, slots=True)
class ForumPost:
    """A top-level comment or nested floor reply inside a KOOK forum thread."""

    post_id: str
    thread_id: str
    author: Sender
    content: str = ""  # raw CardMessage JSON
    content_text: str = ""
    content_segments: tuple[Segment, ...] = ()
    reply_id: str = ""
    belong_to_post_id: str = ""
    create_time: int = 0  # unix seconds
    is_updated: bool = False
    replies: tuple["ForumPost", ...] = ()


def emoji_origin_guild(emoji_id: str | None) -> str | None:
    """Extract the origin guild ID prefix from a KOOK custom emoji ID (``<guild_id>/<hash>``)."""
    if not emoji_id:
        return None
    raw = emoji_id.strip()
    if raw.startswith(("http://", "https://")):
        return None
    prefix, sep, rest = raw.partition("/")
    if sep and prefix and rest:
        return prefix
    return None


@dataclass(frozen=True, slots=True)
class GuildEmoji:
    id: str
    name: str
    emoji_type: int = 0  # 0=static, 1=animated
    guild_id: str = ""

    @property
    def url(self) -> str:
        """KOOK CDN URL for a guild custom emoji (``<guild_or_prefix>/<hash>``)."""
        if self.id.startswith(("http://", "https://")):
            return self.id
        ext = "gif" if self.emoji_type == 1 else "png"
        return f"https://img.kookapp.cn/emojis/{self.id}.{ext}"


@dataclass(slots=True)
class Observed:
    """A non-message KOOK system event worth recording in ``bot_events``."""

    kind: str
    guild_id: str | None = None
    channel_id: str | None = None
    user_id: str | None = None
    operator_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


# --- errors -----------------------------------------------------------------------------------------------------


class KookError(Exception):
    """A KOOK API or gateway operation failed."""

    def __init__(self, message: str, *, code: int | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class KookAuthError(KookError):
    """Bot token is missing, invalid, or revoked."""


class KookRateLimited(KookError):
    """KOOK returned HTTP 429; ``retry_after`` is seconds to wait."""

    def __init__(self, message: str, *, retry_after: float = 1.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after
