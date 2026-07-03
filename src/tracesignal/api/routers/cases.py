"""API routes for cases, sources, timelines, and annotations."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from tracesignal.api.deps import (
    get_current_user,
    get_store,
    require_case_contribute,
    require_case_manage,
    require_case_read,
    require_password_current,
)
from tracesignal.core.config import get_settings
from tracesignal.core.events_bus import publish_annotation_change
from tracesignal.core.jobs import JobStore, get_job_store
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_mappings import validate_field_mappings
from tracesignal.db.postgres import Case, PostgresStore, User, generate_id
from tracesignal.db.qdrant import QdrantStore
from tracesignal.db.queries import EventQueryService
from tracesignal.ingestion.files import hash_file
from tracesignal.ingestion.parser import detect_format
from tracesignal.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline


class CaseCreate(BaseModel):
    """Payload to create a case."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    # None -> a personal case, visible only to its owner and admins.
    team_id: str | None = Field(default=None)


class TimelineCreate(BaseModel):
    """Payload to create a timeline."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    source_ids: list[str] = Field(default_factory=list)
    # Canonical field name -> ordered raw attribute keys (issue #10).
    field_mappings: dict[str, list[str]] | None = Field(default=None)


class TimelineFieldMappingsUpdate(BaseModel):
    """Payload to replace a timeline's field mappings (None/{} clears them)."""

    field_mappings: dict[str, list[str]] | None = Field(default=None)


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
    """Response shape for a source upload.

    For a new (non-duplicate) upload, ingestion runs as a background job:
    ``job_id`` identifies it in ``GET /api/jobs/{job_id}`` and the event
    counts are 0 until the job completes (the job result carries the final
    counts). Duplicate uploads return the existing source's counts and no
    job.
    """

    source_id: str
    events_parsed: int
    events_inserted: int
    parser: str
    duplicate: bool
    job_id: str | None = None


