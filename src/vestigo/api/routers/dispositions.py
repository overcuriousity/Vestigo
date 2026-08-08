"""API routes for the unified finding-disposition taxonomy.

A **disposition** is one analyst verdict on an anomaly finding — ``normal``
(expected behavior; extends the baseline and suppresses detection),
``dismissed`` (noise for this investigation; presentation-only, detectors
keep scoring), ``confirmed`` (escalated true positive; durable across
re-scans) or ``routine`` (a real recurring pattern from the sequence_motif
miner; presentation-only, but its occurrences are materialized to ClickHouse
so the event grid can collapse them behind an explicit count). Undecided is
the absence of a row. See ``db/postgres.py::FindingDisposition`` and
``docs/ANOMALY_DETECTION.md``.

Every mutation is audited — dispositions are analytical assertions, so who
declared what, and when, is part of the case record. Rows stay freely
deletable: forensic reproducibility is carried by the DetectorRun snapshot
(``dispositions_hash`` in ``params``), never by these rows surviving.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from vestigo.api.deps import (
    get_store,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from vestigo.core.jobs import JobStore, get_job_store
from vestigo.db._template import TEMPLATE_NORMALIZE_VERSION
from vestigo.db.postgres import DISPOSITION_KINDS, Case, User

router = APIRouter(prefix="/api/cases", tags=["dispositions"])

MAX_BULK_ITEMS = 500


class DispositionCreate(BaseModel):
    """Body for declaring one disposition. Exactly one scope:

    - value scope: ``field`` + ``value`` (the pair is dispositioned on every
      event of the timeline);
    - event scope: ``source_id`` + ``event_id`` (one concrete event).
    """

    kind: str = Field(pattern="^(normal|dismissed|confirmed|routine)$")
    detector: str = Field(default="*", min_length=1, max_length=32)
    field: str | None = Field(default=None, min_length=1, max_length=255)
    value: str | None = Field(default=None, max_length=4096)
    source_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=4096)
    details: dict | None = None
    #: The analysis comparison this verdict was reached under — ``frame``,
    #: ``baseline_id``, ``baseline_name``, as echoed by the analysis endpoints.
    #: A verdict is an assertion about a comparison, so without it "confirmed
    #: on 4 March" cannot say what the finding was compared against. Optional:
    #: clients that do not know their scope record none rather than a guess.
    #: Named ``analysis_scope`` because "scope" is already taken above for the
    #: value-vs-event distinction.
    analysis_scope: dict | None = None


class DispositionBulkCreate(BaseModel):
    """Body for declaring several dispositions in one audited action."""

    items: list[DispositionCreate] = Field(min_length=1, max_length=MAX_BULK_ITEMS)


def _validate_scope(p: DispositionCreate) -> str:
    """Enforce the scope invariant; returns "value" or "event".

    Exactly one of value scope (``field`` + ``value``) or event scope
    (``source_id`` + ``event_id``) must be fully given. ``confirmed``
    additionally requires event scope and a concrete detector — confirming
    "some value, any detector" is not a meaningful escalation.
    """
    has_value = p.field is not None and p.value is not None
    has_event = p.source_id is not None and p.event_id is not None
    half_value = (p.field is None) != (p.value is None)
    half_event = (p.source_id is None) != (p.event_id is None)
    if half_value or half_event or has_value == has_event:
        raise HTTPException(
            status_code=422,
            detail="Exactly one scope required: field+value, or source_id+event_id",
        )
    if p.kind == "confirmed":
        if not has_event:
            raise HTTPException(status_code=422, detail="confirmed requires event scope")
        if p.detector == "*":
            raise HTTPException(status_code=422, detail="confirmed requires a concrete detector")
    if p.kind == "routine":
        if p.detector not in ("sequence_motif", "log_template"):
            raise HTTPException(
                status_code=422,
                detail="routine requires detector 'sequence_motif' or 'log_template'",
            )
        if not has_value:
            raise HTTPException(status_code=422, detail="routine requires value scope")
        if p.detector == "sequence_motif":
            # sequence_motif's verdict: value scope is the (series_field,
            # " → "-joined n-gram) pair, and the occurrence materialization
            # needs the motif's values/n from `details`.
            values = (p.details or {}).get("values")
            if (
                not isinstance(values, list)
                or not 2 <= len(values) <= 5
                or not all(isinstance(v, str) and v for v in values)
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "routine requires details.values (the motif's 2–5 non-empty string values)"
                    ),
                )
            # The displayed value and the materialized occurrences must
            # describe the same motif — an inconsistent pair would collapse
            # events the row doesn't announce.
            if p.value != " → ".join(values):
                raise HTTPException(
                    status_code=422,
                    detail="routine value must equal ' → '.join(details.values)",
                )
            # A snapshotted mining frame must be parseable — materialization
            # honors it, and a malformed value must never silently degrade to
            # an unscoped (wider) collapse.
            for key in ("scope_start", "scope_end"):
                raw = (p.details or {}).get(key)
                if raw is None:
                    continue
                try:
                    if not isinstance(raw, str):
                        raise ValueError
                    datetime.fromisoformat(raw)
                except ValueError:
                    raise HTTPException(
                        status_code=422,
                        detail=f"routine details.{key} must be an ISO timestamp",
                    ) from None
        else:
            # log_template (W6): value is the decimal template_id — the
            # collapse predicate binds it straight into
            # `template_hash IN (...)`, no aux-table materialization, so
            # identity is enforced here instead of at a job boundary.
            if p.field != "template_id":
                raise HTTPException(
                    status_code=422, detail="log_template routine requires field 'template_id'"
                )
            if p.value is None or not p.value.isdigit():
                raise HTTPException(
                    status_code=422,
                    detail="log_template routine value must be the decimal template_id",
                )
            details = p.details or {}
            template = details.get("template")
            if not isinstance(template, str) or not template:
                raise HTTPException(
                    status_code=422, detail="log_template routine requires details.template"
                )
            # The collapse predicate is `template_hash NOT IN (...)` against the
            # materialized column, which is hashed over `message` and nothing
            # else. `list_log_templates` will happily template any field, so a
            # disposition minted from an `attr:*` listing would carry a hash
            # from a different domain and collapse events this row does not
            # describe — the one thing a routine collapse must never do.
            # Reject it here: the disposition is the forensic record, and it
            # must not be creatable in a state the grid cannot honor.
            templated_field = details.get("field") or "message"
            if templated_field != "message":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "log_template routine currently supports only details.field 'message' — "
                        "the grid's collapse predicate uses the materialized message template hash"
                    ),
                )
            if details.get("template_version") != TEMPLATE_NORMALIZE_VERSION:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "log_template routine requires details.template_version == "
                        f"{TEMPLATE_NORMALIZE_VERSION}"
                    ),
                )
    return "value" if has_value else "event"


async def _require_timeline(case_id: str, timeline_id: str) -> None:
    if await get_store().get_timeline(case_id, timeline_id) is None:
        raise HTTPException(status_code=404, detail="Timeline not found")


def _details_scope(details: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Parse the mining frame the motif finding snapshotted into its details.

    ``scope_start``/``scope_end`` are the ISO timestamps ``find_sequence_motifs``
    records when mining was time-scoped; materialization must honor them so
    the collapse covers exactly what the analyst saw mined. Absent values mean
    unscoped mining; a *present but unparseable* value raises — degrading to
    unscoped would collapse MORE than the analyst saw, so the job must fail
    instead (create-time validation makes this unreachable for API-written
    rows; this guards pre-validation rows and future writers).
    """

    def _parse(key: str) -> datetime | None:
        raw = details.get(key)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be an ISO timestamp string, got {type(raw).__name__}")
        return datetime.fromisoformat(raw)

    return _parse("scope_start"), _parse("scope_end")


