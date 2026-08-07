"""Let a deleted view survive while a story block still embeds it.

A ``view_ref`` story block resolves its View live at render and export time,
so hard-deleting a referenced View made that story's export fail. Deleting one
now stamps ``deleted_at`` instead: the row stays, out of every list the analyst
sees, until the last referencing block is gone.

Nullable, so every pre-existing view reads as live.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("views", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_views_deleted_at", "views", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_views_deleted_at", table_name="views")
    op.drop_column("views", "deleted_at")
