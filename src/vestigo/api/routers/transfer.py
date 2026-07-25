"""Case export/import (X1) endpoints. Heavy work runs in JobStore jobs."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from vestigo.api.deps import get_current_user, get_store, require_case_manage
from vestigo.api.uploads import receive_upload_to_tmp
from vestigo.core.config import get_settings
from vestigo.core.jobs import get_job_store
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.postgres import Case, User
from vestigo.transfer.archive import new_archive_path, sweep_stale, temp_root
from vestigo.transfer.exporter import export_case
from vestigo.transfer.importer import import_case

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["transfer"])


class ExportRequest(BaseModel):
    """Options for a case export. Blobs are opt-in: they dominate archive size."""

    include_blobs: bool = False


async def _run_export_job(job_id: str, case_id: str, include_blobs: bool, user: User) -> None:
    job_store = get_job_store()
    job_store.update(job_id, status="running")
    store = get_store()
    work_dir = temp_root() / job_id
    try:
        # No scheduler in this deployment, so the only path that creates
        # archives is also the one that expires them: a completed export that
        # is never downloaded would otherwise sit here until the next restart.
        sweep_stale()
        result = await export_case(
            store,
            ClickHouseStore,
            case_id,
            include_blobs=include_blobs,
            exported_by=user.username,
            dest_dir=work_dir,
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
        logger.exception("case export job %s failed", job_id)
        job_store.update(job_id, status="failed", error=str(exc))
        # A failed attempt to extract a case is at least as interesting to an
        # auditor as a successful one; never let auditing fail the job.
        try:
            await store.record_audit(
                action="case.export",
                actor=user,
                case_id=case_id,
                target_type="case",
                target_id=case_id,
                detail={"job_id": job_id, "include_blobs": include_blobs, "error": str(exc)},
                status_code=500,
            )
        except Exception:
            logger.exception("failed to audit the failed export job %s", job_id)
    finally:
        # The archive itself has been moved out by now; anything left in the
        # working dir is scratch from a failed run.
        shutil.rmtree(work_dir, ignore_errors=True)


@router.post("/cases/{case_id}/export", status_code=202)
async def export_case_endpoint(
    case_id: str,
    background_tasks: BackgroundTasks,
    body: ExportRequest | None = None,
    case: Case = Depends(require_case_manage),
    user: User = Depends(get_current_user),
):
    include_blobs = body.include_blobs if body else False
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
        # The job's working dir is already gone (see _run_export_job); only the
        # archive itself outlives the job, and only until it is downloaded.
        path.unlink(missing_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in case.name)
    date = datetime.now(UTC).date().isoformat()
    return FileResponse(
        path,
        media_type="application/vnd.vestigo+zip",
        filename=f"{safe_name}-{date}.vestigo",
        background=BackgroundTask(_cleanup),
    )


async def _run_import_job(job_id: str, tmp_path: Path, user: User) -> None:
    job_store = get_job_store()
    job_store.update(job_id, status="running")
    store = get_store()
    try:
        result = await import_case(
            store,
            ClickHouseStore,
            tmp_path,
            owner=user,
            progress=lambda p: job_store.update(job_id, progress=p),
        )
        await store.record_audit(
            action="case.import",
            actor=user,
            case_id=result.case_id,
            target_type="case",
            target_id=result.case_id,
            detail={"job_id": job_id, "counts": result.counts, "warnings": result.warnings},
        )
        job_store.update(
            job_id,
            status="completed",
            result={
                "case_id": result.case_id,
                "counts": result.counts,
                "warnings": result.warnings,
            },
        )
    except Exception as exc:  # noqa: BLE001 — job error surface
        logger.exception("case import job %s failed", job_id)
        job_store.update(job_id, status="failed", error=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/cases/import", status_code=202)
async def import_case_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    # Any authenticated user may import; the importer becomes the case owner.
    max_bytes = get_settings().max_upload_bytes or None
    tmp_path, _file_hash, size_bytes = await receive_upload_to_tmp(
        file, max_bytes=max_bytes, suffix=".vestigo"
    )
    job = get_job_store().create(
        kind="case_import",
        progress={"phase": "queued", "bytes": size_bytes},
        created_by=user.id,
    )
    background_tasks.add_task(_run_import_job, job.id, tmp_path, user)
    return {"job_id": job.id}
