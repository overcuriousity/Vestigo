"""API routes for cases, timelines, and uploads."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from tracevector.core.config import get_settings
from tracevector.db.postgres import PostgresStore, generate_id
from tracevector.ingestion.parser import detect_format
from tracevector.ingestion.pipeline import IngestionPipeline


class CaseCreate(BaseModel):
    """Payload to create a case."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)


class TimelineCreate(BaseModel):
    """Payload to create a timeline."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    parser: str | None = Field(default=None)


router = APIRouter(prefix="/api/cases", tags=["cases"])

_store: PostgresStore | None = None


def get_store() -> PostgresStore:
    """Return a cached PostgresStore instance."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = PostgresStore()
    return _store


@router.get("/")
async def list_cases() -> dict[str, Any]:
    """List all cases."""
    store = get_store()
    await store.init_schema()
    cases = await store.list_cases()
    return {"cases": [c.to_dict() for c in cases]}


@router.post("/")
async def create_case(payload: CaseCreate) -> dict[str, Any]:
    """Create a new case."""
    store = get_store()
    await store.init_schema()
    case_id = generate_id(payload.name)
    case = await store.create_case(
        case_id=case_id,
        name=payload.name,
        description=payload.description,
    )
    return {"case": case.to_dict()}


@router.get("/{case_id}")
async def get_case(case_id: str) -> dict[str, Any]:
    """Get a case by ID."""
    store = get_store()
    case = await store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return {"case": case.to_dict()}


@router.get("/{case_id}/timelines")
async def list_timelines(case_id: str) -> dict[str, Any]:
    """List timelines within a case."""
    store = get_store()
    timelines = await store.list_timelines(case_id)
    return {"timelines": [t.to_dict() for t in timelines]}


@router.post("/{case_id}/timelines")
async def create_timeline(case_id: str, payload: TimelineCreate) -> dict[str, Any]:
    """Create a new timeline within a case."""
    store = get_store()
    await store.init_schema()
    case = await store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    timeline_id = generate_id(payload.name)
    timeline = await store.create_timeline(
        case_id=case_id,
        timeline_id=timeline_id,
        name=payload.name,
        description=payload.description,
        parser=payload.parser,
    )
    return {"timeline": timeline.to_dict()}


@router.post("/{case_id}/timelines/{timeline_id}/upload")
async def upload_timeline(
    case_id: str,
    timeline_id: str,
    file: UploadFile = File(...),  # noqa: B008
    parser: str | None = Form(default=None),
) -> dict[str, Any]:
    """Upload a timeline file and run ingestion."""
    store = get_store()
    await store.init_schema()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    suffix = Path(file.filename or "upload").suffix or ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        fmt = parser or detect_format(tmp_path)
        pipeline = IngestionPipeline(
            case_id=case_id,
            timeline_id=timeline_id,
            batch_size=get_settings().embedding_batch_size,
        )
        result = pipeline.run(tmp_path, format_name=fmt)
        await store.update_timeline_counts(
            case_id=case_id,
            timeline_id=timeline_id,
            event_count=result.events_inserted,
            vector_count=result.vectors_inserted,
        )
        return {
            "timeline_id": timeline_id,
            "events_parsed": result.events_parsed,
            "events_inserted": result.events_inserted,
            "vectors_inserted": result.vectors_inserted,
            "parser": fmt,
        }
    finally:
        tmp_path.unlink(missing_ok=True)
