"""Tests for the enricher subsystem: registry, GeoIP plugin, and Postgres CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from vestigo.db.postgres import PostgresStore
from vestigo.enrichers.base import AvailabilityResult
from vestigo.enrichers.geoip import IPV4_REGEX, GeoIPEnricher


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
    from vestigo.enrichers import registry

    monkeypatch.setattr(
        "vestigo.enrichers.geoip.geoip_database_path", lambda: tmp_path / "missing.mmdb"
    )
    # Re-register a fresh GeoIP instance pointed at the patched path so
    # check_availability() actually observes the monkeypatched location.
    from vestigo.enrichers.geoip import GeoIPEnricher

    registry.register(GeoIPEnricher(db_path=tmp_path / "missing.mmdb"))

    assert registry.get_enricher("geoip") is not None
    assert any(e.key == "geoip" for e in registry.all_enrichers())

    availability = registry.refresh_availability()
    assert availability["geoip"].available is False
    assert registry.get_cached_availability("geoip").available is False


def test_refresh_availability_single_key_only_touches_that_enricher(monkeypatch):
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import Enricher

    calls: list[str] = []

    def _make_stub(stub_key):
        class Stub(Enricher):
            key = stub_key
            display_name = "Stub"
            description = ""
            eligibility_regex = ".*"
            output_fields = ("x",)

            def check_availability(self):
                calls.append(self.key)
                return AvailabilityResult(True)

            def enrich_value(self, raw_value):
                return None

        return Stub()

    monkeypatch.setattr(
        registry, "_REGISTRY", {"stub-a": _make_stub("stub-a"), "stub-b": _make_stub("stub-b")}
    )
    monkeypatch.setattr(registry, "_AVAILABILITY_CACHE", {})

    result = registry.refresh_availability("stub-a")
    assert list(result) == ["stub-a"]
    assert calls == ["stub-a"]
    assert registry.get_cached_availability("stub-a").available is True
    assert registry.get_cached_availability("stub-b") is None

    # Unknown key: no-op, empty result.
    assert registry.refresh_availability("nope") == {}
    assert calls == ["stub-a"]

    # No key: full sweep.
    assert set(registry.refresh_availability()) == {"stub-a", "stub-b"}
    assert sorted(calls) == ["stub-a", "stub-a", "stub-b"]


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

    pairs = await store.list_automatic_enrichers_for_source(source.id)
    assert pairs == [(timeline.id, "geoip")]


@pytest.mark.asyncio
async def test_list_automatic_enrichers_global_default_and_override(store):
    await store.create_case("c1", "Case One")
    timeline_a = await store.create_timeline("c1", "t1", "Timeline A")
    timeline_b = await store.create_timeline("c1", "t2", "Timeline B")
    source = await store.create_source("c1", "s1", "source-one", file_hash="a" * 64, size_bytes=10)
    await store.add_source_to_timeline("c1", timeline_a.id, source.id)
    await store.add_source_to_timeline("c1", timeline_b.id, source.id)

    # Timeline A explicitly opts out; timeline B has no row and should
    # inherit the instance-wide default.
    await store.upsert_timeline_enricher(
        timeline_id=timeline_a.id,
        enricher_key="geoip",
        mode="automatic",
        enabled=False,
        updated_by=None,
    )

    pairs = await store.list_automatic_enrichers_for_source(source.id, {"geoip"})
    assert pairs == [(timeline_b.id, "geoip")]

    # Without the default, nothing fires.
    pairs = await store.list_automatic_enrichers_for_source(source.id)
    assert pairs == []


@pytest.mark.asyncio
async def test_upsert_enricher_global_config_creates_then_updates(store):
    created = await store.upsert_enricher_global_config(
        enricher_key="geoip", auto_run_default=True, updated_by="u1"
    )
    assert created.auto_run_default is True

    updated = await store.upsert_enricher_global_config(
        enricher_key="geoip", auto_run_default=False, updated_by="u2"
    )
    assert updated.auto_run_default is False

    rows = await store.list_enricher_global_configs()
    assert len(rows) == 1
    assert rows[0].enricher_key == "geoip"


# ---------------------------------------------------------------------------
# PostgresStore: staging + job-run crash/resume bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_and_delete_staged_rows_for_source(store):
    now = datetime.now(UTC)
    rows = [
        {
            "job_id": "job1",
            "case_id": "c1",
            "source_id": "s1",
            "timeline_id": "t1",
            "event_id": "e1",
            "enricher_key": "geoip",
            "fields": {"ip:geo_country": "DE", "ip:geo_city": "Berlin"},
            "computed_at": now,
        },
        {
            "job_id": "job1",
            "case_id": "c1",
            "source_id": "s1",
            "timeline_id": "t1",
            "event_id": "e2",
            "enricher_key": "geoip",
            "fields": {"ip:geo_country": "US"},
            "computed_at": now,
        },
    ]
    await store.stage_enrichment_results(rows)

    staged = await store.list_staged_rows_for_job("job1", limit=10)
    assert len(staged) == 2
    assert {r.fields["ip:geo_country"] for r in staged} == {"DE", "US"}
    assert staged[0].fields["ip:geo_city"] == "Berlin"

    await store.delete_staged_rows_for_source("job1", "s1")
    assert await store.list_staged_rows_for_job("job1", limit=10) == []


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
                "fields": {"ip:geo_country": "DE"},
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
                "fields": {"ip:geo_country": "US"},
                "computed_at": now,
            }
        ]
    )

    await store.delete_staged_rows_for_job("job1")
    assert await store.list_staged_rows_for_job("job1", limit=10) == []
    assert len(await store.list_staged_rows_for_job("job2", limit=10)) == 1


def test_process_batch_emits_one_row_per_event_with_field_map():
    """M16 staging grain: multi-attribute, multi-output events collapse into
    a single staging row whose ``fields`` map carries every derived key."""
    from vestigo.enrichers.base import Enricher
    from vestigo.enrichers.jobs import _process_batch

    class StubEnricher(Enricher):
        key = "stub"
        display_name = "Stub"
        description = ""
        eligibility_regex = r"^10\..*"
        output_fields = ("geo_country", "geo_city")

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return {"geo_country": "DE", "geo_city": "Berlin"}

    batch = [
        {
            "event_id": "e1",
            "attributes": {"src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "note": "x"},
        },
        {"event_id": "e2", "attributes": {"note": "no match"}},
        # Derived keys from a previous run must not be re-enriched.
        {"event_id": "e3", "attributes": {"src_ip:geo_country": "10.9.9.9"}},
    ]
    rows = _process_batch(StubEnricher(), batch, "c1", "s1", "t1", "job1", "stub", "hash1", {})

    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "e1"
    assert row["fields"] == {
        "src_ip:geo_country": "DE",
        "src_ip:geo_city": "Berlin",
        "dst_ip:geo_country": "DE",
        "dst_ip:geo_city": "Berlin",
    }


def test_process_batch_dedups_lookups_per_distinct_value():
    """A repeated raw value is looked up once, then served from the shared
    value cache — the per-distinct-value dedup that keeps GeoIP runs fast."""
    from vestigo.enrichers.base import Enricher
    from vestigo.enrichers.jobs import _process_batch

    class CountingEnricher(Enricher):
        key = "count"
        display_name = "Count"
        description = ""
        eligibility_regex = r"^10\..*"
        output_fields = ("geo_country",)
        calls = 0

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            type(self).calls += 1
            return {"geo_country": "DE"}

    enricher = CountingEnricher()
    cache: dict = {}
    batch1 = [{"event_id": f"e{i}", "attributes": {"src_ip": "10.0.0.1"}} for i in range(5)]
    batch2 = [{"event_id": "e9", "attributes": {"src_ip": "10.0.0.1", "dst_ip": "10.0.0.2"}}]

    rows1 = _process_batch(enricher, batch1, "c", "s", "t", "j", "count", "h", cache)
    rows2 = _process_batch(enricher, batch2, "c", "s", "t", "j", "count", "h", cache)

    # 5 events + 1 reuse all share "10.0.0.1" -> one lookup; "10.0.0.2" -> one more.
    assert CountingEnricher.calls == 2
    assert len(rows1) == 5
    assert len(rows2) == 1


@pytest.mark.asyncio
async def test_init_schema_drops_legacy_staging_table(tmp_path):
    """M16 destructive migration: a legacy row-per-field staging table
    (recognized by its field_key column) is dropped and recreated in the
    row-per-(job, event) shape; orphaned pre-upgrade rows are discarded.

    Simulates a realistic pre-Alembic database: full create_all schema (so
    init_schema takes the adoption path) with the staging table swapped for
    its legacy row-per-field shape."""
    from sqlalchemy import text

    from vestigo.db.postgres import Base

    db_path = tmp_path / "legacy_staging.db"
    s = PostgresStore(url=f"sqlite+aiosqlite:///{db_path}")
    async with s.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # A real pre-Alembic database has only the revision-0001 tables.
        await conn.execute(text("DROP TABLE baseline_definitions"))
        await conn.execute(text("DROP TABLE finding_dispositions"))
        await conn.execute(text("ALTER TABLE sources DROP COLUMN time_offset_seconds"))
        # 0005 adds completed_source_ids to the job-run marker.
        await conn.execute(text("ALTER TABLE enrichment_job_runs DROP COLUMN completed_source_ids"))
        # 0001-era annotations still carried `pinned` (retired by 0004).
        await conn.execute(
            text("ALTER TABLE annotations ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT false")
        )
        await conn.execute(text("DROP TABLE enrichment_results_staging"))
        await conn.execute(
            text(
                "CREATE TABLE enrichment_results_staging ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, job_id VARCHAR(64), "
                "case_id VARCHAR(64), source_id VARCHAR(64), timeline_id VARCHAR(64), "
                "event_id VARCHAR(64), enricher_key VARCHAR(128), "
                "field_key VARCHAR(128), value VARCHAR(1024), computed_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO enrichment_results_staging "
                "(job_id, case_id, source_id, timeline_id, event_id, enricher_key, "
                "field_key, value, computed_at) VALUES "
                "('j', 'c', 's', 't', 'e', 'geoip', 'ip:geo_country', 'DE', CURRENT_TIMESTAMP)"
            )
        )
    await s.init_schema()
    try:
        # New shape: fields JSON column present, legacy rows discarded.
        await s.stage_enrichment_results(
            [
                {
                    "job_id": "j2",
                    "case_id": "c",
                    "source_id": "s",
                    "timeline_id": "t",
                    "event_id": "e",
                    "enricher_key": "geoip",
                    "fields": {"ip:geo_country": "US"},
                    "computed_at": datetime.now(UTC),
                }
            ]
        )
        assert await s.list_staged_rows_for_job("j", limit=10) == []
        rows = await s.list_staged_rows_for_job("j2", limit=10)
        assert rows[0].fields == {"ip:geo_country": "US"}
        # Idempotent: a second init_schema must not drop the new table.
        await s.init_schema()
        assert len(await s.list_staged_rows_for_job("j2", limit=10)) == 1
    finally:
        await s.engine.dispose()


@pytest.mark.asyncio
async def test_orphaned_enrichment_job_run_lifecycle(store):
    await store.start_enrichment_job_run(
        "job1", timeline_id="t1", case_id="c1", enricher_key="geoip"
    )

    orphans = await store.list_orphaned_enrichment_job_runs()
    assert [o.job_id for o in orphans] == ["job1"]

    await store.finish_enrichment_job_run("job1")
    assert await store.list_orphaned_enrichment_job_runs() == []


class _RecordingClickHouse:
    """Fake ClickHouseStore capturing the stage/finalize enrichment-apply calls."""

    def __init__(self) -> None:
        self._staged_by_suffix: dict[str, list[list]] = {}
        self.applied: list[tuple[str, str, str, list]] = []

    def create_enrichment_scratch(self, scratch_suffix) -> None:
        self._staged_by_suffix[scratch_suffix] = []

    def stage_enrichment_rows(self, scratch_suffix, chunk) -> int:
        self._staged_by_suffix.setdefault(scratch_suffix, []).append(list(chunk))
        return len(chunk)

    def finalize_enrichment_apply(
        self, case_id, source_id, scratch_suffix, owned_suffixes=None
    ) -> None:
        chunks = self._staged_by_suffix.get(scratch_suffix, [])
        self.applied.append((case_id, source_id, scratch_suffix, chunks))

    def drop_enrichment_scratch(self, scratch_suffix) -> None:
        self._staged_by_suffix.pop(scratch_suffix, None)


class _BrokenClickHouse:
    def create_enrichment_scratch(self, scratch_suffix) -> None:
        pass

    def stage_enrichment_rows(self, scratch_suffix, chunk) -> int:
        raise ConnectionError("clickhouse down")

    def drop_enrichment_scratch(self, scratch_suffix) -> None:
        pass


async def _stage_one_row(store, job_id="job1", value="DE", config_hash="hash1"):
    await store.stage_enrichment_results(
        [
            {
                "job_id": job_id,
                "case_id": "c1",
                "source_id": "s1",
                "timeline_id": "t1",
                "event_id": "e1",
                "enricher_key": "geoip",
                "fields": {"ip:geo_country": value},
                "computed_at": datetime.now(UTC),
                "enricher_config_hash": config_hash,
            }
        ]
    )


@pytest.mark.asyncio
async def test_reconcile_orphaned_enrichment_jobs_applies_and_returns_reruns(store):
    from vestigo.enrichers.jobs import reconcile_orphaned_enrichment_jobs

    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "src", file_hash="a" * 64, size_bytes=1)
    await store.start_enrichment_job_run(
        "job1", timeline_id="t1", case_id="c1", enricher_key="geoip"
    )
    await _stage_one_row(store)

    ch = _RecordingClickHouse()
    recovered = await reconcile_orphaned_enrichment_jobs(store, ch)

    # Staged work applied to events.attributes, not discarded.
    assert len(ch.applied) == 1
    case_id, source_id, suffix, chunks = ch.applied[0]
    assert (case_id, source_id, suffix) == ("c1", "s1", "job1")
    assert chunks == [[("e1", "ip:geo_country", "DE")]]
    assert await store.list_staged_rows_for_job("job1", limit=10) == []
    assert await store.list_orphaned_enrichment_job_runs() == []
    # No provenance: the crashed run never marked s1's staging complete on
    # the marker (crash mid-source), and a provenance row off partial staging
    # would make the run route skip the source forever. The re-run records it.
    assert await store.list_source_enrichments("s1") == []
    # The run is returned so the caller can schedule a re-run.
    assert [r.job_id for r in recovered] == ["job1"]


@pytest.mark.asyncio
async def test_reconcile_grants_provenance_to_marker_completed_sources(store):
    """Sources the marker records as fully staged get provenance on recovery.

    A crashed 200-source job that completed 199 must not re-enrich all 200:
    ``mark_enrichment_source_staged`` appends each finished source to the
    durable marker, and reconciliation grants exactly those provenance.
    """
    from vestigo.enrichers.jobs import reconcile_orphaned_enrichment_jobs

    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "src", file_hash="a" * 64, size_bytes=1)
    await store.start_enrichment_job_run(
        "job1", timeline_id="t1", case_id="c1", enricher_key="geoip"
    )
    await _stage_one_row(store)
    # The crashed run had finished staging s1 before dying.
    await store.mark_enrichment_source_staged("job1", "s1")

    recovered = await reconcile_orphaned_enrichment_jobs(store, _RecordingClickHouse())

    provenance = await store.list_source_enrichments("s1")
    assert len(provenance) == 1
    assert provenance[0].enricher_config_hash == "hash1"
    assert [r.job_id for r in recovered] == ["job1"]


@pytest.mark.asyncio
async def test_reconcile_leaves_marker_and_rows_when_apply_fails(store):
    from vestigo.enrichers.jobs import reconcile_orphaned_enrichment_jobs

    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "src", file_hash="a" * 64, size_bytes=1)
    await store.start_enrichment_job_run(
        "job1", timeline_id="t1", case_id="c1", enricher_key="geoip"
    )
    await _stage_one_row(store)

    recovered = await reconcile_orphaned_enrichment_jobs(store, _BrokenClickHouse())

    assert recovered == []
    assert [o.job_id for o in await store.list_orphaned_enrichment_job_runs()] == ["job1"]
    assert len(await store.list_staged_rows_for_job("job1", limit=10)) == 1
    assert await store.list_source_enrichments("s1") == []


@pytest.mark.asyncio
async def test_apply_skips_and_discards_rows_for_deleted_source(store):
    from vestigo.enrichers.jobs import _apply_staged_rows

    await store.create_case("c1", "Case One")
    # Source "s1" is never created — simulates deletion mid-job.
    await _stage_one_row(store)

    ch = _RecordingClickHouse()
    applied = await _apply_staged_rows(store, ch, "job1")

    assert applied == 0
    assert ch.applied == []
    assert await store.list_staged_rows_for_job("job1", limit=10) == []
    assert await store.list_source_enrichments("s1") == []


# ---------------------------------------------------------------------------
# apply_enrichments partition rewrite (fake client)
# ---------------------------------------------------------------------------


class _FakeCHClient:
    def __init__(self):
        self.commands: list[str] = []
        self.queries: list[tuple[str, dict | None]] = []
        self.inserts: list[tuple[str, list]] = []
        self.query_rows: list[list] = []

    def command(self, sql):
        self.commands.append(sql)

    def query(self, sql, parameters=None):
        import types

        self.queries.append((sql, parameters))
        return types.SimpleNamespace(result_rows=self.query_rows)

    def insert(self, table, data, column_names=None, database=None):
        self.inserts.append((table, data))


def _fake_ch_store():
    from vestigo.db.clickhouse import ClickHouseStore

    store = ClickHouseStore.__new__(ClickHouseStore)
    store.database = "vestigo"
    store.client = _FakeCHClient()
    return store


def test_apply_enrichments_runs_atomic_partition_rewrite():
    store = _fake_ch_store()
    store.create_enrichment_scratch("job1")
    applied = store.stage_enrichment_rows(
        "job1", [("e1", "ip:geo_country", "DE"), ("e1", "ip:geo_city", "X")]
    )
    assert applied == 2
    store.finalize_enrichment_apply("c1", "s1", "job1")
    store.drop_enrichment_scratch("job1")

    client = store.client
    # Triples inserted into the scratch rows table.
    assert client.inserts == [
        (
            "vestigo.tmp_enrich_rows_job1",
            [("e1", "ip:geo_country", "DE"), ("e1", "ip:geo_city", "X")],
        )
    ]
    # Enriched partition copy built via mapUpdate join, pinned join_use_nulls.
    # The source map is first passed through mapFilter to strip this enricher's
    # own previously-derived keys (stale-value cleanup) before the merge.
    insert_select = client.queries[0][0]
    assert "mapUpdate(mapFilter(" in insert_select
    assert "m.enr) AS attributes" in insert_select
    assert "owned_suffixes:Array(String)" in insert_select
    assert "join_use_nulls = 0" in insert_select
    # Atomic swap of exactly this source's partition, then scratch cleanup.
    commands = client.commands
    replace = [c for c in commands if "REPLACE PARTITION" in c]
    assert replace == [
        "ALTER TABLE vestigo.events REPLACE PARTITION tuple('c1', 's1') "
        "FROM vestigo.tmp_enrich_events_job1"
    ]
    assert commands[-2:] == [
        "DROP TABLE IF EXISTS vestigo.tmp_enrich_events_job1",
        "DROP TABLE IF EXISTS vestigo.tmp_enrich_rows_job1",
    ]


def test_apply_enrichments_no_rows_is_a_noop_swap():
    store = _fake_ch_store()
    store.create_enrichment_scratch("job1")
    assert store.stage_enrichment_rows("job1", []) == 0
    store.drop_enrichment_scratch("job1")
    assert not any("REPLACE PARTITION" in c for c in store.client.commands)


def test_drop_stale_enrichment_scratch_tables():
    store = _fake_ch_store()
    store.client.query_rows = [("tmp_enrich_rows_x",), ("tmp_enrich_events_x",)]
    assert store.drop_stale_enrichment_scratch_tables() == 2
    assert "DROP TABLE IF EXISTS vestigo.tmp_enrich_rows_x" in store.client.commands
    assert "DROP TABLE IF EXISTS vestigo.tmp_enrich_events_x" in store.client.commands


# ---------------------------------------------------------------------------
# Per-run instances, dedup guard, config hash
# ---------------------------------------------------------------------------


def test_spawn_returns_fresh_instance_preserving_db_path(tmp_path):
    original = GeoIPEnricher(db_path=tmp_path / "custom.mmdb")
    clone = original.spawn()
    assert clone is not original
    assert isinstance(clone, GeoIPEnricher)
    assert clone._db_path == original._db_path


def test_spawn_pins_identity_against_mid_run_db_replacement(tmp_path, monkeypatch):
    """spawn() captures the exact bytes it reads; a later on-disk swap can't change them."""
    import hashlib

    import geoip2.database
    import maxminddb

    db_path = tmp_path / "GeoLite2-City.mmdb"
    db_path.write_bytes(b"v1-bytes")

    captured: dict = {}

    class _FakeMeta:
        build_epoch = 111
        database_type = "GeoLite2-City"

    class _FakeReader:
        def __init__(self, fileish, mode=0):
            # Pinning must hand the Reader an open file object (MODE_FD), not a
            # path — that's what makes the read immune to a later replacement.
            captured["is_fileobj"] = hasattr(fileish, "read")
            captured["mode"] = mode

        def metadata(self):
            return _FakeMeta()

        def close(self):
            pass

    monkeypatch.setattr(geoip2.database, "Reader", _FakeReader)

    enricher = GeoIPEnricher(db_path=db_path).spawn()
    pinned = enricher.config_extras()
    assert captured["is_fileobj"] is True
    assert captured["mode"] == maxminddb.MODE_FD
    assert pinned["database_sha256"] == hashlib.sha256(b"v1-bytes").hexdigest()
    assert pinned["database_type"] == "GeoLite2-City"

    # Admin replaces the database on disk after the run has started.
    db_path.write_bytes(b"v2-completely-different-bytes")

    # Identity is unchanged — still the bytes pinned at spawn.
    assert enricher.config_extras() == pinned
    enricher.close()


