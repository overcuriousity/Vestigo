"""sigma rules and runs

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-17

Adds the two tables backing the Sigma rule runner (W5):

- ``sigma_rules`` — case-scoped uploaded Sigma rules (global rules from
  ``VESTIGO_SIGMA_RULES_PATH`` stay on disk and are hashed at run time).
- ``sigma_runs`` — persisted evaluation records: per-rule compiled
  ClickHouse SQL, content hash, match counts, and status, kept for
  forensic reproducibility like ``detector_runs``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sigma_rules",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("rule_key", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("rule_uuid", sa.String(length=64), nullable=True),
        sa.Column("level", sa.String(length=32), nullable=True),
        sa.Column("logsource", sa.JSON(), nullable=True),
        sa.Column("yaml_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sigma_rules_case_id"), "sigma_rules", ["case_id"], unique=False)
    op.create_index(op.f("ix_sigma_rules_rule_key"), "sigma_rules", ["rule_key"], unique=False)

    op.create_table(
        "sigma_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("timeline_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=4096), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sigma_runs_case_id"), "sigma_runs", ["case_id"], unique=False)
    op.create_index(op.f("ix_sigma_runs_timeline_id"), "sigma_runs", ["timeline_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sigma_runs_timeline_id"), table_name="sigma_runs")
    op.drop_index(op.f("ix_sigma_runs_case_id"), table_name="sigma_runs")
    op.drop_table("sigma_runs")
    op.drop_index(op.f("ix_sigma_rules_rule_key"), table_name="sigma_rules")
    op.drop_index(op.f("ix_sigma_rules_case_id"), table_name="sigma_rules")
    op.drop_table("sigma_rules")
