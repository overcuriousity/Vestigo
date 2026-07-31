"""The demo case: an explicit restore after the user deleted theirs.

Seeding normally happens once, on a user's first session (``core.demo_case``).
This is the escape hatch that keeps deleting the demo case from being a
one-way door — the frontend offers it from an empty case list.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from vestigo.api.deps import get_current_user, get_store
from vestigo.core.config import get_settings
from vestigo.core.demo_case import DemoCaseExists, seed_demo_case
from vestigo.db.postgres import User

router = APIRouter(prefix="/api/demo", tags=["demo"])


@router.post("/seed")
async def restore_demo_case(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Build a fresh copy of the demo case for the calling user.

    One per account: the caller has to delete the copy they have before asking
    for another, since each one writes a quarter of a million events.

    Returns:
        The background job's id, pollable through the jobs router like any
        other background job.
    """
    if not get_settings().demo_case_enabled:
        raise HTTPException(status_code=503, detail="Demo case seeding is disabled")
    # Recorded before the outcome is known: an audit trail that only shows
    # successes cannot show someone hammering this endpoint.
    await get_store().record_audit(action="case.demo_seed_requested", actor=user)
    try:
        job_id = await seed_demo_case(user)
    except DemoCaseExists as exc:
        raise HTTPException(
            status_code=409,
            detail="You already have a demo case. Delete it first to restore a fresh copy.",
        ) from exc
    except RuntimeError as exc:
        # The instance-wide concurrency cap; the build is CPU-heavy and this is
        # a retryable condition, not a broken deployment.
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"job_id": job_id}