def test_base_close_is_noop():
    from vestigo.enrichers.base import Enricher

    class Stub(Enricher):
        key = "stub"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

    Stub().close()  # must not raise


def test_enricher_run_guard_claim_release():
    from vestigo.enrichers import jobs

    assert jobs.get_active_enricher_run("t1", "geoip") is None
    assert jobs.try_claim_enricher_run("t1", "geoip", "jobA") is None
    # Second claim reports the conflicting job.
    assert jobs.try_claim_enricher_run("t1", "geoip", "jobB") == "jobA"
    # Release by a non-owner is a no-op.
    jobs._release_enricher_run("t1", "geoip", "jobB")
    assert jobs.get_active_enricher_run("t1", "geoip") == "jobA"
    # Owner release frees the slot.
    jobs._release_enricher_run("t1", "geoip", "jobA")
    assert jobs.get_active_enricher_run("t1", "geoip") is None


def test_iter_source_events_batches_and_stops():
    from vestigo.db.clickhouse import ClickHouseStore

    calls: list[str | None] = []

    class _FakeCH:
        iter_source_events = ClickHouseStore.iter_source_events

        def list_events(self, case_id, source_id, limit, after=None):
            calls.append(after)
            data = [{"event_id": str(i)} for i in range(5)]  # 5 rows total
            if after is not None:
                data = [e for e in data if e["event_id"] > after]
            page = data[:limit]
            return page, (page[-1]["event_id"] if page else None)

    batches = list(_FakeCH().iter_source_events("c1", "s1", batch_size=2))
    assert [len(b) for b in batches] == [2, 2, 1]
    assert calls == [None, "1", "3"]  # short final batch ends iteration, no extra query

    calls.clear()

    class _EmptyCH(_FakeCH):
        def list_events(self, case_id, source_id, limit, after=None):
            calls.append(after)
            return [], None

    assert list(_EmptyCH().iter_source_events("c1", "s1", batch_size=2)) == []
    assert calls == [None]


