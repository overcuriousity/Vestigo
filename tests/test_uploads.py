"""Tests for source upload idempotency, forensic hashing, and the background
ingestion job (upload returns a job id; events land when the job runs)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

from tests.conftest import _fake_user
from tracevector.api import deps
from tracevector.api.routers import cases
from tracevector.api.routers.cases import upload_source
from tracevector.core.jobs import get_job_store
from tracevector.db.postgres import PostgresStore
from tracevector.ingestion.files import hash_file
from tracevector.ingestion.pipeline import IngestionResult


class _UploadFile:
    """Minimal stand-in for FastAPI's UploadFile."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.file = BytesIO(content)


class FakeIngestionPipeline:
    """In-memory ingestion pipeline that counts non-empty JSONL lines."""

    def __init__(
        self,
        case_id: str,
        source_id: str,
        clickhouse=None,
        batch_size: int | None = None,
        file_hash: str | None = None,
        source_name: str | None = None,
        progress_callback=None,
    ) -> None:
        self.case_id = case_id
        self.source_id = source_id
        self.clickhouse = clickhouse
        self.file_hash = file_hash
        self.source_name = source_name
        self.progress_callback = progress_callback

    def run(self, path: Path, format_name: str | None = None) -> IngestionResult:
        data = path.read_bytes()
        if self.progress_callback is not None:
            self.progress_callback(total=len(data), processed=len(data))
        lines = [line for line in data.split(b"\n") if line.strip()]
        return IngestionResult(
            case_id=self.case_id,
            source_id=self.source_id,
            files=[path],
            events_parsed=len(lines),
            events_inserted=len(lines),
        )


class FailingIngestionPipeline(FakeIngestionPipeline):
    """Ingestion pipeline that always fails, to exercise job cleanup."""

    def run(self, path: Path, format_name: str | None = None) -> IngestionResult:
        raise RuntimeError("parse exploded")


async def _upload(case_obj, filename: str, content: bytes, parser: str | None = None):
    """Call upload_source with a fresh BackgroundTasks, run the scheduled
    ingestion job (as the server would after the response), and return the
    response."""
    background_tasks = BackgroundTasks()
    response = await upload_source(
        background_tasks=background_tasks,
        file=_UploadFile(filename, content),
        parser=parser,
        case=case_obj,
        user=_fake_user(),
    )
    await background_tasks()
    return response


@pytest_asyncio.fixture()
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PostgresStore:
    """In-memory SQLite store wired into the cases router for upload tests."""
    db_path = tmp_path / "test_uploads.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    # get_store() is shared across every router via api.deps now.
    monkeypatch.setattr(deps, "_store", s)
    monkeypatch.setattr(cases, "IngestionPipeline", FakeIngestionPipeline)
    yield s
    await s.engine.dispose()


@pytest_asyncio.fixture()
async def case(store: PostgresStore) -> str:
    c = await store.create_case("case1", "Test case")
    return c.id


@pytest.mark.asyncio
async def test_duplicate_upload_is_idempotent(
    store: PostgresStore,
    case: str,
) -> None:
    content = b'{"message":"login","timestamp":"2024-01-01T00:00:00+00:00"}\n'
    expected_hash = hash_file(BytesIO(content))
    case_obj = await store.get_case(case)

    first = await _upload(case_obj, "events.jsonl", content)
    assert first.duplicate is False
    assert first.job_id is not None

    job = get_job_store().get(first.job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.result == {
        "source_id": first.source_id,
        "events_parsed": 1,
        "events_inserted": 1,
        "parser": job.result["parser"],
    }

    source = await store.get_source_by_hash(case, expected_hash)
    assert source is not None
    assert source.filename == "events.jsonl"
    assert source.event_count == 1

    # Second upload of the same bytes must be a no-op.
    second = await _upload(case_obj, "events.jsonl", content)
    assert second.events_inserted == 0
    assert second.duplicate is True
    assert second.job_id is None
    assert second.events_parsed == 1

    assert len(await store.list_sources(case)) == 1


@pytest.mark.asyncio
async def test_uploading_different_file_adds_events(
    store: PostgresStore,
    case: str,
) -> None:
    first_content = b'{"message":"login","timestamp":"2024-01-01T00:00:00+00:00"}\n'
    second_content = b'{"message":"logout","timestamp":"2024-01-01T00:01:00+00:00"}\n'
    case_obj = await store.get_case(case)

    first = await _upload(case_obj, "first.jsonl", first_content)
    assert get_job_store().get(first.job_id).result["events_inserted"] == 1

    second = await _upload(case_obj, "second.jsonl", second_content)
    assert second.duplicate is False
    assert get_job_store().get(second.job_id).result["events_inserted"] == 1

    sources = await store.list_sources(case)
    assert len(sources) == 2
    assert sum(s.event_count for s in sources) == 2


@pytest.mark.asyncio
async def test_upload_to_missing_case_returns_404(store: PostgresStore) -> None:
    """Case existence/access is now enforced by the `require_case_contribute`
    dependency before the handler body runs, rather than inside the handler
    itself — exercise that dependency directly the way FastAPI would."""
    with pytest.raises(HTTPException) as exc_info:
        await deps.require_case_contribute(case_id="missing", user=_fake_user())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_source_added_to_default_timeline(
    store: PostgresStore,
    case: str,
) -> None:
    case_obj = await store.get_case(case)
    await _upload(case_obj, "events.jsonl", b'{"message":"x"}\n')
    default_timeline = await store.get_default_timeline(case)
    assert default_timeline is not None
    sources = await store.list_timeline_sources(case, default_timeline.id)
    assert len(sources) == 1


@pytest.mark.asyncio
async def test_failed_ingestion_marks_job_failed_and_removes_source(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashing ingest must not leave the up-front source row behind, and a
    re-upload of the same bytes must work (not be treated as a duplicate)."""
    monkeypatch.setattr(cases, "IngestionPipeline", FailingIngestionPipeline)
    monkeypatch.setattr(
        cases,
        "ClickHouseStore",
        lambda: type("CH", (), {"delete_source_events": staticmethod(lambda *a: None)})(),
    )
    case_obj = await store.get_case(case)
    content = b'{"message":"boom"}\n'

    response = await _upload(case_obj, "bad.jsonl", content)
    assert response.duplicate is False

    job = get_job_store().get(response.job_id)
    assert job.status == "failed"
    assert "parse exploded" in job.error
    assert await store.list_sources(case) == []

    # A retry of the same file is a fresh upload, not a duplicate.
    monkeypatch.setattr(cases, "IngestionPipeline", FakeIngestionPipeline)
    retry = await _upload(case_obj, "bad.jsonl", content)
    assert retry.duplicate is False
    assert get_job_store().get(retry.job_id).status == "completed"
