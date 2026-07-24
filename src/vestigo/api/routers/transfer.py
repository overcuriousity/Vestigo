"""Case export/import (X1) endpoints. Heavy work runs in JobStore jobs."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from vestigo.api.deps import get_current_user, get_store, require_case_manage
from vestigo.core.jobs import get_job_store
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.postgres import Case, User
from vestigo.transfer.archive import new_archive_path, temp_root
from vestigo.transfer.exporter import export_case

router = APIRouter(prefix="/api", tags=["transfer"])


async def _run_export_job(job_id: str, case_id: str, include_blobs: bool, user: User) -> None:
    job_store = get_job_store()
    job_store.update(job_id, status="running")
    store = get_store()
    try:
        result = await export_case(
            store,
            ClickHouseStore,
            case_id,
            include_blobs=include_blobs,
            exported_by=user.username,
            dest_dir=temp_root() / job_id,
            progress=lambda p: job_store.update(job_id, progress=p),
        )
        # Move next to a stable per-job path for the download endpoint.
        final = new_archive_path(job_id)
        shutil.move(str(result.path), final)
        await store.record_audit(
            action="case.export",
            actor=user,
            case_id=case_id,
            target_type="case",
            target_id=case_id,
            detail={
                "job_id": job_id,
                "include_blobs": include_blobs,
                "bytes": result.bytes,
                "counts": result.counts,
            },
        )
        job_store.update(
            job_id,
            status="completed",
            result={
                "bytes": result.bytes,
                "counts": result.counts,
                "warnings": result.warnings,
            },
        )
    except Exception as exc:  # noqa: BLE001 — job error surface
        job_store.update(job_id, status="failed", error=str(exc))


@router.post("/cases/{case_id}/export", status_code=202)
async def export_case_endpoint(
    case_id: str,
    background_tasks: BackgroundTasks,
    include_blobs: bool = False,
    case: Case = Depends(require_case_manage),
    user: User = Depends(get_current_user),
):
    job = get_job_store().create(
        kind="case_export",
        progress={"phase": "queued"},
        created_by=user.id,
        case_id=case_id,
    )
    background_tasks.add_task(_run_export_job, job.id, case_id, include_blobs, user)
    return {"job_id": job.id}


@router.get("/cases/{case_id}/export/{job_id}/download")
async def download_export(
    case_id: str,
    job_id: str,
    case: Case = Depends(require_case_manage),
    user: User = Depends(get_current_user),
):
    job = get_job_store().get(job_id)
    if job is None or job.kind != "case_export" or job.case_id != case_id:
        raise HTTPException(status_code=404, detail="Export not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail=f"Export not ready ({job.status})")
    path = new_archive_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archive already downloaded or expired")

    def _cleanup() -> None:
        path.unlink(missing_ok=True)
        shutil.rmtree(temp_root() / job_id, ignore_errors=True)

    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in case.name)
    return FileResponse(
        path,
        media_type="application/vnd.vestigo+zip",
        filename=f"{safe_name}.vestigo",
        background=BackgroundTask(_cleanup),
    )
