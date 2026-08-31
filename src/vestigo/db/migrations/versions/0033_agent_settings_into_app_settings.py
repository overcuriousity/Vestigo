"""fold agent_settings into app_settings and drop the table

Revision ID: 0033
Revises: 0032

The agent shipped with a purpose-built ``agent_settings`` singleton row
(0011/0012/0013/0015) months before the generic ``app_settings`` override
layer existed. Once it did, one configuration had two storage shapes, two
resolvers, two save endpoints and two secret-mode switches — and the eleven
agent knobs showed up read-only on ``Admin → Settings`` with a badge pointing
at a second tab. Every one of them is already a ``VESTIGO_AGENT_*`` field on
``Settings``, so the row was carrying nothing the generic layer could not.

This copies each non-NULL column of the ``id='global'`` row into the matching
``app_settings`` key (prefixing the field name with ``agent_``) and drops the
table. ``api_key`` moves with the rest: both tables hold it as plaintext under
the same "acceptable only when Postgres itself is trusted" contract, so the
copy changes nothing about its exposure, while dropping it would break a
working instance on upgrade.

``compact_threshold`` is gone since 0015 and ``id``/``updated_by``/
``updated_at`` describe the row rather than the configuration, so neither is
carried over. An existing ``app_settings`` key wins over the copied value:
it was written by the newer surface, which means an admin edited it more
recently than the row this migration is retiring.

Downgrade recreates the table (in its 0015 shape) and moves the values back,
deleting the ``app_settings`` rows it consumed so the two never disagree.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

#: Every configuration column of the retired row. The ``app_settings`` key is
#: the same name under the ``agent_`` prefix the instance-wide namespace uses.
_COLUMNS: tuple[str, ...] = (
    "model",
    "provider",
    "api_base_url",
    "api_key",
    "user_agent",
    "extra_headers",
    "max_turns",
    "reasoning_effort",
    "context_window",
    "tool_fidelity",
    "disabled_tools",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agent_settings" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("agent_settings")}
        present = [c for c in _COLUMNS if c in columns]
        if present:
            row = (
                bind.execute(
                    sa.text(
                        f"SELECT {', '.join(present)} FROM agent_settings WHERE id = 'global'"  # noqa: S608 - names come from a literal tuple
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                for column in present:
                    value = row[column]
                    if value is None:
                        continue
                    bind.execute(
                        sa.text(
                            "INSERT INTO app_settings (key, value, updated_by) "
                            "VALUES (:key, CAST(:value AS JSON), 'migration-0033') "
                            "ON CONFLICT (key) DO NOTHING"
                        ),
                        {"key": f"agent_{column}", "value": json.dumps(value)},
                    )
        op.drop_table("agent_settings")

    # Retired with the row: the LLM key now follows the instance-wide
    # VESTIGO_SECRETS_MODE, so a stored override of the old switch would sit
    # there forever as a field no Settings model declares.
    bind.execute(sa.text("DELETE FROM app_settings WHERE key = 'agent_secret_mode'"))


def downgrade() -> None:
    op.create_table(
        "agent_settings",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("api_base_url", sa.String(length=512), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("extra_headers", sa.JSON(), nullable=True),
        sa.Column("max_turns", sa.Integer(), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("tool_fidelity", sa.String(length=16), nullable=True),
        sa.Column("disabled_tools", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    bind = op.get_bind()
    keys = [f"agent_{column}" for column in _COLUMNS]
    stored = {
        row.key: row.value
        for row in bind.execute(
            sa.text("SELECT key, value FROM app_settings WHERE key = ANY(:keys)"), {"keys": keys}
        )
    }
    if not stored:
        return

    values = {column: stored.get(f"agent_{column}") for column in _COLUMNS}
    assignments = ", ".join(f":{column}" for column in _COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO agent_settings (id, {', '.join(_COLUMNS)}, updated_by) "  # noqa: S608 - names come from a literal tuple
            f"VALUES ('global', {assignments}, 'migration-0033')"
        ),
        {
            column: json.dumps(value) if isinstance(value, dict | list) else value
            for column, value in values.items()
        },
    )
    bind.execute(sa.text("DELETE FROM app_settings WHERE key = ANY(:keys)"), {"keys": keys})