def test_effective_enricher_state_resolution():
    from vestigo.enrichers.base import effective_enricher_state

    # Explicit row always wins, in either direction.
    assert effective_enricher_state(True, "automatic", False) == (True, "automatic")
    assert effective_enricher_state(False, "automatic", True) == (False, "automatic")
    assert effective_enricher_state(True, "manual", True) == (True, "manual")
    assert effective_enricher_state(False, "manual", False) == (False, "manual")
    # No explicit row: instance default decides, mode is automatic.
    assert effective_enricher_state(None, None, True) == (True, "automatic")
    assert effective_enricher_state(None, None, False) == (False, "automatic")


def test_config_hash_deterministic_and_sensitive_to_extras():
    from vestigo.enrichers.base import Enricher

    class Stub(Enricher):
        key = "stub"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)
        extras: dict = {}

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

        def config_extras(self):
            return self.extras

    a, b = Stub(), Stub()
    assert a.config_hash() == b.config_hash()
    b.extras = {"database_sha256": "deadbeef"}
    assert a.config_hash() != b.config_hash()


def test_geoip_config_extras_reads_and_writes_sidecar(tmp_path, monkeypatch):
    import geoip2.database

    from vestigo.enrichers.geoip import read_geoip_sidecar, write_geoip_sidecar

    db_path = tmp_path / "GeoLite2-City.mmdb"
    db_path.write_bytes(b"fake-mmdb-content")

    # Sidecar present: no Reader needed at all.
    write_geoip_sidecar(
        db_path, {"sha256": "abc123", "build_epoch": 1700000000, "database_type": "GeoLite2-City"}
    )
    extras = GeoIPEnricher(db_path=db_path).config_extras()
    assert extras == {
        "database_sha256": "abc123",
        "build_epoch": 1700000000,
        "database_type": "GeoLite2-City",
    }

    # Missing sidecar: fallback hashes the file, reads metadata, persists sidecar.
    sidecar_path = tmp_path / "GeoLite2-City.mmdb.meta.json"
    sidecar_path.unlink()

    class _FakeMeta:
        build_epoch = 1710000000
        database_type = "GeoLite2-City"

    class _FakeReader:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def metadata(self):
            return _FakeMeta()

    monkeypatch.setattr(geoip2.database, "Reader", _FakeReader)
    extras = GeoIPEnricher(db_path=db_path).config_extras()
    import hashlib

    expected_sha = hashlib.sha256(b"fake-mmdb-content").hexdigest()
    assert extras["database_sha256"] == expected_sha
    assert extras["build_epoch"] == 1710000000
    assert read_geoip_sidecar(db_path)["sha256"] == expected_sha


