"""Background job evaluating Sigma rules over a timeline's events.

Runs as a sync function under FastAPI ``BackgroundTasks`` (same convention
as the embedding job in ``api/routers/cases.py``): one ``asyncio.run`` owns
every Postgres await (asyncpg pools are loop-bound), while ClickHouse scans
stream on a producer thread bridged through a bounded ``asyncio.Queue`` —
a match-everything rule never materializes its full hit list in memory.

Per rule: compile → clear previous hits (``delete_system_annotations``,
preserving confirmed findings) → stream matching ``(event_id, source_id)``
rows under ``HEAVY_SCAN_GATE`` → write ``Annotation(origin="system",
annotation_type="sigma", detector=<rule_key>)`` in batches. The persisted
``SigmaRun`` record snapshots each rule's content hash, compiled SQL, and
match count — the forensic "why is this event tagged" chain.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from vestigo.core.config import get_settings
from vestigo.core.jobs import JobStore
from vestigo.db._scan import HEAVY_SCAN_GATE, heavy_scan_settings
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.postgres import PostgresStore, generate_id
from vestigo.sigma.backend import compile_rule
from vestigo.sigma.rules import LoadedRule, load_case_rule, load_global_rules

logger = logging.getLogger(__name__)

_QUEUE_DEPTH = 4
_STREAM_END = object()


def _resolve_selected_rules(
    global_rules: list[LoadedRule],
    case_rules: list[LoadedRule],
    selection: list[dict[str, str]] | None,
) -> tuple[list[LoadedRule], list[tuple[str, str]]]:
    """Apply the run request's rule selection; ``None``/empty = all loadable rules.

    Returns ``(rules, missing)`` where *missing* is every selected
    ``(origin, ref)`` that resolved to nothing (deleted or disabled since the
    request was built) — reported per-rule in the run record rather than
    silently dropped, so the record always accounts for the full selection.
    """
    pool = global_rules + case_rules
    if not selection:
        return pool, []
    wanted = [(s["origin"], s["ref"]) for s in selection]
    wanted_set = set(wanted)
    rules = [r for r in pool if (r.origin, r.ref) in wanted_set]
    found = {(r.origin, r.ref) for r in rules}
    return rules, [w for w in wanted if w not in found]


class _ProducerAbort(Exception):
    """Raised inside the producer thread when the consumer signalled stop."""


def _stream_rule_hits(
    ch: ClickHouseStore,
    case_id: str,
    source_ids: list[str],
    condition_sql: str,
    batch_size: int,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
    stop: threading.Event,
) -> None:
    """Producer thread: stream matching rows into *queue* in batches.

    The blocking put is the backpressure that keeps a multi-million-hit rule
    from buffering unboundedly; it polls *stop* so a failed consumer (which
    can no longer drain the queue) unwinds the producer instead of leaving it
    parked forever holding a ``HEAVY_SCAN_GATE`` slot. Exceptions are shipped
    through the queue so the consumer re-raises them in the event loop.
    """

    def _put(item: Any) -> None:
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        while True:
            if stop.is_set():
                future.cancel()
                raise _ProducerAbort
            try:
                future.result(timeout=0.5)
                return
            except TimeoutError:
                continue

    try:
        in_clause, params = ClickHouseStore.string_in_clause("src", source_ids)
        params["case_id"] = case_id
        query = (
            f"SELECT event_id, source_id FROM {ch.database}.events "  # noqa: S608
            f"WHERE case_id = {{case_id:String}} AND source_id IN ({in_clause}) "
            f"AND ({condition_sql}) {heavy_scan_settings()}"
        )
        with HEAVY_SCAN_GATE, ch.client.query_rows_stream(query, parameters=params) as stream:
            batch: list[tuple[str, str]] = []
            for row in stream:
                batch.append((str(row[0]), str(row[1])))
                if len(batch) >= batch_size:
                    _put(batch)
                    batch = []
            if batch:
                _put(batch)
        _put(_STREAM_END)
    except _ProducerAbort:
        logger.debug("Sigma scan producer aborted: consumer stopped draining")
    except BaseException as exc:  # noqa: BLE001 — shipped to the consumer, re-raised there
        try:
            _put(exc)
        except (_ProducerAbort, Exception):  # consumer/loop gone — nothing left to notify
            logger.exception("Sigma scan producer failed after consumer went away")


async def _scan_and_annotate(
    ch: ClickHouseStore,
    store: PostgresStore,
    case_id: str,
    source_ids: list[str],
    rule: LoadedRule,
    condition_sql: str,
    run_id: str,
    confirmed_keys: set[tuple[str, str]],
    batch_size: int,
    on_progress,
) -> int:
    """Stream one rule's hits and persist them as system annotations. Returns match count."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_DEPTH)
    stop = threading.Event()
    producer = threading.Thread(
        target=_stream_rule_hits,
        args=(ch, case_id, source_ids, condition_sql, batch_size, loop, queue, stop),
        name=f"sigma-scan-{rule.rule_key[:8]}",
        daemon=True,
    )
    producer.start()
    matched = 0
    try:
        while True:
            item = await queue.get()
            if item is _STREAM_END:
                break
            if isinstance(item, BaseException):
                raise item
            rows: list[tuple[str, str]] = item
            matched += len(rows)
            annotation_rows = [
                {
                    "annotation_id": generate_id(f"{event_id}_sigma_{rule.rule_key}"),
                    "case_id": case_id,
                    "source_id": source_id,
                    "event_id": event_id,
                    "annotation_type": "sigma",
                    "content": f"sigma: {rule.title}",
                    "origin": "system",
                    "detector": rule.rule_key,
                    "details": {
                        "run_id": run_id,
                        "rule_key": rule.rule_key,
                        "rule_uuid": rule.rule_uuid,
                        "title": rule.title,
                        "level": rule.level,
                        "content_hash": rule.content_hash,
                        "logsource": rule.logsource,
                    },
                }
                for event_id, source_id in rows
                if (event_id, rule.rule_key) not in confirmed_keys
            ]
            await store.bulk_create_annotations(annotation_rows)
            on_progress(matched)
    finally:
        # Unwind the producer even when this consumer failed mid-stream: the
        # stop flag aborts its blocking put so it exits the HEAVY_SCAN_GATE
        # context instead of holding a slot forever.
        stop.set()
        await asyncio.to_thread(producer.join, 30)
        if producer.is_alive():
            logger.warning("Sigma scan producer for rule %s did not exit in time", rule.rule_key)
    return matched


