"""Migration 0034 adds ``timelines.detectors`` and drops ``muted_methods``.

Every pre-existing timeline must come through the upgrade with *no* detector
configured — the new default is that nothing runs unprompted, and a column
that decides what an analyst is shown may not be guessed. The downgrade
restores ``muted_methods`` empty: the mute list only ever subtracted from a
sweep that no longer exists, so there is nothing to translate back.
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


async def _columns(conn) -> set[str]:
    rows = await conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'timelines'")
    )
    return {r[0] for r in rows.all()}


@pytest_asyncio.fixture()
async def engine(blank_pg_database):
    store = pg.PostgresStore(url=blank_pg_database)
    yield store.engine
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_preexisting_timeline_upgrades_with_no_detectors(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0033"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default, muted_methods) "
                "VALUES ('t1', 'c1', 'All sources', true, '[\"entropy\"]')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0034"))
        row = (
            await conn.execute(text("SELECT name, detectors FROM timelines WHERE id = 't1'"))
        ).one()
        columns = await _columns(conn)

    assert row.name == "All sources"
    assert row.detectors is None
    assert "muted_methods" not in columns


@pytest.mark.asyncio
async def test_downgrade_restores_muted_methods_empty_and_keeps_the_row(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0034"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default, detectors) "
                "VALUES ('t1', 'c1', 'All sources', true, "
                '\'[{"method": "entropy", "params": {}, "frame": "self", '
                '"baseline_id": null, "added_by": null, '
                '"added_at": "2026-09-03T00:00:00+00:00"}]\')'
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "downgrade", "0033"))
        columns = await _columns(conn)
        row = (
            await conn.execute(text("SELECT name, muted_methods FROM timelines WHERE id = 't1'"))
        ).one()

    assert "detectors" not in columns
    assert row.name == "All sources"
    assert row.muted_methods is None
