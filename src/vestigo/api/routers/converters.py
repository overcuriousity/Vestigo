"""API routes for converters: vendored downloads and case-bound generated scripts.

``router`` serves the self-contained converter scripts vendored from
https://github.com/overcuriousity/2timesketch (see ``scripts/vendor_converters.py``)
as package data, so downloads work fully offline, plus the copy-paste LLM prompts.

``case_router`` is the generated-converter surface (docs/INPUT_FORMATS.md
§"Generated converters"): start a convert-and-ingest job for a plain-text
upload, list/inspect/download the scripts a case has accumulated, and
regenerate one. Starting work is gated on the operator switch and a reachable
model (503 otherwise); reading is not — the rows are records.
"""

from __future__ import annotations

import json
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from vestigo.api.deps import (
    get_current_user,
    get_store,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from vestigo.api.uploads import receive_upload_to_tmp
from vestigo.converters.job import ConvertJobInputs, run_convert_ingest_job
from vestigo.converters.sample import NotTextError, assert_text_file, safe_filename
from vestigo.core.config import get_settings
from vestigo.core.jobs import get_job_store
from vestigo.core.retention import retain_file, retention_path
from vestigo.db.postgres import Case, User

router = APIRouter(prefix="/api/converters", tags=["converters"])

ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "converters"


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    return json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))


