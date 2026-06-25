"""Tests for the in-memory job store."""

from __future__ import annotations

from tracevector.core.jobs import JobStore


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