def test_geoip_availability_uses_sidecar_without_opening_reader(tmp_path, monkeypatch):
    import geoip2.database

    from vestigo.enrichers.geoip import write_geoip_sidecar

    db_path = tmp_path / "GeoLite2-City.mmdb"
    db_path.write_bytes(b"fake-mmdb-content")

    def _boom(path):
        raise AssertionError("Reader must not be opened when the sidecar has database_type")

    monkeypatch.setattr(geoip2.database, "Reader", _boom)

    # Sidecar with the right flavor: available, no Reader opened.
    write_geoip_sidecar(db_path, {"sha256": "a", "database_type": "GeoLite2-City"})
    assert GeoIPEnricher(db_path=db_path).check_availability() == AvailabilityResult(True)

    # Sidecar with the wrong flavor: unavailable with the flavor message.
    write_geoip_sidecar(db_path, {"sha256": "a", "database_type": "GeoLite2-Country"})
    result = GeoIPEnricher(db_path=db_path).check_availability()
    assert result.available is False
    assert "Wrong database flavor" in result.reason

    # No sidecar (pre-sidecar install): falls back to opening a Reader.
    (tmp_path / "GeoLite2-City.mmdb.meta.json").unlink()

    class _FakeMeta:
        database_type = "GeoLite2-City"

    class _FakeReader:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def metadata(self):
            return _FakeMeta()

    monkeypatch.setattr(geoip2.database, "Reader", _FakeReader)
    assert GeoIPEnricher(db_path=db_path).check_availability() == AvailabilityResult(True)


