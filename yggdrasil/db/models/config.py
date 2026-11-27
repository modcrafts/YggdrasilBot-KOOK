"""Runtime configuration edited from the dashboard and hot-reloaded via Redis pub/sub."""

from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, Boolean, ForeignKey, Identity, Index, LargeBinary, PrimaryKeyConstraint, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import Base, TimestampMixin


class Provider(Base, TimestampMixin):
    """An OpenAI- or Anthropic-compatible LLM endpoint."""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    api_format: Mapped[str] = mapped_column(Text, default="openai", server_default="openai")
    base_url: Mapped[str] = mapped_column(Text)
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    models: Mapped[list[Any]] = mapped_column(default=list)
    models_fetched_at: Mapped[datetime | None]
    model_aliases: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    model_caps: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    upstream_caps: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    balance_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)


class SearchProvider(Base, TimestampMixin):
    """A web search backend. ``web_search`` tries enabled ones in priority order."""

    __tablename__ = "search_providers"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(default=100)
    monthly_limit: Mapped[int | None]
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)


class TaskRoute(Base, TimestampMixin):
    """Which model serves a task (chat, tldr, vision, memory_*, moderation, ...)."""

    __tablename__ = "task_routes"

    task: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"))
    model: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    fallback_provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"))
    fallback_model: Mapped[str | None] = mapped_column(Text)
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Guild(Base, TimestampMixin):
    """A KOOK server (guild). Holds guild-level sparse settings overrides (layer 2 of 4)."""

    __tablename__ = "guilds"

    guild_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Sparse override merged over global settings; categories and channels inherit from this.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Channel(Base, TimestampMixin):
    """A KOOK category (``is_category=True``), text channel (``channel_type=1``), or forum/thread channel (``4``).

    Inherits ``enabled`` and ``settings`` along ``global -> guild -> category -> channel``.
    ``enabled`` is tri-state: ``None`` inherits from parent category (or defaults False if uncategorized);
    ``True`` / ``False`` explicitly overrides at this level.
    """

    __tablename__ = "channels"
    __table_args__ = (Index("ix_channels_guild_parent", "guild_id", "parent_id"),)

    channel_id: Mapped[str] = mapped_column(Text, primary_key=True)
    guild_id: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(Text)
    is_category: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_type: Mapped[int] = mapped_column(default=1, server_default="1")  # 0=category, 1=text, 4=thread/forum
    position: Mapped[int] = mapped_column(default=0)
    name: Mapped[str] = mapped_column(Text, default="")
    topic: Mapped[str] = mapped_column(Text, default="", server_default="")
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    # Sparse override merged over parent layer (category -> guild -> global).
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    backfilled_at: Mapped[datetime | None]


class CommandTrigger(Base, TimestampMixin):
    """Data-driven trigger rule.

    ``scope``: ``global`` | ``guild`` | ``category`` | ``channel`` | ``private``.
    ``scope_id``: None for ``global`` / ``private``, else the matching guild/category/channel id.
    """

    __tablename__ = "command_triggers"
    __table_args__ = (Index("ix_command_triggers_scope", "scope", "scope_id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    handler: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="global")
    scope_id: Mapped[str | None] = mapped_column(Text)
    match_type: Mapped[str] = mapped_column(Text)
    patterns: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    arg_template: Mapped[str | None] = mapped_column(Text)
    require_mention: Mapped[bool] = mapped_column(default=False)
    target: Mapped[str] = mapped_column(Text, default="quoted_or_self")
    priority: Mapped[int] = mapped_column(default=100)
    enabled: Mapped[bool] = mapped_column(default=True)
    cooldown_s: Mapped[int] = mapped_column(default=0)
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    note: Mapped[str] = mapped_column(Text, default="")


class Prompt(Base, TimestampMixin):
    __tablename__ = "prompts"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    active_version_id: Mapped[int | None]


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (Index("ix_prompt_versions_key", "key", "id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Admin(Base):
    """``kind``: ``kook`` (superuser KOOK user id: reviews, ops commands) | ``email`` (dashboard access)."""

    __tablename__ = "admins"
    __table_args__ = (PrimaryKeyConstraint("kind", "value"),)

    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SettingsDoc(Base, TimestampMixin):
    """Settings rows: ``global`` (validated by :class:`yggdrasil.settings.Settings`) and ``kook.bot``
    (encrypted KOOK bot token managed by ``admin.kook``)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)


class EmbedSpace(Base, TimestampMixin):
    __tablename__ = "embed_spaces"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"))
    model: Mapped[str] = mapped_column(Text)
    dim: Mapped[int]
    active: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default="backfilling")


class PipelineState(Base, TimestampMixin):
    """Per-scope watermarks (``scope_id`` = channel_id for chunk/topic, guild_id for glossary)."""

    __tablename__ = "pipeline_state"
    __table_args__ = (PrimaryKeyConstraint("pipeline", "scope_id"),)

    pipeline: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[str] = mapped_column(Text)
    last_msg_id: Mapped[int] = mapped_column(default=0)
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ConfigAudit(Base):
    __tablename__ = "config_audit"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
