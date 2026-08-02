"""Migration 0024 adds ``timelines.recommended_columns`` (issue #213).

A pure additive column, so the interesting part is the round trip: a timeline
that predates the migration must survive the upgrade with a null suggestion
(the explorer's built-in defaults), and the downgrade must leave the row
intact.
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
async def engine(tmp_path):
    store = pg.PostgresStore(url=f"sqlite+aiosqlite:///{tmp_path / 'mig24.db'}")
    yield store.engine
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_preexisting_timeline_upgrades_to_a_null_suggestion(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0023"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default) "
                "VALUES ('t1', 'c1', 'All sources', 1)"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0024"))

        row = (
            await conn.execute(
                text("SELECT name, recommended_columns FROM timelines WHERE id = 't1'")
            )
        ).one()

    assert row.name == "All sources"
    assert row.recommended_columns is None


@pytest.mark.asyncio
async def test_downgrade_drops_the_column_and_keeps_the_row(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0024"))
        await conn.execute(
            text(
                "INSERT INTO timelines (id, case_id, name, is_default, recommended_columns) "
                "VALUES ('t1', 'c1', 'All sources', 1, '{\"status\": \"ok\"}')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "downgrade", "0023"))

        columns = {r[1] for r in (await conn.execute(text("PRAGMA table_info(timelines)"))).all()}
        names = [r[0] for r in (await conn.execute(text("SELECT name FROM timelines"))).all()]

    assert "recommended_columns" not in columns
    assert names == ["All sources"]
