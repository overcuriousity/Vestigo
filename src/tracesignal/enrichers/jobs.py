"""Background enrichment job: read events, run an enricher, stage + apply results.

Processing is crash-safe at the result level (not the cursor level): results
are staged in Postgres as each batch completes (transactional, so they
survive a process crash even though the in-memory JobStore does not), then
applied to ClickHouse **once, at job end**, by merging them into
``events.attributes`` via an atomic per-source partition rewrite
(``ClickHouseStore.stage_enrichment_rows`` / ``finalize_enrichment_apply``). There is no periodic flush — a
partition rewrite is too expensive to repeat mid-run, and staging volume is
modest: one row per (job, event) carrying a ``fields`` JSON map, so 1M
enriched events stage as 1M rows regardless of how many attributes or
output fields matched (the former row-per-(event, attr, output_field)
grain was ~3-6x larger).

If the process dies mid-run, the durable ``EnrichmentJobRun`` marker lets
startup reconciliation apply whatever was staged and schedule a fresh re-run
over the timeline (see ``reconcile_orphaned_enrichment_jobs``) — there is no
resume-from-cursor support by design, since that would require tracking
per-source read offsets durably. Re-applying and re-running are both safe
because ``mapUpdate`` overwrites the same derived keys with recomputed
values (idempotent), not because of any read-time dedup.

Provenance: which enricher config/data version produced a source's derived
fields is recorded per source in Postgres (``SourceEnrichment``) at apply
time, replacing the per-row hash column of the former side-table design.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from tracesignal.core.config import get_settings
from tracesignal.core.jobs import JobStore
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_stats import refresh_source_field_stats
from tracesignal.db.postgres import EnrichmentJobRun, PostgresStore
from tracesignal.enrichers.base import FIELD_KEY_SEPARATOR, Enricher, derived_field_key
from tracesignal.enrichers.registry import get_cached_availability, get_enricher

logger = logging.getLogger(__name__)

# One enrichment run per (timeline_id, enricher_key) at a time. Claim/release
# only happen on the event-loop thread with no await between check and set,
# so a plain dict is race-free without a lock — document-by-invariant, the
# same reasoning core/jobs.py::JobStore relies on. Purely in-memory: a crash
# self-heals on restart (startup reconciliation re-schedules what was lost).
_ACTIVE_RUNS: dict[tuple[str, str], str] = {}

# Strong references to fire-and-forget enrichment tasks so asyncio doesn't
# garbage-collect them mid-run (asyncio only holds a weak reference once
# scheduled). Shared by the auto-trigger (api/routers/cases.py) and startup
# re-run scheduling below. create_task + this set (rather than FastAPI
# BackgroundTasks) is deliberate: both callers run in job/startup context
# with no live request, where BackgroundTasks is structurally unavailable.
background_enrichment_tasks: set[asyncio.Task] = set()

# Serializes partition rewrites per (case_id, source_id). Mandatory, not an
# optimization: two enrichers applying to the same source concurrently would
# each build their copy from the pre-apply partition and the second REPLACE
# would silently discard the first's keys. (The _ACTIVE_RUNS guard is per
# (timeline, enricher) and does not cover this.) Locks are created lazily on
# the event-loop thread; entries are never removed — bounded by the number of
# sources touched since startup.
_APPLY_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _apply_lock(case_id: str, source_id: str) -> asyncio.Lock:
    return _APPLY_LOCKS.setdefault((case_id, source_id), asyncio.Lock())


def spawn_tracked_enrichment_task(coro: Any) -> asyncio.Task:
    """Schedule ``coro`` and hold a strong reference until it finishes.

    Centralizes the fire-and-forget bookkeeping shared by the auto-trigger
    (``api/routers/cases.py``) and startup re-run scheduling: asyncio only
    keeps a weak reference to a scheduled task, so without ``background_
    enrichment_tasks`` the run could be garbage-collected mid-flight.
    """
    task = asyncio.create_task(coro)
    background_enrichment_tasks.add(task)
    task.add_done_callback(background_enrichment_tasks.discard)
    return task


def get_active_enricher_run(timeline_id: str, enricher_key: str) -> str | None:
    """Return the job_id currently holding the run slot, or None if free."""
    return _ACTIVE_RUNS.get((timeline_id, enricher_key))


def try_claim_enricher_run(timeline_id: str, enricher_key: str, job_id: str) -> str | None:
    """Claim the run slot for a job; returns the conflicting job_id if taken, else None."""
    existing = _ACTIVE_RUNS.get((timeline_id, enricher_key))
    if existing is not None:
        return existing
    _ACTIVE_RUNS[(timeline_id, enricher_key)] = job_id
    return None


def _release_enricher_run(timeline_id: str, enricher_key: str, job_id: str) -> None:
    """Release the run slot, but only if this job still owns it."""
    if _ACTIVE_RUNS.get((timeline_id, enricher_key)) == job_id:
        del _ACTIVE_RUNS[(timeline_id, enricher_key)]


def _process_batch(
    enricher: Enricher,
    batch: list[dict[str, Any]],
    case_id: str,
    source_id: str,
    timeline_id: str,
    job_id: str,
    enricher_key: str,
    enricher_config_hash: str,
    value_cache: dict[str, dict[str, str] | None],
) -> list[dict[str, Any]]:
    """Regex-match attributes in a batch of events and run the enricher on matches.

    Synchronous — run inside ``asyncio.to_thread`` by the caller, since it
    calls the (blocking) enricher and iterates in-memory event dicts. Any
    enricher failure propagates (with event context attached) and fails the
    whole job: partially-silently-enriched results are worse than a failed
    job in a forensic tool.

    ``value_cache`` memoizes the (deterministic) enricher result per distinct
    raw value across the whole job. The same IP typically recurs across
    millions of events, so without dedup a GeoIP run does one mmdb lookup per
    event; with it, one per *distinct* value — the histogram-style "list each
    value once, apply in bulk" the user expects. Both resolved dicts and
    ineligible/miss ``None`` are cached (misses are as repetitive as hits).
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for event in batch:
        event_id = str(event["event_id"])
        attributes = event.get("attributes") or {}
        # One staging row per event: every derived key for this event lands
        # in a single field_key -> value map (M16 staging format).
        fields: dict[str, str] = {}
        for attr_key, raw_value in attributes.items():
            # Skip keys an enricher already derived (they carry the
            # "<attr>:<field>" separator): re-running must not enrich prior
            # output, which would grow second-generation keys unboundedly for
            # any enricher whose output can match its own eligibility regex.
            if FIELD_KEY_SEPARATOR in attr_key:
                continue
            if not raw_value:
                continue
            if raw_value in value_cache:
                enriched = value_cache[raw_value]
            elif not enricher.is_field_eligible(raw_value):
                value_cache[raw_value] = None
                continue
            else:
                try:
                    enriched = enricher.enrich_value(raw_value)
                except Exception as exc:
                    # No raw value in the note — attribute values may be
                    # sensitive; event_id + attr_key is enough to reproduce.
                    exc.add_note(f"enricher={enricher_key} event_id={event_id} attr_key={attr_key}")
                    raise
                value_cache[raw_value] = enriched
            if not enriched:
                continue
            for output_field, value in enriched.items():
                if not value:
                    continue
                # Naming contract lives in base.derived_field_key
                # (mirrored in frontend/src/lib/enrichment.ts).
                fields[derived_field_key(attr_key, output_field)] = value
        if fields:
            rows.append(
                {
                    "job_id": job_id,
                    "case_id": case_id,
                    "source_id": source_id,
                    "timeline_id": timeline_id,
                    "event_id": event_id,
                    "enricher_key": enricher_key,
                    "fields": fields,
                    "computed_at": now,
                    "enricher_config_hash": enricher_config_hash,
                }
            )
    return rows


