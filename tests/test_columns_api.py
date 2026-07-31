"""Tests for the column-recommendation job and its endpoint (issue #213).

The field-stats cache is seeded directly in Postgres so the job's read path
(``ensure_source_field_stats``) is a pure cache hit — no ClickHouse needed.
The LLM advisor is monkeypatched at the job's call site; the real one is
gated on ``agent_available()`` and never runs in the suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import as_admin
from vestigo.columns import jobs as columns_jobs
from vestigo.core.config import get_settings
from vestigo.core.jobs import JobStore
from vestigo.db.field_stats import EFFECTIVE_STATS_VERSION


def _payload(attributes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"top_level": {}, "attributes": attributes, "attr_keys_truncated": False}


def _attr(coverage: int, distinct: int, samples: list[str]) -> dict[str, Any]:
    return {"coverage": coverage, "distinct": distinct, "samples": samples}


#: Stand-in for a ClickHouseStore the cache-hit path never dereferences.
_NEVER_USED = object()

RICH_ATTRIBUTES = {
    "user": _attr(1000, 12, ["alice", "bob", "carol"]),
    "src_ip": _attr(1000, 40, ["10.0.0.1", "10.0.0.2", "8.8.8.8"]),
    "status": _attr(1000, 6, ["200", "404", "500"]),
    "host": _attr(1000, 8, ["web01", "web02", "db01"]),
    "action": _attr(1000, 5, ["allow", "deny", "reset"]),
}


async def _seed_case_with_stats(store, attributes=None) -> tuple[str, str, str]:
    """Create a case, a ready source, and its cached field stats.

    Returns ``(case_id, timeline_id, source_id)``.
    """
    await store.init_schema()
    case = await store.create_case("c-columns", "Columns case")
    timeline = await store.get_default_timeline(case.id)
    source = await store.create_source(
        case_id=case.id,
        source_id="s-columns",
        name="proxy.jsonl",
        file_hash="0" * 64,
        size_bytes=1234,
        event_count=1000,
    )
    await store.add_source_to_timeline(case.id, timeline.id, source.id)
    await store.upsert_source_field_stats(
        case_id=case.id,
        source_id=source.id,
        stats_version=EFFECTIVE_STATS_VERSION,
        events_total=1000,
        payload=_payload(attributes if attributes is not None else RICH_ATTRIBUTES),
    )
    return case.id, timeline.id, source.id


async def _run(store, case_id: str, timeline_id: str, **kwargs) -> JobStore:
    job_store = JobStore()
    job = job_store.create(kind=columns_jobs.JOB_KIND, case_id=case_id)
    await columns_jobs.run_column_recommendation_job(
        job_id=job.id,
        case_id=case_id,
        timeline_id=timeline_id,
        job_store=job_store,
        store=store,
        # Never touched: every source in these tests is a cache hit, so
        # `ensure_source_field_stats` never reaches for ClickHouse.
        ch_store=_NEVER_USED,
        **kwargs,
    )
    return job_store


# ── The job ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_persists_a_heuristic_recommendation(store):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)

    await _run(store, case_id, timeline_id)

    timeline = await store.get_timeline(case_id, timeline_id)
    recommended = timeline.recommended_columns
    assert recommended["status"] == "ok"
    assert recommended["method"] == "heuristic"
    assert recommended["model"] is None
    assert recommended["columns"][0] == "timestamp"
    assert 4 <= len(recommended["columns"]) <= 6
    assert set(recommended["columns"][1:]) <= set(RICH_ATTRIBUTES)
    # Every non-pinned column explains itself.
    assert set(recommended["reasons"]) == set(recommended["columns"][1:])


@pytest.mark.asyncio
async def test_job_surfaces_on_the_timeline_payload(store):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)

    timeline = await store.get_timeline(case_id, timeline_id)
    assert timeline.to_dict()["recommended_columns"]["status"] == "ok"


@pytest.mark.asyncio
async def test_job_writes_an_audit_row(store):
    case_id, timeline_id, source_id = await _seed_case_with_stats(store)

    await _run(store, case_id, timeline_id, actor_id="u1", actor_username="tester")

    rows = await store.query_audit(case_id=case_id, action="timeline.recommend_columns")
    entry = rows[0]
    assert entry.target_id == timeline_id
    assert entry.username_snapshot == "tester"
    assert entry.detail["method"] == "heuristic"
    assert entry.detail["source_ids"] == [source_id]
    assert (
        entry.detail["columns"]
        == (await store.get_timeline(case_id, timeline_id)).recommended_columns["columns"]
    )


@pytest.mark.asyncio
async def test_job_records_insufficient_when_nothing_is_worth_suggesting(store):
    """A constant and a per-row-unique field: looked, found nothing."""
    case_id, timeline_id, _ = await _seed_case_with_stats(
        store,
        {
            "env": _attr(1000, 1, ["prod"]),
            "row_seq": _attr(1000, 1000, ["1", "2", "3"]),
        },
    )

    await _run(store, case_id, timeline_id)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert recommended["status"] == "insufficient"
    assert recommended["columns"] == []


@pytest.mark.asyncio
async def test_job_with_no_ready_sources_is_not_a_failure(store):
    await store.init_schema()
    case = await store.create_case("c-empty", "Empty case")
    timeline = await store.get_default_timeline(case.id)

    job_store = await _run(store, case.id, timeline.id)

    recommended = (await store.get_timeline(case.id, timeline.id)).recommended_columns
    assert recommended["status"] == "insufficient"
    assert all(j.status == "completed" for j in job_store.list_by_case(case.id))


@pytest.mark.asyncio
async def test_job_failure_clears_the_running_placeholder(store, monkeypatch):
    """A stuck 'running' would leave the explorer polling forever."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)

    async def _boom(*args, **kwargs):
        raise RuntimeError("clickhouse is on fire")

    monkeypatch.setattr(columns_jobs, "ensure_source_field_stats", _boom)
    job_store = await _run(store, case_id, timeline_id)

    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns is None
    job = job_store.list_by_case(case_id)[0]
    assert job.status == "failed"
    assert "clickhouse is on fire" in job.error


