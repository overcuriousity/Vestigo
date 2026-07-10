"""API routes for the unified finding-disposition taxonomy.

A **disposition** is one analyst verdict on an anomaly finding — ``normal``
(expected behavior; extends the baseline and suppresses detection),
``dismissed`` (noise for this investigation; presentation-only, detectors
keep scoring) or ``confirmed`` (escalated true positive; durable across
re-scans). Undecided is the absence of a row. See
``db/postgres.py::FindingDisposition`` and ``docs/ANOMALY_DETECTION.md``.

Every mutation is audited — dispositions are analytical assertions, so who
declared what, and when, is part of the case record. Rows stay freely
deletable: forensic reproducibility is carried by the DetectorRun snapshot
(``dispositions_hash`` in ``params``), never by these rows surviving.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from tracesignal.api.deps import (
    get_store,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from tracesignal.db.postgres import DISPOSITION_KINDS, Case, User

router = APIRouter(prefix="/api/cases", tags=["dispositions"])

MAX_BULK_ITEMS = 500


class DispositionCreate(BaseModel):
    """Body for declaring one disposition. Exactly one scope:

    - value scope: ``field`` + ``value`` (the pair is dispositioned on every
      event of the timeline);
    - event scope: ``source_id`` + ``event_id`` (one concrete event).
    """

    kind: str = Field(pattern="^(normal|dismissed|confirmed)$")
    detector: str = Field(default="*", min_length=1, max_length=32)
    field: str | None = Field(default=None, min_length=1, max_length=255)
    value: str | None = Field(default=None, max_length=4096)
    source_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=4096)
    details: dict | None = None


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
    return "value" if has_value else "event"


async def _require_timeline(case_id: str, timeline_id: str) -> None:
    if await get_store().get_timeline(case_id, timeline_id) is None:
        raise HTTPException(status_code=404, detail="Timeline not found")


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
        created_by=user.id,
    )
    return row.to_dict()


@router.post("/{case_id}/timelines/{timeline_id}/dispositions")
async def create_disposition(
    case_id: str,
    timeline_id: str,
    payload: DispositionCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Declare one disposition. Idempotent: an identical row is returned, not duplicated."""
    await _require_timeline(case_id, timeline_id)
    row = await _create_one(case_id, timeline_id, payload, user)
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
    return {"disposition": row}


@router.post("/{case_id}/timelines/{timeline_id}/dispositions/bulk")
async def bulk_create_dispositions(
    case_id: str,
    timeline_id: str,
    payload: DispositionBulkCreate,
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
                    "created_by": user.id,
                }
                for item, scope in zip(payload.items, scopes, strict=True)
            ],
        )
    ]
    await get_store().record_audit(
        action="disposition.bulk_create",
        actor=user,
        case_id=case_id,
        target_type="finding_disposition",
        target_id=rows[0]["id"] if rows else None,
        detail={"count": len(rows), "kinds": sorted({i.kind for i in payload.items})},
    )
    return {"dispositions": rows}


@router.delete("/{case_id}/timelines/{timeline_id}/dispositions/{disposition_id}")
async def delete_disposition(
    case_id: str,
    timeline_id: str,
    disposition_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Remove a disposition — the finding becomes flaggable/visible again."""
    store = get_store()
    deleted = await store.delete_disposition(case_id, disposition_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Disposition not found")
    await store.record_audit(
        action="disposition.delete",
        actor=user,
        case_id=case_id,
        target_type="finding_disposition",
        target_id=disposition_id,
    )
    return {"deleted": True, "disposition_id": disposition_id}
