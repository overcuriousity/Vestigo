"""Background job that computes and persists a timeline's recommended columns.

Runs on the event loop as an ordinary async coroutine — every step it needs
(``ensure_source_field_stats``, the Postgres store, the advisor) is already
async, so this needs none of the threadpool/fresh-pool ceremony the
ClickHouse-scanning jobs do.

Triggered from everywhere a timeline's source set becomes knowable: a source
finishing ingestion, an explicitly created timeline, the analyst pressing
"Re-suggest columns", the CLI ingest, and the demo build. The API path goes
through :func:`start_column_recommendation`; the CLI and the demo await
:func:`run_column_recommendation_job` directly.

**Every one of those triggers is local-only.** ``use_llm`` defaults to False,
so a job scheduled by an ingest, a timeline creation, the CLI or the demo
scores the field-stats cache on this machine and sends nothing anywhere. The
advisor is reached from exactly one place: an analyst pressing "Suggest with
AI" for one timeline, after the disclosure dialog told them what that sends.
Egress is never a side effect of uploading a file.

Failure is always survivable — the explorer keeps its built-in defaults — so
every trigger site isolates this from the work that actually matters.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from vestigo.columns.recommend import finalize_columns, pick_columns, score_columns
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
#: not stack four concurrent jobs on the same timeline. What the collapsed
#: triggers would have seen is not lost — see :data:`_DIRTY`.
#:
#: In this process's memory, and only meaningful there — the same single-process
#: assumption ``JobStore`` already makes. A second worker sees an empty dict and
#: would both start a duplicate job and read this one as dead
#: (``api/routers/cases.py::_recommendation_is_dead``). See ``docs/ROADMAP.md``
#: §"Explicitly out of scope" → *Persistent job store*.
_ACTIVE: dict[str, str] = {}

#: Timelines a trigger arrived for while a recommendation was already running.
#:
#: The ``_ACTIVE`` guard exists so an ingest burst does not stack four jobs on
#: one timeline — but those jobs are *not* identical: the running one read its
#: source list before the later sources became ready, so dropping the later
#: triggers outright would leave the timeline recommended from a subset of what
#: it now holds, until the next ingest or a manual re-suggest. Marking it here
#: instead makes the running job re-run itself once, after it releases the
#: claim, however many triggers were collapsed into the mark.
#:
#: Same in-memory, single-process scope as ``_ACTIVE``.
_DIRTY: set[str] = set()


def get_active_recommendation(timeline_id: str) -> str | None:
    """Return the job id currently recommending for *timeline_id*, if any."""
    return _ACTIVE.get(timeline_id)


def spawn_tracked_column_task(coro: Any) -> asyncio.Task:
    """Schedule *coro* and hold a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    background_column_tasks.add(task)
    task.add_done_callback(background_column_tasks.discard)
    return task