def test_derived_field_key_contract():
    from vestigo.enrichers.base import derived_field_key

    assert derived_field_key("src_ip", "geo_country") == "src_ip:geo_country"


def test_geoip_asset_status_and_install(tmp_path, monkeypatch):
    import geoip2.database

    from vestigo.enrichers.base import AssetValidationError
    from vestigo.enrichers.geoip import read_geoip_sidecar

    db_path = tmp_path / "data" / "GeoLite2-City.mmdb"
    enricher = GeoIPEnricher(db_path=db_path)

    assert enricher.asset_status() == {"uploaded": False, "size_bytes": None, "detail": {}}

    class _FakeMeta:
        database_type = "GeoLite2-City"
        build_epoch = 1700000000

    class _FakeReader:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def metadata(self):
            return _FakeMeta()

    monkeypatch.setattr(geoip2.database, "Reader", _FakeReader)
    upload = tmp_path / "upload.mmdb"
    upload.write_bytes(b"payload")
    detail = enricher.install_asset(upload, "sha-abc")
    assert detail["sha256"] == "sha-abc"
    assert db_path.read_bytes() == b"payload"
    assert read_geoip_sidecar(db_path)["database_type"] == "GeoLite2-City"

    status = enricher.asset_status()
    assert status["uploaded"] is True
    assert status["size_bytes"] == len(b"payload")
    assert status["detail"]["sha256"] == "sha-abc"

    # Wrong flavor raises AssetValidationError and installs nothing new.
    class _CountryMeta(_FakeMeta):
        database_type = "GeoLite2-Country"

    monkeypatch.setattr(_FakeReader, "metadata", lambda self: _CountryMeta())
    upload2 = tmp_path / "upload2.mmdb"
    upload2.write_bytes(b"country")
    with pytest.raises(AssetValidationError, match="City database"):
        enricher.install_asset(upload2, "sha-def")
    assert db_path.read_bytes() == b"payload"


