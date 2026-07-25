"""Live-ClickHouse test: keyset pagination yields exactly ``count()`` rows.

The Explorer grid and the streaming export both advance by a keyset cursor on
``(effective_timestamp, event_id)``. That is complete only if the tuple is
unique across the result set — so the risky case is many rows sharing the exact
same millisecond, where the tie-break falls entirely to ``event_id``. This test
inserts such data (plus a run of adjacent milliseconds and no-timestamp sentinel
rows) and asserts a full cursor walk — via both ``EventQueryService.query``
(the grid's path) and ``EventQueryService.iter_events`` (the export path) —
returns every row exactly once, matching ``count()``.

Regression guard for the "grid loaded fewer than the filter matched" class of
bug: every pagination test in ``test_queries.py`` mocks the ClickHouse client,
so none exercise real keyset semantics over tied keys. Requires the dev compose
stack (skipped when ClickHouse is unreachable), same pattern as
``test_search_blob_clickhouse.py``.
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

CASE_ID = f"tc-page-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-page"

TIED_COUNT = 200  # all at the identical millisecond — tie-break is event_id only
ADJACENT_COUNT = 30  # a run of distinct adjacent milliseconds
SENTINEL_COUNT = 10  # no parseable timestamp — stored as the year-2299 sentinel
TOTAL = TIED_COUNT + ADJACENT_COUNT + SENTINEL_COUNT


def _event(i: int, timestamp: str | None) -> Event:
    # event_id derives from byte_offset + content_hash (both unique per i), so
    # ids stay distinct even when timestamps are identical — exactly the shape
    # the keyset comparator must handle.
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="b" * 64,
        parser_name="test-page",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=timestamp,
        timestamp_desc="Test Time",
        artifact="test:page",
    )


def _fixture_events() -> list[Event]:
    evs: list[Event] = []
    i = 0
    for _ in range(TIED_COUNT):
        evs.append(_event(i, "2026-03-01T12:00:00.000+00:00"))
        i += 1
    for j in range(ADJACENT_COUNT):
        evs.append(_event(i, f"2026-03-01T12:00:01.{j:03d}+00:00"))
        i += 1
    for _ in range(SENTINEL_COUNT):
        evs.append(_event(i, None))
        i += 1
    return evs


@pytest.fixture(scope="module")
def store():
    try:
        s = ClickHouseStore()
        s.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    s.insert_events(_fixture_events())
    yield s
    s.delete_source_events(CASE_ID, SOURCE_ID)


def _walk_query(svc: EventQueryService, order: str, page_size: int) -> list[str]:
    """Page through ``query`` exactly as the frontend does, collecting ids."""
    ids: list[str] = []
    after: tuple[datetime, str] | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 1000, "pagination did not terminate"
        page = svc.query(
            EventQuery(
                case_id=CASE_ID,
                source_ids=[SOURCE_ID],
                limit=page_size,
                order=order,  # type: ignore[arg-type]
                after=after,
            )
        )
        ids.extend(e["event_id"] for e in page.events)
        if not page.has_more_after or page.next_cursor is None:
            break
        ts_iso, event_id = page.next_cursor
        after = (datetime.fromisoformat(ts_iso), event_id)
    return ids


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_query_cursor_walk_is_complete(store, order):
    svc = EventQueryService(store=store)
    expected = svc.count(EventQuery(case_id=CASE_ID, source_ids=[SOURCE_ID]))
    assert expected == TOTAL

    # page_size deliberately not a divisor of TIED_COUNT, so page boundaries
    # land *inside* the tied-timestamp group — the exact spot a strict keyset
    # comparator could skip a row.
    ids = _walk_query(svc, order, page_size=7)
    assert len(set(ids)) == TOTAL, "cursor walk lost or duplicated rows"
    assert len(ids) == TOTAL, "cursor walk returned duplicate rows across pages"


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_iter_events_walk_is_complete(store, order):
    svc = EventQueryService(store=store)
    ids = [
        e["event_id"]
        for e in svc.iter_events(
            EventQuery(case_id=CASE_ID, source_ids=[SOURCE_ID], order=order),  # type: ignore[arg-type]
            batch_size=13,
        )
    ]
    assert len(ids) == TOTAL
    assert len(set(ids)) == TOTAL
