"""API routes for cases, sources, timelines, and annotations."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
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
    source_ids: list[str] = Field(default_factory=list)


class ViewCreate(BaseModel):
    """Payload to create a saved view."""

    name: str = Field(..., min_length=1, max_length=255)
    query: str = Field(default="")
    filter: dict[str, Any] = Field(default_factory=dict)


class AnnotationCreate(BaseModel):
    """Payload to create an event annotation."""

    annotation_type: str = Field(..., pattern="^(comment|tag|normal)$")
    content: str = Field(..., min_length=1, max_length=4096)


class SourceUploadResponse(BaseModel):
    """Response shape for a source upload."""

    source_id: str
    events_parsed: int
    events_inserted: int
    parser: str
    duplicate: bool
    embed_job_id: str | None = None


router = APIRouter(prefix="/api/cases", tags=["cases"])

_store: PostgresStore | None = None


def get_store() -> PostgresStore:
    """Return a cached PostgresStore instance."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = PostgresStore()
    return _store


def _retention_dir() -> Path:
    """Return the directory used for content-addressed source file retention."""
    path = Path(get_settings().source_retention_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _retention_path(file_hash: str) -> Path:
    """Return the content-addressed path for a retained source file."""
    # Shard by the first two hash characters to avoid huge flat directories.
    return _retention_dir() / file_hash[:2] / file_hash


# ═════════════════════════════════════════════════════════════════════════════
# Cases
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/")
async def list_cases() -> dict[str, Any]:
    """List all cases."""
    store = get_store()
    await store.init_schema()
    cases = await store.list_cases()
    return {"cases": [c.to_dict() for c in cases]}


@router.post("/")
async def create_case(payload: CaseCreate) -> dict[str, Any]:
    """Create a new case (and its default timeline)."""
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


@router.delete("/{case_id}")
async def delete_case(case_id: str) -> dict[str, Any]:
    """Delete a case and cascade-remove all its sources, timelines, events, and vectors."""
    store = get_store()
    case = await store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    qdrant = QdrantStore()
    ch = ClickHouseStore()

    sources = await store.list_sources(case_id)
    for source in sources:
        qdrant.delete_source_points(case_id, source.id)
        ch.delete_source_events(case_id, source.id)
    qdrant.delete_case_collections(case_id)
    await store.delete_case(case_id)

    return {"deleted": True, "case_id": case_id}


# ═════════════════════════════════════════════════════════════════════════════
# Sources
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/{case_id}/sources")
async def list_sources(case_id: str) -> dict[str, Any]:
    """List all sources within a case."""
    store = get_store()
    sources = await store.list_sources(case_id)
    return {"sources": [s.to_dict() for s in sources]}


@router.get("/{case_id}/sources/{source_id}")
async def get_source(case_id: str, source_id: str) -> dict[str, Any]:
    """Get a single source by ID."""
    store = get_store()
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"source": source.to_dict()}


