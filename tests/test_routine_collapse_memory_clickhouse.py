"""The "N routine events collapsed" count holds no per-event state.

``count_routine_collapsed`` reports the union of two collapse mechanisms: events
whose ``template_hash`` is muted, and members of routine-motif occurrences. It
runs on every Explorer page with collapse on, carries no settings clause, and
used to take the union as ``uniqExact`` over both branches' event ids — one
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

from vestigo.db.clickhouse import ClickHouseStore

pytestmark = pytest.mark.clickhouse

_CASE = f"memtest-collapse-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-heartbeat"
_MUTED = 3_000_000
_CAP = 32 * 1024**2


class _CappedClient:
    """Forward to the real client, running every query under a memory cap."""

    def __init__(self, inner, cap: int) -> None:
        self._inner = inner
        self._cap = cap

    def query(self, sql, parameters=None, settings=None, **kwargs):
        # Threads pinned: per-thread read buffers scale with the server's core
        # count and part layout, and a cap this small must measure what grows
        # with the muted events, not those.
        return self._inner.query(
            sql,
            parameters=parameters,
            settings={**(settings or {}), "max_memory_usage": self._cap, "max_threads": 4},
            **kwargs,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


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
    columns = (
        "event_id, case_id, source_id, source_file, byte_offset, line_number, content_hash, "
        "file_hash, parser_name, parser_version, ingest_time, message, timestamp, "
        "timestamp_desc, artifact, artifact_long, display_name, tags, attributes, "
        "embedding_model, embedding_config_hash"
    )
    store.client.command(
        f"INSERT INTO {db}.events ({columns}) "
        f"SELECT generateUUIDv4(number), '{_CASE}', '{_SOURCE}', 'hb.log', number, number, "
        "repeat('0', 64), repeat('0', 64), 'syslog', '1', now64(3), "
        "if(number < " + str(_MUTED) + ", 'heartbeat ok', 'login failed for root'), "
        "toDateTime64('2026-01-01 00:00:00', 3) + number, 'Event', 'syslog', 'Syslog', "
        f"'Syslog', [], map(), '', repeat('0', 64) FROM numbers({_MUTED + 1})"
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


def test_union_count_does_not_scale_with_muted_events(corpus):
    store, heartbeat_hash = corpus
    capped = ClickHouseStore()
    capped.client = _CappedClient(capped.client, _CAP)

    collapsed = capped.count_routine_collapsed(
        _CASE, [_SOURCE], motif_disposition_ids=["d1", "d2"], template_hashes=[heartbeat_hash]
    )

    assert collapsed == 3_000_001
