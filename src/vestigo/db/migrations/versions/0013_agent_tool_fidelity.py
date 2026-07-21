"""agent tool-result fidelity setting

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-21

``agent_settings.tool_fidelity`` records how much of an example record the
agent's tool results carry (``full`` | ``message`` | ``minimal`` | ``auto`` —
see ``src/vestigo/agent/fidelity.py``). NULL means "not configured", which
resolves to ``full``: a deployment that has declared no context constraint is
assumed to have room, and the overflow backstop costs a retry rather than the
turn.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_settings", sa.Column("tool_fidelity", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_settings", "tool_fidelity")
