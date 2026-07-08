"""Alembic environment for TraceSignal's Postgres metadata schema.

Two entry paths share this file:

1. **In-app startup** — ``PostgresStore.init_schema`` hands an already-open
   synchronous connection in via ``config.attributes["connection"]`` (from
   ``AsyncConnection.run_sync``). We must reuse it and must not open our own.
2. **CLI** (``uv run alembic upgrade head`` etc.) — no connection is provided;
   we build an async engine from the application settings (``TS_POSTGRES_URL``)
   so the CLI can never target a different database than the app.

``render_as_batch`` is enabled on SQLite because the test suite runs the full
migration chain against ``sqlite+aiosqlite`` stores, and SQLite cannot ALTER
columns in place.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from tracesignal.core.config import get_settings
from tracesignal.db.postgres import Base

config = context.config
target_metadata = Base.metadata


def _configure_and_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=get_settings().postgres_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(get_settings().postgres_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_configure_and_run)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
    else:
        asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
