"""Live-ClickHouse semantics for the interval lanes figure (`field_lanes`).

One source, three hosts; `attr:kind` names the start (logon) and end (logoff)
events for the next_end pairing. The cases §10 of the spec names: an unpaired
start, an orphan end, two starts before one end, an end before any start, the
lane cap — plus an undated event and the row cap. Corpus pattern as
`test_change_clickhouse.py`.
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

CASE_ID = f"tc-lanes-{uuid.uuid4().hex[:8]}"
SRC = "src-lanes"


def _event(i: int, ts: str | None, host: str, kind: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-lanes",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"{kind} on {host}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:lanes",
        attributes={"host": host, "kind": kind},
    )


def _t(hour: int, minute: int = 0) -> str:
    return f"2026-07-20T{hour:02d}:{minute:02d}:00+00:00"


# h1: logon 09, logoff 10, logon 11 (never ends), other 12         → 1 closed, 1 open
# h2: logoff 08 (orphan), logon 09, logon 10, logoff 11, other 13  → LIFO: (10,11) closed, 09 open, 1 orphan
# h3: other 09, other 14 only                                       → first_last only
# plus one undated logon on h1.
_ROWS: list[tuple[str | None, str, str]] = [
    (_t(9), "h1", "logon"),
    (_t(10), "h1", "logoff"),
    (_t(11), "h1", "logon"),
    (_t(12), "h1", "other"),
    (_t(8), "h2", "logoff"),
    (_t(9), "h2", "logon"),
    (_t(10), "h2", "logon"),
    (_t(11), "h2", "logoff"),
    (_t(13), "h2", "other"),
    (_t(9), "h3", "other"),
    (_t(14), "h3", "other"),
    (None, "h1", "logon"),
]


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events([_event(i, ts, h, k) for i, (ts, h, k) in enumerate(_ROWS)])
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


def _q(**kw) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC], **kw)


def _kind(value: str) -> EventQuery:
    return _q(field_filters={"attr:kind": [value]})


def _by_key(result: dict) -> dict[str, dict]:
    return {lane["key"]: lane for lane in result["lanes"]}


def test_first_last_draws_one_bar_per_lane_and_ranks_by_count(service):
    result = service.field_lanes(_q(), "attr:host", pairing="first_last", limit_y=10)
    assert result["kind"] == "lanes" and result["pairing"] == "first_last"
    assert [lane["key"] for lane in result["lanes"]] == ["h2", "h1", "h3"]  # 5, 4, 2 dated
    lanes = _by_key(result)
    (h3,) = lanes["h3"]["intervals"]
    assert (h3["start"], h3["end"]) == (_t(9), _t(14))
    assert h3["end_event_id"] is not None and h3["start_event_id"] != h3["end_event_id"]
    assert result["lanes_total"] == 3 and result["lane_cap_hit"] is False
    assert result["undated"] == 1
    assert result["starts"] == 0 and result["unpaired_starts"] == 0 and result["orphan_ends"] == 0
    assert result["slice_start"] == _t(8) and result["slice_end"] == _t(14)


def test_next_end_pairs_lifo_leaves_starts_open_and_counts_orphans(service):
    result = service.field_lanes(
        _q(), "attr:host", pairing="next_end", start=_kind("logon"), end=_kind("logoff"), limit_y=10
    )
    assert result["pairing"] == "next_end"
    assert [lane["key"] for lane in result["lanes"]] == [
        "h2",
        "h1",
    ]  # 4 vs 3 start/end rows; h3 has none
    lanes = _by_key(result)
    h1 = lanes["h1"]["intervals"]
    assert [(i["start"], i["end"]) for i in h1] == [(_t(9), _t(10)), (_t(11), None)]
    assert h1[1]["end_event_id"] is None
    h2 = lanes["h2"]["intervals"]
    # Two starts before one end: the end closes the most recent open start.
    assert [(i["start"], i["end"]) for i in h2] == [(_t(9), None), (_t(10), _t(11))]
    assert result["starts"] == 4 and result["ends"] == 3
    assert result["unpaired_starts"] == 2 and result["orphan_ends"] == 1
    assert result["undated"] == 1
    assert result["rows_truncated"] is False and result["rows_paired"] == 7
    assert result["lanes_total"] == 2


def test_start_and_end_filters_are_anded_with_the_primary(service):
    result = service.field_lanes(
        _q(field_filters={"attr:host": ["h1"]}),
        "attr:host",
        pairing="next_end",
        start=_kind("logon"),
        end=_kind("logoff"),
    )
    assert [lane["key"] for lane in result["lanes"]] == ["h1"]
    assert result["starts"] == 2 and result["ends"] == 1 and result["orphan_ends"] == 0


def test_lane_cap_keeps_the_busiest_lanes_and_discloses_the_rest(service):
    result = service.field_lanes(_q(), "attr:host", pairing="first_last", limit_y=2)
    assert [lane["key"] for lane in result["lanes"]] == ["h2", "h1"]
    assert result["lane_cap"] == 2 and result["lane_cap_hit"] is True
    assert result["lanes_total"] == 3 and result["other_lanes"] == 1


def test_row_cap_pairs_the_earliest_rows_and_discloses_truncation(service):
    result = service.field_lanes(
        _q(), "attr:host", pairing="next_end", start=_kind("logon"), end=_kind("logoff"), rows_cap=3
    )
    # The three earliest rows by time: h2 logoff 08 (orphan), h1 logon 09, h2 logon 09.
    assert result["rows_truncated"] is True and result["rows_paired"] == 3
    assert result["orphan_ends"] == 1 and result["unpaired_starts"] == 2
    assert result["starts"] == 4 and result["ends"] == 3  # the whole, before the cap


def test_explicit_window_pins_the_slice_edges(service):
    window = _q(
        start=datetime(2026, 7, 20, 8, 30, tzinfo=UTC), end=datetime(2026, 7, 20, 12, tzinfo=UTC)
    )
    result = service.field_lanes(window, "attr:host", pairing="first_last")
    assert result["slice_start"] == "2026-07-20T08:30:00+00:00"
    assert result["slice_end"] == "2026-07-20T12:00:00+00:00"
    assert _by_key(result)["h3"]["intervals"][0]["end"] == _t(9)  # 14:00 is outside the window


def test_nothing_matching_is_an_empty_figure(service):
    empty = service.field_lanes(_q(field_filters={"attr:host": ["nope"]}), "attr:host")
    assert empty["lanes"] == [] and empty["lanes_total"] == 0 and empty["other_lanes"] == 0
    assert empty["slice_start"] is None and empty["slice_end"] is None
    assert empty["undated"] == 0