def _run_motif_materialization_job(
    job_id: str,
    case_id: str,
    source_ids: list[str],
    series_field: str,
    values: list[str],
    disposition_id: str,
    start: datetime | None,
    end: datetime | None,
    field_mappings: dict[str, list[str]] | None,
    source_offsets: dict[str, int] | None,
    job_store: JobStore,
) -> None:
    """Resolve a routine motif's occurrences into ClickHouse (background).

    Sync on purpose — BackgroundTasks runs it in the threadpool, keeping the
    blocking ClickHouse scan off the event loop. Failure leaves the grid
    collapse a no-op for this disposition (no occurrence rows), never a wrong
    result; the job error is pollable via the jobs API.

    The outcome (rows written, cap warnings, or the failure) is also persisted
    to the disposition's ``details.materialization`` — the JobStore is
    in-memory, and "this collapse is partial/inactive" must survive a restart.
    """
    from vestigo.api.routers.events import _get_stat_anomaly_service

    job_store.update(job_id, status="running")
    try:
        written, warnings = _get_stat_anomaly_service().resolve_motif_occurrences(
            case_id=case_id,
            source_ids=source_ids,
            series_field=series_field,
            values=values,
            disposition_id=disposition_id,
            start=start,
            end=end,
            field_mappings=field_mappings,
            source_offsets=source_offsets,
        )
        _persist_materialization_outcome(
            case_id,
            disposition_id,
            {"status": "completed", "rows_written": written, "warnings": warnings},
        )
        job_store.update(
            job_id,
            status="completed",
            result={"rows_written": written, "warnings": warnings},
        )
    except Exception as exc:  # noqa: BLE001 — job boundary: report, don't crash the pool
        _persist_materialization_outcome(
            case_id, disposition_id, {"status": "failed", "error": str(exc)}
        )
        job_store.update(job_id, status="failed", error=str(exc))