def run_sigma_job(
    job_id: str,
    run_id: str,
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    selection: list[dict[str, str]] | None,
    job_store: JobStore,
    created_by: str | None,
    username: str | None,
) -> None:
    """Job body: evaluate the selected Sigma rules over the timeline scope."""
    settings = get_settings()
    batch_size = settings.sigma_annotation_batch_size
    store = PostgresStore()
    ch = ClickHouseStore()

    async def _main() -> None:
        timeline = await store.get_timeline(case_id, timeline_id)
        field_mappings = timeline.field_mappings if timeline else None

        global_rules = await asyncio.to_thread(load_global_rules, settings.sigma_rules_path)
        case_rows = await store.list_sigma_rules(case_id)
        case_rules = [
            load_case_rule(row.id, row.yaml_content, {}) for row in case_rows if row.enabled
        ]
        rules, missing = _resolve_selected_rules(global_rules, case_rules, selection)

        await store.update_sigma_run(run_id, status="running")
        job_store.update(
            job_id,
            status="running",
            progress={"rules_total": len(rules), "rules_done": 0, "hits": 0},
        )

        results: list[dict[str, Any]] = []
        # Selected rules that no longer resolve (deleted/disabled between
        # building the request and running it) get an explicit error entry —
        # the run record must account for the full selection.
        for origin, ref in missing:
            results.append(
                {
                    "rule_key": f"{origin}:{ref}",
                    "origin": origin,
                    "ref": ref,
                    "title": ref,
                    "level": None,
                    "logsource": {},
                    "content_hash": "",
                    "sql": None,
                    "match_count": 0,
                    "status": "error",
                    "error": "selected rule not found (deleted or disabled since selection)",
                    "fallback_fields": [],
                }
            )
        total_hits = 0
        for done, rule in enumerate(rules):
            entry: dict[str, Any] = {
                "rule_key": rule.rule_key,
                "origin": rule.origin,
                "ref": rule.ref,
                "title": rule.title,
                "level": rule.level,
                "logsource": rule.logsource,
                "content_hash": rule.content_hash,
                "sql": None,
                "match_count": 0,
                "status": "empty",
                "error": None,
                "fallback_fields": [],
            }
            job_store.update(
                job_id,
                progress={"rules_done": done, "current_rule": rule.title, "hits": total_hits},
            )
            if rule.parsed is None:
                entry["status"] = "error"
                entry["error"] = rule.error
                results.append(entry)
                continue
            compiled = compile_rule(rule.parsed, field_mappings, rule.fieldmap)
            if compiled.sql is None:
                entry["status"] = "not_applicable"
                entry["error"] = compiled.error
                results.append(entry)
                continue
            entry["sql"] = compiled.sql
            entry["fallback_fields"] = compiled.fallback_fields
            try:
                # Scoped per rule: an empty set keeps delete_system_annotations
                # on its fast bulk-DELETE path — the preserve path loads every
                # matching row into memory, unacceptable for a no-cap rule.
                confirmed_keys = await store.list_confirmed_keys(
                    case_id, source_ids, detector=rule.rule_key
                )
                await store.delete_system_annotations(
                    case_id,
                    source_ids,
                    "sigma",
                    detector=rule.rule_key,
                    preserve_keys=confirmed_keys,
                )

                def on_progress(matched: int, _done: int = done, _base: int = total_hits) -> None:
                    job_store.update(
                        job_id, progress={"hits": _base + matched, "rules_done": _done}
                    )

                count = await _scan_and_annotate(
                    ch,
                    store,
                    case_id,
                    source_ids,
                    rule,
                    compiled.sql,
                    run_id,
                    confirmed_keys,
                    batch_size,
                    on_progress,
                )
                total_hits += count
                entry["match_count"] = count
                entry["status"] = "matched" if count else "empty"
            except Exception as exc:  # noqa: BLE001 — one bad rule must not sink the run
                logger.exception("Sigma rule %s failed", rule.ref)
                entry["status"] = "error"
                entry["error"] = str(exc)[:1024]
            results.append(entry)
            # Persist incrementally so a crash mid-run leaves a usable record.
            await store.update_sigma_run(run_id, results=results)

        await store.update_sigma_run(run_id, status="completed", results=results, completed=True)
        await store.record_audit(
            action="sigma.run",
            user_id=created_by,
            username_snapshot=username,
            case_id=case_id,
            target_type="sigma_run",
            target_id=run_id,
            detail={
                "timeline_id": timeline_id,
                "rules_evaluated": len(rules),
                "total_hits": total_hits,
            },
        )
        job_store.update(
            job_id,
            status="completed",
            progress={"rules_total": len(rules), "rules_done": len(rules), "hits": total_hits},
            result={"run_id": run_id, "total_hits": total_hits, "rules_evaluated": len(rules)},
        )

    try:
        asyncio.run(_run_with_dispose(_main, store))
    except Exception as exc:  # noqa: BLE001 — job boundary: record, never raise
        logger.exception("Sigma run %s failed", run_id)
        error_text = str(exc)
        job_store.update(job_id, status="failed", error=error_text[:1024])
        try:
            fail_store = PostgresStore()

            async def _mark_failed() -> None:
                try:
                    await fail_store.update_sigma_run(
                        run_id, status="failed", error=error_text, completed=True
                    )
                finally:
                    await fail_store.engine.dispose()

            asyncio.run(_mark_failed())
        except Exception:  # noqa: BLE001
            logger.exception("Could not mark sigma run %s failed", run_id)


async def _run_with_dispose(main, store: PostgresStore) -> None:
    """Run *main*, always disposing the engine before the loop closes."""
    try:
        await main()
    finally:
        await store.engine.dispose()
