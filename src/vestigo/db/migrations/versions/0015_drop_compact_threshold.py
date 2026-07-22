"""drop agent_settings.compact_threshold

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-22

LLM history compaction was retired in favour of the deterministic sliding
context window (``src/vestigo/agent/window.py``), which has no threshold
setting — ``context_window`` alone drives it. See
``docs/superpowers/specs/2026-07-22-agent-sliding-window-design.md``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table so the drop works on SQLite (tests) as well.
    with op.batch_alter_table("agent_settings") as batch:
        batch.drop_column("compact_threshold")


def downgrade() -> None:
    op.add_column("agent_settings", sa.Column("compact_threshold", sa.Float(), nullable=True))
