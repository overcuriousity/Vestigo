"""sources.converter_input_hash — the raw file a generated converter turned into this source.

Revision ID: 0031
Revises: 0030

A source produced by a generated converter is a Parquet whose bytes are not
stable across runs (converters stamp ``converted_at``), so the source's own
``file_hash`` cannot say "this raw file was already converted with this
script". Recording the raw input's hash next to ``converter_script_id`` is what
lets the convert endpoint refuse that repeat before any work happens.
Additive; downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("converter_input_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_sources_converter_input",
        "sources",
        ["case_id", "converter_script_id", "converter_input_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_sources_converter_input", table_name="sources")
    op.drop_column("sources", "converter_input_hash")
