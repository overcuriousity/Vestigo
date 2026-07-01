"""Tests for source upload idempotency and forensic hashing."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks

from tracevector.api.routers import cases
from tracevector.api.routers.cases import upload_source
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
        batch_size: int | None = None,
        file_hash: str | None = None,
        source_name: str | None = None,
    ) -> None:
        self.case_id = case_id
        self.source_id = source_id
        self.file_hash = file_hash
        self.source_name = source_name

    def run(self, path: Path, format_name: str | None = None) -> IngestionResult:
        data = path.read_bytes()
        lines = [line for line in data.split(b"\n") if line.strip()]
        return IngestionResult(
            case_id=self.case_id,
            source_id=self.source_id,
            files=[path],
            events_parsed=len(lines),
            events_inserted=len(lines),
        )


@pytest_asyncio.fixture()
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PostgresStore:
    """In-memory SQLite store wired into the cases router for upload tests."""
    db_path = tmp_path / "test_uploads.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    monkeypatch.setattr(cases, "_store", s)
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
    upload_file = _UploadFile("events.jsonl", content)
    expected_hash = hash_file(BytesIO(content))

    first = await upload_source(
        case_id=case,
        background_tasks=BackgroundTasks(),
        file=upload_file,
        parser=None,
    )
    assert first.events_inserted == 1
    assert first.duplicate is False

    source = await store.get_source_by_hash(case, expected_hash)
    assert source is not None
    assert source.filename == "events.jsonl"
    assert source.event_count == 1

    # Second upload of the same bytes must be a no-op.
    second = await upload_source(
        case_id=case,
        background_tasks=BackgroundTasks(),
        file=_UploadFile("events.jsonl", content),
        parser=None,
    )
    assert second.events_inserted == 0
    assert second.duplicate is True
    assert second.events_parsed == 1

    assert len(await store.list_sources(case)) == 1


@pytest.mark.asyncio
async def test_uploading_different_file_adds_events(
    store: PostgresStore,
    case: str,
) -> None:
    first_content = b'{"message":"login","timestamp":"2024-01-01T00:00:00+00:00"}\n'
    second_content = b'{"message":"logout","timestamp":"2024-01-01T00:01:00+00:00"}\n'

    first = await upload_source(
        case_id=case,
        background_tasks=BackgroundTasks(),
        file=_UploadFile("first.jsonl", first_content),
        parser=None,
    )
    assert first.events_inserted == 1

    second = await upload_source(
        case_id=case,
        background_tasks=BackgroundTasks(),
        file=_UploadFile("second.jsonl", second_content),
        parser=None,
    )
    assert second.events_inserted == 1
    assert second.duplicate is False

    sources = await store.list_sources(case)
    assert len(sources) == 2
    assert sum(s.event_count for s in sources) == 2


@pytest.mark.asyncio
async def test_upload_to_missing_case_returns_404(
    store: PostgresStore,
) -> None:
    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await upload_source(
            case_id="missing",
            background_tasks=BackgroundTasks(),
            file=_UploadFile("events.jsonl", b'{"message":"x"}\n'),
            parser=None,
        )
    assert exc_info.value.status_code == 404  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_source_added_to_default_timeline(
    store: PostgresStore,
    case: str,
) -> None:
    await upload_source(
        case_id=case,
        background_tasks=BackgroundTasks(),
        file=_UploadFile("events.jsonl", b'{"message":"x"}\n'),
        parser=None,
    )
    default_timeline = await store.get_default_timeline(case)
    assert default_timeline is not None
    sources = await store.list_timeline_sources(case, default_timeline.id)
    assert len(sources) == 1
