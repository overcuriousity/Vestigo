"""Tests for PostgresStore's DetectorRun CRUD and case-delete cascade."""

from __future__ import annotations

import pytest
import pytest_asyncio

from vestigo.db.postgres import PostgresStore


@pytest_asyncio.fixture()
async def store(tmp_path):
    db_path = tmp_path / "test_postgres_store.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    yield s
    await s.engine.dispose()


# ---------------------------------------------------------------------------
# DetectorRun CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_detector_run_round_trips(store):
    await store.create_case("c1", "Case One")
    run = await store.create_detector_run(
        "c1",
        "t1",
        "value_novelty",
        params={"fields": "artifact", "limit": 50},
        result={"status": "ok", "results": [{"event_id": "e1"}]},
    )
    assert run.case_id == "c1"
    assert run.timeline_id == "t1"
    assert run.detector == "value_novelty"

    fetched = await store.get_detector_run("c1", run.id)
    assert fetched is not None
    assert fetched.params == {"fields": "artifact", "limit": 50}
    assert fetched.result == {"status": "ok", "results": [{"event_id": "e1"}]}


@pytest.mark.asyncio
async def test_get_detector_run_returns_none_for_unknown_id(store):
    await store.create_case("c1", "Case One")
    assert await store.get_detector_run("c1", "no-such-run") is None


@pytest.mark.asyncio
async def test_get_detector_run_is_scoped_by_case_id(store):
    """A run_id from a different case must not resolve — run_ids referenced
    via a URL param should never leak cross-case data."""
    await store.create_case("c1", "Case One")
    await store.create_case("c2", "Case Two")
    run = await store.create_detector_run(
        "c1", "t1", "value_novelty", params={}, result={"results": []}
    )
    assert await store.get_detector_run("c2", run.id) is None


# ---------------------------------------------------------------------------
# delete_case cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_case_removes_views_annotations_and_detector_runs(store):
    """View/Annotation/DetectorRun are case-scoped by a plain case_id column
    (no FK cascade), so delete_case must clean them up explicitly or they
    orphan silently on every case delete."""
    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "source one", file_hash="h1", size_bytes=10)
    await store.create_view("c1", "v1", "My View")
    await store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="e1",
        annotation_id="ann1",
        annotation_type="tag",
        content="noted",
    )
    run = await store.create_detector_run(
        "c1", "t1", "value_novelty", params={}, result={"results": []}
    )

    assert await store.delete_case("c1") is True

    assert await store.get_view("c1", "v1") is None
    assert await store.list_annotations("c1", "s1", "e1") == []
    assert await store.get_detector_run("c1", run.id) is None


@pytest.mark.asyncio
async def test_delete_case_leaves_other_cases_untouched(store):
    await store.create_case("c1", "Case One")
    await store.create_case("c2", "Case Two")
    await store.create_view("c2", "v2", "Other Case View")
    run = await store.create_detector_run(
        "c2", "t2", "value_novelty", params={}, result={"results": []}
    )

    assert await store.delete_case("c1") is True

    assert await store.get_view("c2", "v2") is not None
    assert await store.get_detector_run("c2", run.id) is not None


# ---------------------------------------------------------------------------
# SavedChart CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saved_chart_create_list_round_trips_config(store):
    await store.create_case("c1", "Case One")
    config = {
        "v": 1,
        "chartType": "time",
        "metric": "ratio",
        "compare": {"mode": "baseline"},
        "options": {"buckets": 90},
    }
    chart = await store.create_saved_chart("c1", "t1", "chart1", "DoS overview", config)
    assert chart.name == "DoS overview"

    charts = await store.list_saved_charts("c1", "t1")
    assert [c.id for c in charts] == ["chart1"]
    # Config is opaque JSON — must round-trip byte-for-byte semantically.
    assert charts[0].config == config


@pytest.mark.asyncio
async def test_saved_chart_list_is_scoped_by_timeline(store):
    await store.create_case("c1", "Case One")
    await store.create_saved_chart("c1", "t1", "chart1", "A", {"v": 1})
    await store.create_saved_chart("c1", "t2", "chart2", "B", {"v": 1})
    assert [c.id for c in await store.list_saved_charts("c1", "t1")] == ["chart1"]
    assert [c.id for c in await store.list_saved_charts("c1", "t2")] == ["chart2"]


