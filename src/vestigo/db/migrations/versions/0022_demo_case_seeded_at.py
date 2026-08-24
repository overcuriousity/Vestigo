"""Record when a user's demo case was seeded.

Null means "never" — which is every pre-existing row, so an upgrade backfills
the demo case through the same first-login path a brand-new account takes
(``core/demo_case.py``). The stamp survives the user deleting the case, which
is what keeps a deleted demo case deleted.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("demo_case_seeded_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "demo_case_seeded_at")
