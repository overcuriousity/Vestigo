"""API routes for background job status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from tracevector.core.jobs import get_job_store

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    """Return the status of a background job."""
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job.to_dict()}
