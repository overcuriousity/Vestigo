"""agent_proposals: kind + payload (generalized proposal shapes)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-26

``propose_story_block`` (W7 agent parity) records proposals in the same
table as ``propose_annotation`` so the atomic decide / 409-on-redecide
backbone stays single-path. ``kind`` discriminates ("annotation" for every
pre-existing row via the server default), ``payload`` carries the
kind-specific body for non-annotation proposals.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_proposals",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="annotation",
        ),
    )
    op.add_column("agent_proposals", sa.Column("payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_proposals", "payload")
    op.drop_column("agent_proposals", "kind")
