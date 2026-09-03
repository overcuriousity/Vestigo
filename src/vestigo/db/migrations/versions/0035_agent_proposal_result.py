"""``result`` on agent_proposals: what the confirm actually did.

A confirmed proposal can legitimately apply nothing — the title was taken
since propose time, the target story was deleted, a stored chart spec no
longer validates. That outcome lived only in the confirm response, so once
the toast was gone the transcript re-rendered from ``status: confirmed``
alone and claimed the write had happened. For a surface whose whole point is
provenance, a permanent false "created by <analyst>" is the wrong failure.

``result`` is ``{applied, story_id|block_id, reason}`` written at apply time,
nullable so every pre-existing row reads as "outcome not recorded" and the
cards fall back to their old inference for those.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_proposals", sa.Column("result", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_proposals", "result")
