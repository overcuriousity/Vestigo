"""The "N routine events collapsed" count holds no per-event state.

``count_routine_collapsed`` reports the union of two collapse mechanisms: events
whose ``template_hash`` is muted, and members of routine-motif occurrences. It
runs on every Explorer page with collapse on. It used to run without a settings
clause and take the union as ``uniqExact`` over both branches' event ids — one
hash-set entry per muted event. A muted heartbeat template on a billion-event
case is hundreds of millions of entries, charged to the server's ceiling
rather than to any per-query cap, with every concurrent detector scan paying
for it.

Three million muted events under a 32 MiB cap separate the two: the id set
needs well over that, a count does not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.conftest import insert_generated_events
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = [pytest.mark.clickhouse, pytest.mark.slow]

_CASE = f"memtest-collapse-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-heartbeat"
_MUTED = 3_000_000
_CAP = 32 * 1024**2


@pytest.fixture
def bounded_scan(cap_scan):
    # The count carries ``foreground_scan_settings()``: half the heavy cap and
    # half the heavy thread width (``detect_foreground_memory_budget`` is
    # ``heavy * 2 // 4``, ``detect_foreground_max_threads`` is ``max(2, heavy
    # threads * 2 // 4)``), so a 64 MiB / 8-thread heavy probe is what runs it
    # at 32 MiB on four threads. Threads pinned: per-thread read buffers scale
    # with the server's core count and part layout, and a cap this small must
    # measure what grows with the muted events, not those.
    cap_scan(2 * _CAP, threads=8)


@pytest.fixture(scope="module")
def corpus():
    """Muted heartbeats plus one other event, and motif rows that overlap both.

    Motif occurrences name one heartbeat (under two dispositions — the same
    member twice) and the one non-heartbeat event, so the union is every
    heartbeat plus exactly one: ``_MUTED + 1``.
    """
    store = ClickHouseStore()
    store.init_schema()
    db = store.database
    insert_generated_events(
        store,
        case_id=_CASE,
        source_id=_SOURCE,
        n=_MUTED + 1,
        message=f"if(number < {_MUTED}, 'heartbeat ok', 'login failed for root')",
    )
    heartbeat_hash, heartbeat_id, login_id = store.client.query(
        f"SELECT anyIf(template_hash, byte_offset = 0), "
        "anyIf(toString(event_id), byte_offset = 0), "
        f"anyIf(toString(event_id), byte_offset = {_MUTED}) "
        f"FROM {db}.events WHERE case_id = {{c:String}} AND source_id = {{s:String}}",
        parameters={"c": _CASE, "s": _SOURCE},
    ).result_rows[0]
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    store.insert_motif_occurrences(
        [
            (_CASE, "d1", _SOURCE, heartbeat_id, ts),
            (_CASE, "d2", _SOURCE, heartbeat_id, ts),
            (_CASE, "d1", _SOURCE, login_id, ts),
        ]
    )
    yield store, int(heartbeat_hash)
    store.delete_source_events(_CASE, _SOURCE)
    store.delete_motif_occurrences(_CASE, "d1")
    store.delete_motif_occurrences(_CASE, "d2")


def test_union_counts_an_event_in_both_mechanisms_once(corpus):
    store, heartbeat_hash = corpus

    collapsed = store.count_routine_collapsed(
        _CASE, [_SOURCE], motif_disposition_ids=["d1", "d2"], template_hashes=[heartbeat_hash]
    )

    assert collapsed == 3_000_001


def test_union_count_does_not_scale_with_muted_events(bounded_scan, corpus):
    store, heartbeat_hash = corpus

    collapsed = store.count_routine_collapsed(
        _CASE, [_SOURCE], motif_disposition_ids=["d1", "d2"], template_hashes=[heartbeat_hash]
    )

    assert collapsed == 3_000_001
