"""Migration 0035 adds ``agent_proposals.result``.

A proposal decided before this revision has no recorded outcome, and the
upgrade must say exactly that — null, not a guessed ``applied: true``. The
cards read a null as "outcome not recorded" and fall back to their old
inference; a fabricated one would state, permanently and in the analyst's
transcript, that a write happened that nobody checked.
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
async def test_decided_rows_upgrade_with_no_recorded_outcome(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0034"))
        await conn.execute(
            text(
                "INSERT INTO agent_proposals "
                "(id, conversation_id, case_id, timeline_id, status, kind, rationale, events) "
                "VALUES ('p1', 'conv1', 'c1', 't1', 'confirmed', 'story', 'r', '[]')"
            )
        )
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0035"))

        row = (
            await conn.execute(text("SELECT status, result FROM agent_proposals WHERE id = 'p1'"))
        ).one()
        assert row.status == "confirmed"
        assert row.result is None

        # Downgrade drops it without touching the decision itself.
        await conn.run_sync(lambda c: _alembic(c, "downgrade", "0034"))
        columns = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_proposals'"
                    )
                )
            ).all()
        }
        assert "result" not in columns
        assert (
            await conn.execute(text("SELECT status FROM agent_proposals WHERE id = 'p1'"))
        ).scalar_one() == "confirmed"
