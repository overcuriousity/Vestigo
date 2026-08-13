"""Make ``finding_dispositions.analysis_scope`` JSONB on PostgreSQL.

``0026`` added the column as ``sa.JSON()``, which PostgreSQL renders as the
``json`` type. ``json`` has no equality operator there — only ``jsonb`` does —
and ``create_disposition`` compares the column with ``==`` to dedupe
``confirmed`` verdicts by the scope they were reached under. Every confirm
against a real deployment therefore failed with ``operator does not exist:
json = json``; the test suite runs on SQLite, where JSON is text and ``=``
works, so nothing caught it.

PostgreSQL-only. SQLite has one JSON encoding and no ``ALTER COLUMN TYPE``
worth spending here, so the migration is a no-op there and the model declares
the type as ``JSON().with_variant(JSONB(), "postgresql")``. What makes the two
dialects agree is not the storage type but writing the canonical (key-sorted)
form on both — see ``postgres.canonical_scope``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "finding_dispositions",
        "analysis_scope",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
        postgresql_using="analysis_scope::jsonb",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "finding_dispositions",
        "analysis_scope",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
        postgresql_using="analysis_scope::json",
    )
