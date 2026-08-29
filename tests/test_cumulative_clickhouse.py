"""Live-ClickHouse semantics for the cumulative step (`cumulative`).

Three quantities over one bucketed scan. The one that matters most is
`distinct`: the running count of values seen so far must come from merged
per-bucket states, never from adding per-bucket distinct counts — a value
seen in two buckets is one value. Corpus pattern as `test_marks_clickhouse.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-cum-{uuid.uuid4().hex[:8]}"
SRC = "src-cum"


def _event(i: int, ts: str | None, user: str, size: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-cum",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:cum",
        attributes={"user": user, "size": size},
    )


# Four hours, one event per 30 minutes, plus one undated. `user` repeats
# across buckets (alice in hours 0, 1 and 3); `size` has one non-numeric value.
_ROWS: list[tuple[str | None, str, str]] = [
    ("2026-07-20T00:00:00+00:00", "alice", "10"),
    ("2026-07-20T00:30:00+00:00", "bob", "20"),
    ("2026-07-20T01:00:00+00:00", "alice", "5"),
    ("2026-07-20T01:30:00+00:00", "carol", "n/a"),
    ("2026-07-20T02:00:00+00:00", "", "1"),
    ("2026-07-20T03:00:00+00:00", "alice", "4"),
    ("2026-07-20T03:59:59+00:00", "dave", "0"),
    (None, "erin", "99"),  # undated: never in a bucket
]


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events([_event(i, ts, u, s) for i, (ts, u, s) in enumerate(_ROWS)])
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


_T0 = datetime(2026, 7, 20, 0, tzinfo=UTC)
_T4 = datetime(2026, 7, 20, 4, tzinfo=UTC)


def _query(**kw) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC], **kw)


def test_events_quantity_is_a_running_count_zero_filled(service):
    # Explicit 4 buckets over [00:00, 04:00] → 1-hour buckets.
    result = service.cumulative(
        _query(start=_T0, end=_T4),
        quantity="events",
        buckets=4,
    )
    assert result["kind"] == "cumulative" and result["quantity"] == "events"
    assert result["field"] is None and result["unparsed"] == 0
    assert result["events"] == 7
    assert [b["delta"] for b in result["buckets"]] == [2, 2, 1, 2, 0]
    assert [b["value"] for b in result["buckets"]] == [2, 4, 5, 7, 7]
    assert result["total"] == 7 and result["interval_seconds"] == 3600


def test_sum_quantity_skips_non_numeric_values_and_discloses_them(service):
    result = service.cumulative(
        _query(start=_T0, end=_T4),
        field="attr:size",
        quantity="sum",
        buckets=4,
    )
    assert [b["value"] for b in result["buckets"]] == [30.0, 35.0, 36.0, 40.0, 40.0]
    assert result["total"] == 40.0
    assert result["unparsed"] == 1  # "n/a"


def test_distinct_quantity_merges_states_rather_than_summing_per_bucket_distincts(service):
    result = service.cumulative(
        _query(start=_T0, end=_T4),
        field="attr:user",
        quantity="distinct",
        buckets=4,
    )
    # alice, bob | alice, carol | (empty) | alice, dave → 2, 3, 3, 4 — a sum of
    # per-bucket distincts would say 2, 4, 4, 6.
    assert [b["value"] for b in result["buckets"]] == [2, 3, 3, 4, 4]
    assert [b["delta"] for b in result["buckets"]] == [2, 1, 0, 1, 0]
    assert result["unparsed"] == 1  # the empty user
    assert result["total"] == 4


def test_derived_range_buckets_and_empty_slices(service):
    result = service.cumulative(_query(q="event"), quantity="events", buckets=8)
    assert result["min"].startswith("2026-07-20T00:00:00")
    assert result["max"].startswith("2026-07-20T03:59:59")
    assert result["buckets"][-1]["value"] == 7
    # Zero-filled: every aligned bucket between min and max is present, in order.
    starts = [b["start"] for b in result["buckets"]]
    assert starts == sorted(starts) and len(starts) >= 8
    empty = service.cumulative(_query(q="no-such-token-anywhere"), quantity="events")
    assert empty == {
        **empty,
        "buckets": [],
        "total": 0,
        "events": 0,
        "interval_seconds": 0,
        "min": None,
        "max": None,
    }
