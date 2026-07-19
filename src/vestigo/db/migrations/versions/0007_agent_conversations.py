"""agent conversations and messages

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-19

Adds the two tables backing the optional AI investigation agent
(docs/AGENT.md):

- ``agent_conversations`` — one chat per case timeline + user, carrying the
  runtime's replayable message-history snapshot (``history``).
- ``agent_messages`` — append-only human-readable steps, including every
  tool call with exact arguments and result summary, kept for forensic
  reproducibility like ``detector_runs``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("timeline_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("model_id", sa.String(length=255), nullable=True),
        sa.Column("history", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_conversations_case_id"), "agent_conversations", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_conversations_timeline_id"),
        "agent_conversations",
        ["timeline_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_conversations_user_id"), "agent_conversations", ["user_id"], unique=False
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("tool_args", sa.JSON(), nullable=True),
        sa.Column("tool_result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_messages_conversation_id"),
        "agent_messages",
        ["conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_messages_conversation_id"), table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index(op.f("ix_agent_conversations_user_id"), table_name="agent_conversations")
    op.drop_index(op.f("ix_agent_conversations_timeline_id"), table_name="agent_conversations")
    op.drop_index(op.f("ix_agent_conversations_case_id"), table_name="agent_conversations")
    op.drop_table("agent_conversations")
