"""Background enrichment job: read events, run an enricher, stage + flush results.

Processing is crash/resume-safe at the result level (not the cursor level):
results are staged in Postgres as each batch completes (transactional, so
they survive a process crash even though the in-memory JobStore does not),
then bulk-flushed to ClickHouse's append-only ``event_enrichments`` table
periodically and at job completion. If the process dies mid-run, the durable
``EnrichmentJobRun`` marker lets startup reconciliation detect and discard
the orphaned run (see ``reconcile_orphaned_enrichment_jobs``) — there is no
resume-from-cursor support by design, since that would require tracking
per-source read offsets durably, which this design deliberately avoids.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from tracesignal.core.config import get_settings
from tracesignal.core.jobs import JobStore
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.postgres import PostgresStore
from tracesignal.enrichers.base import Enricher
from tracesignal.enrichers.registry import get_enricher

logger = logging.getLogger(__name__)


def _process_batch(
    enricher: Enricher,
    batch: list[dict[str, Any]],
    case_id: str,
    source_id: str,
    timeline_id: str,
    job_id: str,
    enricher_key: str,
) -> list[dict[str, Any]]:
    """Regex-match attributes in a batch of events and run the enricher on matches.

    Synchronous — run inside ``asyncio.to_thread`` by the caller, since it
    calls the (blocking) enricher and iterates in-memory event dicts.
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for event in batch:
        event_id = str(event["event_id"])
        attributes = event.get("attributes") or {}
        for attr_key, raw_value in attributes.items():
            if not raw_value or not enricher.is_field_eligible(raw_value):
                continue
            enriched = enricher.enrich_value(raw_value)
            if not enriched:
                continue
            for output_field, value in enriched.items():
                if not value:
                    continue
                rows.append(
                    {
                        "job_id": job_id,
                        "case_id": case_id,
                        "source_id": source_id,
                        "timeline_id": timeline_id,
                        "event_id": event_id,
                        "enricher_key": enricher_key,
                        "field_key": f"{output_field}__{attr_key}",
                        "value": value,
                        "computed_at": now,
                    }
                )
    return rows


async def _flush_staged_rows(store: PostgresStore, ch_store: ClickHouseStore, job_id: str) -> None:
    """Flush every currently-staged row for a job to ClickHouse, then delete it from staging.

    Only deletes staged rows after the ClickHouse insert succeeds, so a
    failure here leaves the staging rows intact for the next flush attempt.
    """
    while True:
        staged = await store.pop_staged_rows_for_job(job_id, limit=5000)
        if not staged:
            return
        ch_rows = [
            {
                "event_id": row.event_id,
                "case_id": row.case_id,
                "source_id": row.source_id,
                "enricher_key": row.enricher_key,
                "field_key": row.field_key,
                "value": row.value,
                "computed_at": row.computed_at,
            }
            for row in staged
        ]
        await asyncio.to_thread(ch_store.bulk_insert_enrichments, ch_rows)
        await store.delete_staged_rows([row.id for row in staged])


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
    """Run one enricher over a set of sources, staging and flushing results.

    Batches are paginated via the same ``list_events`` primitive the
    embedding pipeline uses, at ``settings.embedding_batch_size``. Every
    ``settings.enrichment_flush_batch_count`` batches (and always at the end)
    staged results are bulk-flushed from Postgres to ClickHouse.
    """
    enricher = get_enricher(enricher_key)
    if enricher is None:
        job_store.update(job_id, status="failed", error=f"Unknown enricher: {enricher_key}")
        return

    settings = get_settings()
    batch_size = settings.embedding_batch_size
    flush_every = settings.enrichment_flush_batch_count

    await store.start_enrichment_job_run(job_id, timeline_id, case_id, enricher_key)
    job_store.update(job_id, status="running", progress={"processed": 0, "total": 0})

    try:
        total = 0
        for source_id in source_ids:
            total += await asyncio.to_thread(
                ch_store.count_events, case_id=case_id, source_id=source_id
            )
        job_store.update(job_id, progress={"processed": 0, "total": total})

        processed = 0
        batches_since_flush = 0
        for source_id in source_ids:
            offset = 0
            while True:
                batch = await asyncio.to_thread(
                    ch_store.list_events,
                    case_id=case_id,
                    source_id=source_id,
                    limit=batch_size,
                    offset=offset,
                )
                if not batch:
                    break
                rows = await asyncio.to_thread(
                    _process_batch,
                    enricher,
                    batch,
                    case_id,
                    source_id,
                    timeline_id,
                    job_id,
                    enricher_key,
                )
                if rows:
                    await store.stage_enrichment_results(rows)

                processed += len(batch)
                offset += batch_size
                batches_since_flush += 1
                job_store.update(job_id, progress={"processed": processed, "total": total})

                if batches_since_flush >= flush_every:
                    await _flush_staged_rows(store, ch_store, job_id)
                    batches_since_flush = 0

                if len(batch) < batch_size:
                    break

        await _flush_staged_rows(store, ch_store, job_id)
        await store.finish_enrichment_job_run(job_id)
        job_store.update(
            job_id,
            status="completed",
            progress={"processed": processed, "total": total},
            result={"enricher_key": enricher_key, "events_processed": processed},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Enrichment job %s failed", job_id)
        job_store.update(job_id, status="failed", error=str(exc))
        # Leave the EnrichmentJobRun marker and any unflushed staging rows in
        # place — startup reconciliation (or a manual retry) handles cleanup.
    finally:
        close = getattr(enricher, "close", None)
        if callable(close):
            close()


async def reconcile_orphaned_enrichment_jobs(store: PostgresStore) -> None:
    """Discard enrichment jobs left running by a mid-run process restart.

    Mirrors the orphaned-ingest cleanup in ``api/main.py``: the in-memory
    JobStore is empty on a fresh boot, so any ``EnrichmentJobRun`` marker
    still present means the process died mid-run. Note that results may
    already have been periodically flushed to ClickHouse before the crash
    (see ``settings.enrichment_flush_batch_count``) — this only discards the
    remaining *unflushed* staged rows and clears the durable marker. Rows
    already committed to ClickHouse are left as-is; hydration reads (see
    ``db/queries.py::_hydrate_enrichments``) must tolerate the resulting
    duplicates from a later re-run rather than assume a clean slate.
    """
    orphaned = await store.list_orphaned_enrichment_job_runs()
    for run in orphaned:
        await store.delete_staged_rows_for_job(run.job_id)
        await store.finish_enrichment_job_run(run.job_id)
        await store.record_audit(
            action="enricher.job_orphaned",
            case_id=run.case_id,
            target_type="timeline",
            target_id=run.timeline_id,
            detail={"job_id": run.job_id, "enricher_key": run.enricher_key},
        )
        logger.warning(
            "Discarded orphaned enrichment job %s (enricher=%s, timeline=%s)",
            run.job_id,
            run.enricher_key,
            run.timeline_id,
        )