@pytest.mark.asyncio
async def test_a_failed_rerun_restores_the_previous_suggestion(store, monkeypatch):
    """Losing a good suggestion to a transient blip is worse than not recomputing."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert good["status"] == "ok"

    async def _boom(*args, **kwargs):
        raise RuntimeError("clickhouse is on fire")

    monkeypatch.setattr(columns_jobs, "ensure_source_field_stats", _boom)
    await _run(store, case_id, timeline_id)

    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns == good


# ── The advisor hand-off ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_advisor_result_wins_when_it_validates(store, monkeypatch):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)

    from vestigo.columns.advisor import AdvisorResult

    async def _advise(candidates, **kwargs):
        return AdvisorResult(
            columns=["status", "user", "host"],
            reasons={"status": "outcome at a glance"},
            model="test-model",
        )

    monkeypatch.setattr("vestigo.columns.advisor.rank_columns_with_llm", _advise)
    await _run(store, case_id, timeline_id)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert recommended["method"] == "llm"
    assert recommended["model"] == "test-model"
    assert recommended["columns"] == ["timestamp", "status", "user", "host"]
    # The model's wording leads; the verifiable statistics stay attached.
    assert recommended["reasons"]["status"].startswith("outcome at a glance — ")
    assert "1/1 source" in recommended["reasons"]["status"]


@pytest.mark.asyncio
async def test_advisor_declining_leaves_the_heuristic_answer(store, monkeypatch):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)

    async def _decline(candidates, **kwargs):
        return None

    monkeypatch.setattr("vestigo.columns.advisor.rank_columns_with_llm", _decline)
    await _run(store, case_id, timeline_id)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert recommended["method"] == "heuristic"
    assert recommended["status"] == "ok"


@pytest.mark.asyncio
async def test_advisor_is_not_consulted_in_heuristic_mode(store, monkeypatch):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    monkeypatch.setenv("VESTIGO_COLUMN_RECOMMEND_MODE", "heuristic")
    get_settings.cache_clear()

    called = False

    async def _advise(candidates, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr("vestigo.columns.advisor.rank_columns_with_llm", _advise)
    try:
        await _run(store, case_id, timeline_id)
    finally:
        get_settings.cache_clear()

    assert called is False
    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns["status"] == "ok"


# ── The advisor's own validation ────────────────────────────────────────────


def _candidates(*tokens):
    from vestigo.columns.recommend import ColumnCandidate

    return [
        ColumnCandidate(
            token=t,
            score=0.9,
            breadth=1.0,
            fill=1.0,
            distinct=10,
            coverage=1000,
            sources_present=1,
            sources_total=1,
            samples=("a", "b", "c"),
            reason="test",
        )
        for t in tokens
    ]


def test_advisor_validation_drops_invented_tokens():
    from vestigo.columns.advisor import ColumnChoice, _validate

    choice = ColumnChoice(columns=["user", "made_up", "host", "src_ip"], reasons={})
    columns, _ = _validate(choice, _candidates("user", "host", "src_ip"), k_min=3, k_max=5)
    assert columns == ["user", "host", "src_ip"]


def test_advisor_validation_rejects_a_response_that_falls_below_the_minimum():
    from vestigo.columns.advisor import ColumnChoice, _validate

    choice = ColumnChoice(columns=["made_up", "also_fake", "user"], reasons={})
    assert _validate(choice, _candidates("user", "host"), k_min=3, k_max=5) is None


def test_advisor_validation_collapses_duplicates_and_caps_at_k_max():
    from vestigo.columns.advisor import ColumnChoice, _validate

    choice = ColumnChoice(columns=["a", "a", "b", "c", "d", "e", "f"], reasons={})
    columns, _ = _validate(choice, _candidates("a", "b", "c", "d", "e", "f"), k_min=3, k_max=5)
    assert columns == ["a", "b", "c", "d", "e"]


def test_advisor_validation_keeps_reasons_only_for_chosen_columns():
    from vestigo.columns.advisor import ColumnChoice, _validate

    choice = ColumnChoice(
        columns=["a", "b", "c"],
        reasons={"a": "who", "zzz": "not chosen", "b": "   "},
    )
    _, reasons = _validate(choice, _candidates("a", "b", "c"), k_min=3, k_max=5)
    assert reasons == {"a": "who"}


# ── The endpoint ────────────────────────────────────────────────────────────


def test_recommend_endpoint_starts_a_job(client, admin_bootstrap, monkeypatch):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases", json={"name": "Columns"}).json()["case"]["id"]
    timeline_id = client.get(f"/api/cases/{case_id}/timelines").json()["timelines"][0]["id"]

    resp = client.post(f"/api/cases/{case_id}/timelines/{timeline_id}/recommend-columns")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["job_id"]


def test_recommend_endpoint_404s_on_an_unknown_timeline(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases", json={"name": "Columns"}).json()["case"]["id"]

    resp = client.post(f"/api/cases/{case_id}/timelines/nope/recommend-columns")

    assert resp.status_code == 404


def test_recommend_endpoint_is_off_when_the_setting_is_off(client, admin_bootstrap, monkeypatch):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases", json={"name": "Columns"}).json()["case"]["id"]
    timeline_id = client.get(f"/api/cases/{case_id}/timelines").json()["timelines"][0]["id"]
    monkeypatch.setenv("VESTIGO_COLUMN_RECOMMEND_MODE", "off")
    get_settings.cache_clear()

    try:
        body = client.post(f"/api/cases/{case_id}/timelines/{timeline_id}/recommend-columns").json()
    finally:
        get_settings.cache_clear()

    assert body["enabled"] is False
    assert body["job_id"] is None


def test_recommend_endpoint_needs_contribute_access(client, admin_bootstrap, store):
    """A read-only member must not change what the timeline opens on for everyone."""
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases", json={"name": "Columns"}).json()["case"]["id"]
    timeline_id = client.get(f"/api/cases/{case_id}/timelines").json()["timelines"][0]["id"]
    client.post(
        "/api/admin/users",
        json={"username": "reader", "password": "reader-pass-123", "is_admin": False},
    )
    client.post(
        f"/api/cases/{case_id}/members",
        json={"username": "reader", "access_level": "read"},
    )
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "reader", "password": "reader-pass-123"})

    resp = client.post(f"/api/cases/{case_id}/timelines/{timeline_id}/recommend-columns")

    assert resp.status_code == 403


# ── Scheduling ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduling_is_skipped_while_one_is_already_running(store, monkeypatch):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    job_store = JobStore()
    monkeypatch.setitem(columns_jobs._ACTIVE, timeline_id, "already-running")

    job_id = columns_jobs.start_column_recommendation(
        case_id=case_id,
        timeline_id=timeline_id,
        job_store=job_store,
        store=store,
        ch_store=_NEVER_USED,
    )

    assert job_id is None
    # No orphan job left in the tray for a run that never dispatched.
    assert job_store.list_by_case(case_id) == []


@pytest.mark.asyncio
async def test_scheduling_is_skipped_when_the_setting_is_off(store, monkeypatch):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    monkeypatch.setenv("VESTIGO_COLUMN_RECOMMEND_MODE", "off")
    get_settings.cache_clear()
    job_store = JobStore()

    try:
        job_id = columns_jobs.start_column_recommendation(
            case_id=case_id,
            timeline_id=timeline_id,
            job_store=job_store,
            store=store,
            ch_store=None,
        )
    finally:
        get_settings.cache_clear()

    assert job_id is None
    assert job_store.list_by_case(case_id) == []
