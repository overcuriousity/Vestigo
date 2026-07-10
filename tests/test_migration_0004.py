"""Migration 0004 data-move: legacy allowlist entries, per-event `normal`
annotations and pinned system annotations become finding_dispositions rows
(see the migration docstring for the mapping)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

import tracesignal.db.postgres as pg


def _alembic(sync_conn: Any, verb: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(pg.__file__).parent / "migrations")
    )
    cfg.attributes["connection"] = sync_conn
    getattr(command, verb)(cfg, target)


@pytest_asyncio.fixture()
async def engine(tmp_path):
    store = pg.PostgresStore(url=f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    yield store.engine
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_moves_legacy_rows(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "0003"))
        await conn.execute(
            text(
                "INSERT INTO detector_allowlist (id, case_id, timeline_id, detector, field, value, note, created_by) "
                "VALUES ('al1', 'c1', 't1', 'value_novelty', 'attr:user', 'svc', 'note', 'u1')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO annotations (id, case_id, source_id, event_id, annotation_type, content, origin, pinned) "
                "VALUES ('an_norm', 'c1', 's1', 'e-norm', 'normal', 'normal operation', 'user', 0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO annotations (id, case_id, source_id, event_id, annotation_type, content, origin, pinned, detector) "
                "VALUES ('an_pin', 'c1', 's1', 'e-pin', 'anomaly', 'confirmed finding', 'system', 1, 'charset')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO annotations (id, case_id, source_id, event_id, annotation_type, content, origin, pinned) "
                "VALUES ('an_tag', 'c1', 's1', 'e-tag', 'tag', 'malware', 'user', 0)"
            )
        )

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: _alembic(c, "upgrade", "head"))

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT kind, detector, field, value, source_id, event_id, note "
                    "FROM finding_dispositions ORDER BY kind"
                )
            )
        ).fetchall()
        # Allowlist entry -> value-scoped normal (1:1 mapping).
        # Pinned anomaly -> confirmed with its detector.
        # normal annotation -> event-scoped normal with wildcard detector.
        assert len(rows) == 3
        as_set = {tuple(r) for r in rows}
        assert ("normal", "value_novelty", "attr:user", "svc", None, None, "note") in as_set
        assert ("normal", "*", None, None, "s1", "e-norm", "normal operation") in as_set
        assert ("confirmed", "charset", None, None, "s1", "e-pin", None) in as_set

        anns = (
            await conn.execute(
                text("SELECT id, annotation_type FROM annotations ORDER BY id")
            )
        ).fetchall()
        # normal annotation deleted; pinned anomaly + tag kept.
        assert [tuple(a) for a in anns] == [("an_pin", "anomaly"), ("an_tag", "tag")]

        # pinned column gone, detector_allowlist gone.
        cols = [
            r[1]
            for r in (await conn.execute(text("PRAGMA table_info(annotations)"))).fetchall()
        ]
        assert "pinned" not in cols
        tables = [
            r[0]
            for r in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).fetchall()
        ]
        assert "detector_allowlist" not in tables
        assert "finding_dispositions" in tables
