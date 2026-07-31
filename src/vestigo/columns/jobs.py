"""Background job that computes and persists a timeline's recommended columns.

Runs on the event loop as an ordinary async coroutine — every step it needs
(``ensure_source_field_stats``, the Postgres store, the advisor) is already
async, so this needs none of the threadpool/fresh-pool ceremony the
ClickHouse-scanning jobs do.

Triggered from three places, all of which mean the same thing ("this
timeline's source set just became knowable"): a source finishing ingestion,
an explicitly created timeline, and the analyst pressing "Re-suggest columns".
Failure is always survivable — the explorer keeps its built-in defaults — so
every trigger site isolates this from the work that actually matters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from vestigo.columns.recommend import finalize_columns, pick_columns, score_columns
from vestigo.core.config import get_settings
from vestigo.core.jobs import JobStore
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.field_stats import ensure_source_field_stats
from vestigo.db.postgres import PostgresStore

logger = logging.getLogger(__name__)

#: Job kind, as surfaced by ``GET /api/cases/{id}/jobs`` and the job tray.
JOB_KIND = "column_recommend"

#: asyncio keeps only a weak reference to a scheduled task; without this the
#: recommendation could be garbage-collected mid-flight. Same reasoning as
#: ``enrichers/jobs.background_enrichment_tasks``.
background_column_tasks: set[asyncio.Task] = set()

#: Timelines with a recommendation currently running, so an ingest burst does
#: not stack four identical jobs on the same timeline.
_ACTIVE: dict[str, str] = {}


def get_active_recommendation(timeline_id: str) -> str | None:
    """Return the job id currently recommending for *timeline_id*, if any."""
    return _ACTIVE.get(timeline_id)


def spawn_tracked_column_task(coro: Any) -> asyncio.Task:
    """Schedule *coro* and hold a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    background_column_tasks.add(task)
    task.add_done_callback(background_column_tasks.discard)
    return task


def recommendation_enabled() -> bool:
    """Whether column recommendation runs at all on this instance."""
    return get_settings().column_recommend_mode != "off"


def _llm_allowed() -> bool:
    """Whether the advisor may be consulted (``auto`` mode only)."""
    return get_settings().column_recommend_mode == "auto"


def _payload(
    *,
    status: str,
    columns: list[str],
    reasons: dict[str, str],
    method: str,
    model: str | None,
    source_ids: list[str],
    job_id: str | None,
) -> dict[str, Any]:
    """Build the stored ``Timeline.recommended_columns`` payload.

    ``status`` is the whole contract with the frontend: ``running`` while a job
    is in flight, ``ok`` when there are columns to apply, and ``insufficient``
    when the corpus genuinely had nothing worth suggesting — recorded rather
    than left null so the explorer can stop waiting and the job does not get
    re-run in the hope of a different answer.
    """
    return {
        "status": status,
        "columns": columns,
        "reasons": reasons,
        "method": method,
        "model": model,
        "source_ids": source_ids,
        "generated_at": datetime.now(UTC).isoformat(),
        "job_id": job_id,
    }