router = APIRouter(prefix="/api/cases", tags=["cases"])


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
async def list_cases(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """List cases visible to the current user: their own, plus their teams' (all, if admin)."""
    store = get_store()
    await store.init_schema()
    if user.is_admin:
        cases = await store.list_cases()
    else:
        memberships = await store.list_user_memberships(user.id)
        team_ids = [m.team_id for m in memberships]
        cases = await store.list_cases_for_user(user.id, team_ids)
    return {"cases": [c.to_dict() for c in cases]}


@router.post("/")
async def create_case(
    payload: CaseCreate, user: User = Depends(require_password_current)
) -> dict[str, Any]:
    """Create a new case (and its default timeline).

    A case with no ``team_id`` is personal — visible only to its creator and
    admins. Assigning a ``team_id`` requires being a manager of that team
    (or an admin); plain team members cannot create team cases.
    """
    store = get_store()
    await store.init_schema()
    if payload.team_id:
        if not user.is_admin:
            membership = await store.get_membership(payload.team_id, user.id)
            if membership is None or membership.role != "manager":
                raise HTTPException(
                    status_code=403,
                    detail="Only a team manager or admin can create a case for this team",
                )
        if await store.get_team(payload.team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")

    case_id = generate_id(payload.name)
    case = await store.create_case(
        case_id=case_id,
        name=payload.name,
        description=payload.description,
        owner_id=user.id,
        team_id=payload.team_id,
    )
    await store.record_audit(
        action="case.create",
        actor=user,
        case_id=case_id,
        target_type="case",
        target_id=case_id,
    )
    return {"case": case.to_dict()}


@router.get("/{case_id}")
async def get_case(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Get a case by ID."""
    return {"case": case.to_dict()}


@router.delete("/{case_id}")
async def delete_case(
    case: Case = Depends(require_case_manage),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a case and cascade-remove all its sources, timelines, events, and vectors."""
    store = get_store()
    case_id = case.id

    qdrant = QdrantStore()
    ch = ClickHouseStore()

    sources = await store.list_sources(case_id)
    for source in sources:
        qdrant.delete_source_points(case_id, source.id)
        ch.delete_source_events(case_id, source.id)
    qdrant.delete_case_collections(case_id)
    await store.delete_case(case_id)

    await store.record_audit(
        action="case.delete",
        actor=user,
        case_id=case_id,
        target_type="case",
        target_id=case_id,
    )
    return {"deleted": True, "case_id": case_id}


# ═════════════════════════════════════════════════════════════════════════════
# Sources
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/{case_id}/sources")
async def list_sources(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List all sources within a case."""
    store = get_store()
    sources = await store.list_sources(case.id)
    return {"sources": [s.to_dict() for s in sources]}


@router.get("/{case_id}/sources/{source_id}")
async def get_source(source_id: str, case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Get a single source by ID."""
    store = get_store()
    source = await store.get_source(case.id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"source": source.to_dict()}


async def _run_ingestion_job(
    job_id: str,
    case_id: str,
    source_id: str,
    tmp_path: Path,
    fmt: str,
    file_hash: str,
    source_name: str,
    filename: str | None,
    size_bytes: int,
    user: User,
    job_store: JobStore,
) -> None:
    """Ingest an uploaded file in the background, updating the job store.

    The source row already exists (with ``event_count=0``, created before the
    job was scheduled so duplicate uploads are rejected immediately); this job
    streams the events into ClickHouse, bumps the stored count, and records
    the audit row. On failure it removes the partial events and the source
    row again so a failed upload leaves no half-populated source behind.
    """
    store = get_store()
    clickhouse = ClickHouseStore()

    def progress_callback(total: int, processed: int) -> None:
        job_store.update(
            job_id,
            status="running",
            progress={"total": total, "processed": processed},
        )

    try:
        pipeline = IngestionPipeline(
            case_id=case_id,
            source_id=source_id,
            clickhouse=clickhouse,
            batch_size=get_settings().embedding_batch_size,
            file_hash=file_hash,
            source_name=source_name,
            progress_callback=progress_callback,
        )
        # The pipeline is synchronous (parsing + ClickHouse inserts) — run it
        # in a worker thread so a large ingest doesn't block the event loop.
        result = await asyncio.to_thread(pipeline.run, tmp_path, fmt)

        await store.update_source_counts(
            case_id=case_id,
            source_id=source_id,
            event_count=result.events_inserted,
        )
        await store.record_audit(
            action="source.upload",
            actor=user,
            case_id=case_id,
            target_type="source",
            target_id=source_id,
            detail={"filename": filename, "events_inserted": result.events_inserted},
        )
        job_store.update(
            job_id,
            status="completed",
            progress={"total": size_bytes, "processed": size_bytes},
            result={
                "source_id": source_id,
                "events_parsed": result.events_parsed,
                "events_inserted": result.events_inserted,
                "parser": fmt,
            },
        )
    except Exception as exc:  # noqa: BLE001
        try:
            await asyncio.to_thread(clickhouse.delete_source_events, case_id, source_id)
            await store.delete_source(case_id, source_id)
        except Exception:  # noqa: BLE001, S110 — best-effort cleanup
            pass
        job_store.update(job_id, status="failed", error=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/{case_id}/sources")
async def upload_source(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008
    parser: str | None = Form(default=None),
    name: str | None = Form(default=None),
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> SourceUploadResponse:
    """Upload a source file and ingest events into ClickHouse.

    ``name`` is supplied as a form field, but the function may also be called
    directly from tests with a plain ``str`` or ``None`` value.

    Ingestion runs as a background job (see ``SourceUploadResponse.job_id``)
    so the UI can show live progress; the source row itself is created
    immediately with ``event_count=0``.

    Embeddings are *not* generated here; use the timeline embed endpoint
    (``POST /{case_id}/timelines/{timeline_id}/embed``) for that.

    Uploading a file whose SHA-256 hash already exists in this case is
    idempotent and returns the existing source without creating duplicate
    events.
    """
    if not isinstance(name, (str, type(None))):
        name = None
    store = get_store()
    case_id = case.id

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

    source_created = False
    try:
        fmt = parser if parser and parser.lower() not in {"undefined", "null", "auto", ""} else None
        if fmt is None:
            try:
                fmt = detect_format(tmp_path)
            except ValueError as exc:
                # Unknown extension is a client problem, not a server crash.
                # detect_format's own message names the server-side temp file,
                # which is useless (and mildly leaky) for the client.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot detect parser format for {file.filename!r}; "
                        "pass an explicit parser (e.g. jsonl, timesketch_csv)."
                    ),
                ) from exc
        source_id = generate_id(f"{case_id}:{file_hash}")
        source_name = name or file.filename or tmp_path.name

        # Retain the original file content-addressed by hash.
        retention_path = _retention_path(file_hash)
        retention_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_path, retention_path)

        # Create the source row up front (event_count=0) so a re-upload of
        # the same bytes is rejected as a duplicate while ingestion runs.
        try:
            await store.create_source(
                case_id=case_id,
                source_id=source_id,
                name=source_name,
                file_hash=file_hash,
                size_bytes=size_bytes,
                filename=file.filename,
                parser=fmt,
                event_count=0,
                created_by=user.id,
            )
            source_created = True
        except IntegrityError:
            # Lost a race against a concurrent upload of the same bytes:
            # treat it the same as the pre-check duplicate response.
            existing_source = await store.get_source_by_hash(case_id, file_hash)
            if existing_source is None:
                raise
            tmp_path.unlink(missing_ok=True)
            return SourceUploadResponse(
                source_id=existing_source.id,
                events_parsed=existing_source.event_count,
                events_inserted=0,
                parser=parser or existing_source.parser or "auto",
                duplicate=True,
            )

        # Auto-add the new source to the case's default timeline.
        default_timeline = await store.get_default_timeline(case_id)
        if default_timeline is not None:
            await store.add_source_to_timeline(case_id, default_timeline.id, source_id)

        job_store = get_job_store()
        job = job_store.create(
            kind="ingest",
            progress={"total": size_bytes, "processed": 0},
            created_by=user.id,
        )
        background_tasks.add_task(
            _run_ingestion_job,
            job.id,
            case_id,
            source_id,
            tmp_path,
            fmt,
            file_hash,
            file.filename or tmp_path.name,
            file.filename,
            size_bytes,
            user,
            job_store,
        )
    except Exception:
        if source_created:
            await store.delete_source(case_id, source_id)
        tmp_path.unlink(missing_ok=True)
        raise

    return SourceUploadResponse(
        source_id=source_id,
        events_parsed=0,
        events_inserted=0,
        parser=fmt,
        duplicate=False,
        job_id=job.id,
    )


@router.get("/{case_id}/sources/{source_id}/download")
async def download_source(source_id: str, case: Case = Depends(require_case_read)) -> FileResponse:
    """Re-download the original source file by its SHA-256 hash."""
    store = get_store()
    source = await store.get_source(case.id, source_id)
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
async def delete_source(
    source_id: str,
    case: Case = Depends(require_case_manage),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a source and cascade-remove its events and vectors.

    The source is removed from all timelines automatically by the foreign-key
    cascade on ``timeline_sources``.
    """
    store = get_store()
    case_id = case.id
    source = await store.get_source(case_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    qdrant = QdrantStore()
    ch = ClickHouseStore()
    qdrant.delete_source_points(case_id, source_id)
    ch.delete_source_events(case_id, source_id)
    await store.delete_source(case_id, source_id)

    await store.record_audit(
        action="source.delete",
        actor=user,
        case_id=case_id,
        target_type="source",
        target_id=source_id,
    )
    return {"deleted": True, "source_id": source_id}


# ═════════════════════════════════════════════════════════════════════════════
# Timelines
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/{case_id}/timelines")
async def list_timelines(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List timelines within a case."""
    store = get_store()
    timelines = await store.list_timelines(case.id)
    return {"timelines": [t.to_dict() for t in timelines]}


@router.get("/{case_id}/timelines/{timeline_id}")
async def get_timeline(timeline_id: str, case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Get a single timeline by ID."""
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return {"timeline": timeline.to_dict()}


async def _check_field_mappings(
    case_id: str, source_ids: list[str], mappings: dict[str, list[str]]
) -> None:
    """Validate mappings against the sources' actual attribute keys; 422 on problems."""
    service = EventQueryService()
    inventory = await run_in_threadpool(service.list_fields, case_id, source_ids)
    problems = validate_field_mappings(mappings, set(inventory["attributes"]))
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))


@router.post("/{case_id}/timelines")
async def create_timeline(
    payload: TimelineCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Create a new timeline (grouping of sources) within a case.

    ``field_mappings`` (issue #10) merges differently-named raw attribute keys
    into canonical fields at query time — validated against the attribute keys
    actually present in the selected sources.
    """
    store = get_store()
    if payload.field_mappings:
        await _check_field_mappings(case.id, payload.source_ids, payload.field_mappings)
    timeline_id = generate_id(payload.name)
    timeline = await store.create_timeline(
        case_id=case.id,
        timeline_id=timeline_id,
        name=payload.name,
        description=payload.description,
        source_ids=payload.source_ids,
        field_mappings=payload.field_mappings,
    )
    await store.record_audit(
        action="timeline.create",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"field_mappings": payload.field_mappings} if payload.field_mappings else None,
    )
    return {"timeline": timeline.to_dict()}


@router.patch("/{case_id}/timelines/{timeline_id}/field-mappings")
async def update_timeline_field_mappings(
    timeline_id: str,
    payload: TimelineFieldMappingsUpdate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Replace a timeline's field mappings (empty/None clears them).

    Mappings are auditable timeline metadata; the underlying events are never
    rewritten, which is why editing them post-creation is forensically sound.
    Every change lands in the audit trail with the before/after mapping.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case.id, timeline_id)
    new_mappings = payload.field_mappings or None
    if new_mappings:
        await _check_field_mappings(case.id, [s.id for s in sources], new_mappings)
    previous = timeline.field_mappings
    updated = await store.update_timeline_field_mappings(case.id, timeline_id, new_mappings)
    if updated is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.update_field_mappings",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"previous": previous, "new": new_mappings},
    )
    return {"timeline": updated.to_dict()}


@router.get("/{case_id}/fields/coverage")
async def get_field_coverage(
    source_ids: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Per-attribute-key coverage across the given sources, for the timeline wizard.

    ``source_ids`` is comma-separated. Returns, per raw field, which sources
    carry it (with non-empty counts and sample values) so the wizard can show
    merge candidates with real data next to them.
    """
    ids = [sid.strip() for sid in source_ids.split(",") if sid.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="source_ids must not be empty")
    service = EventQueryService()
    return await run_in_threadpool(service.field_coverage, case.id, ids)


@router.delete("/{case_id}/timelines/{timeline_id}")
async def delete_timeline(
    timeline_id: str,
    case: Case = Depends(require_case_manage),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a timeline.

    Deleting a timeline does *not* delete its sources, events, or vectors —
    those remain available in the default timeline and other groupings.
    """
    store = get_store()
    deleted = await store.delete_timeline(case.id, timeline_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.delete",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
    )
    return {"deleted": True, "timeline_id": timeline_id}


@router.get("/{case_id}/timelines/{timeline_id}/sources")
async def list_timeline_sources(
    timeline_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """List the sources attached to a timeline."""
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case.id, timeline_id)
    return {"sources": [s.to_dict() for s in sources]}


@router.post("/{case_id}/timelines/{timeline_id}/sources/{source_id}")
async def add_source_to_timeline(
    timeline_id: str,
    source_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Add a source to a timeline."""
    store = get_store()
    added = await store.add_source_to_timeline(case.id, timeline_id, source_id)
    if not added:
        raise HTTPException(
            status_code=400,
            detail="Source is already a member of the timeline, or one of the IDs was not found",
        )
    return {"added": True, "timeline_id": timeline_id, "source_id": source_id}


@router.delete("/{case_id}/timelines/{timeline_id}/sources/{source_id}")
async def remove_source_from_timeline(
    timeline_id: str,
    source_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Remove a source from a timeline."""
    store = get_store()
    removed = await store.remove_source_from_timeline(case.id, timeline_id, source_id)
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
async def list_views(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List all saved views for a case."""
    store = get_store()
    views = await store.list_views(case.id)
    return {"views": [v.to_dict() for v in views]}


@router.post("/{case_id}/views")
async def create_view(
    payload: ViewCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Create a new saved view within a case."""
    store = get_store()
    view_id = generate_id(payload.name)
    view = await store.create_view(
        case_id=case.id,
        view_id=view_id,
        name=payload.name,
        query=payload.query,
        view_filter=payload.filter,
    )
    return {"view": view.to_dict()}


@router.delete("/{case_id}/views/{view_id}")
async def delete_view(
    view_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a saved view."""
    store = get_store()
    deleted = await store.delete_view(case.id, view_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="View not found")
    return {"deleted": True, "view_id": view_id}


# ═════════════════════════════════════════════════════════════════════════════
# Annotations
# ═════════════════════════════════════════════════════════════════════════════


@router.get("/{case_id}/timelines/{timeline_id}/tags")
async def list_timeline_tags(
    timeline_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """Return the distinct user annotation-tag labels for a timeline's sources.

    Used to power tag autocomplete in the UI.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case.id, timeline_id)
    source_ids = [s.id for s in sources]
    tags = await store.list_distinct_tag_contents(case.id, source_ids)
    return {"tags": tags}


@router.get("/{case_id}/timelines/{timeline_id}/annotations")
async def list_timeline_annotations(
    timeline_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """List all annotations for a timeline's sources (used for event-table chips)."""
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case.id, timeline_id)
    source_ids = [s.id for s in sources]
    annotations = await store.list_source_annotations(case.id, source_ids)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.get("/{case_id}/sources/{source_id}/events/{event_id}/annotations")
async def list_event_annotations(
    source_id: str,
    event_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List annotations for a single event."""
    store = get_store()
    source = await store.get_source(case.id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    annotations = await store.list_annotations(case.id, source_id, event_id)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.post("/{case_id}/sources/{source_id}/events/{event_id}/annotations")
async def create_event_annotation(
    source_id: str,
    event_id: str,
    payload: AnnotationCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Add a tag or comment annotation to an event."""
    store = get_store()
    source = await store.get_source(case.id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    annotation_id = generate_id(f"{event_id}_{payload.annotation_type}")
    annotation = await store.create_annotation(
        case_id=case.id,
        source_id=source_id,
        event_id=event_id,
        annotation_id=annotation_id,
        annotation_type=payload.annotation_type,
        content=payload.content,
        created_by=user.id,
    )
    publish_annotation_change(case.id, None, event_id, user)
    return {"annotation": annotation.to_dict()}


@router.delete("/{case_id}/sources/{source_id}/events/{event_id}/annotations/{annotation_id}")
async def delete_event_annotation(
    source_id: str,
    event_id: str,
    annotation_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete an annotation."""
    store = get_store()
    source = await store.get_source(case.id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    deleted = await store.delete_annotation(case.id, event_id, annotation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Annotation not found")
    publish_annotation_change(case.id, None, event_id, user)
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

        # Use a fresh PostgresStore inside the worker thread, and run all of
        # its awaits in a single asyncio.run() loop: pooled asyncpg
        # connections are bound to the loop they were created on, so a second
        # asyncio.run() against the same store would check out a connection
        # whose futures belong to a closed loop ("attached to a different
        # loop"). Dispose the engine before the loop closes so no pooled
        # connection outlives it.
        store = PostgresStore()

        async def _finalize() -> None:
            try:
                await store.update_source_counts(
                    case_id=case_id,
                    source_id=source_id,
                    vector_count=result.vectors_inserted,
                )
            finally:
                await store.engine.dispose()

        asyncio.run(_finalize())
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

        # One asyncio.run() for every await against this store — a pooled
        # asyncpg connection is bound to the loop it was created on, so
        # calling asyncio.run() per statement hands loop-A connections to
        # loop B ("attached to a different loop"). Dispose the engine before
        # the loop closes so no pooled connection outlives it.
        store = PostgresStore()
        embedding_model = get_settings().embedding_model

        async def _finalize() -> None:
            try:
                await store.set_timeline_embedding(
                    case_id=case_id,
                    timeline_id=timeline_id,
                    model=embedding_model,
                    config=field_config or {},
                    config_hash=result.config_hash,
                    embedded_source_ids=source_ids,
                )
                # Update vector counts on each source.
                # EmbeddingPipeline processes all sources in one collection so
                # we set an approximate per-source count (total / n sources)
                # as a best effort; the authoritative vector count is
                # queryable from Qdrant directly.
                per_source = result.vectors_inserted // max(len(source_ids), 1)
                for sid in source_ids:
                    await store.update_source_counts(
                        case_id=case_id,
                        source_id=sid,
                        vector_count=per_source,
                    )
            finally:
                await store.engine.dispose()

        asyncio.run(_finalize())
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
    timeline_id: str,
    background_tasks: BackgroundTasks,
    body: EmbedRequest | None = None,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Embed all sources in a timeline with a single shared field config.

    This is the primary embedding entry point.  The wizard on the frontend
    computes a cross-source-cohesive field selection and submits it here.

    On success the timeline is marked as embedded with a snapshot of the
    current source set.  If sources are later added the timeline becomes
    *stale* (``is_stale=True`` in ``to_dict()``), prompting a re-embed.
    """
    store = get_store()
    case_id = case.id
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
        created_by=user.id,
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