@router.get("")
async def list_converters(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """List the available converter scripts with upstream provenance metadata."""
    return _manifest()


@router.get("/prompt")
async def converter_prompts(user: User = Depends(get_current_user)) -> dict[str, str]:
    """The copy-paste LLM prompts, rendered from the data contract on the server.

    Declared before ``/{name}`` so the literal path wins the match.
    """
    from vestigo.converters.prompt import render_human_prompt_csv, render_human_prompt_parquet

    return {"parquet": render_human_prompt_parquet(), "csv": render_human_prompt_csv()}


@router.get("/{name}")
async def download_converter(name: str, user: User = Depends(get_current_user)) -> FileResponse:
    """Download one converter script by manifest name."""
    entry = next((c for c in _manifest()["converters"] if c["name"] == name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Converter not found")
    return FileResponse(
        ASSETS_DIR / entry["filename"],
        media_type="text/plain",
        filename=entry["filename"],
    )


# ── Generated converters (case-bound) ────────────────────────────────────

case_router = APIRouter(prefix="/api/cases/{case_id}/converters", tags=["converters"])


class RegenerateBody(BaseModel):
    """Optional analyst hint for a regeneration."""

    hint: str | None = None


def _require_switch() -> None:
    """The operator switch alone: what re-running a saved script needs.

    Paired with the ``converter_reuse`` capability on ``/api/health``. A
    saved script sends nothing to the model, so a model endpoint that is down
    or was never configured (an airgapped site that imported a case with
    vetted converters) must not stop it from running.
    """
    if not get_settings().converter_generation_enabled:
        raise HTTPException(
            status_code=503, detail="Converter generation is disabled on this instance."
        )


async def _require_generation_enabled() -> None:
    """Refuse to *start* model work when the subsystem is off or the model is down.

    Paired with the ``converter_generation`` capability on ``/api/health``,
    which is what hides the UI entry points — this is the enforcement behind it.
    """
    from vestigo.agent.availability import agent_available

    _require_switch()
    if not await agent_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "No reachable model endpoint; converter generation needs the AI agent "
                "configured and reachable."
            ),
        )


@case_router.post("/convert", status_code=202)
async def convert_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008
    hint: str | None = Form(default=None),
    converter_script_id: str | None = Form(default=None),
    mtime: float | None = Form(default=None),
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Upload a plain-text log; the model writes (or a saved script re-runs) the converter.

    Re-running the *same* saved script over the *same* raw file is refused
    (409 naming the source it already produced) — the Parquet a converter
    writes is not byte-stable across runs, so the source-level duplicate check
    would not catch it and the evidence would land twice. A fresh generation,
    or another script, over the same raw file is a different question and
    stays allowed. Reuse needs only the operator switch; generation also
    needs a reachable model.

    ``mtime`` is the evidence file's own modification time (POSIX seconds —
    the browser's ``File.lastModified``); it is what the model is told and
    what the script sees on its input. Omitted, the model is told the mtime
    is unknown rather than handed the upload time as a fact.
    """
    if converter_script_id:
        _require_switch()
    else:
        await _require_generation_enabled()
    store = get_store()
    if converter_script_id:
        row = await store.get_converter_script(case.id, converter_script_id)
        if row is None or row.status != "working":
            raise HTTPException(status_code=409, detail="Converter script is not reusable")
    max_bytes = get_settings().max_upload_bytes or None
    suffix = Path(file.filename or "upload").suffix or ".log"
    tmp_path, raw_hash, size = await receive_upload_to_tmp(file, max_bytes=max_bytes, suffix=suffix)
    try:
        await run_in_threadpool(assert_text_file, tmp_path)
    except NotTextError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Not a text file: {exc}") from exc
    except (OSError, EOFError) as exc:
        # A ``.gz``-named upload that is not (or is a truncated) gzip stream:
        # ``gzip.BadGzipFile`` is an OSError, a truncated member raises EOFError.
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Cannot read the upload: {exc}") from exc
    if converter_script_id:
        already = await store.get_source_by_converter_input(case.id, converter_script_id, raw_hash)
        if already is not None:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This file was already converted with this script as source "
                    f"{already.name!r} ({already.id})"
                ),
            )
    job_store = get_job_store()
    job = job_store.create(
        kind="convert_ingest",
        progress={"phase": "queued"},
        created_by=user.id,
        case_id=case.id,
    )
    inputs = ConvertJobInputs(
        case_id=case.id,
        user=user,
        raw_tmp_path=tmp_path,
        raw_hash=raw_hash,
        raw_size=size,
        filename=file.filename or tmp_path.name,
        hint=hint or None,
        reuse_script_id=converter_script_id or None,
        raw_mtime=mtime if mtime and mtime > 0 else None,
    )
    background_tasks.add_task(run_convert_ingest_job, job.id, inputs, job_store=job_store)
    return {"job_id": job.id, "converter_script_id": converter_script_id or None}


@case_router.get("")
async def list_case_converters(case: Case = Depends(require_case_read)) -> dict[str, Any]:
    """Every generated script in the case, newest first, without the code bodies."""
    store = get_store()
    counts = await store.count_sources_by_converter(case.id)
    rows = await store.list_converter_scripts(case.id)
    return {
        "scripts": [{**r.to_dict(), "sources_produced": counts.get(r.id, 0)} for r in rows],
        # What the upload dialog's disclosure quotes: the excerpt budget.
        "sample_bytes": get_settings().converter_sample_bytes,
    }


@case_router.get("/{script_id}")
async def get_case_converter(
    script_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """One script with its code, sample excerpt and attempts."""
    row = await get_store().get_converter_script(case.id, script_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Converter script not found")
    return row.to_dict(include_code=True)


@case_router.get("/{script_id}/download")
async def download_case_converter(
    script_id: str, case: Case = Depends(require_case_read)
) -> Response:
    """The script as a ``.py`` file with a provenance header comment."""
    row = await get_store().get_converter_script(case.id, script_id)
    if row is None or not row.source_code:
        raise HTTPException(status_code=404, detail="Converter script not found")
    generated = row.created_at.isoformat() if row.created_at else "?"
    header = (
        f"# Generated by Vestigo for case {case.name!r} ({case.id})\n"
        f"# converter {row.name} v{row.version} — status {row.status}\n"
        f"# model {row.model} at {row.provider_endpoint}\n"
        f"# generated {generated} — prompt sha256 {row.prompt_hash} — sample sha256 {row.sample_hash}\n"
        f"# raw input {row.raw_filename} sha256 {row.raw_file_hash}\n"
    )
    body = row.source_code
    if body.startswith("#!"):
        first, _, rest = body.partition("\n")
        body = first + "\n" + header + rest
    else:
        body = header + body
    return Response(
        content=body,
        media_type="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="{row.name}_v{row.version}.py"'},
    )


@case_router.post("/{script_id}/regenerate", status_code=202)
async def regenerate_case_converter(
    script_id: str,
    background_tasks: BackgroundTasks,
    body: RegenerateBody | None = None,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Write a new version of the script from the retained raw file (plus a hint)."""
    await _require_generation_enabled()
    store = get_store()
    row = await store.get_converter_script(case.id, script_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Converter script not found")
    raw = retention_path(row.raw_file_hash)
    if not raw.exists():
        raise HTTPException(
            status_code=409,
            detail="The raw file this converter was written from is no longer retained",
        )
    # The job unlinks its raw_tmp_path when done, so hand it a private link/copy.
    tmp_dir = Path(tempfile.mkdtemp(prefix="vestigo-regen-"))
    tmp = tmp_dir / safe_filename(row.raw_filename)
    await run_in_threadpool(retain_file, raw, tmp)
    job_store = get_job_store()
    job = job_store.create(
        kind="convert_ingest",
        progress={"phase": "queued"},
        created_by=user.id,
        case_id=case.id,
    )
    hint = (body.hint if body else None) or None
    inputs = ConvertJobInputs(
        case_id=case.id,
        user=user,
        raw_tmp_path=tmp,
        raw_hash=row.raw_file_hash,
        raw_size=raw.stat().st_size,
        filename=tmp.name,
        hint=hint,
        parent_id=row.id,
        name_hint=row.name,
        raw_tmp_dir=tmp_dir,
        raw_mtime=row.raw_mtime.timestamp() if row.raw_mtime else None,
    )
    await store.record_audit(
        action="converter.regenerate",
        actor=user,
        case_id=case.id,
        target_type="converter_script",
        target_id=row.id,
        detail={"hint": hint},
    )
    background_tasks.add_task(run_convert_ingest_job, job.id, inputs, job_store=job_store)
    return {"job_id": job.id}
