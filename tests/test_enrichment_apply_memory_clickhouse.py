"""The enrichment partition rewrite stays under its per-query cap as the source grows.

``finalize_enrichment_apply`` copies a source's partition through a LEFT JOIN
against the staged ``(event_id, field_key, value)`` rows, with
``join_algorithm = 'grace_hash'`` so the join can spill. Grace hash only splits
into buckets once a bucket exceeds ``max_bytes_in_join``, and at its default of
0 that never happens — and ClickHouse's ``query_plan_join_swap_table = auto``
then picks the *events* side, every column of it, as the in-memory build table.
The rewrite's memory grew with the source's event count and died at the cap on
the airgapped 26.6 production stack (819 MiB, "While executing
FillingRightJoinSide"); on a 4M-event source it still failed at a 4 GiB cap.

Two million narrow events at a 1 GiB cap reproduce that here: the unbounded
join fails with code 241, the bounded one peaks around 750 MiB (measured on
26.6.1.1193, four threads). Thread width is pinned because read-side memory
scales with it, and a width inherited from another test's probe would make the
margin a coin flip.
"""

from __future__ import annotations

import pytest

from vestigo.core.config import set_runtime_overrides
from vestigo.db import _scan
from vestigo.db.clickhouse import ClickHouseStore

_CAP = 1024**3
_EVENTS = 2_000_000
_CASE = "memtest_enrich"
_SOURCE = "memtest_enrich_s1"
_SUFFIX = "memtest_enrich_job"


@pytest.fixture
def bounded_scan(monkeypatch):
    monkeypatch.setattr(_scan, "detect_scan_memory_budget", lambda: _CAP)
    monkeypatch.setattr(_scan, "detect_scan_max_threads", lambda: 4)
    # The merge wait is about server-side merges after the swap, not about the
    # query under test; five minutes of polling would only slow the suite.
    set_runtime_overrides({"enrichment_apply_merge_wait_seconds": 0})
    yield
    set_runtime_overrides({})


@pytest.fixture
def staged_source():
    store = ClickHouseStore()
    store.init_schema()
    db = store.database
    store.delete_source_events(_CASE, _SOURCE)
    store.client.command(
        f"INSERT INTO {db}.events (event_id, case_id, source_id, source_file, byte_offset, "
        "line_number, content_hash, file_hash, parser_name, parser_version, ingest_time, "
        "message, timestamp, timestamp_desc, artifact, artifact_long, display_name, tags, "
        "attributes, embedding_model, embedding_config_hash) "
        f"SELECT generateUUIDv4(number), '{_CASE}', '{_SOURCE}', 'u_ex.log', number, number, "
        "hex(SHA256(toString(number))), repeat('0', 64), 'iis', '1', now64(3), "
        "concat('GET /owa/', toString(number % 997)), "
        "toDateTime64('2026-01-01 00:00:00', 3) + intDiv(number, 10), 'Event', 'iis', 'IIS', "
        "'IIS', [], map('src_ip', IPv4NumToString(toUInt32(number % 30011)), "
        "'dst_ip', IPv4NumToString(toUInt32(number % 7001)), "
        "'http_query', toString(cityHash64(number)), 'username', concat('u', toString(number % 50))), "
        f"'', repeat('0', 64) FROM numbers({_EVENTS})"
    )
    store.create_enrichment_scratch(_SUFFIX)
    rows_table, _ = store._enrichment_scratch_tables(_SUFFIX)
    # Six derived keys per event (two IP fields x three geo outputs): what a
    # GeoIP apply stages for a web log, and what the rewrite's join must carry.
    store.client.command(
        f"INSERT INTO {rows_table} "
        "SELECT event_id, concat(k, ':', f), if(f = 'geo_city', 'Frankfurt am Main', 'DE') "
        f"FROM {db}.events "
        "ARRAY JOIN ['src_ip', 'dst_ip'] AS k "
        "ARRAY JOIN ['geo_country', 'geo_city', 'geo_country_code'] AS f "
        f"WHERE case_id = '{_CASE}' AND source_id = '{_SOURCE}'"
    )
    yield store
    store.drop_enrichment_scratch(_SUFFIX)
    store.delete_source_events(_CASE, _SOURCE)


def test_partition_rewrite_bigger_than_the_cap_completes(bounded_scan, staged_source):
    store = staged_source

    store.finalize_enrichment_apply(
        _CASE, _SOURCE, _SUFFIX, ["geo_country", "geo_city", "geo_country_code"]
    )

    rows = store.client.query(
        f"SELECT count(), "
        "countIf(attributes['src_ip:geo_city'] = 'Frankfurt am Main' "
        "        AND attributes['dst_ip:geo_country_code'] = 'DE'), "
        "countIf(mapContains(attributes, 'http_query')) "
        f"FROM {store.database}.events WHERE case_id = '{_CASE}' AND source_id = '{_SOURCE}'"
    ).result_rows
    # Every event survives the swap, carries its derived keys, and keeps its own.
    assert rows == [(2_000_000, 2_000_000, 2_000_000)]
