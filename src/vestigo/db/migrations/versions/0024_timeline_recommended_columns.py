"""Persist a timeline's recommended event-grid columns (issue #213).

Nullable, so every pre-existing timeline reads as "never recommended" and the
explorer keeps its built-in defaults until a recommendation job runs. The
payload is opaque metadata about *display*, never about the events themselves,
which is why replacing it post hoc is forensically inert.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("timelines", sa.Column("recommended_columns", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("timelines", "recommended_columns")
