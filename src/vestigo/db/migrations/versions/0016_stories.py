"""stories, story_blocks, story_exports (W7)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26

Per-case block documents: ``stories`` (title/metadata), ``story_blocks``
(ordered content, integer gap positions, optimistic ``version`` counter)
and ``story_exports`` (immutable server-resolved snapshots plus the
seal-once client-rendered HTML artifact). See docs/STORIES.md and
``docs/superpowers/specs/2026-07-26-w7-stories-design.md``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stories_case_id"), "stories", ["case_id"], unique=False)
    op.create_table(
        "story_blocks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("story_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_story_blocks_story_id"), "story_blocks", ["story_id"], unique=False)
    op.create_table(
        "story_exports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("story_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("html_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_story_exports_case_id"), "story_exports", ["case_id"], unique=False)
    op.create_index(op.f("ix_story_exports_story_id"), "story_exports", ["story_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_story_exports_story_id"), table_name="story_exports")
    op.drop_index(op.f("ix_story_exports_case_id"), table_name="story_exports")
    op.drop_table("story_exports")
    op.drop_index(op.f("ix_story_blocks_story_id"), table_name="story_blocks")
    op.drop_table("story_blocks")
    op.drop_index(op.f("ix_stories_case_id"), table_name="stories")
    op.drop_table("stories")
