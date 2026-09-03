"""Configured detectors per timeline; the mute list retired.

``detectors`` is the list of analysis methods an analyst has configured for
this timeline — ``{method, params, frame, baseline_id, added_by, added_at}``
per entry, at most one per method — and is the *only* thing the Investigate
rail runs. Nullable, so every pre-existing timeline reads as "nothing
configured": no statistical detector runs unprompted any more, and the safe
upgrade for a column that decides what an analyst is shown is the one that
shows nothing until asked.

``muted_methods`` (0028) existed only to subtract from the unprompted sweep.
With no sweep there is nothing to subtract from, so it is dropped rather than
carried as dead state. The downgrade restores it empty: a mute never said
which detector *should* run, so nothing translates in either direction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("timelines", sa.Column("detectors", sa.JSON(), nullable=True))
    op.drop_column("timelines", "muted_methods")


def downgrade() -> None:
    op.add_column("timelines", sa.Column("muted_methods", sa.JSON(), nullable=True))
    op.drop_column("timelines", "detectors")