def settle_running_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Relabel a ``running`` payload whose job is gone.

    ``JobStore`` is in-memory, so a job that dies — with the process, or as a
    cancelled task — leaves a Postgres payload claiming to be in flight, and
    the explorer polls on exactly that word. The job carries its previous
    answer forward into the placeholder (see :func:`_carry_forward`), so
    settling is only a relabel: ``ok`` when there are columns to show,
    ``insufficient`` otherwise. Nothing is recomputed.

    Shared by the boot-time sweep (``PostgresStore.clear_stale_running_recommendations``)
    and the read path (``GET /cases/{id}/timelines/{id}``) so the two can never
    disagree about what "settled" means.

    ``generated_at`` on a placeholder is the *recompute's* start — that is what
    the explorer measures its staleness floor against — but the columns being
    settled are the previous run's. :func:`_carry_forward` parks their real
    timestamp in ``columns_generated_at``, and settling puts it back, so a
    payload never claims its columns were derived later than they were.
    """
    settled = dict(payload)
    settled["status"] = "ok" if settled.get("columns") else "insufficient"
    settled["job_id"] = None
    carried = settled.pop("columns_generated_at", None)
    if carried:
        settled["generated_at"] = carried
    return settled


def _carry_forward(previous: dict[str, Any] | None) -> dict[str, Any]:
    """The parts of a previous payload a ``running`` placeholder keeps.

    A recompute must not blank a good suggestion while it works: the grid
    would re-lay out to the built-in defaults and back again, and — because
    ``JobStore`` is in-memory — a restart mid-job would leave the timeline
    holding a ``running`` payload with nothing in it. Carrying the previous
    answer forward means the worst a crash can do is mislabel a still-usable
    recommendation, which ``PostgresStore.clear_stale_running_recommendations``
    fixes on the next boot.

    ``columns_generated_at`` carries when those columns were actually derived.
    The placeholder's own ``generated_at`` has to be the recompute's start (the
    explorer reads it as the job's clock), so without this the settled payload
    would date a previous run's columns to a run that never finished. Read from
    the previous ``columns_generated_at`` first, so two crashes in a row do not
    walk the timestamp forward one recompute at a time.
    """
    if not isinstance(previous, dict):
        return {"columns": [], "reasons": {}, "method": "heuristic", "model": None}
    return {
        "columns": list(previous.get("columns") or []),
        "reasons": dict(previous.get("reasons") or {}),
        "method": previous.get("method") or "heuristic",
        "model": previous.get("model"),
        "columns_generated_at": previous.get("columns_generated_at")
        or previous.get("generated_at"),
    }


def _payload(
    *,
    status: str,
    columns: list[str],
    reasons: dict[str, str],
    method: str,
    model: str | None,
    source_ids: list[str],
    job_id: str | None,
    columns_generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the stored ``Timeline.recommended_columns`` payload.

    ``status`` is the whole contract with the frontend: ``running`` while a job
    is in flight, ``ok`` when there are columns to apply, and ``insufficient``
    when the corpus genuinely had nothing worth suggesting — recorded rather
    than left null so the explorer can stop waiting and the job does not get
    re-run in the hope of a different answer.

    ``generated_at`` is always *now*: for a finished payload that is when its
    columns were derived, and for a ``running`` placeholder it is the job's
    clock, which the explorer measures its staleness floor against.
    ``columns_generated_at`` is set on placeholders only — a carried-forward
    answer's real timestamp, which :func:`settle_running_payload` restores.
    """
    payload = {
        "status": status,
        "columns": columns,
        "reasons": reasons,
        "method": method,
        "model": model,
        "source_ids": source_ids,
        "generated_at": datetime.now(UTC).isoformat(),
        "job_id": job_id,
    }
    if columns_generated_at:
        payload["columns_generated_at"] = columns_generated_at
    return payload


async def _restore_previous(
    store: PostgresStore,
    case_id: str,
    timeline_id: str,
    job_id: str,
    previous: dict[str, Any] | None,
) -> None:
    """Undo this job's ``running`` placeholder, if it is still the stored one.

    Re-reads rather than trusting the in-memory view: a failure raised *after*
    the real payload was written (``record_audit`` is the realistic case) must
    leave that payload alone, and a newer job's placeholder is that job's to
    clean up.
    """
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        return
    stored = timeline.recommended_columns
    if not isinstance(stored, dict):
        return
    if stored.get("status") != "running" or stored.get("job_id") != job_id:
        return
    await store.update_timeline_recommended_columns(case_id, timeline_id, previous)


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
    use_llm: bool = False,
) -> None:
    """Compute, validate and persist one timeline's recommended columns.

    ``ch_store`` is constructed here rather than by the caller when omitted:
    the constructor opens a connection, and a ClickHouse outage should fail
    this job — which nothing depends on — rather than the HTTP request or the
    ingest that scheduled it.

    ``use_llm`` defaults to False and is the *only* thing that lets this job
    reach the advisor. It comes from one place — an analyst pressing "Suggest
    with AI" for this timeline, having read the disclosure — so an ingest, a
    timeline creation, the CLI and the demo build all score locally and send
    nothing anywhere. The advisor still applies its own
    ``agent_available()`` gate on top, so asking for it on an instance with no
    model configured simply keeps the heuristic answer.

    ``_ACTIVE`` is claimed *before* the ``try``, and the claim is the guard:
    a timeline already being recommended for belongs to that job, so this one
    returns without touching the payload (whose ``running`` placeholder is the
    other job's to roll back) — after marking the timeline in :data:`_DIRTY`,
    which is what makes the holder re-run rather than the collapsed trigger
    being lost. The API path claims through
    :func:`start_column_recommendation` before spawning the task — so no burst
    can slip a second job in between — and re-claiming with the same id here is
    a no-op. The CLI and the demo call this function directly and are covered by
    the same check.

    The claim is outside the ``try`` only so the ``finally`` cannot release a
    slot this job never took; it releases on every other exit path, including
    any guard clause added later.
    """
    job_store.update(job_id, status="running", progress={"phase": "fields"})
    previous: dict[str, Any] | None = None
    holder = _ACTIVE.setdefault(timeline_id, job_id)
    if holder != job_id:
        logger.info(
            "Column recommendation already running for timeline %s (job %s); skipping job %s",
            timeline_id,
            holder,
            job_id,
        )
        # Not a duplicate of the running job — this trigger knows about state
        # that job may have read too early. The holder re-runs for it.
        _DIRTY.add(timeline_id)
        job_store.update(job_id, status="completed", result={"skipped": True, "running": holder})
        return
    try:
        timeline = await store.get_timeline(case_id, timeline_id)
        if timeline is None:
            job_store.update(job_id, status="failed", error="Timeline not found")
            return

        # Everything from here on reads the current state, so whatever trigger
        # set the mark is covered by this run. A trigger arriving *after* this
        # line sets it again and earns the re-run in the `finally`.
        _DIRTY.discard(timeline_id)
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
            job_store.update(
                job_id, status="completed", result={"columns": [], "method": "heuristic"}
            )
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
                source_ids=source_ids,
                job_id=job_id,
                **_carry_forward(previous),
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

        if tokens and use_llm:
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
        # job that will never finish. Only *this* job's placeholder, though:
        # by the time `record_audit` runs the real payload is already
        # committed, and an audit failure must not replace a fresh answer with
        # the stale one it superseded.
        try:
            await _restore_previous(store, case_id, timeline_id, job_id, previous)
        except Exception:  # noqa: BLE001
            logger.exception("Could not clear the running placeholder for %s", timeline_id)
    finally:
        if _ACTIVE.get(timeline_id) == job_id:
            del _ACTIVE[timeline_id]
            _rerun_if_dirty(
                case_id=case_id,
                timeline_id=timeline_id,
                job_store=job_store,
                store=store,
                ch_store=ch_store,
            )


