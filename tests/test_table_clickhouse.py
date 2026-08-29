"""Live-ClickHouse semantics for the table figure (`field_table`).

The table is the value inventory made bounded: top-N rows on the same
SELECT core as `iter_field_inventory`, a remainder row whenever anything was
cut, share against the filtered slice's non-empty total, and — given a second
field — the number of its distinct values per row. Corpus pattern as
`test_derive_clickhouse.py`.
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

CASE_ID = f"tc-table-{uuid.uuid4().hex[:8]}"
SRC = "src-table"


def _event(i: int, ts: str | None, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-table",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:table",
        attributes=attrs,
    )


# user → (count, hosts). alice 5 events over 2 hosts, bob 3 over 1, carol 2 over 2,
# dave 1 undated (stored as the sentinel) — first/last must be None for dave, not year 2299.
_ROWS = [
    ("alice", "h1", "2026-07-20T01:00:00+00:00"),
    ("alice", "h1", "2026-07-20T02:00:00+00:00"),
    ("alice", "h2", "2026-07-20T03:00:00+00:00"),
    ("alice", "h2", "2026-07-20T04:00:00+00:00"),
    ("alice", "h1", "2026-07-21T05:00:00+00:00"),
    ("bob", "h1", "2026-07-20T06:00:00+00:00"),
    ("bob", "h1", "2026-07-20T07:00:00+00:00"),
    ("bob", "h1", "2026-07-20T08:00:00+00:00"),
    ("carol", "h1", "2026-07-20T09:00:00+00:00"),
    ("carol", "h3", "2026-07-22T10:00:00+00:00"),
    ("dave", "h1", None),
]


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events(
        [
            _event(i, ts, {"user": u, "host": h, "bytes": str(100 * (i + 1))})
            for i, (u, h, ts) in enumerate(_ROWS)
        ]
        + [_event(100, "2026-07-20T00:00:00+00:00", {"host": "h9"})]  # no user: not counted
    )
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


def _query() -> EventQuery:
    return EventQuery(case_id=CASE_ID, source_ids=[SRC])


def test_rows_carry_count_share_and_seen_range_and_no_remainder_when_nothing_is_cut(service):
    result = service.field_table(_query(), "attr:user", 50)
    assert result["total"] == 11 and result["distinct"] == 4
    assert [r["value"] for r in result["rows"]] == ["alice", "bob", "carol", "dave"]
    alice = result["rows"][0]
    assert alice["count"] == 5 and alice["share"] == pytest.approx(5 / 11)
    assert alice["first_seen"].startswith("2026-07-20T01:00:00")
    assert alice["last_seen"].startswith("2026-07-21T05:00:00")
    assert alice["distinct_second"] is None
    dave = result["rows"][3]
    assert dave["first_seen"] is None and dave["last_seen"] is None  # undated, not year 2299
    assert result["remainder"] is None
    assert result["sort"] == {"by": "count", "dir": "desc"}


def test_remainder_row_is_present_exactly_when_values_were_cut(service):
    result = service.field_table(_query(), "attr:user", 2)
    assert [r["value"] for r in result["rows"]] == ["alice", "bob"]
    assert result["remainder"] == {
        "count": 3,
        "share": pytest.approx(3 / 11),
        "distinct_values": 2,
    }
    # Shares of the shown rows plus the remainder account for the whole slice.
    assert sum(r["share"] for r in result["rows"]) + result["remainder"]["share"] == pytest.approx(
        1.0
    )


def test_distinct_second_counts_the_second_field_per_row(service):
    result = service.field_table(_query(), "attr:user", 50, second_field="attr:host")
    by_value = {r["value"]: r["distinct_second"] for r in result["rows"]}
    assert by_value == {"alice": 2, "bob": 1, "carol": 2, "dave": 1}
    assert result["second_field"] == "attr:host"


@pytest.mark.parametrize(
    ("sort_by", "sort_dir", "expected"),
    [
        ("value", "asc", ["alice", "bob", "carol", "dave"]),
        ("value", "desc", ["dave", "carol", "bob", "alice"]),
        ("count", "asc", ["dave", "carol", "bob", "alice"]),
        ("share", "desc", ["alice", "bob", "carol", "dave"]),
        # Undated `dave` sorts last in either direction — NULLS LAST.
        ("first_seen", "asc", ["alice", "bob", "carol", "dave"]),
        ("last_seen", "desc", ["carol", "alice", "bob", "dave"]),
    ],
)
def test_every_column_sorts_both_ways_with_undated_values_last(
    service, sort_by, sort_dir, expected
):
    result = service.field_table(_query(), "attr:user", 50, sort_by=sort_by, sort_dir=sort_dir)
    assert [r["value"] for r in result["rows"]] == expected


def test_sort_by_distinct_second_orders_by_that_count(service):
    result = service.field_table(
        _query(),
        "attr:user",
        50,
        second_field="attr:host",
        sort_by="distinct_second",
        sort_dir="desc",
    )
    # alice and carol tie at 2; ties break on the value.
    assert [r["value"] for r in result["rows"]] == ["alice", "carol", "bob", "dave"]


def test_sort_by_distinct_second_without_a_second_field_is_refused(service):
    with pytest.raises(ValueError, match="distinct_second"):
        service.field_table(_query(), "attr:user", 50, sort_by="distinct_second")


def test_a_derived_field_tabulates_its_ranges(service):
    result = service.field_table(
        _query(), "attr:bytes", 50, derive=DeriveSpec(kind="bins", mode="custom", edges=[500])
    )
    assert {r["value"] for r in result["rows"]} == {"< 500", "≥ 500"}
    assert result["derive"]["labels"] == ["< 500", "≥ 500"]
    assert sum(r["count"] for r in result["rows"]) == 11


def test_table_and_inventory_agree_on_every_shared_cell(service):
    """The table is the inventory made bounded: same values, counts and seen
    range, from one SELECT core."""
    inventory = {
        row["value"]: row
        for row in service.iter_field_inventory(_query(), "attr:user", order_by="count_desc")
    }
    table = service.field_table(_query(), "attr:user", 50)
    for row in table["rows"]:
        inv = inventory[row["value"]]
        assert row["count"] == inv["count"]
        assert (row["first_seen"] or None) == (inv["first_seen"] or None)
        assert (row["last_seen"] or None) == (inv["last_seen"] or None)
