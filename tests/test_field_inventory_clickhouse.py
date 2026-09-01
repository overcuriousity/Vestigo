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

import threading
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from vestigo.db import queries
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

# The second field, by row index, for the combination inventory. Deliberately
# ragged: row 3 carries an empty `user`, rows 4/8/9 carry none at all, and row 6
# carries a `user` while its `src_ip` is empty — a combination that must still be
# written, because only the *all*-empty one describes no event's values.
_USERS: dict[int, str] = {0: "alice", 1: "alice", 2: "bob", 3: "", 5: "alice", 6: "carol"}


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
        attributes={
            **({} if value is None else {"src_ip": value}),
            **({"user": _USERS[i]} if i in _USERS else {}),
        },
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
        row["values"][0]: row
        for row in service.iter_field_inventory(query, "attr:src_ip", **kwargs)
    }


def _combinations(service, query=None, **kwargs) -> dict[tuple[str, ...], dict]:
    """The two-field inventory, keyed by the combination it describes."""
    rows = service.iter_field_inventory(query or _query(), ["attr:src_ip", "attr:user"], **kwargs)
    return {tuple(row["values"]): row for row in rows}


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
        row["values"][0]
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


def test_inventory_decodes_fixed_string_hash_values(service):
    """A hash field must inventory as hex, not as a `bytes` repr.

    `content_hash`/`file_hash` are `FixedString(64)`, which clickhouse-connect
    hands back as NUL-padded `bytes`. Every other read path runs them through
    `decode_fixed_string_columns`; this one yields a bare cell, so it has to
    decode it itself — otherwise the CSV writer stringifies the value as
    `b'aaa…\\x00'` and the exported hash no longer equals the real SHA-256,
    which is the whole point of storing it.
    """
    rows = list(service.iter_field_inventory(_query(), "file_hash"))

    assert [row["values"][0] for row in rows] == ["a" * 64]
    assert rows[0]["count"] == len(_ROWS)

    content = {row["values"][0] for row in service.iter_field_inventory(_query(), "content_hash")}
    assert all(isinstance(value, str) for value in content)
    assert f"{0:064d}" in content


def test_count_matches_the_stream_for_a_fixed_string_field(service):
    """The pre-flight and the stream agree on a hash field too.

    The count is aggregated through a `GROUP BY` subquery rather than
    `uniqExact` (which cannot spill), so it is worth pinning that the swap did
    not change the answer on a column whose type is not `String`.
    """
    expected = service.count_field_inventory(_query(), "content_hash")

    assert expected == len(list(service.iter_field_inventory(_query(), "content_hash")))
    assert expected == len(_ROWS)


# ── Several fields: the inventory of a combination ──────────────────────────


def test_combination_inventory_counts_the_pair_not_the_values(service):
    """Each row is one distinct combination, and its count is the number of
    events carrying *that* combination — not either value's own total."""
    rows = _combinations(service)

    assert rows[("alpha", "alice")]["count"] == 2
    assert rows[("alpha", "bob")]["count"] == 1
    assert rows[("gamma", "alice")]["count"] == 1
    # `alpha` alone occurs three times; splitting it by `user` is the point.
    assert sum(r["count"] for k, r in rows.items() if k[0] == "alpha") == 3


def test_a_combination_survives_unless_every_part_is_empty(service):
    """An empty part is an empty cell, not a dropped row — the value it sits
    beside is still a value the analyst asked to see. Only the all-empty
    combination, which describes no event's values at all, is excluded."""
    rows = _combinations(service)

    assert rows[("beta", "")]["count"] == 2  # `user` empty on one row, absent on the other
    assert rows[("", "carol")]["count"] == 1  # the *first* field may be the empty one
    assert ("", "") not in rows


def test_combination_seen_range_covers_only_that_combination(service):
    """The first/last seen of a row are those of the events carrying the
    combination — a pair seen only on undated events still reports no time."""
    rows = _combinations(service)

    assert rows[("alpha", "alice")]["first_seen"] == "2026-03-01T00:00:00+00:00"
    assert rows[("alpha", "alice")]["last_seen"] == "2026-03-02T00:00:00+00:00"
    assert rows[("delta", "")]["first_seen"] is None


