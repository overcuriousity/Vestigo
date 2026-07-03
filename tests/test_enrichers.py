"""Tests for the enricher subsystem: registry, GeoIP plugin, and Postgres CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from tracesignal.db.postgres import PostgresStore
from tracesignal.enrichers.base import AvailabilityResult
from tracesignal.enrichers.geoip import IPV4_REGEX, GeoIPEnricher


@pytest_asyncio.fixture()
async def store(tmp_path):
    db_path = tmp_path / "test_enrichers.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    yield s
    await s.engine.dispose()


# ---------------------------------------------------------------------------
# GeoIP enricher
# ---------------------------------------------------------------------------


def test_geoip_unavailable_when_database_missing(tmp_path):
    enricher = GeoIPEnricher(db_path=tmp_path / "missing.mmdb")
    result = enricher.check_availability()
    assert result == AvailabilityResult(False, "GeoLite2 database not uploaded")


def test_geoip_eligibility_regex_matches_ipv4():
    enricher = GeoIPEnricher(db_path=None)
    assert enricher.is_field_eligible("8.8.8.8")
    assert enricher.is_field_eligible("192.168.1.1")
    assert not enricher.is_field_eligible("not-an-ip")
    assert not enricher.is_field_eligible("999.999.999.999")


def test_ipv4_regex_rejects_hostnames_and_partial_matches():
    import re

    assert re.match(IPV4_REGEX, "10.0.0.1")
    assert not re.match(IPV4_REGEX, "example.com")
    assert not re.match(IPV4_REGEX, "10.0.0.1extra")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_lists_geoip_and_caches_availability(tmp_path, monkeypatch):
    from tracesignal.enrichers import registry

    monkeypatch.setattr(
        "tracesignal.enrichers.geoip.geoip_database_path", lambda: tmp_path / "missing.mmdb"
    )
    # Re-register a fresh GeoIP instance pointed at the patched path so
    # check_availability() actually observes the monkeypatched location.
    from tracesignal.enrichers.geoip import GeoIPEnricher

    registry.register(GeoIPEnricher(db_path=tmp_path / "missing.mmdb"))

    assert registry.get_enricher("geoip") is not None
    assert any(e.key == "geoip" for e in registry.all_enrichers())

    availability = registry.refresh_availability()
    assert availability["geoip"].available is False
    assert registry.get_cached_availability("geoip").available is False


# ---------------------------------------------------------------------------
# PostgresStore: timeline_enrichers config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_timeline_enricher_creates_then_updates(store):
    await store.create_case("c1", "Case One")
    timeline = await store.create_timeline("c1", "t1", "Timeline One")

    created = await store.upsert_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="geoip",
        mode="manual",
        enabled=True,
        updated_by="u1",
    )
    assert created.mode == "manual"
    assert created.enabled is True

    updated = await store.upsert_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="geoip",
        mode="automatic",
        enabled=False,
        updated_by="u2",
    )
    assert updated.id == created.id
    assert updated.mode == "automatic"
    assert updated.enabled is False

    rows = await store.list_timeline_enrichers(timeline.id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_list_automatic_enrichers_for_source_filters_mode_and_enabled(store):
    await store.create_case("c1", "Case One")
    timeline = await store.create_timeline("c1", "t1", "Timeline One")
    source = await store.create_source("c1", "s1", "source-one", file_hash="a" * 64, size_bytes=10)
    await store.add_source_to_timeline("c1", timeline.id, source.id)

    await store.upsert_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="geoip",
        mode="automatic",
        enabled=True,
        updated_by=None,
    )
    await store.upsert_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="manual-only",
        mode="manual",
        enabled=True,
        updated_by=None,
    )
    await store.upsert_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="disabled",
        mode="automatic",
        enabled=False,
        updated_by=None,
    )

    configs = await store.list_automatic_enrichers_for_source(source.id)
    assert [c.enricher_key for c in configs] == ["geoip"]


# ---------------------------------------------------------------------------
# PostgresStore: staging + job-run crash/resume bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_flush_and_delete_staged_rows(store):
    now = datetime.now(UTC)
    rows = [
        {
            "job_id": "job1",
            "case_id": "c1",
            "source_id": "s1",
            "timeline_id": "t1",
            "event_id": "e1",
            "enricher_key": "geoip",
            "field_key": "geoip_country__ip",
            "value": "DE",
            "computed_at": now,
        },
        {
            "job_id": "job1",
            "case_id": "c1",
            "source_id": "s1",
            "timeline_id": "t1",
            "event_id": "e2",
            "enricher_key": "geoip",
            "field_key": "geoip_country__ip",
            "value": "US",
            "computed_at": now,
        },
    ]
    await store.stage_enrichment_results(rows)

    staged = await store.pop_staged_rows_for_job("job1", limit=10)
    assert len(staged) == 2
    assert {r.value for r in staged} == {"DE", "US"}

    await store.delete_staged_rows([r.id for r in staged])
    assert await store.pop_staged_rows_for_job("job1", limit=10) == []


@pytest.mark.asyncio
async def test_delete_staged_rows_for_job_discards_only_that_job(store):
    now = datetime.now(UTC)
    await store.stage_enrichment_results(
        [
            {
                "job_id": "job1",
                "case_id": "c1",
                "source_id": "s1",
                "timeline_id": "t1",
                "event_id": "e1",
                "enricher_key": "geoip",
                "field_key": "geoip_country__ip",
                "value": "DE",
                "computed_at": now,
            }
        ]
    )
    await store.stage_enrichment_results(
        [
            {
                "job_id": "job2",
                "case_id": "c1",
                "source_id": "s1",
                "timeline_id": "t1",
                "event_id": "e2",
                "enricher_key": "geoip",
                "field_key": "geoip_country__ip",
                "value": "US",
                "computed_at": now,
            }
        ]
    )

    await store.delete_staged_rows_for_job("job1")
    assert await store.pop_staged_rows_for_job("job1", limit=10) == []
    assert len(await store.pop_staged_rows_for_job("job2", limit=10)) == 1


@pytest.mark.asyncio
async def test_orphaned_enrichment_job_run_lifecycle(store):
    await store.start_enrichment_job_run(
        "job1", timeline_id="t1", case_id="c1", enricher_key="geoip"
    )

    orphans = await store.list_orphaned_enrichment_job_runs()
    assert [o.job_id for o in orphans] == ["job1"]

    await store.finish_enrichment_job_run("job1")
    assert await store.list_orphaned_enrichment_job_runs() == []


@pytest.mark.asyncio
async def test_reconcile_orphaned_enrichment_jobs_discards_marker_and_staged_rows(store):
    from tracesignal.enrichers.jobs import reconcile_orphaned_enrichment_jobs

    await store.create_case("c1", "Case One")
    await store.start_enrichment_job_run(
        "job1", timeline_id="t1", case_id="c1", enricher_key="geoip"
    )
    await store.stage_enrichment_results(
        [
            {
                "job_id": "job1",
                "case_id": "c1",
                "source_id": "s1",
                "timeline_id": "t1",
                "event_id": "e1",
                "enricher_key": "geoip",
                "field_key": "geoip_country__ip",
                "value": "DE",
                "computed_at": datetime.now(UTC),
            }
        ]
    )

    await reconcile_orphaned_enrichment_jobs(store)

    assert await store.list_orphaned_enrichment_job_runs() == []
    assert await store.pop_staged_rows_for_job("job1", limit=10) == []
