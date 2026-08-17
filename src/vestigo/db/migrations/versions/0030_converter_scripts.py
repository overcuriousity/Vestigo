"""converter_scripts table + sources.converter_script_id.

Revision ID: 0030
Revises: 0029

Generated converters (1.13): a case-bound row per model-written script, and a
nullable back-reference from the Parquet source it produced. Both additive;
downgrade drops them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "converter_scripts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("provider_endpoint", sa.String(512), nullable=True),
        sa.Column("prompt_hash", sa.String(64), nullable=True),
        sa.Column("sample_hash", sa.String(64), nullable=True),
        sa.Column("sample_excerpt", sa.Text(), nullable=True),
        sa.Column("raw_file_hash", sa.String(64), nullable=False),
        sa.Column("raw_filename", sa.String(255), nullable=True),
        sa.Column("hint", sa.Text(), nullable=True),
        sa.Column("attempts", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_converter_scripts_case_id", "converter_scripts", ["case_id"])
    op.create_index(
        "ix_converter_scripts_case_name_version",
        "converter_scripts",
        ["case_id", "name", "version"],
        unique=True,
    )
    op.add_column("sources", sa.Column("converter_script_id", sa.String(64), nullable=True))
    op.create_index("ix_sources_converter_script_id", "sources", ["converter_script_id"])


def downgrade() -> None:
    op.drop_index("ix_sources_converter_script_id", table_name="sources")
    op.drop_column("sources", "converter_script_id")
    op.drop_index("ix_converter_scripts_case_name_version", table_name="converter_scripts")
    op.drop_index("ix_converter_scripts_case_id", table_name="converter_scripts")
    op.drop_table("converter_scripts")