def _persist_materialization_outcome(
    case_id: str, disposition_id: str, outcome: dict[str, Any]
) -> None:
    """Best-effort write of the materialization outcome onto the disposition.

    Runs in the job's threadpool thread. A fresh store + one ``asyncio.run``,
    engine disposed before the loop closes — the shared ``get_store()`` pool's
    asyncpg connections are bound to the main loop and must not be used here
    (same pattern as the embedding job's ``_finalize``). Swallows failures: a
    Postgres hiccup must not turn a completed ClickHouse materialization into
    a failed job — the job result still carries the same outcome.
    """
    import asyncio
    import contextlib

    from vestigo.db.postgres import PostgresStore

    async def _write() -> None:
        store = PostgresStore()
        try:
            await store.update_disposition_details(
                case_id, disposition_id, {"materialization": outcome}
            )
        finally:
            await store.engine.dispose()

    with contextlib.suppress(Exception):
        asyncio.run(_write())


def _cleanup_motif_occurrences(case_id: str, disposition_id: str) -> None:
    """Best-effort background sweep of a deleted disposition's occurrence rows."""
    import contextlib

    from vestigo.api.routers.events import _get_stat_anomaly_service

    # Suppression is safe: rows are inert either way; this only reclaims space.
    with contextlib.suppress(Exception):
        _get_stat_anomaly_service().ch.delete_motif_occurrences(case_id, disposition_id)


async def _schedule_routine_materialization(
    background_tasks: BackgroundTasks,
    case_id: str,
    timeline_id: str,
    rows: list[dict[str, Any]],
    user: User,
) -> list[str]:
    """Queue one occurrence-materialization job per new routine disposition.

    Returns the job ids (surfaced in the create response so the client can
    poll). Rows that are not routine/sequence_motif are skipped.
    """
    routine_rows = [
        r
        for r in rows
        if r["kind"] == "routine" and r["detector"] == "sequence_motif" and r.get("details")
    ]
    if not routine_rows:
        return []
    from vestigo.api.routers.events import _resolve_timeline_scope

    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    job_store = get_job_store()
    job_ids: list[str] = []
    for row in routine_rows:
        job = job_store.create(
            kind="motif_materialize",
            progress={"disposition_id": row["id"], "value": row["value"]},
            created_by=user.id,
            case_id=case_id,
        )
        start, end = _details_scope(row["details"])
        background_tasks.add_task(
            _run_motif_materialization_job,
            job.id,
            case_id,
            source_ids,
            row["field"],
            list(row["details"]["values"]),
            row["id"],
            start,
            end,
            field_mappings,
            source_offsets,
            job_store,
        )
        job_ids.append(job.id)
    return job_ids