def test_install_asset_default_raises_for_assetless_enricher(tmp_path):
    from vestigo.enrichers.base import Enricher

    class Stub(Enricher):
        key = "stub"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

    assert Stub().asset_spec is None
    assert Stub().asset_status() is None
    with pytest.raises(NotImplementedError):
        Stub().install_asset(tmp_path / "x", "sha")


def test_geoip_output_fields_contract_locked():
    # Order is part of config_hash() — a reorder silently changes every
    # enricher identity, so lock the exact tuple.
    assert GeoIPEnricher(db_path=None).output_fields == (
        "geo_country",
        "geo_city",
        "geo_country_code",
    )


def test_enrich_value_invalid_ip_returns_none_but_reader_errors_propagate(tmp_path):
    enricher = GeoIPEnricher(db_path=tmp_path / "whatever.mmdb")
    # Invalid input is a legitimate None — never touches the reader.
    assert enricher.enrich_value("not-an-ip") is None

    class _BrokenReader:
        def city(self, value):
            raise ValueError("reader is closed or corrupt")

    enricher._reader = _BrokenReader()
    with pytest.raises(ValueError):
        enricher.enrich_value("8.8.8.8")


@pytest.mark.asyncio
async def test_manual_run_skips_sources_already_enriched_at_current_config(store, monkeypatch):
    from fastapi import BackgroundTasks

    from vestigo.api import deps
    from vestigo.api.routers.cases import run_timeline_enricher
    from vestigo.db.postgres import User
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import Enricher

    class Stub(Enricher):
        key = "stub-skip"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

    registry.register(Stub())
    registry.refresh_availability()
    monkeypatch.setattr(deps, "_store", store)
    # The re-run path constructs a ClickHouseStore and claims a run slot before
    # returning (the job body only runs later via BackgroundTasks, never here).
    # Stub the client so no real ClickHouse is needed.
    monkeypatch.setattr("vestigo.api.routers.cases.ClickHouseStore", lambda: object())

    case = await store.create_case("ck", "Skip Case")
    timeline = await store.create_timeline("ck", "tk", "Skip Timeline")
    await store.create_source("ck", "sk", "src", file_hash="h" * 64, size_bytes=1)
    await store.add_source_to_timeline("ck", timeline.id, "sk")

    config_hash = registry.get_enricher("stub-skip").config_hash()
    user = User(id="u1", username="t", is_admin=True, is_active=True)

    # Provenance at the current config hash -> source is skipped, no job starts.
    await store.record_source_enrichment(
        case_id="ck",
        source_id="sk",
        timeline_id=timeline.id,
        enricher_key="stub-skip",
        enricher_config_hash=config_hash,
        job_id="prior-job",
        rows_applied=3,
    )
    res = await run_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="stub-skip",
        background_tasks=BackgroundTasks(),
        case=case,
        user=user,
    )
    assert res["job_id"] is None
    assert res["status"] == "skipped"
    assert res["skipped_source_ids"] == ["sk"]

    # Provenance at a *different* hash (config or GeoIP DB changed) -> re-runs.
    await store.record_source_enrichment(
        case_id="ck",
        source_id="sk",
        timeline_id=timeline.id,
        enricher_key="stub-skip",
        enricher_config_hash="stale" + "0" * 59,
        job_id="prior-job",
        rows_applied=3,
    )
    res = await run_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="stub-skip",
        background_tasks=BackgroundTasks(),
        case=case,
        user=user,
    )
    assert res["job_id"] is not None
    assert res["source_ids"] == ["sk"]
    assert res["skipped_source_ids"] == []
    # Release the slot claimed by the re-run so it doesn't leak into other tests.
    from vestigo.enrichers import jobs as _jobs

    _jobs._release_enricher_run(timeline.id, "stub-skip", res["job_id"])

    # force=True bypasses matching provenance entirely — the recovery path when
    # provenance claims "enriched" but the events disagree (e.g. provenance
    # recorded off a partially-applied run by a pre-session-48c build).
    await store.record_source_enrichment(
        case_id="ck",
        source_id="sk",
        timeline_id=timeline.id,
        enricher_key="stub-skip",
        enricher_config_hash=config_hash,
        job_id="prior-job",
        rows_applied=3,
    )
    res = await run_timeline_enricher(
        timeline_id=timeline.id,
        enricher_key="stub-skip",
        background_tasks=BackgroundTasks(),
        force=True,
        case=case,
        user=user,
    )
    assert res["job_id"] is not None
    assert res["source_ids"] == ["sk"]
    assert res["skipped_source_ids"] == []
    _jobs._release_enricher_run(timeline.id, "stub-skip", res["job_id"])


