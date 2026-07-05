"""API routes for background job status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from tracesignal.api.deps import AccessLevel, get_current_user, get_store, has_case_access
from tracesignal.core.jobs import get_job_store
from tracesignal.db.postgres import User

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return the status of a background job.

    Visibility follows case RBAC (M17): the creator and admins always see the
    job; any user with READ access to the job's case sees it too — case
    members can poll each other's ingest/embed/enrich jobs, and
    system-triggered jobs (``created_by=None``) are visible to the case's
    members instead of admins only. Everyone else gets 404 (not 403), so job
    IDs can't be probed for existence.
    """
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if user.is_admin or (job.created_by is not None and job.created_by == user.id):
        return {"job": job.to_dict()}
    if job.case_id is not None:
        case = await get_store().get_case(job.case_id)
        if await has_case_access(user, case, AccessLevel.READ):
            return {"job": job.to_dict()}
    raise HTTPException(status_code=404, detail="Job not found")
