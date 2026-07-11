"""API routes for cases, sources, timelines, and annotations."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from tracesignal.api.deps import (
    AccessLevel,
    access_level_from_team_role,
    get_current_user,
    get_store,
    require_case_contribute,
    require_case_manage,
    require_case_read,
    require_password_current,
    resolve_case_access,
)
from tracesignal.api.uploads import receive_upload_to_tmp
from tracesignal.core.config import get_settings
from tracesignal.core.eta import ThroughputMeter
from tracesignal.core.events_bus import publish_annotation_change
from tracesignal.core.jobs import JobStore, get_job_store
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_mappings import validate_field_mappings
from tracesignal.db.field_stats import (
    ensure_source_field_stats,
    merged_field_coverage,
    merged_list_fields,
    refresh_source_field_stats,
)
from tracesignal.db.postgres import Case, PostgresStore, User, generate_id
from tracesignal.db.qdrant import QdrantStore
from tracesignal.ingestion.parser import detect_format
from tracesignal.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline
from tracesignal.models.embeddings import embeddings_available
from tracesignal.models.event import ParserConfig


class CaseCreate(BaseModel):
    """Payload to create a case."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    # None -> a personal case, visible only to its owner and admins.
    team_id: str | None = Field(default=None)


class CaseScopeUpdate(BaseModel):
    """Payload to change a case's team scope. ``team_id: None`` releases it to personal."""

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

    # "normal" retired: normality is a disposition (see routers/dispositions.py).
    annotation_type: str = Field(..., pattern="^(comment|tag)$")
    content: str = Field(..., min_length=1, max_length=4096)


class SourceUpdate(BaseModel):
    """Payload to update a source's editable metadata.

    Currently only the analyst-declared clock-skew correction
    (``time_offset_seconds``, W2). Bounded to ±10 years — a wider offset is
    always a data-entry error, never a real forensic clock drift, and an
    unbounded value would overflow the ClickHouse ``addSeconds`` correction.
    """

    time_offset_seconds: int = Field(..., ge=-315_576_000, le=315_576_000)


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
    # Ingest lifecycle of source_id at response time. For a duplicate hitting
    # the create_source race (another concurrent upload of the same bytes won
    # and is still ingesting), this lets the client show real progress
    # instead of claiming the file is already fully ingested.
    status: str = "ready"
    job_id: str | None = None


logger = logging.getLogger(__name__)

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


def _retain_file(tmp_path: Path, retention_path: Path) -> None:
    """Retain an uploaded file at its content-addressed path without a data copy.

    The retention path is content-addressed by hash, so an existing file there
    is guaranteed byte-identical — short-circuit. Otherwise hardlink
    (metadata-only; the ingestion job keeps reading and finally unlinking
    ``tmp_path``, which leaves the retained link untouched), falling back to a
    full copy when the OS temp dir and TS_SOURCE_RETENTION_PATH live on
    different filesystems.
    """
    if retention_path.exists():
        return
    retention_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(tmp_path, retention_path)
    except OSError:
        # EXDEV (cross-device) or a filesystem without hardlink support.
        shutil.copy2(tmp_path, retention_path)


# ═════════════════════════════════════════════════════════════════════════════
# Cases
# ═════════════════════════════════════════════════════════════════════════════


def _bulk_access_level(case: Case, user: User, role_by_team: dict[str, str]) -> AccessLevel:
    """`resolve_case_access` without the per-case membership query.

    The caller supplies the user's team→role map once, so listing N cases
    stays at one membership query total instead of N.
    """
    team_role = role_by_team.get(case.team_id) if case.team_id else None
    return access_level_from_team_role(case, user, team_role)