@pytest.mark.asyncio
async def test_manual_run_409_and_auto_trigger_skip_when_run_active(store, monkeypatch):
    from fastapi import BackgroundTasks, HTTPException

    from vestigo.api import deps
    from vestigo.api.routers.cases import (
        _trigger_automatic_enrichments,
        run_timeline_enricher,
    )
    from vestigo.core.jobs import JobStore
    from vestigo.db.postgres import User
    from vestigo.enrichers import jobs, registry
    from vestigo.enrichers.base import Enricher

    class Stub(Enricher):
        key = "stub-guard"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

    registry.register(Stub())
    registry.refresh_availability()
    monkeypatch.setattr(deps, "_store", store)

    case = await store.create_case("cg", "Guard Case")
    timeline = await store.create_timeline("cg", "tg", "Guard Timeline")
    await store.create_source("cg", "sg", "src", file_hash="h" * 64, size_bytes=1)
    await store.add_source_to_timeline("cg", timeline.id, "sg")

    jobs.try_claim_enricher_run(timeline.id, "stub-guard", "existing-job")
    try:
        with pytest.raises(HTTPException) as excinfo:
            await run_timeline_enricher(
                timeline_id=timeline.id,
                enricher_key="stub-guard",
                background_tasks=BackgroundTasks(),
                case=case,
                user=User(id="u1", username="t", is_admin=True, is_active=True),
            )
        assert excinfo.value.status_code == 409
        assert "existing-job" in excinfo.value.detail

        # Auto-trigger silently skips the busy slot and creates no job.
        await store.upsert_timeline_enricher(
            timeline_id=timeline.id,
            enricher_key="stub-guard",
            mode="automatic",
            enabled=True,
            updated_by=None,
        )
        job_store = JobStore()
        await _trigger_automatic_enrichments(store, None, job_store, "cg", "sg")
        assert job_store._jobs == {}
    finally:
        jobs._release_enricher_run(timeline.id, "stub-guard", "existing-job")


@pytest.mark.asyncio
async def test_run_enrichment_job_stamps_config_hash_and_fails_loudly(store, monkeypatch):
    from vestigo.core.jobs import JobStore
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import Enricher
    from vestigo.enrichers.jobs import get_active_enricher_run, run_enrichment_job

    class Stub(Enricher):
        key = "stub-ok"
        display_name = "Stub"
        description = ""
        eligibility_regex = r"^match-me$"
        output_fields = ("out",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return {"out": "enriched"}

    registry.register(Stub())
    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "src", file_hash="b" * 64, size_bytes=1)

    from vestigo.db.clickhouse import ClickHouseStore

    class _FakeCH(_RecordingClickHouse):
        iter_source_events = ClickHouseStore.iter_source_events

        def count_events(self, case_id, source_ids):
            return len(source_ids)

        def list_events(self, case_id, source_id, limit, after=None):
            if after is not None:
                return [], None
            return [{"event_id": "e1", "attributes": {"field": "match-me"}}], "e1"

    job_store = JobStore()
    job = job_store.create(kind="enrich")
    ch = _FakeCH()
    await run_enrichment_job(
        job_id=job.id,
        case_id="c1",
        timeline_id="t1",
        enricher_key="stub-ok",
        source_ids=["s1"],
        job_store=job_store,
        store=store,
        ch_store=ch,
    )
    assert job_store.get(job.id).status == "completed"
    # One apply for the source, carrying the derived-key naming contract.
    assert len(ch.applied) == 1
    assert ch.applied[0][:2] == ("c1", "s1")
    assert ch.applied[0][3] == [[("e1", "field:out", "enriched")]]
    # Config hash lands in the per-source provenance row.
    provenance = await store.list_source_enrichments("s1")
    assert [p.enricher_config_hash for p in provenance] == [Stub().config_hash()]
    assert await store.list_orphaned_enrichment_job_runs() == []

    # Failing enricher: job fails loudly, marker cleaned up, guard released.
    class StubBroken(Stub):
        key = "stub-broken"

        def enrich_value(self, raw_value):
            raise RuntimeError("boom")

    registry.register(StubBroken())
    job2 = job_store.create(kind="enrich")
    await run_enrichment_job(
        job_id=job2.id,
        case_id="c1",
        timeline_id="t1",
        enricher_key="stub-broken",
        source_ids=["s1"],
        job_store=job_store,
        store=store,
        ch_store=_FakeCH(),
    )
    assert job_store.get(job2.id).status == "failed"
    assert "boom" in job_store.get(job2.id).error
    assert await store.list_orphaned_enrichment_job_runs() == []
    assert get_active_enricher_run("t1", "stub-broken") is None