@pytest.mark.asyncio
async def test_saved_chart_rename_only_changes_name(store):
    await store.create_case("c1", "Case One")
    config = {"v": 1, "chartType": "bar"}
    await store.create_saved_chart("c1", "t1", "chart1", "Old", config)
    renamed = await store.rename_saved_chart("c1", "t1", "chart1", "New")
    assert renamed is not None
    assert renamed.name == "New"
    assert renamed.config == config
    assert await store.rename_saved_chart("c1", "t1", "missing", "X") is None


@pytest.mark.asyncio
async def test_saved_chart_delete_and_case_scoping(store):
    await store.create_case("c1", "Case One")
    await store.create_case("c2", "Case Two")
    await store.create_saved_chart("c1", "t1", "chart1", "A", {"v": 1})
    # A chart_id from another case must not resolve or delete cross-case.
    assert await store.delete_saved_chart("c2", "t1", "chart1") is False
    assert await store.delete_saved_chart("c1", "t1", "chart1") is True
    assert await store.list_saved_charts("c1", "t1") == []


@pytest.mark.asyncio
async def test_saved_chart_rename_and_delete_scoped_by_timeline(store):
    await store.create_case("c1", "Case One")
    await store.create_saved_chart("c1", "t1", "chart1", "A", {"v": 1})
    # Same case, wrong timeline: must not resolve or mutate the chart.
    assert await store.rename_saved_chart("c1", "t2", "chart1", "New") is None
    assert await store.delete_saved_chart("c1", "t2", "chart1") is False
    assert await store.list_saved_charts("c1", "t1") != []


# ---------------------------------------------------------------------------
# Alembic schema management (init_schema paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_schema_fresh_db_reaches_alembic_head(tmp_path):
    """A fresh database is created entirely by `alembic upgrade head`."""
    from sqlalchemy import inspect, text

    s = PostgresStore(url=f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
    await s.init_schema()
    async with s.engine.begin() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    assert "cases" in tables and "detector_runs" in tables and "annotations" in tables
    assert version is not None
    # Idempotent: a second startup is a no-op.
    await s.init_schema()
    await s.engine.dispose()


@pytest.mark.asyncio
async def test_init_schema_adopts_pre_alembic_db(tmp_path):
    """A database created by the old create_all path (no alembic_version) is
    normalized by the legacy fixups, stamped at 0001, then upgraded."""
    from sqlalchemy import inspect, text

    from vestigo.db.postgres import Base

    s = PostgresStore(url=f"sqlite+aiosqlite:///{tmp_path}/legacy.db")
    async with s.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # A real pre-Alembic database has only the revision-0001 tables —
        # drop everything later revisions add, or the upgrade would collide.
        await conn.execute(text("DROP TABLE baseline_definitions"))
        await conn.execute(text("DROP TABLE finding_dispositions"))
        # 0006 adds the Sigma runner tables.
        await conn.execute(text("DROP TABLE sigma_rules"))
        await conn.execute(text("DROP TABLE sigma_runs"))
        await conn.execute(text("ALTER TABLE sources DROP COLUMN time_offset_seconds"))
        # 0005 adds completed_source_ids to the enrichment job-run marker.
        await conn.execute(text("ALTER TABLE enrichment_job_runs DROP COLUMN completed_source_ids"))
        # 0001-era annotations still carried `pinned` (retired by 0004).
        await conn.execute(
            text("ALTER TABLE annotations ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT false")
        )
        # Simulate a database from before one of the hand-rolled ALTERs.
        await conn.execute(text("ALTER TABLE users DROP COLUMN onboarding_completed"))
    await s.init_schema()
    async with s.engine.begin() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        ann_cols = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("annotations")}
        )
        user_cols = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("users")}
        )
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert version is not None
    # 0004 retires pinned and detector_allowlist, adds finding_dispositions.
    assert "pinned" not in ann_cols
    assert "onboarding_completed" in user_cols
    assert "finding_dispositions" in tables
    assert "detector_allowlist" not in tables
    await s.engine.dispose()