@router.get("/")
async def list_cases(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """List cases visible to the current user: their own, plus their teams' (all, if admin).

    Each case carries the caller's resolved ``access_level``
    (``none|read|contribute|manage``) so clients don't have to re-implement
    the access rules.
    """
    store = get_store()
    await store.init_schema()
    if user.is_admin:
        cases = await store.list_cases()
        role_by_team: dict[str, str] = {}
    else:
        memberships = await store.list_user_memberships(user.id)
        role_by_team = {m.team_id: m.role for m in memberships}
        cases = await store.list_cases_for_user(user.id, list(role_by_team))
    return {
        "cases": [
            {**c.to_dict(), "access_level": _bulk_access_level(c, user, role_by_team).name.lower()}
            for c in cases
        ]
    }


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
    access = await resolve_case_access(user, case)
    return {"case": {**case.to_dict(), "access_level": access.name.lower()}}


@router.get("/{case_id}")
async def get_case(
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Get a case by ID, with the caller's resolved ``access_level``."""
    access = await resolve_case_access(user, case)
    return {"case": {**case.to_dict(), "access_level": access.name.lower()}}


@router.patch("/{case_id}/scope")
async def update_case_scope(
    payload: CaseScopeUpdate,
    case: Case = Depends(require_case_manage),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Change a case's team scope: release a personal case to a team, move it to
    a different team, or release a team case back to personal (``team_id: null``).

    Requires MANAGE on the case as it stands (its owner for a personal case, or
    a manager of its current team). Assigning to a *new* team additionally
    requires being a manager of that target team, or an admin — mirroring the
    rule in ``create_case``, so scope changes can't be used to hand a case to a
    team the caller doesn't control.
    """
    store = get_store()
    new_team_id = payload.team_id
    if new_team_id:
        if not user.is_admin:
            membership = await store.get_membership(new_team_id, user.id)
            if membership is None or membership.role != "manager":
                raise HTTPException(
                    status_code=403,
                    detail="Only a team manager or admin can assign a case to this team",
                )
        if await store.get_team(new_team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")

    updated = await store.update_case_team(case.id, new_team_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Case not found")
    await store.record_audit(
        action="case.scope_change",
        actor=user,
        case_id=case.id,
        target_type="case",
        target_id=case.id,
        detail={"old_team_id": case.team_id, "new_team_id": new_team_id},
    )
    access = await resolve_case_access(user, updated)
    return {"case": {**updated.to_dict(), "access_level": access.name.lower()}}


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
    # The Postgres case row is the authoritative record that this evidence
    # exists — it is only removed after every event/vector cascade succeeded.
    # A failed cascade aborts with 502 so the delete stays visible and
    # retryable instead of leaving orphan events behind a "successful" delete.
    try:
        for source in sources:
            qdrant.delete_source_points(case_id, source.id)
            await asyncio.to_thread(ch.delete_source_events, case_id, source.id)
        qdrant.delete_case_collections(case_id)
    except Exception as exc:
        await store.record_audit(
            action="case.delete_failed",
            actor=user,
            case_id=case_id,
            target_type="case",
            target_id=case_id,
            detail={"error": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to delete case events from the event store; case was not deleted. "
            "Retry once the event store is reachable.",
        ) from exc
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


@router.patch("/{case_id}/sources/{source_id}")
async def update_source(
    source_id: str,
    payload: SourceUpdate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Set a source's analyst-declared clock-skew correction (W2).

    The offset is query-time-only metadata — it shifts how the source's events
    are filtered, ordered, bucketed and presented everywhere (explorer,
    histogram, export, detectors), and never mutates the ingested events. The
    previous and new values are recorded in the audit trail so the correction
    itself is forensically reproducible.
    """
    store = get_store()
    existing = await store.get_source(case.id, source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Source not found")
    previous = existing.time_offset_seconds
    updated = await store.set_source_time_offset(case.id, source_id, payload.time_offset_seconds)
    if updated is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if payload.time_offset_seconds != previous:
        await store.record_audit(
            action="source.update_offset",
            actor=user,
            case_id=case.id,
            target_type="source",
            target_id=source_id,
            detail={"previous": previous, "new": payload.time_offset_seconds},
        )
    return {"source": updated.to_dict()}


@router.get("/{case_id}/jobs")
async def list_case_jobs(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """List background jobs (ingest/embed/enrich) scoped to a case, newest-first."""
    job_store = get_job_store()
    jobs = job_store.list_by_case(case.id)
    return {"jobs": [j.to_dict() for j in jobs]}


async def _revalidate_stale_field_mappings(
    store: PostgresStore, case_id: str, source_id: str
) -> None:
    """Re-check a timeline's ``field_mappings`` once one of its sources becomes ready.

    ``create_timeline``/``update_timeline_field_mappings`` skip the
    inventory-dependent checks in ``validate_field_mappings`` when every
    selected source is still "ingesting" (there's no attribute inventory yet
    to check against) — so a mapping with a typo'd raw key can be saved
    unnoticed. There is no blocking re-validation once ingestion finishes
    (rejecting already-persisted timeline metadata post hoc would be a worse
    surprise than a stale mapping); instead this records an audit-log warning
    so the gap is forensically visible rather than silent.
    """
    timelines = await store.list_timelines_for_source(case_id, source_id)
    for timeline in timelines:
        if not timeline.field_mappings:
            continue
        sources = await store.list_timeline_sources(case_id, timeline.id)
        ready_ids = [s.id for s in sources if s.is_ready]
        if not ready_ids:
            continue
        keys = await _resolve_mapping_validation_keys(
            ClickHouseStore(), case_id, ready_ids, timeline.field_mappings
        )
        problems = validate_field_mappings(timeline.field_mappings, keys)
        if problems:
            logger.warning(
                "Timeline %r field_mappings are invalid against its now-ready sources: %s",
                timeline.id,
                "; ".join(problems),
            )
            await store.record_audit(
                action="timeline.field_mappings_stale",
                case_id=case_id,
                target_type="timeline",
                target_id=timeline.id,
                detail={"problems": problems, "source_id": source_id},
            )


async def _trigger_automatic_enrichments(
    store: PostgresStore,
    clickhouse: ClickHouseStore,
    job_store: JobStore,
    case_id: str,
    source_id: str,
) -> None:
    """Fire background enrichment jobs for every timeline configured to auto-run on this source.

    Called right after a source flips to "ready" — the same point
    ``_revalidate_stale_field_mappings`` uses, since that's the single place
    in the codebase that knows ingestion just succeeded. Skips any enricher
    that is currently unavailable (e.g. its required database was never
    uploaded); the config still exists and will fire again on the next
    ingestion once availability is restored.
    """
    from tracesignal.enrichers.jobs import (
        get_active_enricher_run,
        run_enrichment_job,
        spawn_tracked_enrichment_task,
        try_claim_enricher_run,
    )
    from tracesignal.enrichers.registry import get_cached_availability, get_enricher

    global_configs = await store.list_enricher_global_configs()
    default_auto_keys = {c.enricher_key for c in global_configs if c.auto_run_default}
    pairs = await store.list_automatic_enrichers_for_source(source_id, default_auto_keys)
    for timeline_id, enricher_key in pairs:
        enricher = get_enricher(enricher_key)
        availability = get_cached_availability(enricher_key)
        if enricher is None or availability is None or not availability.available:
            continue
        # Check before creating the job so a skip leaves no orphan pending
        # job in the store; check + create + claim happen in the same event-
        # loop tick, so there is no window for a competing claim.
        active = get_active_enricher_run(timeline_id, enricher_key)
        if active is not None:
            logger.info(
                "Enrichment %s already running for timeline %s (job %s); skipping auto-trigger",
                enricher_key,
                timeline_id,
                active,
            )
            continue
        job = job_store.create(
            kind="enrich", progress={"processed": 0, "total": 0}, created_by=None, case_id=case_id
        )
        try_claim_enricher_run(timeline_id, enricher_key, job.id)
        # create_task (not FastAPI BackgroundTasks) is deliberate: this runs
        # inside the background ingestion job, where no request scope exists.
        # spawn_tracked_enrichment_task keeps the strong reference that stops
        # asyncio garbage-collecting the run mid-flight.
        spawn_tracked_enrichment_task(
            run_enrichment_job(
                job_id=job.id,
                case_id=case_id,
                timeline_id=timeline_id,
                enricher_key=enricher_key,
                source_ids=[source_id],
                job_store=job_store,
                store=store,
                ch_store=clickhouse,
            )
        )


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

    # One meter per ingest run: feeds the byte-based progress stream through the
    # same Kalman throughput/ETA filter the CLI uses (see core/eta.py) so the
    # web job tray shows identical rate/ETA figures. Computed server-side, where
    # the callback sees every batch, rather than reconstructed from the UI's
    # sparse polling.
    meter = ThroughputMeter()

    def progress_callback(total: int, processed: int) -> None:
        metrics = meter.observe(total, processed)
        job_store.update(
            job_id,
            status="running",
            progress={"total": total, "processed": processed, **metrics.to_dict()},
        )

    try:
        pipeline = IngestionPipeline(
            case_id=case_id,
            source_id=source_id,
            clickhouse=clickhouse,
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
        # Only now does the source become visible to timeline queries,
        # detectors, and embedding (see events._resolve_timeline_scope).
        await store.set_source_status(case_id, source_id, "ready")
        # Precompute the per-source field-stats cache (M15). Isolated like the
        # auto-enrichment trigger below: a failure must never roll back a
        # successful ingest, and the read path self-heals on a cache miss.
        try:
            await refresh_source_field_stats(store, clickhouse, case_id, source_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Field-stats precompute failed for source %s (case %s); "
                "reads fall back to compute-on-demand",
                source_id,
                case_id,
            )
        await _revalidate_stale_field_mappings(store, case_id, source_id)
        # Auto-enrichment scheduling runs *after* the source is committed
        # "ready"; a failure here must never fall through to the ingest
        # rollback below (which would delete a fully-ingested source). Isolate
        # it — a missed auto-trigger self-heals on the next ingest or a manual
        # run, whereas a destroyed source does not.
        try:
            await _trigger_automatic_enrichments(store, clickhouse, job_store, case_id, source_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Auto-enrichment scheduling failed for source %s (case %s); "
                "ingest itself succeeded and is kept",
                source_id,
                case_id,
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
        # Best-effort rollback (the job is already failing; raising here helps
        # nobody) — but never silent: each failed step is logged and flagged on
        # the job so the orphaned partition/row is visible in the UI.
        cleanup_errors: list[str] = []
        try:
            await asyncio.to_thread(clickhouse.delete_source_events, case_id, source_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Ingest rollback: failed to delete events for source %s (case %s)",
                source_id,
                case_id,
            )
            cleanup_errors.append("event deletion failed")
        try:
            await store.delete_source(case_id, source_id)
            if not await store.source_hash_in_use(file_hash, exclude_source_id=source_id):
                _retention_path(file_hash).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Ingest rollback: failed to remove source row %s (case %s)",
                source_id,
                case_id,
            )
            cleanup_errors.append("source-row removal failed")
        error = str(exc)
        if cleanup_errors:
            error += f" (cleanup incomplete: {'; '.join(cleanup_errors)})"
        job_store.update(job_id, status="failed", error=error)
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

    # Copy to a temp file and hash in one pass, in a worker thread so a
    # multi-GB upload doesn't block the event loop, and capped by
    # TS_MAX_UPLOAD_BYTES so a single request can't fill the disk. Hashing
    # during the copy means the duplicate check now happens after the copy —
    # a duplicate upload costs one temp write, but the common (new file) path
    # reads the stream once instead of twice.
    max_bytes = get_settings().max_upload_bytes or None
    suffix = Path(file.filename or "upload").suffix or ".tmp"
    tmp_path, file_hash, size_bytes = await receive_upload_to_tmp(
        file, max_bytes=max_bytes, suffix=suffix
    )

    existing_source = await store.get_source_by_hash(case_id, file_hash)
    if existing_source is not None:
        tmp_path.unlink(missing_ok=True)
        return SourceUploadResponse(
            source_id=existing_source.id,
            events_parsed=existing_source.event_count,
            events_inserted=0,
            parser=parser or existing_source.parser or "auto",
            duplicate=True,
            status=existing_source.status,
        )

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
                        "pass an explicit parser (e.g. jsonl, timesketch_csv, "
                        "tracesignal_parquet)."
                    ),
                ) from exc
        source_id = generate_id(f"{case_id}:{file_hash}")
        source_name = name or file.filename or tmp_path.name

        # For interchange Parquet uploads, validate the footer now (a broken
        # file should 400 here, not fail the background job) and record the
        # embedded converter identity as the source's parser — that is the
        # real provenance, not the generic format string.
        source_parser = fmt
        if fmt in {"tracesignal_parquet", "parquet"}:
            from tracesignal.ingestion.parquet_reader import ParquetEventsParser

            # The ParserConfig here is a throwaway: read_source_meta only reads
            # the file's footer, and the real parser identity is taken from that
            # footer below (source_parser). This placeholder config is never
            # persisted or hashed.
            reader = ParquetEventsParser(
                case_id, source_id, ParserConfig(name=fmt, version="0.1.0")
            )
            try:
                parquet_meta = await run_in_threadpool(reader.read_source_meta, tmp_path)
            except ValueError as exc:
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            source_parser = f"{parquet_meta.converter_name}@{parquet_meta.converter_version}"

        # Retain the original file content-addressed by hash (hardlink fast
        # path; copy only across filesystems). Threadpool because the copy
        # fallback is a full I/O pass over the upload.
        retention_path = _retention_path(file_hash)
        await run_in_threadpool(_retain_file, tmp_path, retention_path)

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
                parser=source_parser,
                event_count=0,
                created_by=user.id,
                # Excluded from timeline queries/detectors/embedding until
                # the background job flips it to "ready".
                status="ingesting",
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
                status=existing_source.status,
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
            case_id=case_id,
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
        parser=source_parser,
        duplicate=False,
        status="ingesting",
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
    # The Postgres source row is the authoritative record that this evidence
    # exists — it is only removed after the event/vector cascades succeeded.
    # A failed cascade aborts with 502 so the delete stays visible and
    # retryable instead of leaving orphan events behind a "successful" delete.
    try:
        qdrant.delete_source_points(case_id, source_id)
        await asyncio.to_thread(ch.delete_source_events, case_id, source_id)
    except Exception as exc:
        await store.record_audit(
            action="source.delete_failed",
            actor=user,
            case_id=case_id,
            target_type="source",
            target_id=source_id,
            detail={"error": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to delete source events from the event store; source was not "
            "deleted. Retry once the event store is reachable.",
        ) from exc
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


async def _resolve_mapping_validation_keys(
    clickhouse: ClickHouseStore, case_id: str, source_ids: list[str], mappings: dict[str, list[str]]
) -> set[str]:
    """Return the attribute keys to validate *mappings* against.

    Starts from the cached, per-source-capped inventory (cheap, the common
    case) and only falls back to a live existence check for the mapping's raw
    keys that aren't in it — the cache caps attribute keys per source
    (``_MAX_ATTR_KEYS_PER_SOURCE`` in ``field_stats.py``) to bound its
    payload, so a real but low-coverage raw key can rank outside the cap and
    would otherwise be rejected as nonexistent.
    """
    stats = await ensure_source_field_stats(get_store(), clickhouse, case_id, source_ids)
    inventory = merged_list_fields(stats)
    keys = set(inventory["attributes"])
    missing_raw = {r for raws in mappings.values() for r in raws} - keys
    if missing_raw:
        present = await asyncio.to_thread(
            clickhouse.attribute_keys_present, case_id, source_ids, sorted(missing_raw)
        )
        keys |= present
    return keys


async def _check_field_mappings(
    case_id: str, source_ids: list[str], mappings: dict[str, list[str]]
) -> None:
    """Validate mappings against the sources' actual attribute keys; 422 on problems.

    ``source_ids`` should contain only *ready* sources — a half-ingested
    source's attribute inventory is incomplete and would reject mappings that
    are valid once ingestion finishes. With zero ready sources the structural
    rules still apply but the inventory-dependent checks are skipped (see
    ``validate_field_mappings``).
    """
    if source_ids:
        keys: set[str] | None = await _resolve_mapping_validation_keys(
            ClickHouseStore(), case_id, source_ids, mappings
        )
    else:
        keys = None
    problems = validate_field_mappings(mappings, keys)
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
        # Ingesting sources can still be timeline *members* — they're only
        # excluded from the inventory validation (and from queries) until
        # ready.
        sources = await store.list_sources(case.id)
        ready_ids = {s.id for s in sources if s.is_ready}
        validate_ids = [sid for sid in payload.source_ids if sid in ready_ids]
        await _check_field_mappings(case.id, validate_ids, payload.field_mappings)
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
        ready_ids = [s.id for s in sources if s.is_ready]
        await _check_field_mappings(case.id, ready_ids, new_mappings)
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
    merge candidates with real data next to them. Served from the per-source
    field-stats cache (M15) — counts are exact full-source totals, no longer
    a 20k-rows-per-source sample.
    """
    ids = [sid.strip() for sid in source_ids.split(",") if sid.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="source_ids must not be empty")
    stats = await ensure_source_field_stats(get_store(), ClickHouseStore(), case.id, ids)
    return merged_field_coverage(stats)


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
    """Optional body for the timeline embed endpoint.

    When ``embedding_config`` is provided it drives per-artifact field
    selection and is persisted on the timeline after a successful run.  Omit
    the body (or send an empty object) to reuse the timeline's stored config,
    falling back to legacy all-fields behaviour when none has been saved.
    """

    embedding_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            'Per-artifact field selection. Shape: {"version": 1, "artifacts": '
            '{"<artifact>": ["message", "attr:k", ...]}}'
        ),
    )


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
    if not embeddings_available():
        # Fail at request time instead of creating a job that instantly dies
        # with an ImportError in the background worker.
        raise HTTPException(
            status_code=503,
            detail=(
                "Embedding support is not installed. Install the 'embeddings' extra "
                "(uv sync --extra embeddings) or configure TS_EMBEDDING_API_BASE_URL "
                "to use a remote embedding endpoint."
            ),
        )
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
    ingesting = [s.name for s in sources if not s.is_ready]
    if ingesting:
        # Embedding a half-ingested source would persist vectors over an
        # incomplete event set — refuse outright rather than silently
        # embedding a partial timeline (the run is expensive and its
        # source-set snapshot would immediately go stale anyway).
        raise HTTPException(
            status_code=409,
            detail=(
                "Source(s) still ingesting: "
                + ", ".join(ingesting)
                + ". Wait for ingestion to finish before embedding."
            ),
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
        case_id=case_id,
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


class TimelineEnricherConfigUpdate(BaseModel):
    """Payload to enable/configure an enricher for a timeline."""

    mode: str = Field(..., pattern="^(automatic|manual)$")
    enabled: bool


@router.get("/{case_id}/timelines/{timeline_id}/enrichers")
async def list_timeline_enrichers(
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List every *available* enricher for this timeline, with eligibility and current config.

    Enrichers that fail their availability check (e.g. GeoIP with no
    uploaded database) are omitted entirely — they should not appear in the
    GUI until an admin makes them available.
    """
    from tracesignal.enrichers.base import effective_enricher_state
    from tracesignal.enrichers.registry import all_enrichers, get_cached_availability

    store = get_store()
    case_id = case.id
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    sources = await store.list_timeline_sources(case_id, timeline_id)
    ready_source_ids = [s.id for s in sources if s.is_ready]
    configs = {c.enricher_key: c for c in await store.list_timeline_enrichers(timeline_id)}
    global_defaults = {
        c.enricher_key: c.auto_run_default for c in await store.list_enricher_global_configs()
    }

    available = [
        enricher
        for enricher in all_enrichers()
        if (availability := get_cached_availability(enricher.key)) is not None
        and availability.available
    ]

    # Each eligibility check is a ClickHouse scan; run them concurrently so
    # dialog latency stays flat as more enrichers get registered.
    # clickhouse_connect clients are not thread-safe, so each check builds its
    # own ClickHouseStore inside its worker thread instead of sharing one
    # client across the fan-out.
    def _check_one(enricher):
        return enricher.check_eligibility(ClickHouseStore(), case_id, ready_source_ids)

    eligibilities = await asyncio.gather(
        *(run_in_threadpool(_check_one, enricher) for enricher in available)
    )
    result = []
    for enricher, eligibility in zip(available, eligibilities, strict=True):
        config = configs.get(enricher.key)
        enabled, mode = effective_enricher_state(
            config.enabled if config else None,
            config.mode if config else None,
            global_defaults.get(enricher.key, False),
        )
        result.append(
            {
                "key": enricher.key,
                "display_name": enricher.display_name,
                "description": enricher.description,
                "eligible": eligibility.eligible,
                "sample_checked": eligibility.sample_checked,
                "sample_matched": eligibility.sample_matched,
                "mode": mode,
                "enabled": enabled,
            }
        )
    return {"enrichers": result}


@router.put("/{case_id}/timelines/{timeline_id}/enrichers/{enricher_key}")
async def set_timeline_enricher_config(
    timeline_id: str,
    enricher_key: str,
    body: TimelineEnricherConfigUpdate,
    case: Case = Depends(require_case_manage),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Enable/disable an enricher for a timeline and set its trigger mode."""
    from tracesignal.enrichers.registry import get_enricher

    if get_enricher(enricher_key) is None:
        raise HTTPException(status_code=404, detail="Unknown enricher")

    store = get_store()
    case_id = case.id
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    config = await store.upsert_timeline_enricher(
        timeline_id=timeline_id,
        enricher_key=enricher_key,
        mode=body.mode,
        enabled=body.enabled,
        updated_by=user.id,
    )
    await store.record_audit(
        action="timeline.enricher_config",
        actor=user,
        case_id=case_id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"enricher_key": enricher_key, "mode": body.mode, "enabled": body.enabled},
    )
    return {"enricher": config.to_dict()}


@router.post("/{case_id}/timelines/{timeline_id}/enrichers/{enricher_key}/run")
async def run_timeline_enricher(
    timeline_id: str,
    enricher_key: str,
    background_tasks: BackgroundTasks,
    force: bool = False,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Manually trigger an enrichment run for a timeline's sources.

    ``force=true`` re-enriches every ready source, ignoring provenance rows.
    This is the analyst-facing recovery path when provenance claims a source
    is enriched but its events say otherwise (e.g. provenance recorded off a
    partially-applied run by a pre-session-48c build) — the apply is
    idempotent, so forcing is always safe, just a full re-scan.
    """
    from tracesignal.enrichers.jobs import (
        get_active_enricher_run,
        run_enrichment_job,
        try_claim_enricher_run,
    )
    from tracesignal.enrichers.registry import get_cached_availability, get_enricher

    enricher = get_enricher(enricher_key)
    if enricher is None:
        raise HTTPException(status_code=404, detail="Unknown enricher")
    availability = get_cached_availability(enricher_key)
    if availability is None or not availability.available:
        raise HTTPException(status_code=409, detail="Enricher is not currently available")

    store = get_store()
    case_id = case.id
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    sources = await store.list_timeline_sources(case_id, timeline_id)
    source_ids = [s.id for s in sources if s.is_ready]
    if not source_ids:
        raise HTTPException(status_code=422, detail="Timeline has no ready sources to enrich")

    # Skip sources already enriched at the current config: a source's derived
    # fields live on its ClickHouse partition, not on the timeline, so a source
    # carried into a new timeline is already enriched. config_hash folds in the
    # enricher config *and* data version (for GeoIP, the installed database's
    # hash), so an admin swapping the .mmdb bumps the hash and forces a re-run.
    # Compute off the loop — config_extras() reads the sidecar/file from disk.
    config_hash = await asyncio.to_thread(enricher.config_hash)
    skipped_source_ids: list[str] = []
    if not force:
        already = await store.list_enriched_source_ids(case_id, enricher_key, config_hash)
        skipped_source_ids = [sid for sid in source_ids if sid in already]
        source_ids = [sid for sid in source_ids if sid not in already]
    if not source_ids:
        return {
            "job_id": None,
            "status": "skipped",
            "source_ids": [],
            "skipped_source_ids": skipped_source_ids,
        }

    active_job_id = get_active_enricher_run(timeline_id, enricher_key)
    if active_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Enrichment already running (job {active_job_id})",
        )

    job_store = get_job_store()
    # Construct the ClickHouse client *before* claiming the run slot: its
    # constructor can raise when ClickHouse is unreachable, and a claim taken
    # before a raise would never be released (the job never starts, so its
    # finally-block release never runs), wedging this (timeline, enricher) at
    # 409 until restart.
    ch_store = ClickHouseStore()
    job = job_store.create(
        kind="enrich",
        progress={"processed": 0, "total": 0},
        created_by=user.id,
        case_id=case_id,
    )
    # Claim now (before the response) so a double-click is rejected with 409
    # even though the job itself only starts after the response is sent.
    try_claim_enricher_run(timeline_id, enricher_key, job.id)
    background_tasks.add_task(
        run_enrichment_job,
        job_id=job.id,
        case_id=case_id,
        timeline_id=timeline_id,
        enricher_key=enricher_key,
        source_ids=source_ids,
        job_store=job_store,
        store=store,
        ch_store=ch_store,
    )
    await store.record_audit(
        action="enricher.manual_run",
        actor=user,
        case_id=case_id,
        target_type="timeline",
        target_id=timeline_id,
        detail={
            "enricher_key": enricher_key,
            "job_id": job.id,
            "force": force,
            "source_ids": source_ids,
            "skipped_source_ids": skipped_source_ids,
        },
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "source_ids": source_ids,
        "skipped_source_ids": skipped_source_ids,
    }
