"""Migration 0029 adds ``timelines.field_overrides``.

A pure additive column, so the interesting part is the round trip: a timeline
that predates the migration must survive the upgrade with nothing declared — its
detectors picking their own fields exactly as before, which is the only safe
default for a column that decides what an analyst is *not* shown — and the
downgrade must leave the row intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

import vestigo.db.postgres as pg


def _alembic(sync_conn: Any, verb: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(pg.__file__).parent / "migrations"))
    cfg.attributes["connection"] = sync_conn
    getattr(command, verb)(cfg, target)


@pytest_asyncio.fixture()
async def engine(blank_pg_database):
    store = pg.PostgresStore(url=blank_pg_database)
    yield store.engine
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_preexisting_timeline_upgrades_with_nothing_declared(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0028"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default) "
                "VALUES ('t1', 'c1', 'All sources', true)"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0029"))

        row = (
            await conn.execute(text("SELECT name, field_overrides FROM timelines WHERE id = 't1'"))
        ).one()

    assert row.name == "All sources"
    assert row.field_overrides is None


@pytest.mark.asyncio
async def test_downgrade_drops_the_column_and_keeps_the_row(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0029"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default, field_overrides) "
                "VALUES ('t1', 'c1', 'All sources', true, "
                '\'{"numeric_range": {"attr:status_code": false}}\')'
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "downgrade", "0028"))

        columns = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'timelines'"
                    )
                )
            ).all()
        }
        names = [r[0] for r in (await conn.execute(text("SELECT name FROM timelines"))).all()]

    assert "field_overrides" not in columns
    assert names == ["All sources"]
