"""Live-ClickHouse semantics for the virtual ``time:`` fields.

The unit tests in ``test_time_fields.py`` pin the generated SQL's *shape*
against a fake client. This file proves the SQL actually means what the shape
claims when ClickHouse evaluates it — the three properties that fail silently
rather than loudly:

* extraction is UTC, not the server's timezone;
* sentinel (undated) rows contribute no bucket;
* a source's declared clock skew shifts which bucket its events land in.

Requires the dev compose stack (skipped when ClickHouse is unreachable), same
pattern as ``test_viz_timeseries_fused_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db._dt import NULL_TS_SENTINEL_ISO
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

CASE_ID = f"tc-timefields-{uuid.uuid4().hex[:8]}"
SRC_A = "src-tf-a"
SRC_SKEWED = "src-tf-skewed"


def _event(i: int, source_id: str, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=source_id,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-timefields",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:timefields",
        attributes=attrs,
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0

    def add(source_id: str, ts: str, attrs: dict[str, str]) -> None:
        nonlocal i
        events.append(_event(i, source_id, ts, attrs))
        i += 1

    # 2026-07-20 is a Monday (ISO day 1); 2026-07-25 a Saturday (day 6).
    # NL attacks at 02:00, US at 23:00 — two well-separated hour buckets.
    for n in range(5):
        add(SRC_A, f"2026-07-20T02:{n:02d}:00+00:00", {"country": "NL"})
    for n in range(3):
        add(SRC_A, f"2026-07-20T23:{n:02d}:00+00:00", {"country": "US"})
    add(SRC_A, "2026-07-25T09:00:00+00:00", {"country": "NL"})
    # Undated row: must contribute to no hour bucket at all.
    add(SRC_A, NULL_TS_SENTINEL_ISO, {"country": "NL"})
    # A single row on a source whose clock ran 3h slow — corrected, its 22:30
    # becomes 01:30 the next day, i.e. hour "01" not "22".
    add(SRC_SKEWED, "2026-07-20T22:30:00+00:00", {"country": "DE"})
    return events


@pytest.fixture(scope="module")
def service():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    store.insert_events(_fixture_events())
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC_A)
    store.delete_source_events(CASE_ID, SRC_SKEWED)


def _query(**kwargs) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC_A], **kwargs)


def _counts(result: dict) -> dict[str, int]:
    return {v["value"]: v["count"] for v in result["values"]}


def test_hour_of_day_groups_into_utc_hours(service: EventQueryService) -> None:
    counts = _counts(service.field_terms(_query(), "time:hour_of_day", limit=50))
    assert counts == {"02": 5, "23": 3, "09": 1}


def test_undated_events_land_in_no_bucket(service: EventQueryService) -> None:
    """The sentinel row is one of the 10 events on SRC_A but must not show up
    as an hour — without the ``''`` guard it would appear as hour 23 (the
    year-2299 sentinel's own hour) and inflate a real bucket."""
    result = service.field_terms(_query(), "time:hour_of_day", limit=50)
    assert sum(_counts(result).values()) == 9
    assert "" not in _counts(result)


def test_day_of_week_is_iso_and_matches_the_punchcard(service: EventQueryService) -> None:
    counts = _counts(service.field_terms(_query(), "time:day_of_week", limit=50))
    # Mon=1 (8 events on 2026-07-20), Sat=6 (1 event on 2026-07-25).
    assert counts == {"1": 8, "6": 1}
    punch = service.time_punchcard(_query())
    assert {c["dow"] for c in punch["cells"]} == {1, 6}


def test_month_and_date_parts(service: EventQueryService) -> None:
    assert _counts(service.field_terms(_query(), "time:month", limit=50)) == {"07": 9}
    assert _counts(service.field_terms(_query(), "time:year_month", limit=50)) == {"2026-07": 9}
    assert _counts(service.field_terms(_query(), "time:date", limit=50)) == {
        "2026-07-20": 8,
        "2026-07-25": 1,
    }


def test_clock_skew_shifts_which_hour_a_source_lands_in(service: EventQueryService) -> None:
    """A declared offset must move the *bucket*, not just the displayed time —
    otherwise a skewed host's night-time activity hides in the wrong hour."""
    raw = EventQuery(case_id=CASE_ID, source_ids=[SRC_SKEWED])
    assert _counts(service.field_terms(raw, "time:hour_of_day", limit=50)) == {"22": 1}

    corrected = EventQuery(
        case_id=CASE_ID,
        source_ids=[SRC_SKEWED],
        source_offsets={SRC_SKEWED: 3 * 3600},
    )
    assert _counts(service.field_terms(corrected, "time:hour_of_day", limit=50)) == {"01": 1}
    # ...and the corrected event has rolled over into the next day, Tuesday.
    assert _counts(service.field_terms(corrected, "time:day_of_week", limit=50)) == {"2": 1}


def test_filtering_by_hour_selects_the_right_events(service: EventQueryService) -> None:
    """ "Show me only 02:00-03:00 activity" — the forensic filter that comes
    free from resolving time fields at the shared column resolver."""
    counts = _counts(
        service.field_terms(
            _query(field_filters={"time:hour_of_day": ["02"]}), "attr:country", limit=50
        )
    )
    assert counts == {"NL": 5}


def test_pivot_country_by_hour_renders_the_whole_day(service: EventQueryService) -> None:
    """The chart this whole phase exists for: which country attacks when."""
    result = service.field_pivot(_query(), "attr:country", "time:hour_of_day", limit_x=10)
    assert result["y_values"] == [f"{h:02d}" for h in range(24)]
    cells = {(c["x"], c["y"]): c["count"] for c in result["cells"]}
    assert cells[("NL", "02")] == 5
    assert cells[("US", "23")] == 3
    # Empty hours are absent from the sparse cell list, not folded into an
    # "Other" bucket — the frontend zero-fills against the complete axis.
    assert ("NL", "03") not in cells
    assert not [c for c in result["cells"] if c["y"] == ""]
