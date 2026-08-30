"""Live-ClickHouse semantics for the ranked change figure (`field_change`).

Two windows of unequal size, named by an attribute rather than by time so
the corpus stays one source. The property that matters: the encoded
quantity is share-of-window — a value with the same count in both windows
still *fell* when the second window is twice the size — and a value in one
window's top-N only is `new` or `vanished`, never silently absent. Corpus
pattern as `test_cumulative_clickhouse.py`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-change-{uuid.uuid4().hex[:8]}"
SRC = "src-change"


def _event(i: int, phase: str, user: str) -> Event:
    hour = 9 if phase == "a" else 21  # a timestamp-valued attribute for the derived case
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-change",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=f"2026-07-20T{i % 24:02d}:{(i * 7) % 60:02d}:00+00:00",
        timestamp_desc="Test Time",
        artifact="test:change",
        attributes={"phase": phase, "user": user, "logon_at": f"2026-07-20T{hour:02d}:15:00Z"},
    )


# Window a (the reference): alice 6, bob 3, carol 1 → 10 events.
# Window b (the suspect):   alice 4, bob 12, dave 3, erin 1 → 20 events.
# Shares a: alice .6, bob .3, carol .1. Shares b: alice .2, bob .6, dave .15, erin .05.
_ROWS: list[tuple[str, str]] = (
    [("a", "alice")] * 6
    + [("a", "bob")] * 3
    + [("a", "carol")]
    + [("b", "alice")] * 4
    + [("b", "bob")] * 12
    + [("b", "dave")] * 3
    + [("b", "erin")]
)


@pytest.fixture(scope="module")
def service():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events([_event(i, phase, user) for i, (phase, user) in enumerate(_ROWS)])
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC)


def _window(phase: str, **kw) -> EventQuery:
    return EventQuery(
        case_id=CASE_ID, source_ids=[SRC], field_filters={"attr:phase": [phase]}, **kw
    )


def test_shares_are_of_each_window_and_rows_rank_by_absolute_delta(service):
    result = service.field_change(_window("b"), _window("a"), "attr:user", 3, union_cap=200)
    assert result["kind"] == "change" and result["field"] == "attr:user"
    assert result["primary_total"] == 20 and result["comparison_total"] == 10
    assert result["top_n"] == 3 and result["union_size"] == 4
    assert result["truncated"] is False and result["omitted"] == 0
    rows = result["rows"]
    assert [r["value"] for r in rows] == ["alice", "bob", "dave", "carol"]
    assert [r["status"] for r in rows] == ["fell", "rose", "new", "vanished"]
    alice = rows[0]
    assert (alice["primary"], alice["comparison"]) == (4, 6)
    assert alice["primary_share"] == pytest.approx(0.2)
    assert alice["comparison_share"] == pytest.approx(0.6)
    assert alice["delta_share"] == pytest.approx(-0.4)
    dave = rows[2]
    assert (dave["primary"], dave["comparison"]) == (3, 0)
    assert dave["delta_share"] == pytest.approx(0.15)


def test_union_cap_drops_the_smallest_changes_and_discloses_them(service):
    result = service.field_change(_window("b"), _window("a"), "attr:user", 3, union_cap=3)
    assert [r["value"] for r in result["rows"]] == ["alice", "bob", "dave"]
    assert result["union_size"] == 4 and result["rows_shown"] == 3
    assert result["truncated"] is True and result["omitted"] == 1 and result["union_cap"] == 3


def test_top_n_bounds_each_window_before_the_union(service):
    result = service.field_change(_window("b"), _window("a"), "attr:user", 2, union_cap=200)
    # a's top-2: alice, bob; b's top-2: bob, alice → the union is two values.
    assert [r["value"] for r in result["rows"]] == ["alice", "bob"]
    assert result["union_size"] == 2 and result["truncated"] is False


def test_a_derived_field_counts_both_windows_on_the_primarys_expression(service):
    from vestigo.db.derive import DeriveSpec

    derive = DeriveSpec(kind="time_part", part="hour")
    result = service.field_change(_window("b"), _window("a"), "attr:logon_at", 5, derive=derive)
    assert result["derive"] is not None and result["derive"]["kind"] == "time_part"
    assert result["primary_total"] == 20 and result["comparison_total"] == 10
    # Hour 21 is the primary window's only value (new), hour 9 the comparison's (vanished).
    by_status = {r["status"]: r for r in result["rows"]}
    assert set(by_status) == {"new", "vanished"}
    assert by_status["new"]["primary"] == 20 and by_status["vanished"]["comparison"] == 10


def test_no_values_in_either_window_is_an_empty_change(service):
    empty = service.field_change(_window("z"), _window("y"), "attr:user", 5)
    assert empty["rows"] == [] and empty["union_size"] == 0 and empty["rows_shown"] == 0
    assert empty["primary_total"] == 0 and empty["comparison_total"] == 0
    assert empty["truncated"] is False and empty["omitted"] == 0