@router.get("/{case_id}/timelines/{timeline_id}/dispositions")
async def list_dispositions(
    case_id: str,
    timeline_id: str,
    kind: str | None = None,
    detector: str | None = None,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List the dispositions visible from this timeline, newest first.

    Value-scoped rows matching the timeline plus event-scoped rows whose
    source belongs to it. ``kind``/``detector`` narrow the result;
    ``detector`` also matches ``"*"`` wildcard rows.
    """
    if kind is not None and kind not in DISPOSITION_KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown kind {kind!r}")
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    source_ids = [s.id for s in timeline.sources]
    rows = await store.list_dispositions(
        case_id,
        timeline_id=timeline_id,
        source_ids=source_ids,
        kinds=[kind] if kind else None,
        detector=detector,
    )
    return {"dispositions": [d.to_dict() for d in rows]}


@router.get("/{case_id}/timelines/{timeline_id}/dispositions/stats")
async def disposition_stats(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Per-day disposition counts by kind, plus cumulative totals — the
    triage burn-down source.

    Aggregated in Python over the same timeline-scoped row set
    ``list_dispositions`` returns: counts are small, this stays
    dialect-portable (SQLite runs the same code in tests), and there is one
    scoping code path. Days are UTC calendar dates of ``created_at``, sorted
    ascending, gap-free filling left to the client. Counts reflect *current*
    rows only — deleted verdicts are not shown; the audit trail records
    deletions.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    source_ids = [s.id for s in timeline.sources]
    rows = await store.list_dispositions(case_id, timeline_id=timeline_id, source_ids=source_ids)

    by_day: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.created_at is None:
            continue
        # SQLite (tests) returns naive datetimes; they are stored as UTC.
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        day = created.astimezone(UTC).date().isoformat()
        counts = by_day.setdefault(day, dict.fromkeys(DISPOSITION_KINDS, 0))
        counts[row.kind] += 1

    days: list[dict[str, Any]] = []
    cumulative = dict.fromkeys(DISPOSITION_KINDS, 0)
    for day in sorted(by_day):
        counts = by_day[day]
        for kind in DISPOSITION_KINDS:
            cumulative[kind] += counts[kind]
        days.append(
            {
                "date": day,
                **counts,
                "total": sum(counts.values()),
                "cumulative": {**cumulative, "total": sum(cumulative.values())},
            }
        )
    totals = {**cumulative, "total": sum(cumulative.values())}
    return {"days": days, "totals": totals}


async def _create_one(
    case_id: str, timeline_id: str, payload: DispositionCreate, user: User
) -> dict[str, Any]:
    scope = _validate_scope(payload)
    row = await get_store().create_disposition(
        case_id=case_id,
        kind=payload.kind,
        detector=payload.detector,
        # Event-scoped rows carry no timeline (events live once per Source
        # and appear in multiple timelines).
        timeline_id=timeline_id if scope == "value" else None,
        field=payload.field,
        value=payload.value,
        source_id=payload.source_id,
        event_id=payload.event_id,
        note=payload.note,
        details=payload.details,
        analysis_scope=payload.analysis_scope,
        created_by=user.id,
    )
    return row.to_dict()


@router.post("/{case_id}/timelines/{timeline_id}/dispositions")
async def create_disposition(
    case_id: str,
    timeline_id: str,
    payload: DispositionCreate,
    background_tasks: BackgroundTasks,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Declare one disposition. Idempotent: an identical row is returned, not duplicated."""
    await _require_timeline(case_id, timeline_id)
    row = await _create_one(case_id, timeline_id, payload, user)
    job_ids = await _schedule_routine_materialization(
        background_tasks, case_id, timeline_id, [row], user
    )
    await get_store().record_audit(
        action="disposition.create",
        actor=user,
        case_id=case_id,
        target_type="finding_disposition",
        target_id=row["id"],
        detail={
            "kind": payload.kind,
            "detector": payload.detector,
            "field": payload.field,
            "event_id": payload.event_id,
        },
    )
    response: dict[str, Any] = {"disposition": row}
    if job_ids:
        response["materialization_job_id"] = job_ids[0]
    return response


@router.post("/{case_id}/timelines/{timeline_id}/dispositions/bulk")
async def bulk_create_dispositions(
    case_id: str,
    timeline_id: str,
    payload: DispositionBulkCreate,
    background_tasks: BackgroundTasks,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Declare several dispositions in one action (single audit entry)."""
    await _require_timeline(case_id, timeline_id)
    # Validate everything first, then write in ONE transaction — a bulk
    # action is one analyst intent and must not half-apply, neither on a
    # validation error nor on a mid-batch database error.
    scopes = [_validate_scope(item) for item in payload.items]
    rows = [
        r.to_dict()
        for r in await get_store().create_dispositions_bulk(
            case_id,
            [
                {
                    "kind": item.kind,
                    "detector": item.detector,
                    # Event-scoped rows carry no timeline (events live once
                    # per Source and appear in multiple timelines).
                    "timeline_id": timeline_id if scope == "value" else None,
                    "field": item.field,
                    "value": item.value,
                    "source_id": item.source_id,
                    "event_id": item.event_id,
                    "note": item.note,
                    "details": item.details,
                    "analysis_scope": item.analysis_scope,
                    "created_by": user.id,
                }
                for item, scope in zip(payload.items, scopes, strict=True)
            ],
        )
    ]
    job_ids = await _schedule_routine_materialization(
        background_tasks, case_id, timeline_id, rows, user
    )
    await get_store().record_audit(
        action="disposition.bulk_create",
        actor=user,
        case_id=case_id,
        target_type="finding_disposition",
        target_id=rows[0]["id"] if rows else None,
        detail={"count": len(rows), "kinds": sorted({i.kind for i in payload.items})},
    )
    response: dict[str, Any] = {"dispositions": rows}
    if job_ids:
        response["materialization_job_ids"] = job_ids
    return response


@router.delete("/{case_id}/timelines/{timeline_id}/dispositions/{disposition_id}")
async def delete_disposition(
    case_id: str,
    timeline_id: str,
    disposition_id: str,
    background_tasks: BackgroundTasks,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Remove a disposition — the finding becomes flaggable/visible again."""
    store = get_store()
    deleted = await store.delete_disposition(case_id, disposition_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Disposition not found")
    # Routine rows in motif_occurrences are already inert once the disposition
    # is gone (the grid filters by *active* disposition ids); this best-effort
    # sweep just reclaims space. Harmless no-op for non-routine kinds.
    background_tasks.add_task(_cleanup_motif_occurrences, case_id, disposition_id)
    await store.record_audit(
        action="disposition.delete",
        actor=user,
        case_id=case_id,
        target_type="finding_disposition",
        target_id=disposition_id,
    )
    return {"deleted": True, "disposition_id": disposition_id}
