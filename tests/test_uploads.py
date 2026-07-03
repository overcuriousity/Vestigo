"""Tests for source upload idempotency, forensic hashing, and the background
ingestion job (upload returns a job id; events land when the job runs)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

from tests.conftest import _fake_user
from tracesignal.api import deps
from tracesignal.api.routers import cases
from tracesignal.api.routers.cases import upload_source
from tracesignal.core.config import get_settings
from tracesignal.core.jobs import get_job_store
from tracesignal.db.postgres import PostgresStore
from tracesignal.ingestion.files import UploadTooLargeError, copy_and_hash, hash_file
from tracesignal.ingestion.pipeline import IngestionResult


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


@pytest.mark.asyncio
async def test_oversized_upload_rejected_with_413(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upload larger than TS_MAX_UPLOAD_BYTES is rejected mid-stream and
    creates neither a source row nor a job."""
    monkeypatch.setenv("TS_MAX_UPLOAD_BYTES", "8")
    get_settings.cache_clear()
    try:
        case_obj = await store.get_case(case)
        with pytest.raises(HTTPException) as exc_info:
            await _upload(case_obj, "big.jsonl", b'{"message":"way too large"}\n')
        assert exc_info.value.status_code == 413
        assert await store.list_sources(case) == []
    finally:
        get_settings.cache_clear()


def test_copy_and_hash_matches_hash_file_and_caps(tmp_path: Path) -> None:
    """copy_and_hash produces the same digest as hash_file and enforces max_bytes."""
    content = b"line one\nline two\n"
    src_path = tmp_path / "src.log"
    src_path.write_bytes(content)

    dst = BytesIO()
    digest, size = copy_and_hash(BytesIO(content), dst)
    assert size == len(content)
    assert dst.getvalue() == content
    assert digest == hash_file(src_path)

    with pytest.raises(UploadTooLargeError):
        copy_and_hash(BytesIO(content), BytesIO(), max_bytes=len(content) - 1)


@pytest.mark.asyncio
async def test_source_status_lifecycle(
    store: PostgresStore,
    case: str,
) -> None:
    """A source is created "ingesting" and flips to "ready" when the job
    completes — only then does it become visible to timeline queries."""
    case_obj = await store.get_case(case)

    # Call upload_source without running the scheduled background job yet, so
    # the mid-ingest state is observable.
    background_tasks = BackgroundTasks()
    response = await upload_source(
        background_tasks=background_tasks,
        file=_UploadFile("events.jsonl", b'{"message":"x"}\n'),
        parser=None,
        case=case_obj,
        user=_fake_user(),
    )
    source = await store.get_source(case, response.source_id)
    assert source.status == "ingesting"

    await background_tasks()
    source = await store.get_source(case, response.source_id)
    assert source.status == "ready"


@pytest.mark.asyncio
async def test_ingesting_source_excluded_from_timeline_scope(
    store: PostgresStore,
    case: str,
) -> None:
    """_resolve_timeline_scope must never return a half-ingested source."""
    from tracesignal.api.routers.events import _resolve_timeline_scope

    await store.create_source(case, "s_ready", "ready one", file_hash="h1", size_bytes=1)
    await store.create_source(
        case, "s_pending", "pending one", file_hash="h2", size_bytes=1, status="ingesting"
    )
    default_timeline = await store.get_default_timeline(case)
    await store.add_source_to_timeline(case, default_timeline.id, "s_ready")
    await store.add_source_to_timeline(case, default_timeline.id, "s_pending")

    source_ids, _ = await _resolve_timeline_scope(case, default_timeline.id)
    assert source_ids == ["s_ready"]


@pytest.mark.asyncio
async def test_embed_refuses_ingesting_sources(
    store: PostgresStore,
    case: str,
) -> None:
    """Embedding persists vectors — it must refuse a timeline with a
    half-ingested member instead of silently embedding partial data."""
    from tracesignal.api.routers.cases import start_timeline_embedding

    await store.create_source(
        case, "s_pending", "pending one", file_hash="h2", size_bytes=1, status="ingesting"
    )
    default_timeline = await store.get_default_timeline(case)
    await store.add_source_to_timeline(case, default_timeline.id, "s_pending")

    case_obj = await store.get_case(case)
    with pytest.raises(HTTPException) as exc_info:
        await start_timeline_embedding(
            timeline_id=default_timeline.id,
            background_tasks=BackgroundTasks(),
            body=None,
            case=case_obj,
            user=_fake_user(),
        )
    assert exc_info.value.status_code == 409
    assert "still ingesting" in exc_info.value.detail


@pytest.mark.asyncio
async def test_startup_reconciliation_removes_orphaned_ingests(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source stuck in "ingesting" on boot (in-memory job lost to a restart)
    is cleaned up like a failed ingest, so the file can be re-uploaded."""
    from tracesignal.api import main as api_main

    await store.create_source(
        case, "s_orphan", "orphan", file_hash="h9", size_bytes=1, status="ingesting"
    )

    deleted: list[tuple[str, str]] = []

    class FakeClickHouse:
        def delete_source_events(self, case_id: str, source_id: str) -> None:
            deleted.append((case_id, source_id))

    monkeypatch.setattr(api_main, "ClickHouseStore", FakeClickHouse, raising=False)
    monkeypatch.setattr("tracesignal.db.clickhouse.ClickHouseStore", FakeClickHouse)

    await api_main._reconcile_orphaned_ingests()

    assert deleted == [(case, "s_orphan")]
    assert await store.get_source(case, "s_orphan") is None
    assert await store.list_ingesting_sources() == []
