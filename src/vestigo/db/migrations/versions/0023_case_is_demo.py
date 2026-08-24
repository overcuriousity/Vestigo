"""Mark seeded demo cases as such.

False for every pre-existing row, which is correct: a case that predates this
column was created by hand. The flag is what lets the case list keep other
users' fabricated data out of an admin's view, and what lets the restore
endpoint refuse while the caller still has a copy (``core/demo_case.py``).

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("cases", "is_demo")
