"""API routes for baseline definitions.

A **baseline definition** names one baseline time range (the known-normal
reference period) plus 1..N labeled suspect windows on a timeline; temporal
anomaly detectors resolve a ``baseline_id`` to these windows at scan time
(see ``events.py::_run_stat_detector``). Value-level normality lives in the
unified disposition taxonomy (``dispositions.py``,
``docs/ANOMALY_DETECTION.md``) — the former ``/allowlist`` endpoints were
folded into it.

Definitions are analyst-declared metadata, deliberately editable: forensic
reproducibility is carried by the DetectorRun snapshot (windows +
dispositions hash in ``params``), never by these rows surviving.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from tracesignal.api.deps import (
    get_store,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from tracesignal.db.postgres import Case, User

router = APIRouter(prefix="/api/cases", tags=["baselines"])

MAX_SUSPECT_WINDOWS = 10


class SuspectWindowPayload(BaseModel):
    """One suspect window: a labeled half-open time range ``[start, end)``."""

    label: str = Field(min_length=1, max_length=255)
    start: datetime
    end: datetime


class BaselineDefinitionCreate(BaseModel):
    """Body for creating (or fully replacing via PUT) a baseline definition."""

    name: str = Field(min_length=1, max_length=255)
    baseline_start: datetime
    baseline_end: datetime
    suspect_windows: list[SuspectWindowPayload] = Field(min_length=1)


def _validate_windows(payload: BaselineDefinitionCreate) -> list[str]:
    """Validate window geometry; raises 422 on contradictions, returns warnings.

    A suspect window overlapping the baseline is a hard error — "absent from
    the baseline" is meaningless if the windows share events. Two suspect
    windows overlapping each other is allowed (a burst may legitimately be
    examined in two framings) but flagged as a warning.
    """
    if payload.baseline_start >= payload.baseline_end:
        raise HTTPException(status_code=422, detail="baseline_start must be before baseline_end")
    if len(payload.suspect_windows) > MAX_SUSPECT_WINDOWS:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_SUSPECT_WINDOWS} suspect windows are supported"
        )
    labels = [w.label for w in payload.suspect_windows]
    if len(set(labels)) != len(labels):
        raise HTTPException(status_code=422, detail="Suspect window labels must be unique")
    warnings: list[str] = []
    for w in payload.suspect_windows:
        if w.start >= w.end:
            raise HTTPException(
                status_code=422, detail=f"Suspect window {w.label!r}: start must be before end"
            )
        # Half-open ranges [start, end) overlap iff each starts before the other ends.
        if w.start < payload.baseline_end and payload.baseline_start < w.end:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Suspect window {w.label!r} overlaps the baseline range — "
                    "shrink one of them; baseline and suspect windows must be disjoint"
                ),
            )
    for i, a in enumerate(payload.suspect_windows):
        for b in payload.suspect_windows[i + 1 :]:
            if a.start < b.end and b.start < a.end:
                warnings.append(f"Suspect windows {a.label!r} and {b.label!r} overlap")
    return warnings


def _windows_json(payload: BaselineDefinitionCreate) -> list[dict]:
    """Serialize suspect windows for the JSON column (ISO-8601, stable ids by label)."""
    return [
        {
            "id": f"w{i}",
            "label": w.label,
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
        }
        for i, w in enumerate(payload.suspect_windows)
    ]


async def _require_timeline(case_id: str, timeline_id: str) -> None:
    if await get_store().get_timeline(case_id, timeline_id) is None:
        raise HTTPException(status_code=404, detail="Timeline not found")


@router.get("/{case_id}/timelines/{timeline_id}/baselines")
async def list_baseline_definitions(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List a timeline's baseline definitions (newest first)."""
    await _require_timeline(case_id, timeline_id)
    definitions = await get_store().list_baseline_definitions(case_id, timeline_id)
    return {"baselines": [d.to_dict() for d in definitions]}


@router.post("/{case_id}/timelines/{timeline_id}/baselines")
async def create_baseline_definition(
    case_id: str,
    timeline_id: str,
    payload: BaselineDefinitionCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Create a named baseline + suspect-window definition."""
    await _require_timeline(case_id, timeline_id)
    warnings = _validate_windows(payload)
    store = get_store()
    definition = await store.create_baseline_definition(
        case_id=case_id,
        timeline_id=timeline_id,
        name=payload.name,
        baseline_start=payload.baseline_start,
        baseline_end=payload.baseline_end,
        suspect_windows=_windows_json(payload),
        created_by=user.id,
    )
    await store.record_audit(
        action="baseline.create",
        actor=user,
        case_id=case_id,
        target_type="baseline_definition",
        target_id=definition.id,
        detail={"name": payload.name, "config_hash": definition.to_dict()["config_hash"]},
    )
    return {"baseline": definition.to_dict(), "warnings": warnings}


@router.put("/{case_id}/timelines/{timeline_id}/baselines/{baseline_id}")
async def update_baseline_definition(
    case_id: str,
    timeline_id: str,
    baseline_id: str,
    payload: BaselineDefinitionCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Replace a baseline definition's name and windows.

    Safe for reproducibility: past detector runs snapshot the windows they
    actually used, so editing never rewrites history.
    """
    warnings = _validate_windows(payload)
    store = get_store()
    definition = await store.update_baseline_definition(
        case_id,
        timeline_id,
        baseline_id,
        name=payload.name,
        baseline_start=payload.baseline_start,
        baseline_end=payload.baseline_end,
        suspect_windows=_windows_json(payload),
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="Baseline definition not found")
    await store.record_audit(
        action="baseline.update",
        actor=user,
        case_id=case_id,
        target_type="baseline_definition",
        target_id=baseline_id,
        detail={"name": payload.name, "config_hash": definition.to_dict()["config_hash"]},
    )
    return {"baseline": definition.to_dict(), "warnings": warnings}


@router.delete("/{case_id}/timelines/{timeline_id}/baselines/{baseline_id}")
async def delete_baseline_definition(
    case_id: str,
    timeline_id: str,
    baseline_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a baseline definition. Past runs that referenced it stay replayable."""
    store = get_store()
    deleted = await store.delete_baseline_definition(case_id, timeline_id, baseline_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Baseline definition not found")
    await store.record_audit(
        action="baseline.delete",
        actor=user,
        case_id=case_id,
        target_type="baseline_definition",
        target_id=baseline_id,
    )
    return {"deleted": True, "baseline_id": baseline_id}
