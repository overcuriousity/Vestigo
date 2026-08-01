"""Tests for the column-recommendation job and its endpoint (issue #213).

The field-stats cache is seeded directly in Postgres so the job's read path
(``ensure_source_field_stats``) is a pure cache hit — no ClickHouse needed.
The LLM advisor is monkeypatched at the job's call site; the real one is
gated on ``agent_available()`` and never runs in the suite.

There is no instance-wide mode to pin: the advisor is reached only when a
caller passes ``use_llm=True``, which is the analyst's per-timeline opt-in and
nothing else.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import as_admin
from vestigo.api.routers.cases import _settle_dead_recommendations
from vestigo.columns import jobs as columns_jobs
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


@pytest.mark.asyncio
async def test_a_rerun_keeps_the_previous_columns_while_it_runs(store, monkeypatch):
    """The 'running' placeholder carries the old answer, so the grid holds still.

    It is also what makes a mid-job restart survivable: the payload a crash
    leaves behind is still showable, and the startup sweep only relabels it.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    seen: dict[str, Any] = {}
    real = columns_jobs.ensure_source_field_stats

    async def _peek(*args, **kwargs):
        timeline = await store.get_timeline(case_id, timeline_id)
        seen.update(timeline.recommended_columns)
        return await real(*args, **kwargs)

    monkeypatch.setattr(columns_jobs, "ensure_source_field_stats", _peek)
    await _run(store, case_id, timeline_id)

    assert seen["status"] == "running"
    assert seen["columns"] == good["columns"]
    assert seen["reasons"] == good["reasons"]


@pytest.mark.asyncio
async def test_a_failure_after_the_payload_write_keeps_the_new_answer(store, monkeypatch):
    """Only this job's own placeholder is rolled back, never a committed result."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    first = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    async def _boom(*args, **kwargs):
        raise RuntimeError("audit log is unavailable")

    monkeypatch.setattr(store, "record_audit", _boom)
    job_store = await _run(store, case_id, timeline_id)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert recommended["status"] == "ok"
    assert recommended["columns"] == first["columns"]
    # The audit failure is still a job failure — it just costs no data.
    assert job_store.list_by_case(case_id)[0].status == "failed"


@pytest.mark.asyncio
async def test_the_job_releases_its_active_slot_on_every_exit(store):
    """A leaked `_ACTIVE` entry wedges the timeline: no job can ever start again.

    The missing timeline is the cheapest early return the job has; it must
    still go through the `finally` that releases the slot.
    """
    case_id, _, _ = await _seed_case_with_stats(store)

    job_store = await _run(store, case_id, "no-such-timeline")

    assert columns_jobs.get_active_recommendation("no-such-timeline") is None
    assert job_store.list_by_case(case_id)[0].status == "failed"


@pytest.mark.asyncio
async def test_a_second_job_on_the_same_timeline_stands_down(store, monkeypatch):
    """The `_ACTIVE` claim is the guard for every caller, not only the API path.

    The CLI and the demo build call the job directly, so a check that only
    `start_column_recommendation` performed would leave those two able to run
    two jobs over one timeline — trading writes, and each rolling back a
    placeholder the other owns.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    monkeypatch.setitem(columns_jobs._ACTIVE, timeline_id, "job-already-running")
    job_store = await _run(store, case_id, timeline_id)

    # Neither the payload nor the other job's claim was touched.
    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns == good
    assert columns_jobs.get_active_recommendation(timeline_id) == "job-already-running"
    job = job_store.list_by_case(case_id)[0]
    assert job.status == "completed"
    assert job.result == {"skipped": True, "running": "job-already-running"}


@pytest.mark.asyncio
async def test_the_default_run_never_reaches_the_advisor(store, monkeypatch):
    """Ingest, timeline creation, the CLI and the demo all rely on this default.

    Egress is never a side effect of uploading a file: only an explicit
    ``use_llm=True`` — the analyst's per-timeline opt-in — consults the model.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)

    async def _never(candidates, **kwargs):
        raise AssertionError("the advisor must not be consulted")

    monkeypatch.setattr("vestigo.columns.advisor.rank_columns_with_llm", _never)
    await _run(store, case_id, timeline_id)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert recommended["status"] == "ok"
    assert recommended["method"] == "heuristic"


# ── Recovering from a restart ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_restart_settles_a_running_recommendation(store):
    """`JobStore` is in-memory: a job that died has to be settled, not awaited."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    orphaned = dict(good, status="running", job_id="job-that-died")
    await store.update_timeline_recommended_columns(case_id, timeline_id, orphaned)

    assert await store.clear_stale_running_recommendations() == 1

    settled = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert settled["status"] == "ok"
    assert settled["job_id"] is None
    assert settled["columns"] == good["columns"]


