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
from tracevector.db.postgres import PostgresStore, generate_id
from tracevector.db.qdrant import QdrantStore
from tracevector.ingestion.files import hash_file
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


class AnnotationCreate(BaseModel):
    """Payload to create an event annotation."""

    annotation_type: str = Field(..., pattern="^(comment|tag|normal)$")
    content: str = Field(..., min_length=1, max_length=4096)


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
    Uploading a file whose SHA-256 hash has already been ingested for this
    timeline is idempotent and returns the existing result without creating
    duplicate events.
    """
    store = get_store()
    await store.init_schema()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    file_hash = hash_file(file.file)
    existing_upload = await store.get_timeline_upload_by_hash(
        case_id=case_id,
        timeline_id=timeline_id,
        file_hash=file_hash,
    )
    if existing_upload is not None:
        return {
            "timeline_id": timeline_id,
            "events_parsed": existing_upload.event_count,
            "events_inserted": 0,
            "parser": parser or existing_upload.parser or "auto",
            "duplicate": True,
        }

    suffix = Path(file.filename or "upload").suffix or ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        # Treat sentinel strings as unspecified and fall back to auto-detection.
        fmt = parser if parser and parser.lower() not in {"undefined", "null", "auto", ""} else None
        fmt = fmt or detect_format(tmp_path)
        pipeline = IngestionPipeline(
            case_id=case_id,
            timeline_id=timeline_id,
            batch_size=get_settings().embedding_batch_size,
            file_hash=file_hash,
            source_name=file.filename or tmp_path.name,
        )
        result = pipeline.run(tmp_path, format_name=fmt)
        await store.update_timeline_counts(
            case_id=case_id,
            timeline_id=timeline_id,
            event_count=result.events_inserted,
            vector_count=0,
        )
        await store.create_timeline_upload(
            case_id=case_id,
            timeline_id=timeline_id,
            upload_id=generate_id(f"{timeline_id}:{file_hash}"),
            file_hash=file_hash,
            filename=file.filename,
            event_count=result.events_inserted,
            parser=fmt,
        )
        return {
            "timeline_id": timeline_id,
            "events_parsed": result.events_parsed,
            "events_inserted": result.events_inserted,
            "parser": fmt,
            "duplicate": False,
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
    await store.delete_timeline_uploads_for_timeline(case_id, timeline_id)
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


@router.get("/{case_id}/timelines/{timeline_id}/annotations")
async def list_timeline_annotations(case_id: str, timeline_id: str) -> dict[str, Any]:
    """List all annotations for a timeline (used for bulk event-table chips)."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    annotations = await store.list_timeline_annotations(case_id, timeline_id)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.get("/{case_id}/timelines/{timeline_id}/events/{event_id}/annotations")
async def list_event_annotations(case_id: str, timeline_id: str, event_id: str) -> dict[str, Any]:
    """List annotations for a single event."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    annotations = await store.list_annotations(case_id, timeline_id, event_id)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.post("/{case_id}/timelines/{timeline_id}/events/{event_id}/annotations")
async def create_event_annotation(
    case_id: str,
    timeline_id: str,
    event_id: str,
    payload: AnnotationCreate,
) -> dict[str, Any]:
    """Add a tag or comment annotation to an event."""
    store = get_store()
    await store.init_schema()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    annotation_id = generate_id(f"{event_id}_{payload.annotation_type}")
    annotation = await store.create_annotation(
        case_id=case_id,
        timeline_id=timeline_id,
        event_id=event_id,
        annotation_id=annotation_id,
        annotation_type=payload.annotation_type,
        content=payload.content,
    )
    return {"annotation": annotation.to_dict()}


@router.delete("/{case_id}/timelines/{timeline_id}/events/{event_id}/annotations/{annotation_id}")
async def delete_event_annotation(
    case_id: str,
    timeline_id: str,
    event_id: str,
    annotation_id: str,
) -> dict[str, Any]:
    """Delete an annotation."""
    store = get_store()
    deleted = await store.delete_annotation(case_id, event_id, annotation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Annotation not found")
    return {"deleted": True, "annotation_id": annotation_id}


class EmbedRequest(BaseModel):
    """Optional body for the embed endpoint.

    When ``embedding_config`` is provided it is persisted on the timeline and
    used to drive per-source field selection.  Omit the body (or send an empty
    object) to reuse the timeline's stored config, falling back to legacy
    all-fields behaviour when none has been saved.
    """

    embedding_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            'Per-source field selection. Shape: {"version": 1, "sources": '
            '{"<source>": ["message", "attr:user_agent", ...]}}'
        ),
    )


def _run_embedding_job(
    job_id: str,
    case_id: str,
    timeline_id: str,
    job_store: JobStore,
    field_config: dict[str, Any] | None = None,
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
            field_config=field_config,
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
    body: EmbedRequest | None = None,
) -> dict[str, Any]:
    """Start a background job to generate embeddings for a timeline.

    Accepts an optional ``embedding_config`` body produced by the embedding
    wizard.  When supplied it is persisted on the timeline and used to control
    which fields of which sources get embedded.  Omit the body to reuse the
    timeline's previously stored config (or fall back to all-fields behaviour).
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    # Resolve effective field config: request body > stored on timeline > None.
    field_config: dict[str, Any] | None = None
    if body is not None and body.embedding_config is not None:
        field_config = body.embedding_config
        await store.update_timeline_embedding_config(case_id, timeline_id, field_config)
    elif timeline.embedding_config is not None:
        field_config = timeline.embedding_config

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
        field_config,
    )
    return {"job_id": job.id, "status": job.status}
