"""Persist the per-method field decisions an analyst has declared for a timeline.

Nullable, so every pre-existing timeline reads as "nothing declared" and its
detectors pick their own fields exactly as before on upgrade. The payload is
``{method_id: {field_token: bool}}`` and only ever steers a detector's
*automatic* field selection — an explicit ``fields=[…]`` still scans an excluded
field, the analysis plan does not consult it, and a run that held a field back
discloses it — so replacing it post hoc is forensically inert.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("timelines", sa.Column("field_overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("timelines", "field_overrides")