@pytest.mark.asyncio
async def test_a_restart_settles_an_empty_running_recommendation_as_insufficient(store):
    """A first run that never finished has nothing to show — say so and stop."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await store.update_timeline_recommended_columns(
        case_id,
        timeline_id,
        {"status": "running", "columns": [], "reasons": {}, "job_id": "job-that-died"},
    )

    assert await store.clear_stale_running_recommendations() == 1
    settled = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert settled["status"] == "insufficient"


@pytest.mark.asyncio
async def test_a_finished_recommendation_carries_no_placeholder_timestamp(store):
    """`columns_generated_at` exists for placeholders only — a finished answer
    was derived at its own `generated_at`, and a second key would be a second
    thing to keep true."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert "columns_generated_at" not in recommended


@pytest.mark.asyncio
async def test_a_settled_recommendation_dates_its_columns_honestly(store):
    """Settling relabels a placeholder; it must not re-date the columns in it.

    The placeholder's `generated_at` is the *recompute's* start — the clock the
    explorer measures its staleness floor against — while the columns it
    carries are the previous run's. A settled payload that kept the recompute's
    timestamp would claim an answer was derived by a run that never finished,
    in the one record a case export and the audit trail carry forward.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    # The placeholder exactly as a recompute writes it, then killed mid-flight.
    orphaned = columns_jobs._payload(
        status="running",
        source_ids=good["source_ids"],
        job_id="job-that-died",
        **columns_jobs._carry_forward(good),
    )
    assert orphaned["generated_at"] >= good["generated_at"]
    await store.update_timeline_recommended_columns(case_id, timeline_id, orphaned)

    assert await store.clear_stale_running_recommendations() == 1

    settled = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert settled["status"] == "ok"
    assert settled["columns"] == good["columns"]
    assert settled["generated_at"] == good["generated_at"]
    # Restored, not merely stored: the settled payload is a finished answer
    # again and reads like one.
    assert "columns_generated_at" not in settled


@pytest.mark.asyncio
async def test_two_crashes_in_a_row_do_not_walk_the_timestamp_forward(store):
    """Each carry-forward reads the previous *columns* timestamp, not the last
    placeholder's, so repeated failed recomputes cannot age an answer into
    looking fresh."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    payload = good
    for job_id in ("died-once", "died-twice"):
        payload = columns_jobs._payload(
            status="running",
            source_ids=good["source_ids"],
            job_id=job_id,
            **columns_jobs._carry_forward(payload),
        )

    assert columns_jobs.settle_running_payload(payload)["generated_at"] == good["generated_at"]


@pytest.mark.asyncio
async def test_a_restart_leaves_settled_recommendations_alone(store):
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    assert await store.clear_stale_running_recommendations() == 0
    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns == good


@pytest.mark.asyncio
async def test_reading_a_timeline_settles_a_recommendation_whose_job_is_gone(store):
    """A cancelled task leaves `running` behind without restarting the process.

    The boot sweep never sees that one, and the explorer polls on the word
    `running` — so the read path has to settle it or the timeline claims to be
    thinking forever.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    await store.update_timeline_recommended_columns(
        case_id, timeline_id, dict(good, status="running", job_id="job-that-died")
    )

    timeline = await store.get_timeline(case_id, timeline_id)
    await _settle_dead_recommendations(store, case_id, [timeline])

    assert timeline.recommended_columns["status"] == "ok"
    assert timeline.recommended_columns["job_id"] is None
    # Persisted, not only patched in memory — the next reader must agree.
    stored = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert stored["status"] == "ok"
    assert stored["columns"] == good["columns"]


@pytest.mark.asyncio
async def test_reading_a_timeline_leaves_a_live_recommendation_running(store):
    """A job genuinely in flight must not be settled out from under itself."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    await store.update_timeline_recommended_columns(
        case_id, timeline_id, dict(good, status="running", job_id="job-in-flight")
    )

    columns_jobs._ACTIVE[timeline_id] = "job-in-flight"
    try:
        timeline = await store.get_timeline(case_id, timeline_id)
        await _settle_dead_recommendations(store, case_id, [timeline])
    finally:
        columns_jobs._ACTIVE.pop(timeline_id, None)

    assert timeline.recommended_columns["status"] == "running"


