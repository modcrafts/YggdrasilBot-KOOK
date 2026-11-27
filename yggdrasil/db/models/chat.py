"""Raw chat history, guild member aliases, and KOOK sticker tables.

``messages`` is range-partitioned by month on ``sent_at``.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import Base


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        PrimaryKeyConstraint("id", "sent_at"),
        Index("ix_messages_channel_sent", "channel_id", text("sent_at DESC")),
        Index("ix_messages_guild_sent", "guild_id", text("sent_at DESC")),
        Index("ix_messages_channel_kook", "channel_id", "kook_message_id"),
        Index("ix_messages_guild_user_sent", "guild_id", "user_id", text("sent_at DESC")),
        {"postgresql_partition_by": "RANGE (sent_at)"},
    )

    id: Mapped[int] = mapped_column(Identity(always=True))
    sent_at: Mapped[datetime]
    channel_id: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    # KOOK globally unique message UUID.
    kook_message_id: Mapped[str] = mapped_column(Text)
    reply_to_msg_id: Mapped[str | None] = mapped_column(Text)
    sender_name: Mapped[str] = mapped_column(Text, default="")
    plain_text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list[Any]]
    is_self: Mapped[bool] = mapped_column(default=False)
    recalled: Mapped[bool] = mapped_column(default=False)
    recalled_at: Mapped[datetime | None]


class UserAlias(Base):
    """Guild-level nickname history so old messages stay attributable across renames."""

    __tablename__ = "user_aliases"
    __table_args__ = (PrimaryKeyConstraint("guild_id", "user_id", "name"),)

    guild_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class Sticker(Base):
    """A KOOK sticker seen in chat, one row per distinct sticker.

    ``kind = 'emoji'``: a KOOK guild custom emoji (``(emj)name(emj)[id]``); ``ref`` is the emoji id, ``guild_id``
    records the owning guild so same-guild drops can send the native emoji directly and cross-guild drops can send
    its cached image blob.
    ``kind = 'image'``: an animated GIF/WebP or small sticker-like image sent in chat; ``ref`` is content sha256.
    """

    __tablename__ = "stickers"
    __table_args__ = (UniqueConstraint("kind", "ref"), Index("ix_stickers_status", "status"))

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # emoji | image
    ref: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str | None] = mapped_column(Text)  # owning guild for kind='emoji'
    emoji_id: Mapped[str | None] = mapped_column(Text)  # KOOK emoji id when kind='emoji'
    summary: Mapped[str] = mapped_column(Text, default="")  # KOOK emoji name or filename
    status: Mapped[str] = mapped_column(Text, default="fetching")  # fetching | untagged | ready | failed
    error: Mapped[str] = mapped_column(Text, default="")
    mime: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    frames: Mapped[int | None]
    description: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[str] = mapped_column(Text, default="")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    emotions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    unsafe_category: Mapped[str] = mapped_column(Text, default="")
    tag_attempts: Mapped[int] = mapped_column(default=0)
    tagged_at: Mapped[datetime | None]
    seen_count: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class StickerBlob(Base):
    """Original image bytes of a sticker (for vision tagging and image drops)."""

    __tablename__ = "sticker_blobs"

    sticker_id: Mapped[int] = mapped_column(ForeignKey("stickers.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class StickerUsage(Base):
    """How often each guild sends a sticker (guild-shared popularity)."""

    __tablename__ = "sticker_usage"
    __table_args__ = (PrimaryKeyConstraint("sticker_id", "guild_id"),)

    sticker_id: Mapped[int] = mapped_column(ForeignKey("stickers.id", ondelete="CASCADE"))
    guild_id: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(default=1)
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class StickerDrop(Base):
    """One per-channel sticker-drop decision (``stickers.drop``)."""

    __tablename__ = "sticker_drops"
    __table_args__ = (Index("ix_sticker_drops_channel_at", "channel_id", text("at DESC")),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    channel_id: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    fit: Mapped[float | None]
    sticker_id: Mapped[int | None] = mapped_column(ForeignKey("stickers.id", ondelete="SET NULL"))
    confidence: Mapped[float | None]
    candidates: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    last_message_id: Mapped[int]
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float | None]


class KookThread(Base):
    """Cached index of threads inside KOOK forum/thread channels (``channel_type = 4``)."""

    __tablename__ = "kook_threads"
    __table_args__ = (
        Index("ix_kook_threads_channel_active", "channel_id", text("latest_active_time DESC")),
        Index("ix_kook_threads_guild_active", "guild_id", text("latest_active_time DESC")),
    )

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str] = mapped_column(Text)
    category_id: Mapped[str | None] = mapped_column(Text)
    category_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    post_id: Mapped[str] = mapped_column(Text, default="", server_default="")
    title: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[str] = mapped_column(Text, default="")
    author_name: Mapped[str] = mapped_column(Text, default="")
    preview_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    content_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    medias: Mapped[list[Any]] = mapped_column(default=list)
    post_count: Mapped[int] = mapped_column(default=0, server_default="0")
    collect_num: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    latest_active_time: Mapped[datetime] = mapped_column(server_default=func.now())
    content_deleted: Mapped[bool] = mapped_column(default=False, server_default="false")
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())
