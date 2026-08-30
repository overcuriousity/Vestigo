"""Live-ClickHouse semantics for the calendar heatmap (`calendar`).

Day boundaries are UTC — an event at 23:30 UTC belongs to that day even if a
viewer's clock says tomorrow — and the figure keeps the latest `max_weeks`
ISO weeks, disclosing the earlier events it did not draw. Corpus pattern as
`test_marks_clickhouse.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-cal-{uuid.uuid4().hex[:8]}"
SRC = "src-cal"


def _event(i: int, ts: str | None, user: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-cal",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:cal",
        attributes={"user": user},
    )


# 2026-07-20 is a Monday. Two events that day, one at 23:30 UTC on the 21st,
# one with an empty user on the 22nd, one 60 weeks earlier, one undated.
_ROWS: list[tuple[str | None, str]] = [
    ("2026-07-20T09:00:00+00:00", "alice"),
    ("2026-07-20T17:00:00+00:00", "bob"),
    ("2026-07-21T23:30:00+00:00", "alice"),
    ("2026-07-22T12:00:00+00:00", ""),
    ((datetime(2026, 7, 20, tzinfo=UTC) - timedelta(weeks=60)).isoformat(), "old"),
    (None, "erin"),
]

_WEEK_START = datetime(2026, 7, 20, tzinfo=UTC)
_WEEK_END = datetime(2026, 7, 27, tzinfo=UTC)


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events([_event(i, ts, u) for i, (ts, u) in enumerate(_ROWS)])
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


def _query(**kw) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC], **kw)


def test_days_are_utc_and_sparse(service):
    result = service.calendar(_query(start=_WEEK_START, end=_WEEK_END))
    assert result["kind"] == "calendar" and result["timezone"] == "UTC"
    assert result["days"] == [
        {"date": "2026-07-20", "count": 2},
        {"date": "2026-07-21", "count": 1},
        {"date": "2026-07-22", "count": 1},
    ]
    assert result["start"] == "2026-07-20" and result["end"] == "2026-07-22"
    assert result["total"] == 4 and result["max_count"] == 2
    assert result["weeks"] == 1 and result["weeks_total"] == 1
    assert result["truncated"] is False and result["dropped"] == 0
    assert result["field"] is None


def test_a_field_counts_only_events_with_a_value(service):
    result = service.calendar(_query(start=_WEEK_START, end=_WEEK_END), field="attr:user")
    assert [d["count"] for d in result["days"]] == [2, 1]  # the 22nd's empty user is not a value
    assert result["field"] == "attr:user" and result["total"] == 3


def test_the_week_cap_keeps_the_latest_weeks_and_discloses_the_rest(service):
    full = service.calendar(_query(q="event"))
    assert full["weeks_total"] == 61 and full["truncated"] is True
    assert full["weeks"] == 53 and full["dropped"] == 1
    assert full["days"][0]["date"] == "2026-07-20"  # the old event is before `start`
    assert full["start"] <= "2026-07-20" and full["end"] == "2026-07-22"
    widened = service.calendar(_query(q="event"), max_weeks=80)
    assert widened["truncated"] is False and widened["dropped"] == 0
    assert widened["days"][0]["count"] == 1 and widened["weeks"] == 61


def test_no_dated_events_is_an_empty_calendar(service):
    result = service.calendar(_query(q="no-such-token-anywhere"))
    assert result["days"] == [] and result["start"] is None and result["end"] is None
    assert result["weeks"] == 0 and result["weeks_total"] == 0 and result["total"] == 0
