"""API routes for cases, timelines, and uploads."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from tracevector.core.config import get_settings
from tracevector.core.jobs import JobStore, get_job_store
from tracevector.db.clickhouse import ClickHouseStore
from tracevector.db.postgres import PostgresStore, View, generate_id
from tracevector.db.qdrant import QdrantStore
from tracevector.ingestion.parser import detect_format
from tracevector.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline


class CaseCreate(BaseModel):
    """Payload to create a case."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)


class TimelineCreate(BaseModel):
    """Payload to create a timeline."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    parser: str | None = Field(default=None)


class ViewCreate(BaseModel):
    """Payload to create a saved view."""

    name: str = Field(..., min_length=1, max_length=255)
    query: str = Field(default="")
    filter: dict[str, Any] = Field(default_factory=dict)


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


@router.get("/{case_id}/timelines/{timeline_id}")
async def get_timeline(case_id: str, timeline_id: str) -> dict[str, Any]:
    """Get a single timeline by ID."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return {"timeline": timeline.to_dict()}


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
    """Upload a timeline file and ingest events into ClickHouse.

    Embeddings are *not* generated here; use the embed endpoint for that.
    """
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
        # The frontend may send the literal string "undefined" when no parser
        # is selected, or "auto" when the user leaves the default, so treat
        # those as unspecified and fall back to detection.
        fmt = parser if parser and parser.lower() not in {"undefined", "null", "auto", ""} else None
        fmt = fmt or detect_format(tmp_path)
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
            vector_count=0,
        )
        return {
            "timeline_id": timeline_id,
            "events_parsed": result.events_parsed,
            "events_inserted": result.events_inserted,
            "parser": fmt,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@router.delete("/{case_id}/timelines/{timeline_id}")
async def delete_timeline(case_id: str, timeline_id: str) -> dict[str, Any]:
    """Delete a timeline and cascade-remove its events and vectors.

    Removes data from all three stores in this order:
    1. Qdrant vector points filtered by timeline_id (payload filter, per-collection).
    2. ClickHouse events partition (case_id, timeline_id) via DROP PARTITION.
    3. PostgreSQL Timeline row.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    qdrant = QdrantStore()
    ch = ClickHouseStore()
    qdrant.delete_timeline_points(case_id, timeline_id)
    ch.delete_timeline_events(case_id, timeline_id)
    await store.delete_timeline(case_id, timeline_id)

    return {"deleted": True, "timeline_id": timeline_id}


@router.delete("/{case_id}")
async def delete_case(case_id: str) -> dict[str, Any]:
    """Delete a case and cascade-remove all its timelines, events, and vectors.

    Removes data from all three stores in this order:
    1. Qdrant vector points per timeline, then all case collections.
    2. ClickHouse events partitions per timeline.
    3. PostgreSQL Case row (and Timeline rows via delete_case).
    """
    store = get_store()
    case = await store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    timelines = await store.list_timelines(case_id)

    qdrant = QdrantStore()
    ch = ClickHouseStore()
    for tl in timelines:
        qdrant.delete_timeline_points(case_id, tl.id)
        ch.delete_timeline_events(case_id, tl.id)
    qdrant.delete_case_collections(case_id)
    await store.delete_case(case_id)

    return {"deleted": True, "case_id": case_id}


@router.get("/{case_id}/views")
async def list_views(case_id: str) -> dict[str, Any]:
    """List all saved views for a case."""
    store = get_store()
    views = await store.list_views(case_id)
    return {"views": [v.to_dict() for v in views]}


@router.post("/{case_id}/views")
async def create_view(case_id: str, payload: ViewCreate) -> dict[str, Any]:
    """Create a new saved view within a case."""
    store = get_store()
    case = await store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    view_id = generate_id(payload.name)
    view = await store.create_view(
        case_id=case_id,
        view_id=view_id,
        name=payload.name,
        query=payload.query,
        view_filter=payload.filter,
    )
    return {"view": view.to_dict()}


@router.delete("/{case_id}/views/{view_id}")
async def delete_view(case_id: str, view_id: str) -> dict[str, Any]:
    """Delete a saved view."""
    store = get_store()
    deleted = await store.delete_view(case_id, view_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="View not found")
    return {"deleted": True, "view_id": view_id}


def _run_embedding_job(
    job_id: str,
    case_id: str,
    timeline_id: str,
    job_store: JobStore,
) -> None:
    """Run the embedding pipeline and update the job store."""

    def progress_callback(total: int, processed: int) -> None:
        job_store.update(
            job_id,
            status="running",
            progress={"total": total, "processed": processed},
        )

    try:
        pipeline = EmbeddingPipeline(
            case_id=case_id,
            timeline_id=timeline_id,
            batch_size=get_settings().embedding_batch_size,
            progress_callback=progress_callback,
        )
        result = pipeline.run()

        # Use a fresh PostgresStore inside the worker thread.
        store = PostgresStore()
        asyncio.run(
            store.update_timeline_counts(
                case_id=case_id,
                timeline_id=timeline_id,
                vector_count=result.vectors_inserted,
            )
        )
        job_store.update(
            job_id,
            status="completed",
            progress={"total": result.events_processed, "processed": result.events_processed},
            result={"vectors_inserted": result.vectors_inserted},
        )
    except Exception as exc:  # noqa: BLE001
        job_store.update(job_id, status="failed", error=str(exc))


@router.post("/{case_id}/timelines/{timeline_id}/embed")
async def start_embedding(
    case_id: str,
    timeline_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Start a background job to generate embeddings for a timeline."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    job_store = get_job_store()
    job = job_store.create(
        kind="embed",
        progress={"total": 0, "processed": 0},
    )
    background_tasks.add_task(
        _run_embedding_job,
        job.id,
        case_id,
        timeline_id,
        job_store,
    )
    return {"job_id": job.id, "status": job.status}
