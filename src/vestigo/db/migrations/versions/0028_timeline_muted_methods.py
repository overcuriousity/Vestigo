"""Persist the analysis methods an analyst has muted for a timeline.

Nullable, so every pre-existing timeline reads as "nothing muted" and its sweep
is unchanged on upgrade. The payload is a list of ``METHOD_IDS`` entries and is
a *reading* preference about the unprompted sweep — the analysis plan does not
consult it and ``/analysis/findings`` still runs a muted method on request — so
replacing it post hoc is forensically inert.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("timelines", sa.Column("muted_methods", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("timelines", "muted_methods")