async def _apply_staged_rows(store: PostgresStore, ch_store: ClickHouseStore, job_id: str) -> int:
    """Apply a job's staged rows to ``events.attributes``, one source at a time.

    Per staged source: serialize on the per-(case, source) apply lock, verify
    the source still exists (a source deleted mid-job must not be resurrected
    by our partition REPLACE; a millisecond residual window between check and
    swap remains — acceptable pre-release), stream the staged triples page by
    page into the ClickHouse scratch table via ``stage_enrichment_rows`` (each
    page discarded from Python memory as soon as it's staged — a large source
    never holds more than one page's worth of triples at once), then finalize
    with the atomic partition rewrite (``finalize_enrichment_apply``), and
    only then delete the staged rows and upsert the ``SourceEnrichment``
    provenance row. A failure leaves that source's staged rows intact for the
    next attempt; the rewrite is idempotent, so a crash between REPLACE and
    the staged-row delete just re-applies identical values.

    No concurrent ingest can append to the partition mid-apply: enrichers
    only run on ``is_ready`` sources, and sources are ingest-once.

    Timeline/enricher/config-hash metadata is read off the staged rows
    themselves (uniform per job), so this works identically for a live job
    and for startup reconciliation of an orphaned one.

    Returns the number of enrichment pairs applied across all sources.
    """
    applied_total = 0
    for case_id, source_id in await store.list_staged_sources(job_id):
        async with _apply_lock(case_id, source_id):
            if await store.get_source(case_id, source_id) is None:
                logger.warning(
                    "Skipping enrichment apply for source %s (job %s): source was deleted",
                    source_id,
                    job_id,
                )
                await store.delete_staged_rows_for_source(job_id, source_id)
                continue

            # The ClickHouse client is sync (blocking); each page is staged in
            # its own worker-thread call and dropped from Python memory
            # immediately after, so total memory stays bounded by one page
            # (not the whole source) regardless of source size. The scratch
            # table (once created) is always cleaned up in the finally below,
            # matching the crash-mid-apply cleanup already handled by
            # ``drop_stale_enrichment_scratch_tables`` at startup.
            had_page = False
            applied = 0
            timeline_id = enricher_key = config_hash = ""
            after_id = 0
            try:
                while True:
                    # 4000 rows/page (was 10000 per-field rows): each row now
                    # expands into several (event_id, field_key, value) triples,
                    # so this keeps per-page memory in the same ballpark.
                    staged = await store.list_staged_rows_for_source(
                        job_id, source_id, limit=4000, after_id=after_id
                    )
                    if not staged:
                        break
                    if not had_page:
                        timeline_id = staged[0].timeline_id
                        enricher_key = staged[0].enricher_key
                        config_hash = staged[0].enricher_config_hash
                        await asyncio.to_thread(ch_store.create_enrichment_scratch, job_id)
                        had_page = True
                    after_id = staged[-1].id
                    chunk = [
                        (row.event_id, field_key, value)
                        for row in staged
                        for field_key, value in row.fields.items()
                    ]
                    applied += await asyncio.to_thread(
                        ch_store.stage_enrichment_rows, job_id, chunk
                    )
                if had_page and applied:
                    # Pass the enricher's output-field names so the finalize
                    # step can strip stale derived keys (values that no
                    # longer resolve) instead of leaving them behind. Unknown
                    # enricher -> no stripping.
                    enricher = get_enricher(enricher_key)
                    owned_suffixes = list(enricher.output_fields) if enricher is not None else []
                    await asyncio.to_thread(
                        ch_store.finalize_enrichment_apply,
                        case_id,
                        source_id,
                        job_id,
                        owned_suffixes,
                    )
            finally:
                if had_page:
                    await asyncio.to_thread(ch_store.drop_enrichment_scratch, job_id)
            if not had_page or not applied:
                continue
            # Close the check-then-swap window: finalize_enrichment_apply rebuilds the
            # partition from a pre-apply snapshot, so a source deleted *during*
            # the rewrite (its DROP PARTITION already applied) would be
            # resurrected by our REPLACE. Re-verify after the swap and, if the
            # source is now gone, drop the partition we just re-materialized so
            # no orphaned evidence survives. (A delete landing after this point
            # runs its own DROP PARTITION and is unaffected.)
            if await store.get_source(case_id, source_id) is None:
                logger.warning(
                    "Source %s deleted during enrichment apply (job %s); "
                    "dropping resurrected partition",
                    source_id,
                    job_id,
                )
                await asyncio.to_thread(ch_store.delete_source_events, case_id, source_id)
                await store.delete_staged_rows_for_source(job_id, source_id)
                continue
            await store.record_source_enrichment(
                case_id=case_id,
                source_id=source_id,
                timeline_id=timeline_id,
                enricher_key=enricher_key,
                enricher_config_hash=config_hash,
                job_id=job_id,
                rows_applied=applied,
            )
            await store.delete_staged_rows_for_source(job_id, source_id)
            # Enrichment just added/stripped derived keys in events.attributes
            # — the only mutation path for an ingested source — so refresh the
            # per-source field-stats cache (M15). On failure the now-stale row
            # is dropped instead: a missing row is a cache miss the read path
            # heals, whereas a stale current-version row would be trusted.
            try:
                await refresh_source_field_stats(store, ch_store, case_id, source_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Field-stats refresh failed after enrichment apply for source %s; "
                    "dropping the stale cache row",
                    source_id,
                )
                # Retry the compensating delete a few times — if the refresh's
                # failure was a transient Postgres hiccup, a single failed
                # delete would otherwise leave a stale current-version row
                # trusted by every future read, indefinitely and silently.
                deleted = False
                for attempt in range(3):
                    try:
                        await store.delete_source_field_stats(source_id)
                        deleted = True
                        break
                    except Exception:  # noqa: BLE001
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                if not deleted:
                    logger.error("Could not drop stale field-stats row for %s", source_id)
                    await store.record_audit(
                        action="source.field_stats_stale",
                        case_id=case_id,
                        target_type="source",
                        target_id=source_id,
                        detail={"job_id": job_id},
                    )
            await store.record_audit(
                action="enricher.applied",
                case_id=case_id,
                target_type="source",
                target_id=source_id,
                detail={
                    "job_id": job_id,
                    "enricher_key": enricher_key,
                    "enricher_config_hash": config_hash,
                    "rows_applied": applied,
                },
            )
            applied_total += applied
    return applied_total


