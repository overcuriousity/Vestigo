"""Live-ClickHouse round-trip test for the Arrow bulk insert path.

Validates that ``client.insert_arrow`` with ``EVENT_ARROW_SCHEMA`` (UUID and
FixedString columns travelling as strings, Map/Array columns, DateTime64
sentinel) lands correctly in the real ``events`` table. Requires the dev
compose stack (skipped when ClickHouse is unreachable), same pattern as
``test_field_mappings_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore, _events_to_record_batch
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-arrow-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-arrow"


def _event(i: int, timestamp: str | None) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-arrow",
        parser_version="1.0.0",
        raw_line=f"raw line {i}",
        message=f"event {i}",
        timestamp=timestamp,
        timestamp_desc="Test Time",
        artifact="test:arrow",
        tags=["alpha", "beta"] if i % 2 else [],
        attributes={"src_ip": f"10.0.0.{i}", "empty": ""},
    )


@pytest.fixture(scope="module")
def ch_store():
    store = ClickHouseStore()
    store.init_schema()
    yield store
    store.delete_source_events(CASE_ID, SOURCE_ID)


def test_arrow_round_trip(ch_store):
    events = [_event(1, "2026-01-01T10:00:00+00:00"), _event(2, None)]
    assert ch_store.insert_events_arrow(_events_to_record_batch(events)) == 2

    result = ch_store.client.query(
        f"SELECT event_id, byte_offset, content_hash, file_hash, timestamp, "
        f"tags, attributes FROM {ch_store.database}.events "
        f"WHERE case_id = {{c:String}} AND source_id = {{s:String}} ORDER BY byte_offset",
        parameters={"c": CASE_ID, "s": SOURCE_ID},
    )
    rows = result.result_rows
    assert len(rows) == 2
    for row, event in zip(rows, events, strict=True):
        event_id, byte_offset, chash, fhash, ts, tags, attributes = row
        assert str(event_id) == str(event.event_id)
        assert byte_offset == event.byte_offset
        # FixedString comes back as bytes.
        raw = chash.decode() if isinstance(chash, bytes) else str(chash)
        assert raw == event.content_hash
        fraw = fhash.decode() if isinstance(fhash, bytes) else str(fhash)
        assert fraw == "f" * 64
        assert tags == event.tags
        # Empty attribute values are stripped by to_clickhouse_row.
        assert attributes == {"src_ip": event.attributes["src_ip"]}
    # Event 1 has a real timestamp; event 2 stores the year-2299 sentinel.
    assert rows[0][4].year == 2026
    assert rows[1][4].year == 2299
