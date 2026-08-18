"""Tests for source upload idempotency, forensic hashing, and the background
ingestion job (upload returns a job id; events land when the job runs)."""

from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

from tests.conftest import _fake_user
from vestigo.api import deps
from vestigo.api.routers import cases
from vestigo.api.routers.cases import upload_source
from vestigo.core.config import get_settings
from vestigo.core.jobs import get_job_store
from vestigo.db.postgres import PostgresStore
from vestigo.ingestion.files import UploadTooLargeError, copy_and_hash, hash_file
from vestigo.ingestion.pipeline import IngestionResult


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
async def store(pg_database: str, monkeypatch: pytest.MonkeyPatch) -> PostgresStore:
    """A private PostgreSQL database wired into the cases router for upload tests."""
    s = PostgresStore(url=pg_database)
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
async def test_registration_failure_after_row_creation_rolls_the_row_back(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure between create_source and the timeline add must not leave an
    orphaned status='ingesting' row that every re-upload sees as a duplicate."""

    original = store.add_source_to_timeline
    fail = True

    async def flaky(*a, **k):
        if fail:
            raise RuntimeError("transient db error")
        return await original(*a, **k)

    monkeypatch.setattr(store, "add_source_to_timeline", flaky)
    case_obj = await store.get_case(case)
    with pytest.raises(RuntimeError, match="transient"):
        await _upload(case_obj, "events.jsonl", b'{"message":"x"}\n')
    assert await store.list_sources(case) == []
    fail = False
    # The same bytes now upload as new — not "duplicate, still ingesting".
    response = await _upload(case_obj, "events.jsonl", b'{"message":"x"}\n')
    assert response.duplicate is False and response.status == "ingesting"


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
    """An upload larger than VESTIGO_MAX_UPLOAD_BYTES is rejected mid-stream and
    creates neither a source row nor a job."""
    monkeypatch.setenv("VESTIGO_MAX_UPLOAD_BYTES", "8")
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
    from vestigo.api.routers.events import _resolve_timeline_scope

    await store.create_source(case, "s_ready", "ready one", file_hash="h1", size_bytes=1)
    await store.create_source(
        case, "s_pending", "pending one", file_hash="h2", size_bytes=1, status="ingesting"
    )
    default_timeline = await store.get_default_timeline(case)
    await store.add_source_to_timeline(case, default_timeline.id, "s_ready")
    await store.add_source_to_timeline(case, default_timeline.id, "s_pending")

    source_ids, _, _ = await _resolve_timeline_scope(case, default_timeline.id)
    assert source_ids == ["s_ready"]


@pytest.mark.asyncio
async def test_embed_refuses_ingesting_sources(
    store: PostgresStore,
    case: str,
) -> None:
    """Embedding persists vectors — it must refuse a timeline with a
    half-ingested member instead of silently embedding partial data."""
    from vestigo.api.routers.cases import start_timeline_embedding

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
    from vestigo.api import main as api_main

    await store.create_source(
        case, "s_orphan", "orphan", file_hash="h9", size_bytes=1, status="ingesting"
    )

    deleted: list[tuple[str, str]] = []

    class FakeClickHouse:
        def delete_source_events(self, case_id: str, source_id: str) -> None:
            deleted.append((case_id, source_id))

    monkeypatch.setattr(api_main, "ClickHouseStore", FakeClickHouse, raising=False)
    monkeypatch.setattr("vestigo.db.clickhouse.ClickHouseStore", FakeClickHouse)

    await api_main._reconcile_orphaned_ingests()

    assert deleted == [(case, "s_orphan")]
    assert await store.get_source(case, "s_orphan") is None
    assert await store.list_ingesting_sources() == []


class _FakeQdrant:
    def delete_source_points(self, case_id: str, source_id: str) -> None:
        pass

    def delete_case_collections(self, case_id: str) -> None:
        pass


class _BrokenClickHouse:
    def delete_source_events(self, case_id: str, source_id: str) -> None:
        raise RuntimeError("event store unreachable")


@pytest.mark.asyncio
async def test_source_delete_fails_closed_when_event_store_delete_fails(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ClickHouse delete must abort the request: the Postgres source
    row stays (delete remains visible and retryable), a source.delete_failed
    audit row is written, and no success audit exists."""
    monkeypatch.setattr(cases, "QdrantStore", _FakeQdrant)
    monkeypatch.setattr(cases, "ClickHouseStore", _BrokenClickHouse)
    await store.create_source(case, "s_del", "victim", file_hash="hd", size_bytes=1)
    case_obj = await store.get_case(case)

    with pytest.raises(HTTPException) as exc_info:
        await cases.delete_source(source_id="s_del", case=case_obj, user=_fake_user())

    assert exc_info.value.status_code == 502
    assert await store.get_source(case, "s_del") is not None
    assert await store.query_audit(case_id=case, action="source.delete_failed") != []
    assert await store.query_audit(case_id=case, action="source.delete") == []


@pytest.mark.asyncio
async def test_case_delete_fails_closed_when_event_store_delete_fails(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cases, "QdrantStore", _FakeQdrant)
    monkeypatch.setattr(cases, "ClickHouseStore", _BrokenClickHouse)
    await store.create_source(case, "s_keep", "victim", file_hash="hk", size_bytes=1)
    case_obj = await store.get_case(case)

    with pytest.raises(HTTPException) as exc_info:
        await cases.delete_case(case=case_obj, user=_fake_user())

    assert exc_info.value.status_code == 502
    assert await store.get_case(case) is not None
    assert await store.get_source(case, "s_keep") is not None
    assert await store.query_audit(case_id=case, action="case.delete_failed") != []
    assert await store.query_audit(case_id=case, action="case.delete") == []


@pytest.mark.asyncio
async def test_source_delete_succeeds_and_audits(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cases, "QdrantStore", _FakeQdrant)
    monkeypatch.setattr(
        cases,
        "ClickHouseStore",
        lambda: type("CH", (), {"delete_source_events": staticmethod(lambda *a: None)})(),
    )
    await store.create_source(case, "s_ok", "victim", file_hash="ho", size_bytes=1)
    case_obj = await store.get_case(case)

    response = await cases.delete_source(source_id="s_ok", case=case_obj, user=_fake_user())

    assert response == {"deleted": True, "source_id": "s_ok"}
    assert await store.get_source(case, "s_ok") is None
    assert await store.query_audit(case_id=case, action="source.delete") != []


class _ThreadRecordingQdrant:
    """Records which thread each blocking Qdrant call ran on."""

    threads: list[str] = []

    def delete_source_points(self, case_id: str, source_id: str) -> None:
        type(self).threads.append(threading.current_thread().name)

    def delete_case_collections(self, case_id: str) -> None:
        type(self).threads.append(threading.current_thread().name)


@pytest.mark.asyncio
async def test_case_delete_runs_qdrant_calls_off_the_event_loop(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Qdrant client is synchronous HTTP — calling it inline blocks the
    event loop for the whole cascade, exactly like the ClickHouse deletes it
    sits next to (which are already offloaded)."""
    _ThreadRecordingQdrant.threads = []
    monkeypatch.setattr(cases, "QdrantStore", _ThreadRecordingQdrant)
    monkeypatch.setattr(
        cases,
        "ClickHouseStore",
        lambda: type("CH", (), {"delete_source_events": staticmethod(lambda *a: None)})(),
    )
    await store.create_source(case, "s_thr", "victim", file_hash="ht", size_bytes=1)
    case_obj = await store.get_case(case)

    await cases.delete_case(case=case_obj, user=_fake_user())

    loop_thread = threading.current_thread().name
    assert _ThreadRecordingQdrant.threads  # both point + collection deletes ran
    assert loop_thread not in _ThreadRecordingQdrant.threads


@pytest.mark.asyncio
async def test_source_delete_runs_qdrant_call_off_the_event_loop(
    store: PostgresStore,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ThreadRecordingQdrant.threads = []
    monkeypatch.setattr(cases, "QdrantStore", _ThreadRecordingQdrant)
    monkeypatch.setattr(
        cases,
        "ClickHouseStore",
        lambda: type("CH", (), {"delete_source_events": staticmethod(lambda *a: None)})(),
    )
    await store.create_source(case, "s_thr2", "victim", file_hash="ht2", size_bytes=1)
    case_obj = await store.get_case(case)

    await cases.delete_source(source_id="s_thr2", case=case_obj, user=_fake_user())

    assert _ThreadRecordingQdrant.threads
    assert threading.current_thread().name not in _ThreadRecordingQdrant.threads


@pytest.mark.asyncio
async def test_receive_upload_to_tmp_413_cleans_up(tmp_path, monkeypatch):
    import tempfile

    from vestigo.api.uploads import receive_upload_to_tmp

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    upload = _UploadFile("big.bin", b"x" * 100)

    with pytest.raises(HTTPException) as exc_info:
        await receive_upload_to_tmp(upload, max_bytes=10, suffix=".bin")

    assert exc_info.value.status_code == 413
    assert list(tmp_path.iterdir()) == []  # temp file unlinked on failure


@pytest.mark.asyncio
async def test_receive_upload_to_tmp_returns_hash_and_size(tmp_path, monkeypatch):
    import hashlib
    import tempfile

    from vestigo.api.uploads import receive_upload_to_tmp

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    payload = b"hello world"
    upload = _UploadFile("ok.bin", payload)

    result_path, sha256, size = await receive_upload_to_tmp(upload, max_bytes=None, suffix=".bin")
    try:
        assert result_path.read_bytes() == payload
        assert sha256 == hashlib.sha256(payload).hexdigest()
        assert size == len(payload)
    finally:
        result_path.unlink(missing_ok=True)


def test_retain_file_hardlinks_same_filesystem(tmp_path: Path) -> None:
    src = tmp_path / "upload.tmp"
    src.write_bytes(b"evidence")
    dest = tmp_path / "ab" / "abcdef"

    cases._retain_file(src, dest)

    assert dest.read_bytes() == b"evidence"
    assert dest.stat().st_ino == src.stat().st_ino  # hardlink, no data copy
    # Unlinking the temp file (as the ingestion job does) keeps the retained copy.
    src.unlink()
    assert dest.read_bytes() == b"evidence"


def test_retain_file_falls_back_to_copy_on_exdev(tmp_path: Path, monkeypatch) -> None:
    import errno
    import os

    src = tmp_path / "upload.tmp"
    src.write_bytes(b"evidence")
    dest = tmp_path / "ab" / "abcdef"

    def _exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", _exdev)
    cases._retain_file(src, dest)

    assert dest.read_bytes() == b"evidence"
    assert dest.stat().st_ino != src.stat().st_ino


def test_retain_file_short_circuits_when_retained(tmp_path: Path, monkeypatch) -> None:
    import os

    src = tmp_path / "upload.tmp"
    src.write_bytes(b"evidence")
    dest = tmp_path / "ab" / "abcdef"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"evidence")  # content-addressed: existing == identical

    def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("no link/copy for an already-retained file")

    monkeypatch.setattr(os, "link", _fail)
    cases._retain_file(src, dest)
    assert dest.read_bytes() == b"evidence"


def _interchange_parquet_bytes() -> bytes:
    """Build a minimal valid Vestigo interchange parquet in memory."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from vestigo.ingestion import parquet_format

    row = {
        "source_file": "access.log",
        "file_hash": "a" * 64,
        "byte_offset": 0,
        "content_hash": "b" * 64,
        "message": "GET / 200",
        "timestamp": None,
        "timestamp_desc": "HTTP Request Time",
        "artifact": "nginx:access",
        "artifact_long": "web:access:request",
        "display_name": "",
        "tags": [],
        "attributes": {"status_code": "200"},
    }
    import json as _json

    meta = {
        parquet_format.META_FORMAT_VERSION: parquet_format.FORMAT_VERSION,
        parquet_format.META_CONVERTER_NAME: "nginx2vestigo",
        parquet_format.META_CONVERTER_VERSION: "1.0.0",
        parquet_format.META_ORIGINAL_FILES: _json.dumps(
            [{"name": "access.log", "sha256": "a" * 64, "size_bytes": 10}]
        ),
    }
    table = pa.Table.from_pylist(
        [row], schema=parquet_format.PARQUET_EVENT_SCHEMA.with_metadata(meta)
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


@pytest.mark.asyncio
async def test_parquet_upload_records_converter_identity(store: PostgresStore, case: str) -> None:
    case_obj = await store.get_case(case)
    response = await _upload(case_obj, "events.parquet", _interchange_parquet_bytes())
    # The Source's parser is the embedded converter identity, not the format string.
    assert response.parser == "nginx2vestigo@1.0.0"
    source = await store.get_source(case, response.source_id)
    assert source.parser == "nginx2vestigo@1.0.0"


@pytest.mark.asyncio
async def test_non_interchange_parquet_rejected_400(store: PostgresStore, case: str) -> None:
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pydict({"message": ["not an interchange file"]})
    sink = io.BytesIO()
    pq.write_table(table, sink)

    case_obj = await store.get_case(case)
    with pytest.raises(HTTPException) as exc_info:
        await _upload(case_obj, "events.parquet", sink.getvalue())
    assert exc_info.value.status_code == 400
    assert "Vestigo" in exc_info.value.detail
    # No orphaned source row.
    assert await store.list_sources(case) == []
