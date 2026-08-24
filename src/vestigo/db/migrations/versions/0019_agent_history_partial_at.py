"""agent_conversations.history_partial_at

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-26

``history`` is the only thing a follow-up agent turn replays. It is now written
mid-turn as well as on completion, so the blob may be a checkpoint of a turn
that was stopped, errored or died with the process. This column records that:
stamped by a checkpoint write, cleared only when a turn completes. A column
rather than something inferred from the blob, because a completed turn and an
interrupted one both end in a ``ModelResponse``. Nullable with no backfill — an
existing conversation's history is a completed turn by definition of how it was
written.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversations",
        sa.Column("history_partial_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_conversations", "history_partial_at")
