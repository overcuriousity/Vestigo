"""The demo case: an explicit restore after the user deleted theirs.

Seeding normally happens once, on a user's first session (``core.demo_case``).
This is the escape hatch that keeps deleting the demo case from being a
one-way door — the frontend offers it from an empty case list.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from vestigo.api.deps import get_current_user
from vestigo.core.config import get_settings
from vestigo.core.demo_case import seed_demo_case
from vestigo.db.postgres import User

router = APIRouter(prefix="/api/demo", tags=["demo"])


@router.post("/seed")
async def restore_demo_case(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Import a fresh copy of the demo case for the calling user.

    Returns:
        The background job's id, pollable through the jobs router like any
        other case import.
    """
    if not get_settings().demo_case_enabled:
        raise HTTPException(status_code=503, detail="Demo case seeding is disabled")
    try:
        job_id = await seed_demo_case(user)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"job_id": job_id}
