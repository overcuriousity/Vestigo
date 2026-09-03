"""API routes for cases, sources, timelines, and annotations."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from vestigo.api.deps import (
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
from vestigo.api.routers.analysis import DetectorEntryIn, validate_detector_entry
from vestigo.api.uploads import receive_upload_to_tmp
from vestigo.columns.jobs import (
    get_active_recommendation,
    schedule_for_source,
    start_column_recommendation,
)
from vestigo.core.config import get_settings
from vestigo.core.eta import ThroughputMeter
from vestigo.core.events_bus import publish_annotation_change
from vestigo.core.jobs import JobStore, get_job_store
from vestigo.core.retention import retain_file as _retain_file
from vestigo.core.retention import retention_path as _retention_path
from vestigo.db.analysis_plan import FIELD_OVERRIDE_METHOD_IDS, METHOD_IDS
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.field_mappings import validate_field_mappings
from vestigo.db.field_stats import (
    ensure_source_field_stats,
    merged_field_coverage,
    merged_list_fields,
    refresh_source_field_stats,
)
from vestigo.db.postgres import (
    Case,
    EnrichmentJobRun,
    PostgresStore,
    Source,
    Timeline,
    User,
    generate_id,
)
from vestigo.db.qdrant import QdrantStore
from vestigo.ingestion.parser import detect_format
from vestigo.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline
from vestigo.models.availability import unavailable_detail
from vestigo.models.embeddings import embeddings_available
from vestigo.models.event import ParserConfig


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


class TimelineFieldOverridesUpdate(BaseModel):
    """Payload to replace a timeline's per-method field overrides (None/{} clears them)."""

    field_overrides: dict[str, dict[str, bool]] | None = Field(default=None)


class ViewCreate(BaseModel):
    """Payload to create a saved view."""

    name: str = Field(..., min_length=1, max_length=255)
    query: str = Field(default="")
    filter: dict[str, Any] = Field(default_factory=dict)


