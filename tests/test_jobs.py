"""Tests for the in-memory job store."""

from __future__ import annotations

from tracesignal.core.jobs import JobStore


def test_create_returns_job_with_progress() -> None:
    store = JobStore()
    job = store.create("embed", progress={"total": 0, "processed": 0})
    assert job.id
    assert job.kind == "embed"
    assert job.status == "queued"
    assert job.progress == {"total": 0, "processed": 0}


def test_get_returns_job() -> None:
    store = JobStore()
    job = store.create("embed")
    fetched = store.get(job.id)
    assert fetched is not None
    assert fetched.id == job.id


def test_get_missing_returns_none() -> None:
    store = JobStore()
    assert store.get("not-a-job") is None


def test_update_changes_status_and_progress() -> None:
    store = JobStore()
    job = store.create("embed")
    store.update(job.id, status="running", progress={"total": 10, "processed": 5})
    fetched = store.get(job.id)
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.progress == {"total": 10, "processed": 5}


def test_update_returns_none_for_missing_job() -> None:
    store = JobStore()
    assert store.update("missing", status="running") is None


def test_to_dict_is_serializable() -> None:
    store = JobStore()
    job = store.create("embed", progress={"total": 1, "processed": 1})
    store.update(job.id, status="completed", result={"vectors_inserted": 42})
    data = store.get(job.id).to_dict()
    assert data["status"] == "completed"
    assert data["result"] == {"vectors_inserted": 42}


def test_terminal_jobs_evicted_beyond_cap_oldest_first() -> None:
    store = JobStore(max_terminal=5)
    jobs = [store.create("embed") for _ in range(10)]
    for job in jobs:
        store.update(job.id, status="completed")
    assert [store.get(j.id) for j in jobs[:5]] == [None] * 5
    assert all(store.get(j.id) is not None for j in jobs[5:])


def test_running_jobs_never_evicted() -> None:
    store = JobStore(max_terminal=5)
    running = [store.create("ingest") for _ in range(3)]
    for job in running:
        store.update(job.id, status="running")
    for _ in range(10):
        job = store.create("embed")
        store.update(job.id, status="completed")
    assert all(store.get(j.id) is not None for j in running)


def test_failed_and_completed_share_cap_in_completion_order() -> None:
    store = JobStore(max_terminal=2)
    a = store.create("a")
    b = store.create("b")
    c = store.create("c")
    store.update(b.id, status="failed")
    store.update(a.id, status="completed")
    store.update(c.id, status="failed")
    # b finished first, so b is the one evicted.
    assert store.get(b.id) is None
    assert store.get(a.id) is not None
    assert store.get(c.id) is not None


def test_list_by_case_filters_and_orders_newest_first() -> None:
    store = JobStore()
    other_case = store.create("ingest", case_id="case-b")
    first = store.create("ingest", case_id="case-a")
    second = store.create("embed", case_id="case-a")
    caseless = store.create("embed")
    first.created_at, second.created_at = 1.0, 2.0

    jobs = store.list_by_case("case-a")
    assert [j.id for j in jobs] == [second.id, first.id]
    assert other_case.id not in [j.id for j in jobs]
    assert caseless.id not in [j.id for j in jobs]


def test_list_by_case_empty_when_no_match() -> None:
    store = JobStore()
    store.create("ingest", case_id="case-a")
    assert store.list_by_case("case-b") == []


def test_repeated_terminal_updates_do_not_double_count() -> None:
    store = JobStore(max_terminal=2)
    job = store.create("a")
    store.update(job.id, status="completed")
    store.update(job.id, status="completed")
    store.update(job.id, status="failed")
    other = store.create("b")
    store.update(other.id, status="completed")
    # Cap of 2 with only 2 distinct terminal jobs: both must survive.
    assert store.get(job.id) is not None
    assert store.get(other.id) is not None
