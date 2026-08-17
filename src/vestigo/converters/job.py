"""The convert-and-ingest job: sample → generate → sample-run → validate → repair → full run → ingest.

Deterministic control flow around one typed model call per attempt. Every
attempt is recorded on the ``converter_scripts`` row; the produced Parquet is
handed to the same registration/ingest path a manual Parquet upload takes, so
the resulting source is indistinguishable from one the analyst converted
locally — except for ``converter_script_id`` pointing at the script.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from vestigo.converters.generator import GeneratedScript, GenerationUnavailable, generate_script
from vestigo.converters.prompt import render_generation_prompt, render_repair_prompt
from vestigo.converters.runner import RunResult, check_script, run_converter
from vestigo.converters.sample import Sample, build_sample, sample_as_file
from vestigo.converters.validate import Check, ValidationReport, validate_output
from vestigo.core.config import get_settings
from vestigo.core.jobs import JobStore
from vestigo.core.retention import retain_file, retention_path
from vestigo.db.postgres import PostgresStore, User
from vestigo.ingestion.files import hash_file

logger = logging.getLogger(__name__)

#: The sample run is a probe, not a conversion: cap it independently of the
#: full-file timeout so a pathological first draft cannot burn the budget.
SAMPLE_RUN_TIMEOUT_S = 60.0


@dataclass
class ConvertJobInputs:
    """Everything the job needs; the endpoint/CLI/regenerate route build one."""

    case_id: str
    user: User
    raw_tmp_path: Path
    raw_hash: str
    raw_size: int
    filename: str
    hint: str | None = None
    reuse_script_id: str | None = None
    parent_id: str | None = None
    #: Regeneration: keep the parent's name so the version is known before the
    #: first prompt (a fresh generation learns the name from the model).
    name_hint: str | None = None


class _Attempts:
    """Attempt bookkeeping that keeps the row in sync after every entry."""

    def __init__(self, store: PostgresStore, script_id: str, existing: list[dict] | None):
        self.store = store
        self.script_id = script_id
        self.entries: list[dict[str, Any]] = list(existing or [])

    async def record(
        self,
        phase: str,
        *,
        model: str | None,
        result: RunResult | None,
        report: ValidationReport | None,
        script: str | None,
        error: str | None = None,
        **row_updates: Any,
    ) -> None:
        self.entries.append(
            {
                "n": len(self.entries) + 1,
                "phase": phase,
                "model": model,
                "elapsed_ms": result.elapsed_ms if result else 0,
                "exit_code": result.exit_code if result else None,
                "stderr_tail": (result.stderr_tail if result else "")[-4096:],
                "validation": report.to_dict() if report else None,
                "script_hash": hashlib.sha256(script.encode()).hexdigest() if script else None,
                "error": error,
            }
        )
        await self.store.update_converter_script(
            self.script_id, attempts=self.entries, **row_updates
        )


def _phase(job_store: JobStore, job_id: str, phase: str, **more: Any) -> None:
    job_store.update(job_id, status="running", progress={"phase": phase, **more})


def _summ(report: ValidationReport | None) -> str:
    if report is None:
        return "no report"
    failed = [f"{c.name} ({c.detail})" for c in report.checks if c.enforced and not c.ok]
    return "; ".join(failed) or "ok"


def _run_failure_report(result: RunResult) -> ValidationReport:
    detail = "timed out" if result.timed_out else f"exit code {result.exit_code}, no output file"
    return ValidationReport(ok=False, checks=[Check("run", False, detail)])


async def _run_and_validate(
    script: str,
    input_path: Path,
    *,
    raw_sha256: str,
    version: int,
    timeout_s: float,
    on_progress: Any = None,
) -> tuple[RunResult, ValidationReport, Path]:
    """Run the script and validate what it wrote; the report is never ``None``."""
    s = get_settings()
    out_dir = Path(tempfile.mkdtemp(prefix="vestigo-conv-out-"))
    out = out_dir / "events.parquet"
    result = await asyncio.to_thread(
        run_converter,
        script,
        input_path,
        output_path=out,
        timeout_s=timeout_s,
        memory_mb=s.converter_run_memory_mb,
        output_mb=s.converter_run_output_mb,
        on_progress=on_progress,
    )
    if result.exit_code == 0 and out.exists():
        report = await asyncio.to_thread(
            validate_output, out, raw_sha256=raw_sha256, expected_version=version
        )
    else:
        report = _run_failure_report(result)
    return result, report, out


def _prompt_kwargs(inputs: ConvertJobInputs, sample: Sample, version: int) -> dict[str, Any]:
    return {
        "sample": sample,
        "filename": inputs.filename,
        "size_bytes": inputs.raw_size,
        "line_count": sample.line_count,
        "mtime_iso": sample.mtime_iso,
        "version": version,
        "hint": inputs.hint,
    }


async def _generate_loop(
    *,
    store: PostgresStore,
    job_store: JobStore,
    job_id: str,
    inputs: ConvertJobInputs,
    sample: Sample,
) -> tuple[str, str, int, str, _Attempts]:
    """Generate → sample-run → validate → repair until a script passes or attempts run out.

    Returns ``(script_id, script, version, name, attempts)``; raises
    ``RuntimeError`` (with the row already marked ``failed``) when exhausted.
    """
    settings = get_settings()
    max_attempts = settings.converter_max_attempts
    script_id: str | None = None
    attempts: _Attempts | None = None
    name = inputs.name_hint
    version = await store.next_converter_version(inputs.case_id, name) if name else 1
    script = ""
    report: ValidationReport | None = None
    stderr_tail = ""
    gen: GeneratedScript | None = None
    sample_dir = Path(tempfile.mkdtemp(prefix="vestigo-conv-sample-"))
    try:
        sample_file = sample_as_file(sample, sample_dir, inputs.filename)
        sample_sha = hash_file(sample_file)
        for n in range(1, max_attempts + 1):
            _phase(
                job_store,
                job_id,
                "generating",
                attempt=n,
                max_attempts=max_attempts,
                converter_script_id=script_id,
            )
            if n == 1:
                system, task = render_generation_prompt(**_prompt_kwargs(inputs, sample, version))
            else:
                system, task = render_repair_prompt(
                    previous_script=script,
                    report=report.to_dict() if report else {"ok": False, "checks": []},
                    stderr_tail=stderr_tail,
                    **_prompt_kwargs(inputs, sample, version),
                )
            try:
                gen = await generate_script(system, task)
            except GenerationUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 — a model error is a failed attempt
                stderr_tail = f"model call failed: {exc}"
                report = ValidationReport(ok=False, checks=[Check("model", False, stderr_tail)])
                if attempts is not None:
                    await attempts.record(
                        "generate",
                        model=None,
                        result=None,
                        report=report,
                        script=None,
                        error=stderr_tail,
                    )
                continue
            if script_id is None:
                if name is None:
                    name = gen.name
                    version = await store.next_converter_version(inputs.case_id, name)
                    if version != 1:
                        # The task named 1.0.0 and the footer check would fail;
                        # ask once more with the real version (rare: a fresh
                        # generation whose proposed name already exists).
                        system, task = render_generation_prompt(
                            **_prompt_kwargs(inputs, sample, version)
                        )
                        gen = await generate_script(system, task)
                row = await store.create_converter_script(
                    case_id=inputs.case_id,
                    name=name,
                    version=version,
                    raw_file_hash=inputs.raw_hash,
                    raw_filename=inputs.filename,
                    model=gen.model,
                    provider_endpoint=gen.provider_endpoint,
                    prompt_hash=gen.prompt_hash,
                    sample_hash=sample.sha256,
                    sample_excerpt=sample.text,
                    hint=inputs.hint,
                    created_by=inputs.user.id,
                    parent_id=inputs.parent_id,
                )
                script_id = row.id
                attempts = _Attempts(store, script_id, None)
                job_store.update(job_id, progress={"converter_script_id": script_id})
            assert attempts is not None  # noqa: S101 — set together with script_id
            script = gen.script
            violations = check_script(script)
            if violations:
                report = ValidationReport(
                    ok=False, checks=[Check("static_check", False, "; ".join(violations))]
                )
                stderr_tail = ""
                await attempts.record(
                    "sample",
                    model=gen.model,
                    result=None,
                    report=report,
                    script=script,
                    source_code=script,
                )
                continue
            _phase(job_store, job_id, "sample_run", attempt=n, max_attempts=max_attempts)
            result, report, out = await _run_and_validate(
                script,
                sample_file,
                raw_sha256=sample_sha,
                version=version,
                timeout_s=min(SAMPLE_RUN_TIMEOUT_S, settings.converter_run_timeout_seconds),
            )
            shutil.rmtree(out.parent, ignore_errors=True)
            stderr_tail = result.stderr_tail
            await attempts.record(
                "sample",
                model=gen.model,
                result=result,
                report=report,
                script=script,
                source_code=script,
            )
            if report.ok:
                break
        else:
            if script_id is not None:
                await store.update_converter_script(script_id, status="failed")
                await store.record_audit(
                    action="converter.generate",
                    actor=inputs.user,
                    case_id=inputs.case_id,
                    target_type="converter_script",
                    target_id=script_id,
                    detail={
                        "outcome": "failed",
                        "attempts": len(attempts.entries) if attempts else 0,
                    },
                )
            raise RuntimeError(
                f"no working converter after {max_attempts} attempts; last report: {_summ(report)}"
            )
    finally:
        shutil.rmtree(sample_dir, ignore_errors=True)
    assert script_id is not None and attempts is not None and gen is not None and name  # noqa: S101
    await store.record_audit(
        action="converter.generate",
        actor=inputs.user,
        case_id=inputs.case_id,
        target_type="converter_script",
        target_id=script_id,
        detail={
            "outcome": "working",
            "attempts": len(attempts.entries),
            "model": gen.model,
            "prompt_hash": gen.prompt_hash,
            "sample_hash": sample.sha256,
        },
    )
    return script_id, script, version, name, attempts


async def run_convert_ingest_job(
    job_id: str, inputs: ConvertJobInputs, *, job_store: JobStore
) -> None:
    """Drive one upload through generation (or reuse), conversion and ingest."""
    from vestigo.api.deps import get_store
    from vestigo.api.routers.cases import _run_ingestion_job, register_source_for_ingest

    store = get_store()
    settings = get_settings()
    script_id: str | None = None
    parquet_out: Path | None = None
    try:
        # 1. Sample + retain the raw file (always — the row references it).
        _phase(job_store, job_id, "sampling")
        sample = await asyncio.to_thread(
            build_sample, inputs.raw_tmp_path, settings.converter_sample_bytes
        )
        await asyncio.to_thread(retain_file, inputs.raw_tmp_path, retention_path(inputs.raw_hash))

        # 2. Script: reuse or generate.
        if inputs.reuse_script_id:
            row = await store.get_converter_script(inputs.case_id, inputs.reuse_script_id)
            if row is None or row.status != "working" or not row.source_code:
                raise RuntimeError("converter script is not reusable (missing or not working)")
            script_id, script, version = row.id, row.source_code, row.version
            attempts = _Attempts(store, script_id, row.attempts)
            job_store.update(job_id, progress={"converter_script_id": script_id})
        else:
            script_id, script, version, _name, attempts = await _generate_loop(
                store=store, job_store=job_store, job_id=job_id, inputs=inputs, sample=sample
            )

        # 3. Full run.
        _phase(
            job_store,
            job_id,
            "converting",
            processed=0,
            total=sample.line_count,
            converter_script_id=script_id,
        )

        def on_progress(n: int) -> None:
            job_store.update(job_id, progress={"processed": min(n, sample.line_count)})

        result, report, parquet_out = await _run_and_validate(
            script,
            retention_path(inputs.raw_hash),
            raw_sha256=inputs.raw_hash,
            version=version,
            timeout_s=settings.converter_run_timeout_seconds,
            on_progress=on_progress,
        )
        await attempts.record("full", model=None, result=result, report=report, script=script)
        await store.record_audit(
            action="converter.run",
            actor=inputs.user,
            case_id=inputs.case_id,
            target_type="converter_script",
            target_id=script_id,
            detail={
                "phase": "full",
                "ok": report.ok,
                "elapsed_ms": result.elapsed_ms,
                "rows": report.rows,
            },
        )
        if not report.ok:
            if not inputs.reuse_script_id:
                await store.update_converter_script(script_id, status="failed")
            raise RuntimeError(f"converter failed on the full file: {_summ(report)}")
        await store.update_converter_script(script_id, status="working", source_code=script)

        # 4. Hand the Parquet to the normal path.
        _phase(job_store, job_id, "ingesting", converter_script_id=script_id)
        pq_hash = await asyncio.to_thread(hash_file, parquet_out)
        pq_size = parquet_out.stat().st_size
        pq_name = Path(inputs.filename).stem + ".parquet"
        try:
            reg = await register_source_for_ingest(
                store=store,
                case_id=inputs.case_id,
                tmp_path=parquet_out,
                file_hash=pq_hash,
                size_bytes=pq_size,
                filename=pq_name,
                name=inputs.filename,
                parser="vestigo_parquet",
                user=inputs.user,
                converter_script_id=script_id,
            )
        except HTTPException as exc:
            raise RuntimeError(f"produced Parquet was rejected: {exc.detail}") from exc
        if reg.duplicate_of is not None:
            parquet_out = None  # the helper unlinked it
            job_store.update(
                job_id,
                status="completed",
                result={
                    "source_id": reg.duplicate_of.id,
                    "converter_script_id": script_id,
                    "duplicate": True,
                },
            )
            return
        await _run_ingestion_job(
            job_id,
            inputs.case_id,
            reg.source_id,
            parquet_out,
            reg.fmt,
            pq_hash,
            inputs.filename,
            pq_name,
            pq_size,
            inputs.user,
            job_store,
        )
        parquet_dir = parquet_out.parent
        parquet_out = None  # _run_ingestion_job unlinks the file
        shutil.rmtree(parquet_dir, ignore_errors=True)
        job = job_store.get(job_id)
        if job is not None and job.status == "completed":
            job_store.update(
                job_id, result={**(job.result or {}), "converter_script_id": script_id}
            )
    except Exception as exc:  # noqa: BLE001 — every failure lands on the job
        logger.warning("convert_ingest job %s failed: %s", job_id, exc, exc_info=True)
        # The generate loop already published the script id on the job before
        # it could raise; keep whichever is known so the tray can link to it.
        current = job_store.get(job_id)
        known = script_id or (
            (current.progress or {}).get("converter_script_id") if current else None
        )
        job_store.update(
            job_id,
            status="failed",
            error=str(exc),
            progress={"converter_script_id": known},
        )
    finally:
        inputs.raw_tmp_path.unlink(missing_ok=True)
        if parquet_out is not None:
            shutil.rmtree(parquet_out.parent, ignore_errors=True)
