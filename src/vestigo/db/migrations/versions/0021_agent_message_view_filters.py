"""Persist the Explorer filter snapshot sent with each agent user message.

The agent already receives ``view_filters`` per turn for prompt context
(``agent/runtime.py``); storing it on the user message row makes the
transcript self-describing about what the agent saw per turn (issue #205).
Nullable: historical rows and messages sent without a snapshot simply have
no stamp.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_messages", sa.Column("view_filters", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_messages", "view_filters")
