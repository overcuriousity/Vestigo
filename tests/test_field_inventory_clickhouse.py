"""Live-ClickHouse verification of the field value inventory aggregation (#295).

``EventQueryService.iter_field_inventory`` answers "which distinct values did
this field take, and when was each first and last seen" in one streamed
GROUP BY. The properties pinned here are the ones an analyst's file is only
trustworthy if they hold: the times are the *offset-corrected* min/max over
real timestamps (never the year-2299 no-timestamp sentinel), empty values are
skipped, the Explorer's filters scope the scan, every ordering is honoured,
and the distinct-value pre-flight count matches what the stream yields.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-inventory-{uuid.uuid4().hex[:8]}"
SRC = "src-inventory"
SRC_B = "src-inventory-b"

# (attr value, timestamp) — `alpha` spans the widest window and is the most
# frequent, `beta` sits inside it, `gamma` appears once. The empty-string and
# missing-attribute rows must not become inventory rows at all.
_ROWS: list[tuple[str | None, str | None]] = [
    ("alpha", "2026-03-01T00:00:00+00:00"),
    ("alpha", "2026-03-02T00:00:00+00:00"),
    ("alpha", "2026-03-05T12:00:00+00:00"),
    ("beta", "2026-03-03T06:00:00+00:00"),
    ("beta", "2026-03-04T06:00:00+00:00"),
    ("gamma", "2026-03-02T18:00:00+00:00"),
    ("", "2026-03-02T18:00:00+00:00"),
    (None, "2026-03-02T18:00:00+00:00"),
    # `delta` only ever appears on events with no timestamp: it must still be
    # inventoried (with a count) but must not report a year-2299 sentinel.
    ("delta", None),
    ("delta", None),
]


def _event(i: int, value: str | None, ts: str | None, source_id: str = SRC) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=source_id,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="a" * 64,
        parser_name="test-inventory",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i} {value}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:inventory",
        attributes={} if value is None else {"src_ip": value},
    )


def _fixture_events() -> list[Event]:
    events = [_event(i, value, ts) for i, (value, ts) in enumerate(_ROWS)]
    # A second source, so a per-source clock-skew offset has something to shift.
    events.append(_event(100, "epsilon", "2026-03-06T00:00:00+00:00", source_id=SRC_B))
    return events


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events(_fixture_events())
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)
    store.delete_source_events(CASE_ID, SRC_B)


def _query(**kwargs) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC], **kwargs)


def _inventory(service, query, **kwargs) -> dict[str, dict]:
    return {
        row["value"]: row for row in service.iter_field_inventory(query, "attr:src_ip", **kwargs)
    }


def test_inventory_reports_count_and_first_last_seen(service):
    rows = _inventory(service, _query())

    assert set(rows) == {"alpha", "beta", "gamma", "delta"}
    assert rows["alpha"]["count"] == 3
    assert rows["alpha"]["first_seen"] == "2026-03-01T00:00:00+00:00"
    assert rows["alpha"]["last_seen"] == "2026-03-05T12:00:00+00:00"
    assert rows["gamma"]["count"] == 1
    assert rows["gamma"]["first_seen"] == rows["gamma"]["last_seen"]


def test_inventory_skips_empty_values(service):
    """Neither an empty attribute nor a missing one is a value worth listing."""
    assert "" not in _inventory(service, _query())


def test_inventory_reports_no_time_for_sentinel_only_values(service):
    """A value seen only on no-timestamp events keeps its count but reports no
    times — the year-2299 storage sentinel must never reach the file."""
    delta = _inventory(service, _query())["delta"]

    assert delta["count"] == 2
    assert delta["first_seen"] is None
    assert delta["last_seen"] is None


def test_inventory_honours_filters(service):
    """The inventory is computed inside the currently-filtered view — an
    inventory that ignored the filters would be a forensic footgun."""
    rows = _inventory(service, _query(start=datetime.fromisoformat("2026-03-03T00:00:00+00:00")))

    assert set(rows) == {"alpha", "beta"}
    assert rows["alpha"]["count"] == 1
    assert rows["alpha"]["first_seen"] == "2026-03-05T12:00:00+00:00"


def test_inventory_applies_source_time_offsets(service):
    """First/last seen are the offset-corrected times, matching the events export."""
    query = EventQuery(
        case_id=CASE_ID,
        source_ids=[SRC, SRC_B],
        source_offsets={SRC_B: 3600},
    )
    rows = _inventory(service, query)

    assert rows["epsilon"]["first_seen"] == "2026-03-06T01:00:00+00:00"
    # An unshifted source stays where it was.
    assert rows["alpha"]["first_seen"] == "2026-03-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("count_desc", ["alpha", "beta", "delta", "gamma"]),
        ("count_asc", ["gamma", "beta", "delta", "alpha"]),
        ("value_asc", ["alpha", "beta", "delta", "gamma"]),
        ("value_desc", ["gamma", "delta", "beta", "alpha"]),
        ("first_seen_asc", ["alpha", "gamma", "beta", "delta"]),
        ("last_seen_desc", ["alpha", "beta", "gamma", "delta"]),
    ],
)
def test_inventory_ordering(service, order, expected):
    """Ties break on the value so every ordering is total (a stream the analyst
    re-runs must not reshuffle), and a value with no known time sorts last in
    either direction rather than to the year 2299 or to the top of a
    most-recent-first file."""
    values = [
        row["value"]
        for row in service.iter_field_inventory(_query(), "attr:src_ip", order_by=order)
    ]

    assert values == expected


def test_inventory_rejects_unknown_ordering(service):
    with pytest.raises(ValueError):
        list(service.iter_field_inventory(_query(), "attr:src_ip", order_by="count_sideways"))


def test_count_field_inventory_matches_the_stream(service):
    """The pre-flight count is what the export's completeness trailer proves
    against — it must count exactly the rows the stream yields."""
    query = _query()

    expected = service.count_field_inventory(query, "attr:src_ip")

    assert expected == len(list(service.iter_field_inventory(query, "attr:src_ip")))
    assert expected == 4


def test_inventory_streams_in_batches_smaller_than_the_result(service):
    """Blocks are streamed rather than materialized — a high-cardinality field
    must not be held in the app's memory in full."""
    rows = list(service.iter_field_inventory(_query(), "attr:src_ip", block_size=2))

    assert len(rows) == 4
