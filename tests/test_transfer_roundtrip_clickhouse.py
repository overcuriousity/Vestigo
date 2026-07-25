"""Live-ClickHouse round-trip: ingest → export → import → same events.

Skipped (visibly, via the clickhouse marker) when the dev compose stack is
absent. Uses the SQLite `store` fixture for Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from vestigo.db import postgres as pg
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.models.event import Event
from vestigo.transfer.exporter import export_case
from vestigo.transfer.importer import import_case

pytestmark = pytest.mark.clickhouse

CASE_ID = f"rt-case-{uuid.uuid4().hex[:8]}"
SOURCE_ID = f"rt-src-{uuid.uuid4().hex[:8]}"
FILE_HASH = "ab" * 32


@pytest.fixture(autouse=True)
async def _schema(store):
    """The conftest store is a bare SQLite file; bring it to Alembic head."""
    await store.init_schema()


@pytest.fixture(scope="module")
def ch_store():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    yield store
    store.delete_source_events(CASE_ID, SOURCE_ID)


def _event(i: int) -> Event:
    ts = datetime(2026, 7, 24, 9, 0, i, tzinfo=UTC)
    # event_id is init=False on the model: __post_init__ derives it from these
    # exact inputs (file_hash as source_identity), so identity is deterministic.
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("demo.log"),
        byte_offset=i * 100,
        line_number=i,
        content_hash=f"{i:064x}",
        file_hash=FILE_HASH,
        parser_name="demo",
        parser_version="1",
        raw_line=f"round-trip raw line {i}",
        ingest_time=datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC),
        message=f"round-trip line {i}",
        timestamp=ts.isoformat(),
        timestamp_desc="parsed",
        artifact="x",
        artifact_long="y",
        display_name="d",
        tags=["rt"],
        attributes={"idx": str(i)},
        # Embedded upstream: the import must strip these, since the vectors
        # they point at live in a Qdrant collection that does not travel.
        embedding_model="bge-small",
        embedding_config_hash="cd" * 32,
    )


async def _pg_case(store) -> None:
    async with store.session_factory() as session:
        session.add(pg.Case(id=CASE_ID, name="Round Trip", owner_id=None, team_id=None))
        session.add(
            pg.Source(
                id=SOURCE_ID, case_id=CASE_ID, name="src", file_hash=FILE_HASH, status="ready"
            )
        )
        await session.commit()


def _comparable(rows):
    return sorted(
        (
            str(r["event_id"]),
            r["message"],
            r["content_hash"].decode().rstrip("\x00")
            if isinstance(r["content_hash"], bytes)
            else r["content_hash"],
            r["byte_offset"],
        )
        for r in rows
    )


async def test_roundtrip_events_identical(store, ch_store, tmp_path):
    await _pg_case(store)
    ch_store.insert_events([_event(i) for i in range(3)])
    imported_ch = ClickHouseStore()  # same server; captures nothing — real CH
    try:
        archive = await export_case(
            store,
            lambda: ch_store,
            CASE_ID,
            include_blobs=False,
            exported_by="test",
            dest_dir=tmp_path,
        )
        assert archive.counts["events"] == 3

        bob = pg.User(
            id=f"rt-user-{uuid.uuid4().hex[:8]}",
            username=f"rt-{uuid.uuid4().hex[:6]}",
            is_admin=False,
            is_active=True,
        )
        async with store.session_factory() as session:
            session.add(bob)
            await session.commit()

        result = await import_case(store, lambda: imported_ch, archive.path, owner=bob)
        assert result.counts["events"] == 3

        async with store.session_factory() as session:
            new_src = (
                await session.execute(select(pg.Source).where(pg.Source.case_id == result.case_id))
            ).scalar_one()

        original = [
            r for batch in ch_store.iter_source_events(CASE_ID, SOURCE_ID, 1000) for r in batch
        ]
        restored = [
            r
            for batch in imported_ch.iter_source_events(result.case_id, new_src.id, 1000)
            for r in batch
        ]
        assert _comparable(restored) == _comparable(original)
        # Qdrant vectors don't travel, so a restored event must not claim one —
        # against real ClickHouse column types, not the fake's dicts.
        assert all(r["embedding_model"] == "bge-small" for r in original)
        assert all(not r["embedding_model"] for r in restored)
        assert all(
            not (
                r["embedding_config_hash"].decode().rstrip("\x00")
                if isinstance(r["embedding_config_hash"], bytes)
                else r["embedding_config_hash"]
            ).strip("\x00")
            for r in restored
        )
    finally:
        # Clean up the imported case's events as well (partition drop).
        try:
            async with store.session_factory() as session:
                rows = (
                    (await session.execute(select(pg.Source).where(pg.Source.case_id != CASE_ID)))
                    .scalars()
                    .all()
                )
                for r in rows:
                    imported_ch.delete_source_events(r.case_id, r.id)
        except Exception:
            pass