@pytest.mark.asyncio
async def test_reading_a_timeline_respects_a_job_the_store_still_knows(store, monkeypatch):
    """`_ACTIVE` is cleared before the job's last write lands; the tray is the backstop."""
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns

    job_store = JobStore()
    job = job_store.create(kind=columns_jobs.JOB_KIND, case_id=case_id)
    await store.update_timeline_recommended_columns(
        case_id, timeline_id, dict(good, status="running", job_id=job.id)
    )
    monkeypatch.setattr("vestigo.api.routers.cases.get_job_store", lambda: job_store)

    timeline = await store.get_timeline(case_id, timeline_id)
    await _settle_dead_recommendations(store, case_id, [timeline])

    assert timeline.recommended_columns["status"] == "running"


@pytest.mark.asyncio
async def test_listing_timelines_settles_a_recommendation_whose_job_is_gone(store):
    """The list has to agree with the single read, or a lister never sees it resolve."""
    from vestigo.api.routers.cases import list_timelines

    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    await store.update_timeline_recommended_columns(
        case_id, timeline_id, dict(good, status="running", job_id="job-that-died")
    )

    case = await store.get_case(case_id)
    listed = await list_timelines(case=case)

    payload = next(t["recommended_columns"] for t in listed["timelines"] if t["id"] == timeline_id)
    assert payload["status"] == "ok"
    assert payload["job_id"] is None
    stored = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert stored["status"] == "ok"


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
    await _run(store, case_id, timeline_id, use_llm=True)

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
    await _run(store, case_id, timeline_id, use_llm=True)

    recommended = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    assert recommended["method"] == "heuristic"
    assert recommended["status"] == "ok"


# ── What actually crosses the wire ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_prompt_carries_the_candidate_table_and_nothing_else(store):
    """Every privacy claim the disclosure dialog makes rests on this function.

    The dialog tells an analyst that the request carries field names, coverage
    statistics and up to three truncated sample values — and no event rows, no
    case/source/timeline identifiers and no credentials. That promise is only
    as good as `_format_candidates`, so assert it directly against candidates
    scored from a real seeded case.
    """
    from vestigo.columns.advisor import MAX_CANDIDATES_IN_PROMPT, _format_candidates
    from vestigo.columns.recommend import score_columns
    from vestigo.db.field_stats import ensure_source_field_stats

    long_value = "x" * 200
    case_id, timeline_id, source_id = await _seed_case_with_stats(
        store, attributes=dict(RICH_ATTRIBUTES, note=_attr(1000, 9, [long_value, "b", "c"]))
    )
    stats = await ensure_source_field_stats(store, _NEVER_USED, case_id, [source_id])
    candidates = score_columns(stats)

    rendered = _format_candidates(candidates[:MAX_CANDIDATES_IN_PROMPT])

    assert "user" in rendered
    assert "alice" in rendered
    for identifier in (case_id, timeline_id, source_id):
        assert identifier not in rendered
    # Samples are truncated at 40 characters, so the oversized value can only
    # appear as a prefix of itself — never whole.
    assert long_value not in rendered
    assert "x" * 40 in rendered
    assert "x" * 41 not in rendered


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


def test_recommend_endpoint_starts_a_local_job_by_default(client, admin_bootstrap):
    """No body means no AI: the plain "Re-suggest" button must not send anything."""
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "Columns"}).json()["case"]["id"]
    timeline_id = client.get(f"/api/cases/{case_id}/timelines").json()["timelines"][0]["id"]

    resp = client.post(f"/api/cases/{case_id}/timelines/{timeline_id}/recommend-columns")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["use_ai"] is False
    assert body["job_id"]


