"""app_settings: admin-editable configuration overrides

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-27

Key/value rather than column-per-setting: the catalog of settings lives in
``core/settings_registry.py`` and changes with the code, so a mirrored schema
would need a migration per tunable. Values are JSON so an int survives the
round-trip as an int. A row exists only for a field an admin actually set;
clearing an override deletes it (see ``PostgresStore.set_app_settings``).

No backfill — an empty table means "everything resolves from the environment
and the built-in defaults", which is exactly the pre-upgrade behaviour.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
