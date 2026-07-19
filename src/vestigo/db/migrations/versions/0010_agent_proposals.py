"""agent proposals

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-19

Adds ``agent_proposals`` — an agent-proposed annotation awaiting analyst
confirmation (A1, docs/AGENT.md). The agent resolves the target events at
propose time and states its reasoning; an analyst then confirms or rejects.
``status`` starts at ``proposed`` and transitions exactly once via an atomic
update, the idempotency backbone for the API's 409-on-redecide behavior.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_proposals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("timeline_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column("tag", sa.String(length=255), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_proposals_conversation_id"),
        "agent_proposals",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_proposals_case_id"), "agent_proposals", ["case_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_proposals_case_id"), table_name="agent_proposals")
    op.drop_index(op.f("ix_agent_proposals_conversation_id"), table_name="agent_proposals")
    op.drop_table("agent_proposals")
