"""Live-ClickHouse semantics for mark resolution (`mark_instants`).

An `events` mark source is a filter; this is the aggregation that turns it
into instants: the earliest N dated events, with the undated ones counted
rather than drawn at the sentinel year. Corpus pattern as
`test_table_clickhouse.py`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-marks-{uuid.uuid4().hex[:8]}"
SRC = "src-marks"


def _event(i: int, ts: str | None, message: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-marks",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=message,
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:marks",
        attributes={"user": message.split()[-1]},
    )


_ROWS: list[tuple[str | None, str]] = [
    ("2026-07-20T05:00:00+00:00", "beacon alice"),
    ("2026-07-20T01:00:00+00:00", "beacon alice"),
    ("2026-07-20T03:00:00+00:00", "beacon alice"),
    ("2026-07-20T02:00:00+00:00", "logon bob"),
    ("2026-07-20T04:00:00+00:00", "beacon alice"),
    (None, "beacon alice"),  # undated: counted, never drawn
]


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events([_event(i, ts, msg) for i, (ts, msg) in enumerate(_ROWS)])
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


def _query(**kw) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC], **kw)


def test_instants_are_the_earliest_dated_events_in_time_order(service):
    result = service.mark_instants(_query(q="beacon"), limit=10)
    assert [i["at"][:19] for i in result["instants"]] == [
        "2026-07-20T01:00:00",
        "2026-07-20T03:00:00",
        "2026-07-20T04:00:00",
        "2026-07-20T05:00:00",
    ]
    assert all(i["source_id"] == SRC and i["event_id"] for i in result["instants"])
    assert result == {**result, "dated": 4, "undated": 1, "overflow": False}


def test_cap_keeps_the_earliest_and_reports_overflow(service):
    result = service.mark_instants(_query(q="beacon"), limit=2)
    assert [i["at"][:19] for i in result["instants"]] == [
        "2026-07-20T01:00:00",
        "2026-07-20T03:00:00",
    ]
    assert result["dated"] == 4 and result["overflow"] is True


def test_the_filter_is_the_events_view_filter(service):
    result = service.mark_instants(_query(q="logon"), limit=10)
    assert [i["at"][:19] for i in result["instants"]] == ["2026-07-20T02:00:00"]
    assert result["undated"] == 0
