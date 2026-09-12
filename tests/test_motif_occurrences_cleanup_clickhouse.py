"""Deleting a source takes its motif-membership rows with it.

``motif_occurrences`` names events by id. After the events are gone the rows
are normally out of scope — every read filters by the timeline's live source
ids — but a source id is derived from the file hash, so re-ingesting the same
file (under another parser, with different event ids) brings the id back and
the stale rows with it: ``count_routine_collapsed`` would count them as
collapsed while the grid hid nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.conftest import insert_generated_events
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = pytest.mark.clickhouse

_CASE = f"motif-cleanup-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-motif-cleanup"


def test_source_delete_drops_its_motif_occurrences():
    store = ClickHouseStore()
    store.init_schema()
    insert_generated_events(store, case_id=_CASE, source_id=_SOURCE, n=3)
    ids = [
        row[0]
        for row in store.client.query(
            f"SELECT toString(event_id) FROM {store.database}.events "
            "WHERE case_id = {c:String} AND source_id = {s:String}",
            parameters={"c": _CASE, "s": _SOURCE},
        ).result_rows
    ]
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    store.insert_motif_occurrences([(_CASE, "d1", _SOURCE, event_id, ts) for event_id in ids])
    assert store.count_motif_occurrences(_CASE, ["d1"], [_SOURCE]) == 3

    store.delete_source_events(_CASE, _SOURCE)
    # Same source id, new ingest: the stale rows must not be counted against it.
    insert_generated_events(store, case_id=_CASE, source_id=_SOURCE, n=3)
    try:
        assert store.count_motif_occurrences(_CASE, ["d1"], [_SOURCE]) == 0
        assert store.count_routine_collapsed(_CASE, [_SOURCE], ["d1"], None) == 0
    finally:
        store.delete_source_events(_CASE, _SOURCE)
        store.delete_motif_occurrences(_CASE, "d1")
