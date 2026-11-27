"""Add ``channels.channel_type`` (support KOOK forum/thread channels type=4) and ``kook_threads`` cache table.

Revision ID: 0002_forum_threads
Revises: 0001_kook_baseline
Create Date: 2026-11-27 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_forum_threads"
down_revision: str | Sequence[str] | None = "0001_kook_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("channel_type", sa.Integer(), server_default="1", nullable=False))
    op.execute("UPDATE channels SET channel_type = 0 WHERE is_category = true")

    op.create_table(
        "kook_threads",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Text(), nullable=True),
        sa.Column("category_name", sa.Text(), server_default="", nullable=False),
        sa.Column("post_id", sa.Text(), server_default="", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Text(), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("preview_text", sa.Text(), server_default="", nullable=False),
        sa.Column("content_text", sa.Text(), server_default="", nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("medias", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("post_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("collect_num", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("latest_active_time", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("content_deleted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", name=op.f("pk_kook_threads")),
    )
    op.create_index(
        "ix_kook_threads_channel_active",
        "kook_threads",
        ["channel_id", sa.text("latest_active_time DESC")],
    )
    op.create_index(
        "ix_kook_threads_guild_active",
        "kook_threads",
        ["guild_id", sa.text("latest_active_time DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_kook_threads_guild_active", table_name="kook_threads")
    op.drop_index("ix_kook_threads_channel_active", table_name="kook_threads")
    op.drop_table("kook_threads")
    op.drop_column("channels", "channel_type")