@router.post("/{case_id}/sources")
async def upload_source(
    case_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008
    parser: str | None = Form(default=None),
    name: str | None = Form(default=None),
) -> SourceUploadResponse:
    """Upload a source file and ingest events into ClickHouse.

    ``name`` is supplied as a form field, but the function may also be called
    directly from tests with a plain ``str`` or ``None`` value.

    Embedding starts automatically in the background using the default
    all-fields configuration, so every source becomes searchable without a
    manual step; ``embed_job_id`` in the response lets the caller track that
    job. The field-selection wizard remains available to re-embed a timeline
    with a curated, cohesion-optimized field set.

    Uploading a file whose SHA-256 hash already exists in this case is
    idempotent and returns the existing source without creating duplicate
    events or a new embed job.
    """
    if not isinstance(name, (str, type(None))):
        name = None
    store = get_store()
    await store.init_schema()
    case = await store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    file_hash = hash_file(file.file)
    try:
        file.file.seek(0)
    except (OSError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file stream is not seekable; cannot ingest",
        ) from exc

    existing_source = await store.get_source_by_hash(case_id, file_hash)
    if existing_source is not None:
        return SourceUploadResponse(
            source_id=existing_source.id,
            events_parsed=existing_source.event_count,
            events_inserted=0,
            parser=parser or existing_source.parser or "auto",
            duplicate=True,
        )

    suffix = Path(file.filename or "upload").suffix or ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
        size_bytes = tmp_path.stat().st_size

    try:
        fmt = parser if parser and parser.lower() not in {"undefined", "null", "auto", ""} else None
        fmt = fmt or detect_format(tmp_path)
        source_id = generate_id(f"{case_id}:{file_hash}")
        source_name = name or file.filename or tmp_path.name

        # Retain the original file content-addressed by hash.
        retention_path = _retention_path(file_hash)
        retention_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_path, retention_path)

        pipeline = IngestionPipeline(
            case_id=case_id,
            source_id=source_id,
            batch_size=get_settings().embedding_batch_size,
            file_hash=file_hash,
            source_name=file.filename or tmp_path.name,
        )
        result = pipeline.run(tmp_path, format_name=fmt)

        await store.create_source(
            case_id=case_id,
            source_id=source_id,
            name=source_name,
            file_hash=file_hash,
            size_bytes=size_bytes,
            filename=file.filename,
            parser=fmt,
            event_count=result.events_inserted,
        )

        # Auto-add the new source to the case's default timeline.
        default_timeline = await store.get_default_timeline(case_id)
        if default_timeline is not None:
            await store.add_source_to_timeline(case_id, default_timeline.id, source_id)

        # field_config=None -> default all-fields embedding, which keeps every
        # source in the case's shared default collection so cross-source
        # search works without requiring the curated field-selection wizard.
        job_store = get_job_store()
        embed_job = job_store.create(kind="embed", progress={"total": 0, "processed": 0})
        background_tasks.add_task(
            _run_embedding_job,
            embed_job.id,
            case_id,
            source_id,
            job_store,
            None,
        )

        return SourceUploadResponse(
            source_id=source_id,
            events_parsed=result.events_parsed,
            events_inserted=result.events_inserted,
            parser=fmt,
            duplicate=False,
            embed_job_id=embed_job.id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/{case_id}/sources/{source_id}/download")
async def download_source(case_id: str, source_id: str) -> FileResponse:
    """Re-download the original source file by its SHA-256 hash."""
    store = get_store()
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    retention_path = _retention_path(source.file_hash)
    if not retention_path.exists():
        raise HTTPException(status_code=404, detail="Original source file no longer retained")

    filename = source.filename or f"{source.file_hash}.bin"
    return FileResponse(
        path=retention_path,
        filename=filename,
        media_type="application/octet-stream",
    )


@router.delete("/{case_id}/sources/{source_id}")
async def delete_source(case_id: str, source_id: str) -> dict[str, Any]:
    """Delete a source and cascade-remove its events and vectors.

    The source is removed from all timelines automatically by the foreign-key
    cascade on ``timeline_sources``.
    """
    store = get_store()
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    qdrant = QdrantStore()
    ch = ClickHouseStore()
    qdrant.delete_source_points(case_id, source_id)
    ch.delete_source_events(case_id, source_id)
    await store.delete_source(case_id, source_id)

    return {"deleted": True, "source_id": source_id}


# ═════════════════════════════════════════════════════════════════════════════
# Timelines
# ═════════════════════════════════════════════════════════════════════════════


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
    """Create a new timeline (grouping of sources) within a case."""
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
        source_ids=payload.source_ids,
    )
    return {"timeline": timeline.to_dict()}