async def run_enrichment_job(
    job_id: str,
    case_id: str,
    timeline_id: str,
    enricher_key: str,
    source_ids: list[str],
    job_store: JobStore,
    store: PostgresStore,
    ch_store: ClickHouseStore,
) -> None:
    """Run one enricher over a set of sources, staging results and applying once at the end.

    Works on a fresh per-run enricher instance (``Enricher.spawn()``) so
    concurrent runs never share mutable state such as an open database
    reader. Batches are paginated via the same ``list_events`` primitive the
    embedding pipeline uses. All
    results are staged in Postgres and merged into ``events.attributes`` in
    one atomic per-source partition rewrite at job end
    (``_apply_staged_rows``).
    """
    prototype = get_enricher(enricher_key)
    if prototype is None:
        job_store.update(job_id, status="failed", error=f"Unknown enricher: {enricher_key}")
        _release_enricher_run(timeline_id, enricher_key, job_id)
        return
    # spawn() can do blocking I/O (GeoIP reads the whole database into memory to
    # pin its identity for the run) — keep it off the event loop. spawn is
    # best-effort and does not raise, so the finally-block cleanup contract of
    # the try below is unaffected by running it here.
    enricher = await asyncio.to_thread(prototype.spawn)

    settings = get_settings()
    # Read paging over ClickHouse — enrichment is regex + lookup work per
    # value, not model-bound like embedding, so page in large chunks
    # (TS_ENRICHMENT_BATCH_SIZE, default 20k) to keep HTTP round-trip overhead
    # low on large sources. On a 180M-event timeline this is ~9k round-trips
    # instead of ~180k at the old 1000-row floor.
    batch_size = settings.enrichment_batch_size

    await store.start_enrichment_job_run(job_id, timeline_id, case_id, enricher_key)
    job_store.update(job_id, status="running", progress={"processed": 0, "total": 0})

    # Defined before the try so the failure path can always report coverage,
    # even if the very first step (config_hash / count) is what raised.
    processed = 0
    completed_sources = 0
    try:
        # Worker thread: config_hash may do disk I/O on enrichers without a
        # pinned identity. For GeoIP the identity is already pinned in spawn(),
        # so this is cheap, but stay off the loop for the general case.
        config_hash = await asyncio.to_thread(enricher.config_hash)

        total = await asyncio.to_thread(
            ch_store.count_events, case_id=case_id, source_ids=source_ids
        )
        job_store.update(job_id, progress={"processed": 0, "total": total})

        # Dedup lookups across the whole job: enricher output is deterministic
        # per raw value (config pinned at spawn), so a value seen once need
        # never be looked up again — collapses per-event lookups to per-
        # distinct-value. Shared across sources; bounded by the number of
        # distinct attribute values in the timeline.
        value_cache: dict[str, dict[str, str] | None] = {}
        for source_id in source_ids:
            batches = ch_store.iter_source_events(case_id, source_id, batch_size)
            # Each next() runs one blocking ClickHouse query — keep it off the
            # event loop.
            while (batch := await asyncio.to_thread(next, batches, None)) is not None:
                rows = await asyncio.to_thread(
                    _process_batch,
                    enricher,
                    batch,
                    case_id,
                    source_id,
                    timeline_id,
                    job_id,
                    enricher_key,
                    config_hash,
                    value_cache,
                )
                if rows:
                    await store.stage_enrichment_results(rows)

                processed += len(batch)
                job_store.update(job_id, progress={"processed": processed, "total": total})
            completed_sources += 1

        applied = await _apply_staged_rows(store, ch_store, job_id)
        await store.finish_enrichment_job_run(job_id)
        job_store.update(
            job_id,
            status="completed",
            progress={"processed": processed, "total": total},
            result={
                "enricher_key": enricher_key,
                "events_processed": processed,
                "fields_applied": applied,
                "enricher_config_hash": config_hash,
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Coverage is partial: sources after completed_sources were never
        # processed. Surface that explicitly (job error, result, audit) rather
        # than silently marking "failed" — the marker is cleared below so this
        # won't auto re-run, so an operator must re-trigger to finish coverage.
        covered = completed_sources
        remaining = max(len(source_ids) - covered, 0)
        logger.exception(
            "Enrichment job %s failed after covering %d/%d sources; %d source(s) left unenriched",
            job_id,
            covered,
            len(source_ids),
            remaining,
        )
        job_store.update(
            job_id,
            status="failed",
            error=str(exc),
            result={
                "enricher_key": enricher_key,
                "sources_covered": covered,
                "sources_total": len(source_ids),
                "sources_remaining": remaining,
                "partial_coverage": remaining > 0,
            },
        )
        # The process is still alive, so clean up now instead of leaving the
        # marker for startup reconciliation: apply what was staged (those
        # results are valid — partial coverage, idempotent rewrite) and clear
        # the marker so a deterministic failure isn't auto re-run on every
        # restart. If the apply itself fails (e.g. ClickHouse down), the
        # marker stays and reconciliation gets it.
        try:
            await _apply_staged_rows(store, ch_store, job_id)
            await store.finish_enrichment_job_run(job_id)
            if remaining > 0:
                await store.record_audit(
                    action="enricher.partial",
                    case_id=case_id,
                    target_type="timeline",
                    target_id=timeline_id,
                    detail={
                        "job_id": job_id,
                        "enricher_key": enricher_key,
                        "sources_covered": covered,
                        "sources_total": len(source_ids),
                        "sources_remaining": remaining,
                        "error": str(exc),
                    },
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Post-failure cleanup for enrichment job %s failed; leaving marker for "
                "startup reconciliation",
                job_id,
            )
    finally:
        enricher.close()
        _release_enricher_run(timeline_id, enricher_key, job_id)


async def reconcile_orphaned_enrichment_jobs(
    store: PostgresStore, ch_store: ClickHouseStore
) -> list[EnrichmentJobRun]:
    """Recover enrichment jobs left running by a mid-run process restart.

    Mirrors the *shape* of the orphaned-ingest cleanup in ``api/main.py`` but
    deliberately not its code: ingest recovery rolls partial data back
    (delete), this recovers forward (apply + reschedule) — a shared helper
    would need mode flags that obscure both. The in-memory JobStore is empty
    on a fresh boot, so any ``EnrichmentJobRun`` marker still present means
    the process died mid-run. Staged rows are valid,
    complete results — they are applied to ``events.attributes`` here rather
    than discarded, then the marker is cleared. Returns the recovered runs so
    the caller can schedule fresh re-runs (``schedule_enrichment_reruns``) to
    cover whatever the crashed run never processed; the run/re-run overlap is
    safe because ``mapUpdate`` overwrites the same derived keys with
    recomputed values.

    If applying fails (e.g. ClickHouse unreachable), the marker and staged
    rows are left intact for the next restart.
    """
    orphaned = await store.list_orphaned_enrichment_job_runs()
    recovered: list[EnrichmentJobRun] = []
    for run in orphaned:
        try:
            applied = await _apply_staged_rows(store, ch_store, run.job_id)
            await store.finish_enrichment_job_run(run.job_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not recover orphaned enrichment job %s (enricher=%s, timeline=%s); "
                "leaving marker and staged rows for next restart",
                run.job_id,
                run.enricher_key,
                run.timeline_id,
            )
            continue
        await store.record_audit(
            action="enricher.job_recovered",
            case_id=run.case_id,
            target_type="timeline",
            target_id=run.timeline_id,
            detail={
                "job_id": run.job_id,
                "enricher_key": run.enricher_key,
                "fields_applied": applied,
            },
        )
        logger.warning(
            "Recovered orphaned enrichment job %s (enricher=%s, timeline=%s): "
            "applied %d staged enrichment fields, scheduling re-run",
            run.job_id,
            run.enricher_key,
            run.timeline_id,
            applied,
        )
        recovered.append(run)
    return recovered


async def schedule_enrichment_reruns(
    runs: list[EnrichmentJobRun],
    job_store: JobStore,
    store: PostgresStore,
) -> None:
    """Schedule fresh enrichment runs for jobs recovered at startup.

    Re-resolves each run's scope to the timeline's *current* ready sources —
    the crashed run's exact source scope isn't persisted, and the full
    timeline is a coverage-complete superset (safe: ``mapUpdate`` overwrites
    the same derived keys idempotently). Skips runs whose enricher is
    unavailable or whose timeline no longer exists; already-applied fields
    remain valid either way.
    """
    for run in runs:
        availability = get_cached_availability(run.enricher_key)
        if availability is None or not availability.available:
            logger.info(
                "Skipping enrichment re-run for timeline %s: enricher %s unavailable",
                run.timeline_id,
                run.enricher_key,
            )
            continue
        sources = await store.list_timeline_sources(run.case_id, run.timeline_id)
        source_ids = [s.id for s in sources if s.is_ready]
        if not source_ids:
            logger.info(
                "Skipping enrichment re-run for timeline %s: no ready sources",
                run.timeline_id,
            )
            continue
        # Construct the ClickHouse client before claiming the run slot: a
        # constructor failure (ClickHouse unreachable) after a claim would
        # never be released and would wedge this (timeline, enricher) at 409.
        try:
            ch_store = ClickHouseStore()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not construct ClickHouse client for enrichment re-run of timeline %s; "
                "leaving it for the next restart",
                run.timeline_id,
            )
            continue
        job = job_store.create(
            kind="enrich", progress={"processed": 0, "total": 0}, case_id=run.case_id
        )
        if try_claim_enricher_run(run.timeline_id, run.enricher_key, job.id) is not None:
            job_store.update(job.id, status="failed", error="Enrichment already running")
            continue
        spawn_tracked_enrichment_task(
            run_enrichment_job(
                job_id=job.id,
                case_id=run.case_id,
                timeline_id=run.timeline_id,
                enricher_key=run.enricher_key,
                source_ids=source_ids,
                job_store=job_store,
                store=store,
                ch_store=ch_store,
            )
        )