def _rerun_if_dirty(
    *,
    case_id: str,
    timeline_id: str,
    job_store: JobStore,
    store: PostgresStore,
    ch_store: ClickHouseStore | None,
) -> None:
    """Start one more run when a trigger was collapsed into :data:`_DIRTY`.

    Called from the job's ``finally``, *after* the claim is released, so the
    new job is not turned away as a duplicate of the one that is finishing.

    Bounded to one extra run per burst however many triggers were collapsed:
    the mark is cleared here and again by the new job before it reads state, so
    the only way to earn a third run is a trigger arriving after the second run
    already started — which is a trigger that genuinely knows something newer.

    **Always local.** The re-run answers an ingest, not an analyst, so it never
    passes ``use_llm`` — a burst must not turn one opted-in "Suggest with AI"
    into repeated egress. Never raises: a re-run that cannot be scheduled is a
    slightly stale suggestion, and the caller is a ``finally`` on a job that has
    already done its work.
    """
    if timeline_id not in _DIRTY:
        return
    _DIRTY.discard(timeline_id)
    try:
        start_column_recommendation(
            case_id=case_id,
            timeline_id=timeline_id,
            job_store=job_store,
            store=store,
            ch_store=ch_store,
        )
    except Exception:  # noqa: BLE001 — housekeeping: the stale answer stands
        logger.exception(
            "Could not re-run the column recommendation for timeline %s (case %s)",
            timeline_id,
            case_id,
        )