@router.delete("/{case_id}/timelines/{timeline_id}")
async def delete_timeline(case_id: str, timeline_id: str) -> dict[str, Any]:
    """Delete a timeline.

    Deleting a timeline does *not* delete its sources, events, or vectors —
    those remain available in the default timeline and other groupings.
    """
    store = get_store()
    deleted = await store.delete_timeline(case_id, timeline_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return {"deleted": True, "timeline_id": timeline_id}


@router.get("/{case_id}/timelines/{timeline_id}/sources")
async def list_timeline_sources(case_id: str, timeline_id: str) -> dict[str, Any]:
    """List the sources attached to a timeline."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case_id, timeline_id)
    return {"sources": [s.to_dict() for s in sources]}


@router.post("/{case_id}/timelines/{timeline_id}/sources/{source_id}")
async def add_source_to_timeline(
    case_id: str,
    timeline_id: str,
    source_id: str,
) -> dict[str, Any]:
    """Add a source to a timeline."""
    store = get_store()
    added = await store.add_source_to_timeline(case_id, timeline_id, source_id)
    if not added:
        raise HTTPException(
            status_code=400,
            detail="Source is already a member of the timeline, or one of the IDs was not found",
        )
    return {"added": True, "timeline_id": timeline_id, "source_id": source_id}


@router.delete("/{case_id}/timelines/{timeline_id}/sources/{source_id}")
async def remove_source_from_timeline(
    case_id: str,
    timeline_id: str,
    source_id: str,
) -> dict[str, Any]:
    """Remove a source from a timeline."""
    store = get_store()
    removed = await store.remove_source_from_timeline(case_id, timeline_id, source_id)
    if not removed:
        raise HTTPException(
            status_code=400,
            detail="Source is not a member of the timeline, or one of the IDs was not found",
        )
    return {"removed": True, "timeline_id": timeline_id, "source_id": source_id}


# ═════════════════════════════════════════════════════════════════════════════
# Views
# ═════════════════════════════════════════════════════════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
# Annotations
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/{case_id}/timelines/{timeline_id}/tags")
async def list_timeline_tags(case_id: str, timeline_id: str) -> dict[str, Any]:
    """Return the distinct user annotation-tag labels for a timeline's sources.

    Used to power tag autocomplete in the UI.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case_id, timeline_id)
    source_ids = [s.id for s in sources]
    tags = await store.list_distinct_tag_contents(case_id, source_ids)
    return {"tags": tags}