# ---------------------------------------------------------------------------
# BaselineDefinition / DetectorAllowlistEntry store CRUD + cascades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_definition_crud_and_timeline_scoping(store):
    from datetime import UTC, datetime

    await store.create_case("c1", "Case One")
    d = await store.create_baseline_definition(
        "c1",
        "t1",
        "def1",
        baseline_start=datetime(2026, 1, 1, tzinfo=UTC),
        baseline_end=datetime(2026, 1, 15, tzinfo=UTC),
        suspect_windows=[
            {
                "id": "w0",
                "label": "x",
                "start": "2026-02-01T00:00:00+00:00",
                "end": "2026-02-02T00:00:00+00:00",
            }
        ],
        created_by="u1",
    )
    assert (await store.get_baseline_definition("c1", "t1", d.id)) is not None
    # Wrong timeline or case: not visible.
    assert (await store.get_baseline_definition("c1", "t2", d.id)) is None
    assert (await store.get_baseline_definition("c2", "t1", d.id)) is None
    assert await store.list_baseline_definitions("c1", "t2") == []

    updated = await store.update_baseline_definition("c1", "t1", d.id, name="renamed")
    assert updated is not None and updated.name == "renamed"
    # The derived hash only depends on the windows, not the name.
    assert updated.to_dict()["config_hash"] == d.to_dict()["config_hash"]

    assert await store.delete_baseline_definition("c1", "t1", d.id) is True
    assert await store.delete_baseline_definition("c1", "t1", d.id) is False


@pytest.mark.asyncio
async def test_disposition_dedupe_and_detector_filter(store):
    await store.create_case("c1", "Case One")
    e1 = await store.create_disposition(
        "c1",
        kind="normal",
        detector="value_novelty",
        timeline_id="t1",
        field="attr:user",
        value="svc",
    )
    e2 = await store.create_disposition(
        "c1",
        kind="normal",
        detector="value_novelty",
        timeline_id="t1",
        field="attr:user",
        value="svc",
    )
    assert e1.id == e2.id
    await store.create_disposition(
        "c1", kind="normal", detector="frequency", timeline_id="t1", field="artifact", value="cron"
    )
    assert len(await store.list_dispositions("c1", timeline_id="t1")) == 2
    # detector filter matches the concrete detector plus "*" wildcard rows.
    assert len(await store.list_dispositions("c1", timeline_id="t1", detector="frequency")) == 1


@pytest.mark.asyncio
async def test_create_dispositions_bulk_empty_returns_empty(store):
    await store.create_case("c1", "Case One")
    assert await store.create_dispositions_bulk("c1", []) == []


@pytest.mark.asyncio
async def test_create_dispositions_bulk_dedupes_within_batch_and_against_existing(store):
    await store.create_case("c1", "Case One")
    existing = await store.create_disposition(
        "c1",
        kind="normal",
        detector="value_novelty",
        timeline_id="t1",
        field="attr:user",
        value="svc",
    )

    rows = await store.create_dispositions_bulk(
        "c1",
        [
            # Duplicate of the pre-existing row.
            {
                "kind": "normal",
                "detector": "value_novelty",
                "timeline_id": "t1",
                "field": "attr:user",
                "value": "svc",
            },
            # New row.
            {
                "kind": "normal",
                "detector": "frequency",
                "timeline_id": "t1",
                "field": "artifact",
                "value": "cron",
            },
            # Duplicate of the previous item, within the same batch.
            {
                "kind": "normal",
                "detector": "frequency",
                "timeline_id": "t1",
                "field": "artifact",
                "value": "cron",
            },
        ],
    )

    assert [r.id for r in rows] == [existing.id, rows[1].id, rows[1].id]
    assert len({r.id for r in rows}) == 2
    assert len(await store.list_dispositions("c1", timeline_id="t1")) == 2


