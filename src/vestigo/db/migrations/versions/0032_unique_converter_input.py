"""ix_sources_converter_input becomes a partial unique index; converter_scripts.raw_mtime.

Revision ID: 0032
Revises: 0031

The "same saved script over the same raw file" refusal lived only in the
HTTP handler (PR #277 review, third pass): the CLI and two concurrent submits
could still land the evidence twice, because the source row is created only
after the multi-minute conversion. The job now checks before it starts and
again before it registers, and this index is the backstop under both — one
source per (case, converter script, raw input). Rows without a converter are
untouched by the partial predicate. Downgrade restores the plain index.

``converter_scripts.raw_mtime`` records the evidence file's own modification
time as the uploader stated it — what the prompt told the model, and what a
regeneration replays — nullable because a raw API upload may carry none.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_WHERE = "converter_script_id IS NOT NULL AND converter_input_hash IS NOT NULL"


def upgrade() -> None:
    op.drop_index("ix_sources_converter_input", table_name="sources")
    op.create_index(
        "ix_sources_converter_input",
        "sources",
        ["case_id", "converter_script_id", "converter_input_hash"],
        unique=True,
        postgresql_where=_WHERE,
    )
    op.add_column(
        "converter_scripts", sa.Column("raw_mtime", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("converter_scripts", "raw_mtime")
    op.drop_index("ix_sources_converter_input", table_name="sources")
    op.create_index(
        "ix_sources_converter_input",
        "sources",
        ["case_id", "converter_script_id", "converter_input_hash"],
    )