def test_combination_value_ordering_sorts_across_every_column(service):
    """`value_asc` is the tuple ordering, left to right — otherwise a file
    sorted "by value" would look shuffled inside each first-column group."""
    ordered = list(_combinations(service, order_by="value_asc"))

    assert ordered == [
        ("", "carol"),
        ("alpha", "alice"),
        ("alpha", "bob"),
        ("beta", ""),
        ("delta", ""),
        ("gamma", "alice"),
    ]


def test_count_field_inventory_matches_the_combination_stream(service):
    """The completeness trailer is proven against this number, so the two must
    agree on combinations exactly as they do on single values."""
    fields = ["attr:src_ip", "attr:user"]

    expected = service.count_field_inventory(_query(), fields)

    assert expected == len(list(service.iter_field_inventory(_query(), fields)))
    assert expected == 6


def test_combination_inventory_honours_filters(service):
    """The same scope rule as the single-field inventory."""
    rows = _combinations(service, _query(start=datetime.fromisoformat("2026-03-03T00:00:00+00:00")))

    assert set(rows) == {("alpha", "bob"), ("beta", "")}


@pytest.mark.parametrize("fields", [[], ["attr:src_ip", "attr:src_ip"]])
def test_invalid_field_selections_are_rejected(service, fields):
    with pytest.raises(ValueError):
        list(service.iter_field_inventory(_query(), fields))
    with pytest.raises(ValueError):
        service.count_field_inventory(_query(), fields)


def _free(sem: threading.BoundedSemaphore) -> bool:
    """Whether *sem* has a slot available right now (non-destructive)."""
    if sem.acquire(blocking=False):
        sem.release()
        return True
    return False


@pytest.fixture
def gates(service, monkeypatch):
    """One-slot stand-ins for both scan gates, bound where `queries` reads them.

    `db/_scan.py` is imported *by value* into `queries`, so patching the
    module's own attributes would not reach the running code.
    """
    detector = threading.BoundedSemaphore(1)
    export = threading.BoundedSemaphore(1)
    monkeypatch.setattr(queries, "HEAVY_SCAN_GATE", detector)
    monkeypatch.setattr(queries, "EXPORT_SCAN_GATE", export)
    return detector, export


def test_detector_slot_is_handed_back_when_rows_start_flowing(service, gates, monkeypatch):
    """The detector gate covers the aggregation, not the client-paced drain.

    A sorted aggregate cannot emit its first row until every group exists, so
    the first block proves the whole-corpus scan is over. Everything after it
    is paced by the analyst's browser, and a backgrounded download that kept a
    detector slot would starve every sweep on the box for as long as it sat
    there.
    """
    detector, export = gates
    observed: dict[str, bool] = {}
    real_blocks = service._select_row_blocks

    def spy(sql, parameters=None, **kwargs):
        observed["held_during_scan"] = not _free(detector)
        yield from real_blocks(sql, parameters=parameters, **kwargs)

    monkeypatch.setattr(service, "_select_row_blocks", spy)

    stream = service.iter_field_inventory(_query(), "attr:src_ip")
    assert _free(detector) and _free(export), "nothing is taken before iteration starts"

    next(stream)
    assert observed["held_during_scan"], "the aggregation runs inside the detector gate"
    assert _free(detector), "the detector slot is handed back at the first block"
    assert not _free(export), "the export gate is held for the drain"

    list(stream)
    assert _free(detector) and _free(export)


def test_abandoned_export_releases_both_gates(service, gates):
    """A download the analyst cancels must not wedge a slot.

    Closing the generator (what Starlette does when the client disconnects)
    unwinds the `finally`, and the drain must not leave the export gate — the
    one slot every other export queues behind — permanently taken.
    """
    detector, export = gates

    stream = service.iter_field_inventory(_query(), "attr:src_ip")
    next(stream)
    stream.close()

    assert _free(detector) and _free(export)


def test_empty_inventory_releases_the_detector_gate(service, gates):
    """A scan that yields no block at all still hands its slot back.

    The release is driven by the first block, so the no-rows path is exactly
    where a leak would hide — and `BoundedSemaphore` would then raise on the
    *next* export's release rather than at the leak.
    """
    detector, export = gates

    assert list(service.iter_field_inventory(_query(q="nothing-matches-this"), "attr:src_ip")) == []
    assert _free(detector) and _free(export)
