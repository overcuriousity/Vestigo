"""agent message tool_call_id

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-21

``agent_messages.tool_call_id`` is the provider-issued id shared by a tool
call row and its result row. Models that batch parallel tool calls persist
N call rows followed by N result rows in completion order, so this id is the
only reliable pairing key (the UI's chart-proposal cards depend on it). NULL
on rows written before this migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_messages", sa.Column("tool_call_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_messages", "tool_call_id")
