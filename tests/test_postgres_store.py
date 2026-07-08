"""Tests for PostgresStore's DetectorRun CRUD and case-delete cascade."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tracesignal.db.postgres import PostgresStore


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

    from tracesignal.db.postgres import Base

    s = PostgresStore(url=f"sqlite+aiosqlite:///{tmp_path}/legacy.db")
    async with s.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Simulate a database from before two of the hand-rolled ALTERs.
        await conn.execute(text("ALTER TABLE annotations DROP COLUMN pinned"))
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
    assert version is not None
    assert "pinned" in ann_cols
    assert "onboarding_completed" in user_cols
    await s.engine.dispose()