def start_column_recommendation(
    *,
    case_id: str,
    timeline_id: str,
    job_store: JobStore,
    store: PostgresStore,
    ch_store: ClickHouseStore | None = None,
    actor_id: str | None = None,
    actor_username: str | None = None,
    use_llm: bool = False,
) -> str | None:
    """Create and spawn a recommendation job for one timeline.

    Returns the job id, or ``None`` when a job for this timeline is already in
    flight. The active check happens before the job is created, so a skip
    leaves no orphan job in the tray — the same ordering
    ``_trigger_automatic_enrichments`` uses.

    Pass ``ch_store`` when the caller already holds one (the ingest hook
    does); otherwise the job opens its own, off the request path.

    ``use_llm`` is passed straight through and defaults to False; only the
    "Suggest with AI" endpoint sets it.
    """
    active = get_active_recommendation(timeline_id)
    if active is not None:
        logger.info(
            "Column recommendation already running for timeline %s (job %s); skipping",
            timeline_id,
            active,
        )
        # Marked rather than dropped: the running job may have read this
        # timeline's source list before whatever prompted this call happened,
        # so it re-runs once for it (see :data:`_DIRTY`).
        _DIRTY.add(timeline_id)
        return None
    job = job_store.create(
        kind=JOB_KIND,
        progress={"phase": "queued"},
        created_by=actor_id,
        case_id=case_id,
    )
    _ACTIVE[timeline_id] = job.id
    coro = run_column_recommendation_job(
        job_id=job.id,
        case_id=case_id,
        timeline_id=timeline_id,
        job_store=job_store,
        store=store,
        ch_store=ch_store,
        actor_id=actor_id,
        actor_username=actor_username,
        use_llm=use_llm,
    )
    try:
        spawn_tracked_column_task(coro)
    except BaseException:
        # The claim is released only by the job's own `finally`, which a job
        # that never started never reaches. A leaked claim is not a slow job:
        # `_recommendation_is_dead` reads an active claim as proof the job is
        # alive, so the timeline would report `running` for the rest of the
        # process's life *and* every later recommendation for it would be
        # skipped as a duplicate. Cheaper to hand the claim back here than to
        # make the liveness check second-guess itself.
        if _ACTIVE.get(timeline_id) == job.id:
            del _ACTIVE[timeline_id]
        # Suppressed because the failure may have come *after* `create_task`
        # took the coroutine over, in which case closing it is both wrong and
        # an error; a never-scheduled coroutine, meanwhile, warns at collection
        # if nothing closes it. The claim above is the part that matters.
        with suppress(RuntimeError):
            coro.close()
        job_store.update(job.id, status="failed", error="Could not schedule the recommendation")
        raise
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
    under them. Local scoring only — an ingest never reaches the advisor.

    A parallel ingest into one timeline lands here several times over: the
    later calls are turned away by the ``_ACTIVE`` claim and mark the timeline
    in :data:`_DIRTY` instead, so the running job re-runs once and the
    recommendation ends up derived from every source, not just the ones that
    were ready when the first job read the list.
    """
    timelines = await store.list_timelines_for_source(case_id, source_id)
    for timeline in timelines:
        start_column_recommendation(
            case_id=case_id,
            timeline_id=timeline.id,
            job_store=job_store,
            store=store,
            ch_store=ch_store,
        )