@router.get("/{case_id}/timelines/{timeline_id}/annotations")
async def list_timeline_annotations(case_id: str, timeline_id: str) -> dict[str, Any]:
    """List all annotations for a timeline's sources (used for event-table chips)."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case_id, timeline_id)
    source_ids = [s.id for s in sources]
    annotations = await store.list_source_annotations(case_id, source_ids)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.get("/{case_id}/sources/{source_id}/events/{event_id}/annotations")
async def list_event_annotations(
    case_id: str,
    source_id: str,
    event_id: str,
) -> dict[str, Any]:
    """List annotations for a single event."""
    store = get_store()
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    annotations = await store.list_annotations(case_id, source_id, event_id)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.post("/{case_id}/sources/{source_id}/events/{event_id}/annotations")
async def create_event_annotation(
    case_id: str,
    source_id: str,
    event_id: str,
    payload: AnnotationCreate,
) -> dict[str, Any]:
    """Add a tag or comment annotation to an event."""
    store = get_store()
    await store.init_schema()
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    annotation_id = generate_id(f"{event_id}_{payload.annotation_type}")
    annotation = await store.create_annotation(
        case_id=case_id,
        source_id=source_id,
        event_id=event_id,
        annotation_id=annotation_id,
        annotation_type=payload.annotation_type,
        content=payload.content,
    )
    return {"annotation": annotation.to_dict()}


@router.delete("/{case_id}/sources/{source_id}/events/{event_id}/annotations/{annotation_id}")
async def delete_event_annotation(
    case_id: str,
    source_id: str,
    event_id: str,
    annotation_id: str,
) -> dict[str, Any]:
    """Delete an annotation."""
    store = get_store()
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    deleted = await store.delete_annotation(case_id, event_id, annotation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Annotation not found")
    return {"deleted": True, "annotation_id": annotation_id}


# ═════════════════════════════════════════════════════════════════════════════
# Embeddings
# ═════════════════════════════════════════════════════════════════════════════


class EmbedRequest(BaseModel):
    """Optional body for the embed endpoint.

    When ``embedding_config`` is provided it is persisted on the source and
    used to drive per-artifact field selection.  Omit the body (or send an empty
    object) to reuse the source's stored config, falling back to legacy
    all-fields behaviour when none has been saved.
    """

    embedding_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            'Per-artifact field selection. Shape: {"version": 1, "artifacts": '
            '{"<artifact>": ["message", "attr:k", ...]}}'
        ),
    )


def _run_embedding_job(
    job_id: str,
    case_id: str,
    source_id: str,
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
            source_ids=[source_id],
            batch_size=get_settings().embedding_batch_size,
            progress_callback=progress_callback,
            field_config=field_config,
        )
        result = pipeline.run()

        # Use a fresh PostgresStore inside the worker thread.
        store = PostgresStore()
        asyncio.run(
            store.update_source_counts(
                case_id=case_id,
                source_id=source_id,
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


def _run_timeline_embedding_job(
    job_id: str,
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    job_store: JobStore,
    field_config: dict[str, Any] | None = None,
) -> None:
    """Embed all sources of a timeline with a shared field config."""

    def progress_callback(total: int, processed: int) -> None:
        job_store.update(
            job_id,
            status="running",
            progress={"total": total, "processed": processed},
        )

    try:
        pipeline = EmbeddingPipeline(
            case_id=case_id,
            source_ids=source_ids,
            batch_size=get_settings().embedding_batch_size,
            progress_callback=progress_callback,
            field_config=field_config,
        )
        result = pipeline.run()

        store = PostgresStore()
        embedding_model = get_settings().embedding_model
        asyncio.run(
            store.set_timeline_embedding(
                case_id=case_id,
                timeline_id=timeline_id,
                model=embedding_model,
                config=field_config or {},
                config_hash=result.config_hash,
                embedded_source_ids=source_ids,
            )
        )
        # Update vector counts on each source.
        # EmbeddingPipeline processes all sources in one collection so we set
        # an approximate per-source count (total / n sources) as a best effort;
        # the authoritative vector count is queryable from Qdrant directly.
        per_source = result.vectors_inserted // max(len(source_ids), 1)
        for sid in source_ids:
            asyncio.run(
                store.update_source_counts(
                    case_id=case_id,
                    source_id=sid,
                    vector_count=per_source,
                )
            )
        job_store.update(
            job_id,
            status="completed",
            progress={
                "total": result.events_processed,
                "processed": result.events_processed,
            },
            result={
                "vectors_inserted": result.vectors_inserted,
                "config_hash": result.config_hash,
                "source_ids": source_ids,
            },
        )
    except Exception as exc:  # noqa: BLE001
        job_store.update(job_id, status="failed", error=str(exc))


@router.post("/{case_id}/timelines/{timeline_id}/embed")
async def start_timeline_embedding(
    case_id: str,
    timeline_id: str,
    background_tasks: BackgroundTasks,
    body: EmbedRequest | None = None,
) -> dict[str, Any]:
    """Embed all sources in a timeline with a single shared field config.

    This is the primary embedding entry point.  The wizard on the frontend
    computes a cross-source-cohesive field selection and submits it here.

    On success the timeline is marked as embedded with a snapshot of the
    current source set.  If sources are later added the timeline becomes
    *stale* (``is_stale=True`` in ``to_dict()``), prompting a re-embed.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    sources = await store.list_timeline_sources(case_id, timeline_id)
    if not sources:
        raise HTTPException(
            status_code=422,
            detail="Timeline has no sources — add at least one source before embedding.",
        )
    source_ids = [s.id for s in sources]

    # Resolve effective field config: request body > timeline's stored config > None.
    field_config: dict[str, Any] | None = None
    if body is not None and body.embedding_config is not None:
        field_config = body.embedding_config
    elif timeline.embedding_config:
        field_config = timeline.embedding_config

    job_store = get_job_store()
    job = job_store.create(
        kind="embed",
        progress={"total": 0, "processed": 0},
    )
    background_tasks.add_task(
        _run_timeline_embedding_job,
        job.id,
        case_id,
        timeline_id,
        source_ids,
        job_store,
        field_config,
    )
    return {"job_id": job.id, "status": job.status, "source_ids": source_ids}