@pytest.mark.asyncio
async def test_failed_run_records_provenance_only_for_fully_staged_sources(store):
    """A mid-run failure must not mark the in-flight source as enriched.

    The run route skips provenance-matched sources, so a provenance row
    written off partial staging would permanently block finishing the source
    ("no enricher started" with the source left unenriched).
    """
    from vestigo.core.jobs import JobStore
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import Enricher
    from vestigo.enrichers.jobs import run_enrichment_job

    class StubSecondSourceBoom(Enricher):
        key = "stub-partial"
        display_name = "Stub"
        description = ""
        eligibility_regex = r"^v-s\d$"
        output_fields = ("out",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            if raw_value == "v-s2":
                raise RuntimeError("boom on second source")
            return {"out": "enriched"}

    registry.register(StubSecondSourceBoom())
    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "src1", file_hash="c" * 64, size_bytes=1)
    await store.create_source("c1", "s2", "src2", file_hash="d" * 64, size_bytes=1)

    from vestigo.db.clickhouse import ClickHouseStore

    class _FakeCH(_RecordingClickHouse):
        iter_source_events = ClickHouseStore.iter_source_events

        def count_events(self, case_id, source_ids):
            return len(source_ids)

        def list_events(self, case_id, source_id, limit, after=None):
            if after is not None:
                return [], None
            return [
                {"event_id": f"e-{source_id}", "attributes": {"field": f"v-{source_id}"}}
            ], f"e-{source_id}"

    job_store = JobStore()
    job = job_store.create(kind="enrich")
    await run_enrichment_job(
        job_id=job.id,
        case_id="c1",
        timeline_id="t1",
        enricher_key="stub-partial",
        source_ids=["s1", "s2"],
        job_store=job_store,
        store=store,
        ch_store=_FakeCH(),
    )
    assert job_store.get(job.id).status == "failed"
    assert job_store.get(job.id).result["sources_covered"] == 1
    # s1 finished staging before the failure: applied + provenance recorded.
    assert len(await store.list_source_enrichments("s1")) == 1
    # s2 was in flight: no provenance, so a re-run is not skipped.
    assert await store.list_source_enrichments("s2") == []
    # Marker cleared either way — the failure was deterministic, no auto re-run.
    assert await store.list_orphaned_enrichment_job_runs() == []


@pytest.mark.asyncio
async def test_eligibility_fanout_builds_one_clickhouse_store_per_check(store, monkeypatch):
    # clickhouse_connect clients are not thread-safe — the enrichers-list
    # endpoint must construct a fresh ClickHouseStore inside each threadpool
    # eligibility check instead of sharing one client across the gather fan-out.
    from vestigo.api import deps
    from vestigo.api.routers.cases import list_timeline_enrichers
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import EligibilityResult, Enricher

    seen_stores: list[object] = []

    class _CountingStub(Enricher):
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def check_eligibility(self, ch_store, case_id, source_ids):
            seen_stores.append(ch_store)
            return EligibilityResult(eligible=True, sample_checked=1, sample_matched=1)

        def enrich_value(self, raw_value):
            return None

    class StubA(_CountingStub):
        key = "stub-fanout-a"

    class StubB(_CountingStub):
        key = "stub-fanout-b"

    # Isolate the module-global registry: stubs registered by other tests would
    # otherwise join the fan-out and hit the fake store with real queries.
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_AVAILABILITY_CACHE", {})
    registry.register(StubA())
    registry.register(StubB())
    registry.refresh_availability()
    monkeypatch.setattr(deps, "_store", store)

    created = 0

    class _FakeStore:
        pass

    def _make_store():
        nonlocal created
        created += 1
        return _FakeStore()

    monkeypatch.setattr("vestigo.api.routers.cases.ClickHouseStore", _make_store)

    case = await store.create_case("cf", "Fanout Case")
    timeline = await store.create_timeline("cf", "tf", "Fanout Timeline")

    res = await list_timeline_enrichers(timeline_id=timeline.id, case=case)

    keys = {e["key"] for e in res["enrichers"]}
    assert {"stub-fanout-a", "stub-fanout-b"} <= keys
    # One store per registered available enricher's check, none shared.
    assert created == 2
    assert len(seen_stores) == 2
    assert seen_stores[0] is not seen_stores[1]
