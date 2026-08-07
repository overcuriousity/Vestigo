"""Memoized analysis results, plus the scope a verdict was reached under.

``analysis_cache`` holds derived data only, keyed on a fingerprint of every
input that can change an answer — dropping the whole table costs a rescan and
nothing else, which is why it carries no TTL and no audit trail.

``finding_dispositions.analysis_scope`` closes a real gap: a confirmed verdict recorded
nothing about the comparison it was reached under, so "which scope produced
this finding" was unanswerable from the database. Nullable, so every
pre-existing row reads as "scope not recorded" rather than being backfilled
with a fabricated one.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_cache",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_analysis_cache_case_id", "analysis_cache", ["case_id"])
    op.create_index("ix_analysis_cache_computed_at", "analysis_cache", ["computed_at"])
    op.create_index(
        "ix_analysis_cache_case_key", "analysis_cache", ["case_id", "cache_key"], unique=True
    )
    op.add_column("finding_dispositions", sa.Column("analysis_scope", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("finding_dispositions", "analysis_scope")
    op.drop_index("ix_analysis_cache_case_key", table_name="analysis_cache")
    op.drop_index("ix_analysis_cache_computed_at", table_name="analysis_cache")
    op.drop_index("ix_analysis_cache_case_id", table_name="analysis_cache")
    op.drop_table("analysis_cache")
