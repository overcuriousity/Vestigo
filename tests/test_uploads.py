"""Tests for timeline upload idempotency and forensic hashing."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio

from tracevector.api.routers import cases
from tracevector.api.routers.cases import upload_timeline
from tracevector.db.postgres import PostgresStore
from tracevector.ingestion.files import hash_file


@pytest_asyncio.fixture()
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PostgresStore:
    """In-memory SQLite store wired into the cases router for upload tests."""
    db_path = tmp_path / "test_uploads.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    # Force the cases router to use this isolated store.
    monkeypatch.setattr(cases, "_store", s)
    return s


@pytest_asyncio.fixture()
async def case_and_timeline(store: PostgresStore) -> tuple[str, str]:
    case = await store.create_case("case1", "Test case")
    timeline = await store.create_timeline(
        case_id=case.id,
        timeline_id="timeline1",
        name="Test timeline",
    )
    return case.id, timeline.id


class _UploadFile:
    """Minimal stand-in for FastAPI's UploadFile."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.file = BytesIO(content)


@pytest.mark.asyncio
async def test_duplicate_upload_is_idempotent(
    store: PostgresStore,
    case_and_timeline: tuple[str, str],
) -> None:
    case_id, timeline_id = case_and_timeline
    content = b'{"message":"login","timestamp":"2024-01-01T00:00:00+00:00"}\n'
    upload_file = _UploadFile("events.jsonl", content)
    expected_hash = hash_file(BytesIO(content))

    first = await upload_timeline(
        case_id=case_id,
        timeline_id=timeline_id,
        file=upload_file,
        parser=None,
    )
    assert first["events_inserted"] == 1
    assert first["duplicate"] is False

    timeline = await store.get_timeline(case_id, timeline_id)
    assert timeline is not None
    assert timeline.event_count == 1

    upload_record = await store.get_timeline_upload_by_hash(
        case_id=case_id,
        timeline_id=timeline_id,
        file_hash=expected_hash,
    )
    assert upload_record is not None
    assert upload_record.filename == "events.jsonl"
    assert upload_record.event_count == 1

    # Second upload of the same bytes must be a no-op.
    second = await upload_timeline(
        case_id=case_id,
        timeline_id=timeline_id,
        file=_UploadFile("events.jsonl", content),
        parser=None,
    )
    assert second["events_inserted"] == 0
    assert second["duplicate"] is True
    assert second["events_parsed"] == 1

    timeline = await store.get_timeline(case_id, timeline_id)
    assert timeline is not None
    assert timeline.event_count == 1


@pytest.mark.asyncio
async def test_uploading_different_file_adds_events(
    store: PostgresStore,
    case_and_timeline: tuple[str, str],
) -> None:
    case_id, timeline_id = case_and_timeline
    first_content = b'{"message":"login","timestamp":"2024-01-01T00:00:00+00:00"}\n'
    second_content = b'{"message":"logout","timestamp":"2024-01-01T00:01:00+00:00"}\n'

    first = await upload_timeline(
        case_id=case_id,
        timeline_id=timeline_id,
        file=_UploadFile("first.jsonl", first_content),
        parser=None,
    )
    assert first["events_inserted"] == 1

    second = await upload_timeline(
        case_id=case_id,
        timeline_id=timeline_id,
        file=_UploadFile("second.jsonl", second_content),
        parser=None,
    )
    assert second["events_inserted"] == 1
    assert second["duplicate"] is False

    timeline = await store.get_timeline(case_id, timeline_id)
    assert timeline is not None
    assert timeline.event_count == 2


@pytest.mark.asyncio
async def test_upload_to_missing_timeline_returns_404(
    store: PostgresStore,
) -> None:
    case = await store.create_case("case1", "Test case")
    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await upload_timeline(
            case_id=case.id,
            timeline_id="missing",
            file=_UploadFile("events.jsonl", b'{"message":"x"}\n'),
            parser=None,
        )
    assert exc_info.value.status_code == 404  # type: ignore[attr-defined]