async def run_column_recommendation_job(
    *,
    job_id: str,
    case_id: str,
    timeline_id: str,
    job_store: JobStore,
    store: PostgresStore,
    ch_store: ClickHouseStore | None = None,
    actor_id: str | None = None,
    actor_username: str | None = None,
) -> None:
    """Compute, validate and persist one timeline's recommended columns.

    ``ch_store`` is constructed here rather than by the caller when omitted:
    the constructor opens a connection, and a ClickHouse outage should fail
    this job — which nothing depends on — rather than the HTTP request or the
    ingest that scheduled it.
    """
    _ACTIVE[timeline_id] = job_id
    job_store.update(job_id, status="running", progress={"phase": "fields"})
    previous: dict[str, Any] | None = None
    try:
        timeline = await store.get_timeline(case_id, timeline_id)
        if timeline is None:
            job_store.update(job_id, status="failed", error="Timeline not found")
            return

        sources = await store.list_timeline_sources(case_id, timeline_id)
        source_ids = sorted(s.id for s in sources if s.is_ready)
        if not source_ids:
            # Nothing ingested yet. Not a failure — the next source to finish
            # ingesting schedules this again.
            await store.update_timeline_recommended_columns(
                case_id,
                timeline_id,
                _payload(
                    status="insufficient",
                    columns=[],
                    reasons={},
                    method="heuristic",
                    model=None,
                    source_ids=[],
                    job_id=job_id,
                ),
            )
            job_store.update(job_id, status="completed", result={"columns": [], "method": "none"})
            return

        # Kept so a failed *re*-run restores what the timeline already had —
        # losing a good suggestion because a later recompute hit a ClickHouse
        # blip would be a worse outcome than not recomputing at all.
        previous = timeline.recommended_columns
        await store.update_timeline_recommended_columns(
            case_id,
            timeline_id,
            _payload(
                status="running",
                columns=[],
                reasons={},
                method="heuristic",
                model=None,
                source_ids=source_ids,
                job_id=job_id,
            ),
        )

        stats = await ensure_source_field_stats(
            store, ch_store or ClickHouseStore(), case_id, source_ids
        )
        candidates = score_columns(stats, timeline.field_mappings or None)
        tokens = pick_columns(candidates)
        method = "heuristic"
        model: str | None = None
        llm_reasons: dict[str, str] = {}

        if tokens and _llm_allowed():
            job_store.update(job_id, progress={"phase": "model"})
            from vestigo.columns.advisor import rank_columns_with_llm

            advised = await rank_columns_with_llm(candidates)
            if advised is not None:
                tokens = advised.columns
                llm_reasons = advised.reasons
                method = "llm"
                model = advised.model

        columns, reasons = finalize_columns(tokens, candidates)
        # The model's phrasing is better copy than the statistics string, but
        # the statistics are the part that is verifiable — keep both, model
        # first, so a tooltip reads as a reason and still carries the evidence.
        for token, clause in llm_reasons.items():
            if token in reasons:
                reasons[token] = f"{clause} — {reasons[token]}"

        payload = _payload(
            status="ok" if columns else "insufficient",
            columns=columns,
            reasons=reasons,
            method=method,
            model=model,
            source_ids=source_ids,
            job_id=job_id,
        )
        await store.update_timeline_recommended_columns(case_id, timeline_id, payload)
        await store.record_audit(
            action="timeline.recommend_columns",
            # No `actor=` User object here: the common trigger is post-ingest,
            # where nobody asked for this. A manual re-run passes both fields.
            user_id=actor_id,
            username_snapshot=actor_username,
            case_id=case_id,
            target_type="timeline",
            target_id=timeline_id,
            detail={
                "columns": columns,
                "method": method,
                "model": model,
                "candidates": [c.token for c in candidates],
                "source_ids": source_ids,
            },
        )
        job_store.update(
            job_id,
            status="completed",
            result={"columns": columns, "method": method, "status": payload["status"]},
        )
    except Exception as exc:  # noqa: BLE001 — job boundary: report, don't crash the loop
        logger.exception(
            "Column recommendation failed for timeline %s (case %s)", timeline_id, case_id
        )
        job_store.update(job_id, status="failed", error=str(exc))
        # Roll the "running" placeholder back to whatever was there before, or
        # to null on a first run — either way the explorer stops waiting on a
        # job that will never finish.
        try:
            await store.update_timeline_recommended_columns(case_id, timeline_id, previous)
        except Exception:  # noqa: BLE001
            logger.exception("Could not clear the running placeholder for %s", timeline_id)
    finally:
        if _ACTIVE.get(timeline_id) == job_id:
            del _ACTIVE[timeline_id]


def start_column_recommendation(
    *,
    case_id: str,
    timeline_id: str,
    job_store: JobStore,
    store: PostgresStore,
    ch_store: ClickHouseStore | None = None,
    actor_id: str | None = None,
    actor_username: str | None = None,
) -> str | None:
    """Create and spawn a recommendation job for one timeline.

    Returns the job id, or ``None`` when recommendation is switched off or a
    job for this timeline is already in flight. The active check happens
    before the job is created, so a skip leaves no orphan job in the tray —
    the same ordering ``_trigger_automatic_enrichments`` uses.

    Pass ``ch_store`` when the caller already holds one (the ingest hook
    does); otherwise the job opens its own, off the request path.
    """
    if not recommendation_enabled():
        return None
    active = get_active_recommendation(timeline_id)
    if active is not None:
        logger.info(
            "Column recommendation already running for timeline %s (job %s); skipping",
            timeline_id,
            active,
        )
        return None
    job = job_store.create(
        kind=JOB_KIND,
        progress={"phase": "queued"},
        created_by=actor_id,
        case_id=case_id,
    )
    _ACTIVE[timeline_id] = job.id
    spawn_tracked_column_task(
        run_column_recommendation_job(
            job_id=job.id,
            case_id=case_id,
            timeline_id=timeline_id,
            job_store=job_store,
            store=store,
            ch_store=ch_store,
            actor_id=actor_id,
            actor_username=actor_username,
        )
    )
    return job.id


async def schedule_for_source(
    store: PostgresStore,
    ch_store: ClickHouseStore,
    job_store: JobStore,
    case_id: str,
    source_id: str,
) -> None:
    """Re-recommend every timeline that contains a newly-ready source.

    Called from the same post-ingest hook as ``_revalidate_stale_field_mappings``
    — the one place that knows a source just became queryable. A per-user
    column choice lives in the browser and always outranks the stored
    recommendation, so replacing it here never moves anyone's columns out from
    under them.
    """
    if not recommendation_enabled():
        return
    timelines = await store.list_timelines_for_source(case_id, source_id)
    for timeline in timelines:
        start_column_recommendation(
            case_id=case_id,
            timeline_id=timeline.id,
            job_store=job_store,
            store=store,
            ch_store=ch_store,
        )