class ViewRename(BaseModel):
    """Payload to rename a saved view."""

    name: str = Field(..., min_length=1, max_length=255)


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

    An admin sees every case except other users' seeded demo cases: those are
    identical fabricated copies, one per account, and listing fifty of them
    buries the real work. They remain reachable by id, and deleting a user
    still cascades theirs.
    """
    store = get_store()
    if user.is_admin:
        cases = [
            case
            for case in await store.list_cases()
            if not case.is_demo or case.owner_id == user.id
        ]
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
    """Delete a case and cascade-remove all its sources, timelines, events, and vectors.

    A case carrying sealed story exports is admin-only to delete, and the
    deleted exports' hashes go into the audit record. Deleting a single export
    is admin-only because an export is an immutable attestation
    (``routers/stories.py``), and a story carrying any is admin-only for the
    same reason — the case cascade takes them too, so without the same gate it
    would be the way around both.
    """
    store = get_store()
    case_id = case.id

    attestations = await store.list_case_export_attestations(case_id)
    if attestations and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                f"case has {len(attestations)} sealed story export(s); "
                "deleting it requires an administrator"
            ),
        )

    qdrant = QdrantStore()
    ch = ClickHouseStore()

    sources = await store.list_sources(case_id)
    # The Postgres case row is the authoritative record that this evidence
    # exists — it is only removed after every event/vector cascade succeeded.
    # A failed cascade aborts with 502 so the delete stays visible and
    # retryable instead of leaving orphan events behind a "successful" delete.
    try:
        for source in sources:
            await asyncio.to_thread(qdrant.delete_source_points, case_id, source.id)
            await asyncio.to_thread(ch.delete_source_events, case_id, source.id)
        await asyncio.to_thread(qdrant.delete_case_collections, case_id)
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
        # The rows are gone; the audit log is the only place the attestations
        # survive. Omitted entirely when there were none, rather than logging
        # an empty list on every ordinary case delete.
        detail={"story_exports": attestations} if attestations else None,
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
    from vestigo.enrichers.jobs import (
        get_active_enricher_run,
        oldest_unfinished_run,
        run_enrichment_job,
        spawn_tracked_enrichment_task,
        try_claim_enricher_run,
    )
    from vestigo.enrichers.registry import get_cached_availability, get_enricher

    global_configs = await store.list_enricher_global_configs()
    default_auto_keys = {c.enricher_key for c in global_configs if c.auto_run_default}
    pairs = await store.list_automatic_enrichers_for_source(source_id, default_auto_keys)
    for timeline_id, enricher_key in pairs:
        enricher = get_enricher(enricher_key)
        availability = get_cached_availability(enricher_key)
        if enricher is None or availability is None or not availability.available:
            continue
        # Skip (never raise) when an unfinished run is waiting to be resumed:
        # a fresh run would strand its staged rows, and this code path runs
        # inside the ingestion job where an exception is swallowed with a
        # misleading log. The analyst resumes it from the enrichers dialog.
        # Awaited here rather than below so the check/create/claim sequence
        # stays await-free.
        #
        # A *live* run has a marker too, so exclude the job currently holding
        # the run slot — same "marker present and slot not held by it" rule the
        # enrichers dialog applies. Without it, a healthy auto-run still going
        # on a sibling source would be reported as needing a resume that would
        # only 409; the truthful message is the "already running" one below.
        # This read is for message accuracy only — the authoritative slot check
        # is re-read after the await.
        running_job_id = get_active_enricher_run(timeline_id, enricher_key)
        markers = await store.list_enrichment_job_runs_for_timeline(
            case_id, timeline_id, enricher_key=enricher_key
        )
        dead = oldest_unfinished_run([m for m in markers if m.job_id != running_job_id])
        if dead is not None:
            logger.info(
                "Skipping auto-enrichment %s for timeline %s: unfinished run %s must be "
                "resumed first (enrichers dialog)",
                enricher_key,
                timeline_id,
                dead.job_id,
            )
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
        # Checked, not discarded: the read above is only as authoritative as the
        # absence of an await between it and here, and that is an invariant a
        # later edit can break silently. The claim itself cannot be raced.
        conflict = try_claim_enricher_run(timeline_id, enricher_key, job.id)
        if conflict is not None:
            job_store.update(job.id, status="failed", error="Enrichment already running")
            logger.info(
                "Enrichment %s already running for timeline %s (job %s); skipping auto-trigger",
                enricher_key,
                timeline_id,
                conflict,
            )
            continue
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
        # Recommended columns follow the field-stats precompute above, since
        # that is what they read. Isolated for the same reason as the
        # auto-enrichment trigger below: a failure here must never fall through
        # to the ingest rollback, which would delete a fully-ingested source
        # over a cosmetic suggestion. A missed run self-heals on the next
        # ingest or a manual re-run from the Columns picker.
        try:
            await schedule_for_source(store, clickhouse, job_store, case_id, source_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Column recommendation scheduling failed for source %s (case %s); "
                "the explorer falls back to its built-in default columns",
                source_id,
                case_id,
            )
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


@dataclass
class RegisteredSource:
    """What :func:`register_source_for_ingest` produced.

    ``duplicate_of`` set means nothing was created: the bytes already exist as
    that source in this case (pre-check or lost race) and the caller answers
    with it. For a generated-converter registration it can also mean the same
    saved script already turned the same raw file into that source
    (``ix_sources_converter_input``) — a converter's Parquet is not
    byte-stable, so ``file_hash`` alone would let that evidence land twice.
    """

    source_id: str
    parser: str
    fmt: str
    duplicate_of: Source | None = None


async def register_source_for_ingest(
    *,
    store: PostgresStore,
    case_id: str,
    tmp_path: Path,
    file_hash: str,
    size_bytes: int,
    filename: str | None,
    name: str | None,
    parser: str | None,
    user: User,
    converter_script_id: str | None = None,
    converter_input_hash: str | None = None,
) -> RegisteredSource:
    """Dedup, detect the format, validate a Parquet footer, retain, create the row.

    Shared by the upload endpoint and the generated-converter job so both
    register a file the same way. Raises ``HTTPException`` (400) for an
    undetectable format or an invalid Parquet footer; the caller keeps
    ownership of ``tmp_path`` on every path except the duplicate ones, where
    it is unlinked here.
    """

    async def _existing() -> Source | None:
        found = await store.get_source_by_hash(case_id, file_hash)
        if found is None and converter_script_id and converter_input_hash:
            found = await store.get_source_by_converter_input(
                case_id, converter_script_id, converter_input_hash
            )
        return found

    existing_source = await _existing()
    if existing_source is not None:
        tmp_path.unlink(missing_ok=True)
        return RegisteredSource(
            source_id=existing_source.id,
            parser=existing_source.parser or "auto",
            fmt=existing_source.parser or "auto",
            duplicate_of=existing_source,
        )

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
                    f"Cannot detect parser format for {filename!r}; "
                    "pass an explicit parser (e.g. jsonl, timesketch_csv, "
                    "vestigo_parquet)."
                ),
            ) from exc
    source_id = generate_id(f"{case_id}:{file_hash}")
    source_name = name or filename or tmp_path.name

    # For interchange Parquet uploads, validate the footer now (a broken
    # file should 400 here, not fail the background job) and record the
    # embedded converter identity as the source's parser — that is the
    # real provenance, not the generic format string.
    source_parser = fmt
    if fmt in {"vestigo_parquet", "parquet"}:
        from vestigo.ingestion.parquet_reader import ParquetEventsParser

        # The ParserConfig here is a throwaway: read_source_meta only reads
        # the file's footer, and the real parser identity is taken from that
        # footer below (source_parser). This placeholder config is never
        # persisted or hashed.
        reader = ParquetEventsParser(case_id, source_id, ParserConfig(name=fmt, version="0.1.0"))
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
            filename=filename,
            parser=source_parser,
            event_count=0,
            created_by=user.id,
            # Excluded from timeline queries/detectors/embedding until
            # the background job flips it to "ready".
            status="ingesting",
            converter_script_id=converter_script_id,
            converter_input_hash=converter_input_hash,
        )
    except IntegrityError:
        # Lost a race against a concurrent upload of the same bytes (or a
        # concurrent run of the same script over the same raw file): treat it
        # the same as the pre-check duplicate response.
        existing_source = await _existing()
        if existing_source is None:
            raise
        tmp_path.unlink(missing_ok=True)
        return RegisteredSource(
            source_id=existing_source.id,
            parser=existing_source.parser or "auto",
            fmt=fmt,
            duplicate_of=existing_source,
        )

    # Auto-add the new source to the case's default timeline. From here on the
    # row exists with status="ingesting", and nothing else will ever flip it:
    # a failure must take the row (and an unshared retention copy) back out,
    # or every re-upload of these bytes reads as "duplicate, still ingesting".
    try:
        default_timeline = await store.get_default_timeline(case_id)
        if default_timeline is not None:
            await store.add_source_to_timeline(case_id, default_timeline.id, source_id)
    except Exception:
        try:
            await store.delete_source(case_id, source_id)
            if not await store.source_hash_in_use(file_hash, exclude_source_id=source_id):
                retention_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — the original error is the one to surface
            logger.exception(
                "Registration rollback: failed to remove source row %s (case %s)",
                source_id,
                case_id,
            )
        raise

    return RegisteredSource(source_id=source_id, parser=source_parser, fmt=fmt)


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
    # VESTIGO_MAX_UPLOAD_BYTES so a single request can't fill the disk. Hashing
    # during the copy means the duplicate check now happens after the copy —
    # a duplicate upload costs one temp write, but the common (new file) path
    # reads the stream once instead of twice.
    max_bytes = get_settings().max_upload_bytes or None
    suffix = Path(file.filename or "upload").suffix or ".tmp"
    tmp_path, file_hash, size_bytes = await receive_upload_to_tmp(
        file, max_bytes=max_bytes, suffix=suffix
    )

    try:
        reg = await register_source_for_ingest(
            store=store,
            case_id=case_id,
            tmp_path=tmp_path,
            file_hash=file_hash,
            size_bytes=size_bytes,
            filename=file.filename,
            name=name,
            parser=parser,
            user=user,
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    if reg.duplicate_of is not None:
        existing_source = reg.duplicate_of
        return SourceUploadResponse(
            source_id=existing_source.id,
            events_parsed=existing_source.event_count,
            events_inserted=0,
            parser=parser or existing_source.parser or "auto",
            duplicate=True,
            status=existing_source.status,
        )
    source_id, source_parser = reg.source_id, reg.parser

    try:
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
            reg.fmt,
            file_hash,
            file.filename or tmp_path.name,
            file.filename,
            size_bytes,
            user,
            job_store,
        )
    except Exception:
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
        await asyncio.to_thread(qdrant.delete_source_points, case_id, source_id)
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
    # Same settling as the single-timeline read (#213): a list that keeps
    # reporting `running` for a job that died would have the two endpoints
    # disagreeing about the same timeline, and callers that only ever list
    # (the timelines page) would never see it resolve. Batched — a restart that
    # orphaned several would otherwise cost one round trip each on a page that
    # is opened constantly.
    await _settle_dead_recommendations(store, case.id, timelines)
    return {"timelines": [t.to_dict() for t in timelines]}


def _recommendation_is_dead(timeline: Timeline) -> bool:
    """Whether this timeline claims a ``running`` job that no longer exists (#213).

    The explorer polls on the word ``running``, and ``JobStore`` is in-memory:
    a job killed as a cancelled task (rather than with the whole process) is
    never settled by the boot-time sweep, and the timeline would claim to be
    thinking forever. Answering it needs both checks — ``_ACTIVE`` covers a job
    that is genuinely mid-flight, and the job store covers one that finished
    without writing (which the placeholder rollback normally handles).

    Pure and synchronous, so the list path can filter the whole page before
    touching the database at all. False for every timeline that is not
    mid-recommendation — which is all of them, almost always.

    **Single-process, like everything it reads.** Both ``_ACTIVE`` and the job
    store live in this process's memory, so under ``uvicorn --workers N`` a
    second worker would read another worker's live job as dead and relabel the
    payload; that job then writes its real answer anyway, so the visible damage
    is a spinner ending early. Inherited from ``JobStore``, not introduced here
    — ``docs/ROADMAP.md`` §"Explicitly out of scope" carries the standing
    decision and names this as one more thing multi-process scale-out has to
    move to a shared backend.
    """
    payload = timeline.recommended_columns
    if not isinstance(payload, dict) or payload.get("status") != "running":
        return False
    if get_active_recommendation(timeline.id) is not None:
        return False
    job_id = payload.get("job_id")
    return not (job_id and get_job_store().get(job_id) is not None)


async def _settle_dead_recommendations(
    store: PostgresStore, case_id: str, timelines: list[Timeline]
) -> None:
    """Relabel every dead ``running`` suggestion in *timelines*, in one write.

    Mutates the passed timelines in place so the caller serializes the settled
    payloads without a second read. Returns without touching the database when
    nothing is stale, which is the overwhelmingly common case.

    **This writes from a ``require_case_read`` endpoint, deliberately.** A
    read-only member is the one caller who can never repair the row any other
    way — they cannot re-run the job, and the alternative is a timeline that
    reports "suggesting columns…" at them forever. The write is bounded to what
    that makes safe: it only ever relabels a ``running`` payload whose job is
    provably gone, it never recomputes, it touches no evidence and no
    analyst-authored content, the settled columns are the ones already stored,
    and the endpoint's own access check still governs *which* case's rows are
    even considered. It is housekeeping on display metadata, not a mutation the
    caller authored, which is also why it records no audit row — an audit trail
    that logged "read-only user changed a timeline" every time a process
    restarted would be describing something that did not happen.
    """
    stale = [t for t in timelines if _recommendation_is_dead(t)]
    if not stale:
        return
    settled = await store.settle_running_recommendations(case_id, [t.id for t in stale])
    for timeline in stale:
        if timeline.id in settled:
            timeline.recommended_columns = settled[timeline.id]


@router.get("/{case_id}/timelines/{timeline_id}")
async def get_timeline(timeline_id: str, case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Get a single timeline by ID."""
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await _settle_dead_recommendations(store, case.id, [timeline])
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
    # A timeline built from already-ingested sources never passes the
    # post-ingest hook, so it would otherwise open on the built-in defaults
    # until someone asked for a suggestion by hand.
    start_column_recommendation(
        case_id=case.id,
        timeline_id=timeline_id,
        job_store=get_job_store(),
        store=store,
        actor_id=user.id,
        actor_username=user.username,
    )
    return {"timeline": timeline.to_dict()}


class RecommendColumnsRequest(BaseModel):
    """Whether this run may consult the LLM (issue #213)."""

    use_ai: bool = False


@router.post("/{case_id}/timelines/{timeline_id}/recommend-columns")
async def recommend_timeline_columns(
    timeline_id: str,
    payload: RecommendColumnsRequest | None = None,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Re-derive this timeline's recommended event-grid columns (issue #213).

    The same job the post-ingest hook runs, started by hand from the Columns
    picker. Contribute access, because the result is shared with everyone who
    can see the timeline — a read-only member changing what the timeline opens
    on for the whole case would be a surprise. Returns ``job_id: null`` when a
    job for this timeline is already in flight, so the caller can say so rather
    than showing a job that never appears.

    ``use_ai`` is the analyst's per-timeline opt-in to the advisor
    (``columns/advisor.py``), which sends candidate field names and up to three
    real sample values per field to the configured model endpoint. This
    authenticated, explicit request *is* the authorization — the acknowledgement
    stored in the caller's preferences is the UI's memory of having shown the
    disclosure, not a second gate. Every other trigger for this job leaves it
    False and stays local.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    use_ai = bool(payload and payload.use_ai)
    job_id = start_column_recommendation(
        case_id=case.id,
        timeline_id=timeline_id,
        job_store=get_job_store(),
        store=store,
        actor_id=user.id,
        actor_username=user.username,
        use_llm=use_ai,
    )
    return {"job_id": job_id, "use_ai": use_ai}


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


@router.patch("/{case_id}/timelines/{timeline_id}/field-overrides")
async def update_timeline_field_overrides(
    timeline_id: str,
    payload: TimelineFieldOverridesUpdate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Replace the per-method field declarations for this timeline.

    ``{method_id: {field_token: bool}}`` — True pins a field into that
    detector's automatic selection, False takes it out, absent leaves the
    recommender's own answer standing. It exists because the recommenders type
    fields *syntactically*: an HTTP status code parses as a number, so the range
    detector offers it and then reports every 500 as an outlier forever. Which
    detector reads which field is the analyst's call; the recommenders suggest.

    Like a mute, this is advice and not a gate. It steers only the automatic
    selection — ``/analysis/findings`` with an explicit ``fields`` still scans
    an excluded field, the analysis plan does not consult it, and a run that
    held a field back says so in its warnings. So every change owes an audit
    row, and unknown method ids are rejected rather than stored, where they
    would read as a deliberate declaration that never applied to anything.

    A *known* method that selects no fields is rejected for that same reason:
    ``frequency`` and ``sequence_novelty`` take a single ``series_field``,
    ``timestamp_order`` reads no field, and ``log_template`` clusters the
    message text, so a declaration against any of them would be audited and
    rendered as "declared" while the detector went on scanning exactly as
    before, without so much as a warning to say so.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    requested = payload.field_overrides or {}
    unknown = sorted(set(requested) - set(METHOD_IDS))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown analysis method(s): {', '.join(unknown)}",
        )
    fieldless = sorted(set(requested) - FIELD_OVERRIDE_METHOD_IDS)
    if fieldless:
        raise HTTPException(
            status_code=422,
            detail=(
                "These analysis method(s) select no fields, so a field declaration "
                f"would never apply to them: {', '.join(fieldless)}"
            ),
        )
    empty_tokens = sorted(
        {m for m, fields in requested.items() for tok in fields if not tok.strip()}
    )
    if empty_tokens:
        raise HTTPException(
            status_code=422,
            detail=f"Empty field token declared for: {', '.join(empty_tokens)}",
        )
    previous = timeline.field_overrides or {}
    updated = await store.update_timeline_field_overrides(case.id, timeline_id, requested)
    if updated is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.update_field_overrides",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"previous": previous, "new": updated.field_overrides or {}},
    )
    return {"timeline": updated.to_dict()}


@router.put("/{case_id}/timelines/{timeline_id}/detectors/{method}")
async def set_timeline_detector(
    timeline_id: str,
    method: str,
    payload: DetectorEntryIn,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Configure ``method`` on this timeline, replacing an existing entry in place.

    The configured list is the only thing the Investigate rail runs, so what
    it may hold is held to the runner's contract: params are validated with
    the same models ``/analysis/findings`` uses, the frame/baseline pair must
    describe one question, and a baseline frame must name a definition that
    exists on *this* timeline — an id from another timeline would store a
    comparison that can never be run. Every change is audited: which
    detectors an investigation ran is part of its record.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    entry = validate_detector_entry(method, payload)
    if entry["baseline_id"] is not None:
        baseline = await store.get_baseline_definition(case.id, timeline_id, entry["baseline_id"])
        if baseline is None:
            raise HTTPException(
                status_code=422,
                detail=f"No baseline definition {entry['baseline_id']} on this timeline.",
            )
    entry["added_by"] = user.id
    entry["added_at"] = datetime.now(UTC).isoformat()
    previous = next((e for e in (timeline.detectors or []) if e.get("method") == method), None)
    updated = await store.set_timeline_detector(case.id, timeline_id, entry)
    if updated is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.set_detector",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"method": method, "previous": previous, "new": entry},
    )
    return {"timeline": updated.to_dict()}


@router.delete("/{case_id}/timelines/{timeline_id}/detectors/{method}")
async def remove_timeline_detector(
    timeline_id: str,
    method: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Take ``method`` out of this timeline's configured detectors.

    404 when it was not configured: a delete that "succeeds" against nothing
    would audit a removal that removed nothing.
    """
    store = get_store()
    timeline = await store.get_timeline(case.id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    previous = next((e for e in (timeline.detectors or []) if e.get("method") == method), None)
    if previous is None:
        raise HTTPException(status_code=404, detail=f"{method} is not configured on this timeline")
    updated = await store.remove_timeline_detector(case.id, timeline_id, method)
    if updated is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    await store.record_audit(
        action="timeline.remove_detector",
        actor=user,
        case_id=case.id,
        target_type="timeline",
        target_id=timeline_id,
        detail={"method": method, "previous": previous, "new": None},
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


@router.patch("/{case_id}/views/{view_id}")
async def rename_view(
    view_id: str,
    payload: ViewRename,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Rename a saved view."""
    store = get_store()
    view = await store.rename_view(case.id, view_id, payload.name)
    if view is None:
        raise HTTPException(status_code=404, detail="View not found")
    return {"view": view.to_dict()}


@router.delete("/{case_id}/views/{view_id}")
async def delete_view(
    view_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a saved view, or hide it when a story block still references it.

    A ``view_ref`` block resolves its View at render and export time, so
    removing one out from under a story would make that story's export fail.
    Such a view is hidden instead, and swept once the last block referencing it
    goes away; ``hidden`` in the response is what lets the UI say which of the
    two happened. ``deleted`` reports whether the row is actually gone — a
    client that reads only that field must not be told the view no longer
    exists when it does.
    """
    store = get_store()
    outcome = await store.delete_view(case.id, view_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="View not found")
    return {
        "deleted": outcome == "deleted",
        "view_id": view_id,
        "hidden": outcome == "hidden",
    }


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
            detail=unavailable_detail(),
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


class EnricherResumeRequest(BaseModel):
    """Body for resuming an unfinished enrichment run."""

    job_id: str = Field(..., min_length=1, max_length=64)


def _unfinished_run_payload(
    run: EnrichmentJobRun | None, staged: dict[str, tuple[int, set[str]]]
) -> dict[str, Any] | None:
    """Describe a dead enrichment run for the enrichers dialog, or None.

    ``partial_sources`` is the partial-coverage signal: staged sources that the
    run never finished staging, which get their values applied but stay
    provenance-free and re-runnable. It is computed as a set difference rather
    than by comparing ``staged_sources`` with ``completed_sources``, because
    those two are not measured the same way — ``staged_sources`` is what is
    *still* staged now, and a resume that dies partway through has already
    deleted the staged rows of the sources it applied, driving that count below
    the durable ``completed_sources`` and flipping a naive comparison to "not
    partial" exactly when the caveat is true.

    A marker with zero staged rows is still reported — it is still resumable (a
    no-op apply that clears the marker), which is how an analyst lifts the
    "resume before running" conflict. The dialog says something different for
    that case, since "0 events were enriched but never written" is not what
    happened.
    """
    if run is None:
        return None
    rows, staged_source_ids = staged.get(run.job_id, (0, set()))
    completed = set(run.completed_source_ids or [])
    started = run.created_at
    if started.tzinfo is None:  # SQLite round-trips naive datetimes
        started = started.replace(tzinfo=UTC)
    return {
        "job_id": run.job_id,
        "started_at": started.isoformat(),
        "age_seconds": max(0, int((datetime.now(UTC) - started).total_seconds())),
        "staged_rows": rows,
        "staged_sources": len(staged_source_ids),
        "completed_sources": len(completed),
        "partial_sources": len(staged_source_ids - completed),
    }


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
    from vestigo.enrichers.base import effective_enricher_state
    from vestigo.enrichers.jobs import get_active_enricher_run, oldest_unfinished_run
    from vestigo.enrichers.registry import all_enrichers, get_cached_availability

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

    # Unfinished runs: a marker whose (timeline, enricher) run slot is not held
    # by that same job id is provably dead — the slot is claimed before the
    # marker is written and released after it would have been deleted. A run
    # orphaned while the process stayed up (ClickHouse died mid-apply) is
    # invisible to startup reconciliation, so this is how an analyst finds it.
    markers = await store.list_enrichment_job_runs_for_timeline(case_id, timeline_id)
    stale = [m for m in markers if get_active_enricher_run(timeline_id, m.enricher_key) != m.job_id]
    staged_counts = await store.staged_summary_by_job([m.job_id for m in stale])
    # One marker per enricher, chosen by the shared oldest-first tie-break so the
    # banner names the same job as the run route's 409 and the auto-trigger's log.
    by_key: dict[str, list[EnrichmentJobRun]] = defaultdict(list)
    for marker in stale:
        by_key[marker.enricher_key].append(marker)
    stale_by_key = {
        key: run for key, markers_ in by_key.items() if (run := oldest_unfinished_run(markers_))
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

    # return_exceptions: one unreachable/failing check must not blank the whole
    # dialog. ClickHouse being down is exactly when an analyst needs to see the
    # unfinished-run banner, and eligibility is only advisory anyway.
    eligibilities = await asyncio.gather(
        *(run_in_threadpool(_check_one, enricher) for enricher in available),
        return_exceptions=True,
    )
    result = []
    for enricher, eligibility in zip(available, eligibilities, strict=True):
        config = configs.get(enricher.key)
        enabled, mode = effective_enricher_state(
            config.enabled if config else None,
            config.mode if config else None,
            global_defaults.get(enricher.key, False),
        )
        failed = isinstance(eligibility, BaseException)
        if failed:
            logger.warning(
                "Eligibility check failed for enricher %s on timeline %s: %s",
                enricher.key,
                timeline_id,
                eligibility,
            )
        result.append(
            {
                "key": enricher.key,
                "display_name": enricher.display_name,
                "description": enricher.description,
                "eligible": False if failed else eligibility.eligible,
                "sample_checked": 0 if failed else eligibility.sample_checked,
                "sample_matched": 0 if failed else eligibility.sample_matched,
                "eligibility_error": str(eligibility) if failed else None,
                "mode": mode,
                "enabled": enabled,
                # The job holding the run slot, or None. A run in flight is
                # exactly the case where `unfinished_run` is None *and* a run
                # would 409: its marker is filtered out of `stale` above because
                # the slot is held by it. Without this the dialog offers "Run
                # now" during a live run — including one startup reconciliation
                # is still applying — and the click fails with an "already
                # running" error the analyst had no way to anticipate.
                "running_job_id": get_active_enricher_run(timeline_id, enricher.key),
                # None unless this enricher has a dead run waiting to be resumed.
                # Markers for a currently-*unavailable* enricher are not rendered
                # (it is filtered out of `available` above) — that enricher cannot
                # be run either, so no new deadlock; startup reconciliation and a
                # later re-availability both still reach it.
                "unfinished_run": _unfinished_run_payload(
                    stale_by_key.get(enricher.key), staged_counts
                ),
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
    from vestigo.enrichers.registry import get_enricher

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
    from vestigo.enrichers.jobs import (
        get_active_enricher_run,
        oldest_unfinished_run,
        run_enrichment_job,
        try_claim_enricher_run,
    )
    from vestigo.enrichers.registry import get_cached_availability, get_enricher

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

    # Both conflict checks run *before* the provenance skip below: if a run is
    # in flight or unfinished, "everything is already enriched, nothing to do"
    # would be a misleading answer — the staged rows of an unfinished run are
    # precisely the work that has not been applied yet.
    #
    # This read is for the *early* 409 only: several awaits separate it from the
    # claim below, so it cannot be the authoritative check. The claim's own
    # return value is (see there).
    active_job_id = get_active_enricher_run(timeline_id, enricher_key)
    if active_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Enrichment already running (job {active_job_id})",
        )

    # An unfinished run's staged rows are completed enrichment output stamped
    # with a pinned enricher config/data version. A fresh run would strand them
    # permanently (_apply_staged_rows only ever lists its own job's staged
    # sources) and, if the enricher's data version has since changed, they are
    # not recomputable at all — discarding them would destroy derived evidence
    # that the pinned-hash design exists to keep reproducible. Make the analyst
    # resume first; resume is always available and always terminal, so this can
    # never wedge. Any marker reaching here is dead by the run-slot invariant:
    # a live run would have 409'd on the check above.
    unfinished = oldest_unfinished_run(
        await store.list_enrichment_job_runs_for_timeline(
            case_id, timeline_id, enricher_key=enricher_key
        )
    )
    if unfinished is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"An unfinished enrichment run (job {unfinished.job_id}) must be "
                "resumed before a new run can start"
            ),
        )

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
    #
    # The claim's return value is the authoritative conflict check, not the read
    # near the top of this handler: three awaits sit between them (the marker
    # query, the config_hash thread hop, the provenance query), so two "Run now"
    # clicks — or one racing ``_trigger_automatic_enrichments`` from a concurrent
    # ingest — can both pass that read. Losing the claim and spawning anyway
    # would put two applies on the same source *and* leave the loser's live
    # marker looking dead to the enrichers dialog (marker present, slot held by
    # the winner), which offers Resume on a running job — exactly the concurrent
    # partition rewrite ``enrichers/jobs.py`` exists to prevent.
    conflict = try_claim_enricher_run(timeline_id, enricher_key, job.id)
    if conflict is not None:
        job_store.update(job.id, status="failed", error="Enrichment already running")
        raise HTTPException(
            status_code=409,
            detail=f"Enrichment already running (job {conflict})",
        )
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


@router.post("/{case_id}/timelines/{timeline_id}/enrichers/{enricher_key}/resume")
async def resume_timeline_enricher(
    timeline_id: str,
    enricher_key: str,
    body: EnricherResumeRequest,
    background_tasks: BackgroundTasks,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Finish an enrichment run that died before its results were applied.

    Applies the run's already-staged rows and clears its marker — no re-scan,
    no recomputation. This is the recovery path when ClickHouse dies mid-apply
    and comes back under a live app, which startup reconciliation never sees.

    Auth matches the run route: resume performs the same partition rewrite, so
    it cannot require less, and it computes nothing new, so it must not require
    ``require_case_manage`` (reserved for changing enricher configuration).

    ``body.job_id`` is optimistic concurrency, not routing: the dialog may have
    been open for minutes, and echoing the id the analyst actually saw turns a
    stale view into a clean 404 instead of a silent rewrite of a partition
    nobody looked at.
    """
    from vestigo.enrichers.jobs import (
        get_active_enricher_run,
        run_resume_job,
        try_claim_enricher_run,
    )
    from vestigo.enrichers.registry import get_enricher

    if get_enricher(enricher_key) is None:
        raise HTTPException(status_code=404, detail="Unknown enricher")
    # Deliberately no availability check: resume applies already-computed rows
    # and never calls enrich_value, so a removed .mmdb must not strand valid
    # results. _apply_staged_rows already degrades gracefully for an enricher
    # it cannot resolve (it just skips stale-key stripping).

    store = get_store()
    case_id = case.id
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    run = await store.get_enrichment_job_run(body.job_id)
    if (
        run is None
        or run.case_id != case_id
        or run.timeline_id != timeline_id
        or run.enricher_key != enricher_key
    ):
        # One 404 for all four cases — a mismatched marker must not confirm
        # that some other case's job id exists.
        raise HTTPException(status_code=404, detail="No unfinished enrichment run with that id")

    summary = await store.staged_summary_by_job([run.job_id])
    staged_rows, staged_source_ids = summary.get(run.job_id, (0, set()))

    # From here to the claim there is no await: check-then-claim must happen in
    # one event-loop tick or two concurrent resumes both pass. Same
    # document-by-invariant discipline _ACTIVE_RUNS relies on throughout.
    active_job_id = get_active_enricher_run(timeline_id, enricher_key)
    if active_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Enrichment already running (job {active_job_id})",
        )

    job_store = get_job_store()
    # Before the claim, for the same reason as the run route: a constructor
    # raise after claiming would wedge this (timeline, enricher) at 409.
    ch_store = ClickHouseStore()
    job = job_store.create(
        kind="enrich",
        progress={"processed": 0, "total": 0},
        created_by=user.id,
        case_id=case_id,
    )
    # Claim under the *marker's* job id, not the poll job's. That keeps the
    # discovery invariant sound (marker present + slot held by it = live), so a
    # second analyst cannot fire a second resume over the same staged rows, and
    # it makes the run route's 409 name the run they already know about.
    # Checked rather than discarded for the same reason as the run route: the
    # read above is only authoritative while nothing awaits between it and here.
    conflict = try_claim_enricher_run(timeline_id, enricher_key, run.job_id)
    if conflict is not None:
        job_store.update(job.id, status="failed", error="Enrichment already running")
        raise HTTPException(
            status_code=409,
            detail=f"Enrichment already running (job {conflict})",
        )

    # Recorded before spawning so the named actor is on file even if the apply
    # then fails.
    await store.record_audit(
        action="enricher.resume_requested",
        actor=user,
        case_id=case_id,
        target_type="timeline",
        target_id=timeline_id,
        detail={
            "enricher_key": enricher_key,
            "job_id": run.job_id,
            "poll_job_id": job.id,
            "staged_rows": staged_rows,
            "staged_sources": len(staged_source_ids),
            "completed_sources": len(run.completed_source_ids or []),
            "partial_sources": len(staged_source_ids - set(run.completed_source_ids or [])),
        },
    )
    background_tasks.add_task(
        run_resume_job,
        poll_job_id=job.id,
        run=run,
        job_store=job_store,
        store=store,
        ch_store=ch_store,
        actor_user_id=user.id,
        actor_username=user.username,
    )
    return {
        # Two distinct ids: `job_id` polls the JobStore, `resumed_job_id` is the
        # enrichment run whose staged rows are being applied. They cannot be the
        # same value — the latter keys the staged rows and names the ClickHouse
        # scratch table, so it stays pinned to the original run.
        "job_id": job.id,
        "resumed_job_id": run.job_id,
        "status": job.status,
        "staged_rows": staged_rows,
        "staged_sources": len(staged_source_ids),
    }