@pytest.mark.asyncio
async def test_create_dispositions_bulk_scope_narrowing_does_not_cross_timelines(store):
    """The prefetch narrows candidate rows by (case_id, kind, detector) only,
    then dedupes exactly in memory on the full scope tuple — same kind and
    detector in a different timeline must not be treated as a duplicate."""
    await store.create_case("c1", "Case One")
    other = await store.create_disposition(
        "c1",
        kind="normal",
        detector="value_novelty",
        timeline_id="t-other",
        field="attr:user",
        value="svc",
    )

    rows = await store.create_dispositions_bulk(
        "c1",
        [
            {
                "kind": "normal",
                "detector": "value_novelty",
                "timeline_id": "t1",
                "field": "attr:user",
                "value": "svc",
            }
        ],
    )

    assert len(rows) == 1
    assert rows[0].id != other.id
    assert rows[0].timeline_id == "t1"


@pytest.mark.asyncio
async def test_create_dispositions_bulk_event_scope(store):
    await store.create_case("c1", "Case One")
    rows = await store.create_dispositions_bulk(
        "c1",
        [
            {"kind": "dismissed", "source_id": "s1", "event_id": "e1"},
            {"kind": "dismissed", "source_id": "s1", "event_id": "e1"},
            {"kind": "dismissed", "source_id": "s1", "event_id": "e2"},
        ],
    )
    assert rows[0].id == rows[1].id
    assert rows[0].id != rows[2].id
    assert rows[0].detector == "*"
    assert rows[0].created_at is not None


@pytest.mark.asyncio
async def test_timeline_and_case_delete_cascade_baseline_rows(store):
    from datetime import UTC, datetime

    case = await store.create_case("c1", "Case One")
    assert case is not None
    tl = await store.create_timeline("c1", "tl-extra", "extra")
    await store.create_baseline_definition(
        "c1",
        tl.id,
        "def1",
        baseline_start=datetime(2026, 1, 1, tzinfo=UTC),
        baseline_end=datetime(2026, 1, 2, tzinfo=UTC),
        suspect_windows=[],
    )
    await store.create_disposition(
        "c1",
        kind="normal",
        detector="value_novelty",
        timeline_id=tl.id,
        field="artifact",
        value="x",
    )
    assert await store.delete_timeline("c1", tl.id) is True
    assert await store.list_baseline_definitions("c1", tl.id) == []
    assert await store.list_dispositions("c1", timeline_id=tl.id) == []

    # Case delete cascades rows on the default timeline too.
    default_tl = (await store.list_timelines("c1"))[0]
    await store.create_disposition(
        "c1",
        kind="normal",
        detector="value_novelty",
        timeline_id=default_tl.id,
        field="artifact",
        value="y",
    )
    assert await store.delete_case("c1") is True
    assert await store.list_dispositions("c1", timeline_id=default_tl.id) == []


# ---------------------------------------------------------------------------
# Source clock-skew offset (W2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_time_offset_defaults_to_zero(store):
    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "source one", file_hash="h1", size_bytes=10)
    source = await store.get_source("c1", "s1")
    assert source is not None
    assert source.time_offset_seconds == 0
    assert source.to_dict()["time_offset_seconds"] == 0


@pytest.mark.asyncio
async def test_set_source_time_offset_persists_and_returns_updated(store):
    await store.create_case("c1", "Case One")
    await store.create_source("c1", "s1", "source one", file_hash="h1", size_bytes=10)
    updated = await store.set_source_time_offset("c1", "s1", -3600)
    assert updated is not None
    assert updated.time_offset_seconds == -3600
    # Re-read confirms it was committed, not just returned from the session.
    reread = await store.get_source("c1", "s1")
    assert reread.time_offset_seconds == -3600


@pytest.mark.asyncio
async def test_set_source_time_offset_unknown_source_returns_none(store):
    await store.create_case("c1", "Case One")
    assert await store.set_source_time_offset("c1", "no-such-source", 60) is None


@pytest.mark.asyncio
async def test_set_source_time_offset_is_case_scoped(store):
    """A source_id from another case must not be writable through the wrong case."""
    await store.create_case("c1", "Case One")
    await store.create_case("c2", "Case Two")
    await store.create_source("c1", "s1", "source one", file_hash="h1", size_bytes=10)
    assert await store.set_source_time_offset("c2", "s1", 60) is None
    unchanged = await store.get_source("c1", "s1")
    assert unchanged.time_offset_seconds == 0
