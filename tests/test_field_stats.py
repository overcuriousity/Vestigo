"""Per-source field-stats cache (M15): compute, merge parity, self-heal, refresh.

Live-ClickHouse integration tests (skipped when unreachable), SQLite for the
Postgres side — same pattern as tests/test_field_mappings_clickhouse.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from tracesignal.db import field_stats
from tracesignal.db.anomaly_stats import StatisticalAnomalyService
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_stats import (
    EFFECTIVE_STATS_VERSION,
    compute_source_field_stats,
    ensure_source_field_stats,
    merged_field_coverage,
    merged_inventory,
    merged_list_fields,
    refresh_source_field_stats,
)
from tracesignal.db.postgres import PostgresStore
from tracesignal.db.queries import EventQueryService
from tracesignal.models.event import Event

CASE_ID = f"tc-fieldstats-{uuid.uuid4().hex[:8]}"
SRC_A, SRC_B = "fs-src-a", "fs-src-b"


def _event(source_id: str, i: int, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=source_id,
        source_file=Path(f"{source_id}.csv"),
        byte_offset=i * 100,
        content_hash=f"ch-{source_id}-{i}",
        file_hash=f"fh-{source_id}",
        parser_name="test",
        parser_version="1",
        raw_line=f"line {i}",
        message=f"event {i} from {source_id}",
        timestamp=f"2026-01-01T10:{i:02d}:00Z",
        timestamp_desc="Test Time",
        artifact="test:artifact",
        attributes=attrs,
    )


@pytest.fixture(scope="module")
def ch_store():
    store = ClickHouseStore()
    try:
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    events = [
        _event(SRC_A, 1, {"src_ip": "10.0.0.1", "status": "200"}),
        _event(SRC_A, 2, {"src_ip": "10.0.0.2", "status": "200"}),
        _event(SRC_A, 3, {"src_ip": "10.0.0.2", "status": "500"}),
        _event(SRC_B, 4, {"proto": "tcp", "status": "301"}),
        _event(SRC_B, 5, {"proto": "udp"}),
    ]
    store.insert_events(events)
    yield store
    for sid in (SRC_A, SRC_B):
        store.delete_source_events(CASE_ID, sid)


@pytest_asyncio.fixture()
async def pg_store(tmp_path):
    s = PostgresStore(url=f"sqlite+aiosqlite:///{tmp_path}/field_stats.db")
    await s.init_schema()
    yield s
    await s.engine.dispose()


def _stats_for(ch_store) -> dict:
    return {sid: compute_source_field_stats(ch_store, CASE_ID, sid) for sid in (SRC_A, SRC_B)}


def test_compute_single_source_payload(ch_store):
    total, payload = compute_source_field_stats(ch_store, CASE_ID, SRC_A)
    assert total == 3
    attrs = payload["attributes"]
    assert attrs["src_ip"]["distinct"] == 2
    assert attrs["src_ip"]["coverage"] == 3
    assert set(attrs["src_ip"]["samples"]) == {"10.0.0.1", "10.0.0.2"}
    assert attrs["status"]["distinct"] == 2
    assert payload["top_level"]["artifact"]["coverage"] == 3


def test_merged_list_fields_matches_live(ch_store):
    live = EventQueryService(store=ch_store).list_fields(CASE_ID, [SRC_A, SRC_B])
    cached = merged_list_fields(_stats_for(ch_store))
    assert cached["attributes"] == live["attributes"]
    assert cached["top_level"] == live["top_level"]
    assert cached["mapped"] == live["mapped"]


def test_merged_inventory_coverage_matches_live(ch_store):
    svc = StatisticalAnomalyService(clickhouse=ch_store)
    live_inv, live_total = svc.field_inventory(CASE_ID, [SRC_A, SRC_B])
    cached_inv, cached_total = merged_inventory(_stats_for(ch_store))
    assert cached_total == live_total == 5
    # Coverage counts merge exactly; distinct is max-across-sources (an
    # approximation), so compare per-token coverage only.
    live_cov = {token: cov for token, _, cov in live_inv}
    cached_cov = {token: cov for token, _, cov in cached_inv}
    assert cached_cov == live_cov
    # Single-source scope has no union to approximate — full parity there.
    live_a, _ = svc.field_inventory(CASE_ID, [SRC_A])
    cached_a, _ = merged_inventory({SRC_A: compute_source_field_stats(ch_store, CASE_ID, SRC_A)})
    assert sorted(cached_a) == sorted(live_a)


def test_merged_field_coverage_matches_live_counts(ch_store):
    live = EventQueryService(store=ch_store).field_coverage(CASE_ID, [SRC_A, SRC_B])
    cached = merged_field_coverage(_stats_for(ch_store))
    live_counts = {
        (f["key"], s["source_id"]): s["count"] for f in live["fields"] for s in f["sources"]
    }
    cached_counts = {
        (f["key"], s["source_id"]): s["count"] for f in cached["fields"] for s in f["sources"]
    }
    # The fixture is far below the live sample cap, so counts must be equal.
    assert cached_counts == live_counts


@pytest.mark.asyncio
async def test_ensure_self_heals_and_caches(ch_store, pg_store, monkeypatch):
    stats = await ensure_source_field_stats(pg_store, ch_store, CASE_ID, [SRC_A])
    assert stats[SRC_A][0] == 3
    rows = await pg_store.get_source_field_stats([SRC_A])
    assert len(rows) == 1 and rows[0].stats_version == EFFECTIVE_STATS_VERSION

    # Second read must come from the cache: computing again would blow up.
    def _boom(*a, **kw):
        raise AssertionError("cache miss — compute called despite cached row")

    monkeypatch.setattr(field_stats, "compute_source_field_stats", _boom)
    stats2 = await ensure_source_field_stats(pg_store, ch_store, CASE_ID, [SRC_A])
    assert stats2[SRC_A] == stats[SRC_A]


@pytest.mark.asyncio
async def test_stats_version_mismatch_recomputes(ch_store, pg_store):
    await pg_store.upsert_source_field_stats(
        case_id=CASE_ID,
        source_id=SRC_A,
        stats_version=EFFECTIVE_STATS_VERSION - 1,
        events_total=999,
        payload={"top_level": {}, "attributes": {"stale": {}}},
    )
    stats = await ensure_source_field_stats(pg_store, ch_store, CASE_ID, [SRC_A])
    assert stats[SRC_A][0] == 3
    rows = await pg_store.get_source_field_stats([SRC_A])
    assert rows[0].stats_version == EFFECTIVE_STATS_VERSION


@pytest.mark.asyncio
async def test_refresh_after_enrichment_apply_sees_derived_keys(ch_store, pg_store):
    src = f"fs-src-enr-{uuid.uuid4().hex[:6]}"
    events = [_event(src, i, {"src_ip": f"10.1.0.{i}"}) for i in (1, 2)]
    ch_store.insert_events(events)
    try:
        await refresh_source_field_stats(pg_store, ch_store, CASE_ID, src)
        rows = await pg_store.get_source_field_stats([src])
        assert "src_ip:geo_country" not in rows[0].payload["attributes"]

        chunk = [(str(e.event_id), "src_ip:geo_country", "Germany") for e in events]
        ch_store.create_enrichment_scratch("job-fs")
        ch_store.stage_enrichment_rows("job-fs", chunk)
        ch_store.finalize_enrichment_apply(CASE_ID, src, "job-fs", ["geo_country"])
        ch_store.drop_enrichment_scratch("job-fs")

        await refresh_source_field_stats(pg_store, ch_store, CASE_ID, src)
        rows = await pg_store.get_source_field_stats([src])
        derived = rows[0].payload["attributes"]["src_ip:geo_country"]
        assert derived["coverage"] == 2
        assert derived["samples"] == ["Germany"]
    finally:
        ch_store.delete_source_events(CASE_ID, src)


@pytest.mark.asyncio
async def test_delete_source_field_stats(pg_store):
    await pg_store.upsert_source_field_stats(
        case_id=CASE_ID,
        source_id="gone",
        stats_version=EFFECTIVE_STATS_VERSION,
        events_total=1,
        payload={},
    )
    await pg_store.delete_source_field_stats("gone")
    assert await pg_store.get_source_field_stats(["gone"]) == []
