"""Live-ClickHouse semantics for derivations (bins, calendar part).

`test_derive.py` pins the SQL's shape; this proves it means what it claims:
bins land where the edges say, `log` gives `<= 0` its own bin, unparseable
values fall out of every bin, and a calendar part over an attribute agrees
with the `time:` field it reuses. Same corpus pattern as
`test_time_fields_clickhouse.py`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.derive import DeriveSpec
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-derive-{uuid.uuid4().hex[:8]}"
SRC = "src-derive"


def _event(i: int, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-derive",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:derive",
        attributes=attrs,
    )


# bytes: 0, 512, 2048, 4096, 8192, 100000, -5, "n/a"; logon_at: three parseable, one not.
_BYTES = ["0", "512", "2048", "4096", "8192", "100000", "-5", "n/a"]
_LOGON = ["2026-07-20T02:15:00Z", "2026-07-20T02:45:00Z", "2026-07-21 23:10:00", "yesterday"]


def _fixture_events() -> list[Event]:
    events = [
        _event(i, "2026-07-20T10:00:00+00:00", {"bytes": b, "host": "h1"})
        for i, b in enumerate(_BYTES)
    ]
    events += [
        _event(100 + i, "2026-07-20T10:00:00+00:00", {"logon_at": t, "host": "h2"})
        for i, t in enumerate(_LOGON)
    ]
    return events


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events(_fixture_events())
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


def _query() -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC])


def _counts(result: dict) -> dict[str, int]:
    return {v["value"]: v["count"] for v in result["values"]}


def test_custom_edges_are_open_ended_and_unparseable_values_fall_out(service) -> None:
    result = service.field_terms(
        _query(),
        "attr:bytes",
        50,
        derive=DeriveSpec(kind="bins", mode="custom", edges=[1024, 10240]),
    )
    assert result["derive"]["labels"] == ["< 1,024", "1,024 – 10,240", "≥ 10,240"]
    assert _counts(result) == {"< 1,024": 3, "1,024 – 10,240": 3, "≥ 10,240": 1}
    # "n/a" is in no bin, and is counted nowhere — the caption owes the analyst that number.
    assert result["total"] == 7
    assert result["derive"]["edges"] == [1024.0, 10240.0]


def test_log_bins_give_non_positive_values_their_own_bin(service) -> None:
    result = service.field_terms(
        _query(), "attr:bytes", 50, derive=DeriveSpec(kind="bins", mode="log", count=2)
    )
    labels = result["derive"]["labels"]
    assert labels[0] == "≤ 0"
    assert result["derive"]["negative_bin"] is True
    assert _counts(result)["≤ 0"] == 2  # 0 and -5
    # Edges span the *positive* range 512..100000, not from 0.
    assert result["derive"]["edges"][0] > 512


def test_width_bins_cover_the_whole_range_in_equal_steps(service) -> None:
    result = service.field_terms(
        _query(), "attr:bytes", 50, derive=DeriveSpec(kind="bins", mode="width", count=4)
    )
    # -5 .. 100000 in four equal steps; every parseable value lands in exactly one bin.
    assert sum(_counts(result).values()) == 7
    assert len(result["derive"]["labels"]) == 4


def test_time_part_over_an_attribute_agrees_with_the_time_field_spec(service) -> None:
    result = service.field_terms(
        _query(), "attr:logon_at", 50, derive=DeriveSpec(kind="time_part", part="hour")
    )
    assert _counts(result) == {"02": 2, "23": 1}
    assert result["derive"]["labels"] == [f"{h:02d}" for h in range(24)]
    assert result["derive"]["timezone"] == "UTC"
    # "yesterday" parses as nothing → contributes no bucket.
    assert result["total"] == 3


def test_timeseries_and_pivot_take_the_same_derivation(service) -> None:
    derive = DeriveSpec(kind="bins", mode="custom", edges=[1024])
    ts = service.field_value_timeseries(_query(), "attr:bytes", 10, 12, derive=derive)
    assert {s["value"] for s in ts["series"]} == {"< 1,024", "≥ 1,024"}
    assert ts["derive"]["labels"] == ["< 1,024", "≥ 1,024"]
    pivot = service.field_pivot(_query(), "attr:bytes", "attr:host", 10, 10, derive_x=derive)
    # A derived axis is a bounded domain: every label, in order, empty or not.
    assert pivot["x_values"] == ["< 1,024", "≥ 1,024"]
    assert pivot["derive_x"]["labels"] == ["< 1,024", "≥ 1,024"]


def test_compare_terms_counts_both_layers_on_the_primary_edges(service) -> None:
    derive = DeriveSpec(kind="bins", mode="custom", edges=[1024])
    result = service.compare_field_terms(_query(), _query(), "attr:bytes", 50, derive=derive)
    assert {v["value"] for v in result["values"]} == {"< 1,024", "≥ 1,024"}
    assert all(v["primary"] == v["comparison"] for v in result["values"])
    assert result["derive"]["labels"] == ["< 1,024", "≥ 1,024"]


_SENTINEL_SRC = "src-derive-undated"


@pytest.fixture(scope="module")
def sentinel_service():
    """A corpus of dated + undated events under its own source.

    Kept off the module corpus so the counts every other test asserts stay
    put; `_query()` scopes to `SRC`, so this source is invisible to them.
    """
    store = ClickHouseStore()
    store.init_schema()
    events = [
        _event(200, "2026-07-20T08:00:00+00:00", {"host": "h9"}),
        _event(201, "2026-07-20T08:30:00+00:00", {"host": "h9"}),
        _event(202, None, {"host": "h9"}),
        _event(203, None, {"host": "h9"}),
    ]
    for event in events:
        event.source_id = _SENTINEL_SRC
    store.insert_events(events)
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, _SENTINEL_SRC)


def _sentinel_query(**kw) -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[_SENTINEL_SRC], **kw)


def test_time_part_over_the_timestamp_column_blanks_undated_events(sentinel_service) -> None:
    """Deriving a calendar part from `timestamp` must answer as `time:` does (#332).

    An undated event carries the year-2299 null sentinel, which
    `parseDateTimeBestEffortOrNull` parses perfectly well — so every undated
    event used to pile into hour 23, and a derived chart disagreed with the
    equivalent `time:hour_of_day` chart. That is the exact disagreement the
    module exists to prevent.
    """
    derived = sentinel_service.field_terms(
        _sentinel_query(), "timestamp", 50, derive=DeriveSpec(kind="time_part", part="hour")
    )
    direct = sentinel_service.field_terms(_sentinel_query(), "time:hour_of_day", 50)
    assert _counts(derived) == {"08": 2}
    assert _counts(derived) == _counts(direct)
    assert "23" not in _counts(derived)


def test_time_part_over_the_timestamp_column_applies_the_clock_offset(sentinel_service) -> None:
    """`time_part_expr` read the raw column; a `time:` field reads the corrected one."""
    offsets = {_SENTINEL_SRC: 7200}
    derived = sentinel_service.field_terms(
        _sentinel_query(source_offsets=offsets),
        "timestamp",
        50,
        derive=DeriveSpec(kind="time_part", part="hour"),
    )
    direct = sentinel_service.field_terms(
        _sentinel_query(source_offsets=offsets), "time:hour_of_day", 50
    )
    assert _counts(derived) == {"10": 2}
    assert _counts(derived) == _counts(direct)


def test_time_part_over_an_attribute_still_ignores_the_events_sentinel(sentinel_service) -> None:
    """The guard is scoped to the timestamp column on purpose.

    The sentinel predicate names the `timestamp` column, so applying it to an
    attribute holding a timestamp would blank a value the attribute genuinely
    carries merely because the event around it is undated.
    """
    store = sentinel_service.store
    extra = _event(204, None, {"host": "h9", "logon_at": "2026-07-20T05:00:00Z"})
    extra.source_id = _SENTINEL_SRC
    store.insert_events([extra])
    result = sentinel_service.field_terms(
        _sentinel_query(), "attr:logon_at", 50, derive=DeriveSpec(kind="time_part", part="hour")
    )
    assert _counts(result) == {"05": 1}