def test_recommend_endpoint_passes_the_opt_in_through(client, admin_bootstrap, monkeypatch):
    """`use_ai` is the whole opt-in; it has to reach the job, not stop at the router."""
    seen: dict[str, Any] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return "job-1"

    monkeypatch.setattr("vestigo.api.routers.cases.start_column_recommendation", _capture)
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "Columns"}).json()["case"]["id"]
    timeline_id = client.get(f"/api/cases/{case_id}/timelines").json()["timelines"][0]["id"]

    resp = client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/recommend-columns",
        json={"use_ai": True},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["use_ai"] is True
    assert seen["use_llm"] is True


def test_recommend_endpoint_404s_on_an_unknown_timeline(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "Columns"}).json()["case"]["id"]

    resp = client.post(f"/api/cases/{case_id}/timelines/nope/recommend-columns")

    assert resp.status_code == 404


def test_recommend_endpoint_needs_contribute_access(client, admin_bootstrap, store):
    """A read-only member must not change what the timeline opens on for everyone."""
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "Columns"}).json()["case"]["id"]
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


@pytest.mark.asyncio
async def test_listing_settles_a_dead_recommendation_without_an_audit_row(store):
    """The list endpoint writes from a `require_case_read` dependency, deliberately.

    A read-only member cannot re-run the job, so without this they are the one
    caller left watching "suggesting columns…" forever. What makes that safe is
    how bounded the write is, and that is what this locks down: a `running`
    payload whose job is provably gone is *relabelled* — same columns, no
    recompute, no audit row. An audit trail that recorded "read-only user
    changed a timeline" every time a process restarted would be describing
    something that did not happen.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    await _run(store, case_id, timeline_id)
    good = (await store.get_timeline(case_id, timeline_id)).recommended_columns
    await store.update_timeline_recommended_columns(
        case_id, timeline_id, dict(good, status="running", job_id="job-that-died")
    )
    audit_before = len(await store.query_audit(case_id=case_id))

    timelines = await store.list_timelines(case_id)
    await _settle_dead_recommendations(store, case_id, timelines)

    # Serialized straight off the mutated objects — no second read needed.
    settled = next(t for t in timelines if t.id == timeline_id).recommended_columns
    assert settled["status"] == "ok"
    assert settled["job_id"] is None
    assert settled["columns"] == good["columns"]
    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns == settled
    assert len(await store.query_audit(case_id=case_id)) == audit_before


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
async def test_a_failed_spawn_hands_the_active_slot_back(store, monkeypatch):
    """A claim taken for a job that never starts would wedge the timeline forever.

    `start_column_recommendation` claims `_ACTIVE` *before* spawning, and the
    claim is normally released by the job's own `finally` — which a coroutine
    that was never scheduled never reaches. A leak here is not a slow job:
    `_recommendation_is_dead` reads an active claim as proof the job is alive,
    so the timeline would report `running` for the life of the process and
    every later recommendation for it would be skipped as a duplicate.
    """
    case_id, timeline_id, _ = await _seed_case_with_stats(store)
    job_store = JobStore()

    def _boom(coro):
        coro.close()
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(columns_jobs, "spawn_tracked_column_task", _boom)

    with pytest.raises(RuntimeError):
        columns_jobs.start_column_recommendation(
            case_id=case_id,
            timeline_id=timeline_id,
            job_store=job_store,
            store=store,
            ch_store=_NEVER_USED,
        )

    assert columns_jobs.get_active_recommendation(timeline_id) is None
    # And the tray says what happened rather than holding a job stuck at
    # "queued" that nothing will ever advance.
    assert job_store.list_by_case(case_id)[0].status == "failed"

    # The real proof: the next attempt is not turned away as a duplicate.
    monkeypatch.undo()
    await _run(store, case_id, timeline_id)
    assert (await store.get_timeline(case_id, timeline_id)).recommended_columns["status"] == "ok"


@pytest.mark.asyncio
async def test_scheduling_defaults_to_a_local_run(store, monkeypatch):
    """`schedule_for_source` is the post-ingest path — it must never opt in for anyone."""
    case_id, timeline_id, source_id = await _seed_case_with_stats(store)
    seen: dict[str, Any] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return "job-1"

    monkeypatch.setattr(columns_jobs, "start_column_recommendation", _capture)
    await columns_jobs.schedule_for_source(store, _NEVER_USED, JobStore(), case_id, source_id)

    assert seen["timeline_id"] == timeline_id
    assert seen.get("use_llm", False) is False
