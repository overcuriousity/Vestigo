"""API routes for background job status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from tracesignal.api.deps import get_current_user
from tracesignal.core.jobs import get_job_store
from tracesignal.db.postgres import User

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return the status of a background job.

    Scoped to the user who started it (jobs created before this field existed
    have ``created_by=None`` and are only visible to admins) — otherwise one
    analyst could poll another's job by guessing its ID.
    """
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not user.is_admin and (job.created_by is None or job.created_by != user.id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job.to_dict()}
