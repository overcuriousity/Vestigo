"""The convert-and-ingest job: sample → generate → sample-run → validate → repair → full run → ingest.

Deterministic control flow around one typed model call per attempt. Every
attempt is recorded on the ``converter_scripts`` row; the produced Parquet is
handed to the same registration/ingest path a manual Parquet upload takes, so
the resulting source is indistinguishable from one the analyst converted
locally — except for ``converter_script_id`` pointing at the script.

The trail is the contract (docs/INPUT_FORMATS.md §"What is kept"): whatever
fails, and wherever, there is an attempt entry and an audit row saying so —
including model errors before a row exists (buffered and flushed onto the
row, or onto a ``failed`` row named from the file when no draft ever
arrived), the endpoint going away mid-loop, and a failure after the full run
passed (retention disk full, the Parquet footer refused, the database).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from vestigo.converters.generator import GeneratedScript, GenerationUnavailable, generate_script
from vestigo.converters.prompt import render_generation_prompt, render_repair_prompt
from vestigo.converters.runner import RunResult, check_script, run_converter
from vestigo.converters.sample import (
    Sample,
    build_sample,
    count_lines,
    safe_filename,
    sample_as_file,
)
from vestigo.converters.validate import Check, ValidationReport, validate_output
from vestigo.core.config import get_settings
from vestigo.core.jobs import JobStore
from vestigo.core.retention import retain_file, retention_path
from vestigo.db.postgres import PostgresStore, User, converter_attempt_entry
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
    #: A private scratch directory the caller made for ``raw_tmp_path`` (the
    #: regenerate route and the CLI do); the job removes it when done.
    raw_tmp_dir: Path | None = None
    #: The evidence file's own mtime (POSIX seconds) as the uploader knew it —
    #: browser ``lastModified``, CLI ``stat``, a regeneration's stored value.
    #: ``None`` means the model is told the mtime is unknown; the staging
    #: copy's mtime is never used, it is just the upload time.
    raw_mtime: float | None = None

    def __post_init__(self) -> None:
        # The upload's filename is client-controlled and gets joined onto temp
        # directories (sample file, staged input) — a bare basename, always.
        self.filename = safe_filename(self.filename)


class _Failed(RuntimeError):
    """A failure whose attempt entry and audit row are already written."""


class DuplicateConversion(RuntimeError):
    """The same saved script already turned this raw file into a source of this case."""

    def __init__(self, source_id: str, source_name: str) -> None:
        super().__init__(
            f"This file was already converted with this script as source "
            f"{source_name!r} ({source_id})"
        )
        self.source_id = source_id


@dataclass
class _Trail:
    """Attempt and audit bookkeeping for one job.

    Entries recorded before the script row exists are buffered and flushed
    onto the row the moment it is created (``bind``); afterwards every entry
    is appended under a row lock (``PostgresStore.append_converter_attempt``),
    so two jobs re-running the same saved script cannot overwrite each other's
    trail. ``count`` is what the audit rows report.
    """

    store: PostgresStore
    script_id: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0

    async def bind(self, script_id: str, existing: int) -> None:
        """Attach to a row; buffered entries are appended first, in order."""
        self.script_id = script_id
        self.count = existing
        for entry in self.pending:
            entries = await self.store.append_converter_attempt(
                script_id,
                entry["phase"],
                model=entry["model"],
                elapsed_ms=entry["elapsed_ms"],
                exit_code=entry["exit_code"],
                stderr_tail=entry["stderr_tail"],
                validation=entry["validation"],
                script_hash=entry["script_hash"],
                prompt_hash=entry["prompt_hash"],
                error=entry["error"],
            )
            self.count = len(entries)
        self.pending = []

    async def record(
        self,
        phase: str,
        *,
        model: str | None = None,
        result: RunResult | None = None,
        report: ValidationReport | None = None,
        script: str | None = None,
        prompt_hash: str | None = None,
        error: str | None = None,
        row_updates: dict[str, Any] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "model": model,
            "elapsed_ms": result.elapsed_ms if result else 0,
            "exit_code": result.exit_code if result else None,
            "stderr_tail": result.stderr_tail if result else "",
            "validation": report.to_dict() if report else None,
            "script_hash": hashlib.sha256(script.encode()).hexdigest() if script else None,
            "prompt_hash": prompt_hash,
            "error": error,
        }
        if self.script_id is None:
            self.pending.append(converter_attempt_entry(len(self.pending) + 1, phase, **kwargs))
            return
        entries = await self.store.append_converter_attempt(
            self.script_id, phase, row_updates=row_updates, **kwargs
        )
        self.count = len(entries)


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


def _fallback_name(filename: str) -> str:
    """A converter name derived from the file, for a failed draft the model never named.

    ``^[a-z0-9_]{1,32}2vestigo$`` like a model-proposed name; the row it names
    exists only so the attempts (all model errors) are on record and the
    analyst can regenerate with a hint instead of finding nothing.
    """
    stem = re.sub(r"\.gz$", "", filename.lower())
    stem = re.sub(r"[^a-z0-9]+", "_", stem).strip("_") or "input"
    return f"{stem[:24]}2vestigo"


async def _run_and_validate(
    script: str,
    input_path: Path,
    *,
    raw_sha256: str,
    version: int,
    name: str | None,
    input_name: str,
    input_mtime: float | None,
    timeout_s: float,
    on_progress: Any = None,
) -> tuple[RunResult, ValidationReport, Path]:
    """Run the script and validate what it wrote; the report is never ``None``.

    ``input_name``/``input_mtime`` are what the script sees on ``-i`` — the
    evidence file's real name and mtime in both the sample and the full run,
    so a ``.gz``-by-suffix script, a year taken from the mtime, and the
    recorded ``source_file``/``original_files`` behave the same in both phases.
    """
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
        input_name=input_name,
        input_mtime=input_mtime,
    )
    if result.exit_code == 0 and out.exists():
        report = await asyncio.to_thread(
            validate_output,
            out,
            raw_sha256=raw_sha256,
            expected_version=version,
            expected_name=name,
        )
    else:
        report = _run_failure_report(result)
    return result, report, out


def _prompt_kwargs(
    inputs: ConvertJobInputs, sample: Sample, version: int, name: str | None
) -> dict[str, Any]:
    return {
        "sample": sample,
        "filename": inputs.filename,
        "size_bytes": inputs.raw_size,
        "line_count": sample.line_count,
        "mtime_iso": sample.mtime_iso,
        "version": version,
        "hint": inputs.hint,
        "name": name,
    }


class _Retention:
    """Retain the raw file only when a row is about to reference it, and take it back on failure.

    Retaining first thing meant a job that failed in seconds (model down, not
    a text file, a mistyped script id) left a full plaintext copy of the
    evidence under ``data/sources/<hash>`` that no row named and nothing ever
    swept. So: retain lazily — right before ``_create_row`` and right before
    ``register_source_for_ingest`` — and on failure unlink the copy *this job*
    created if nothing references it (``source_hash_in_use`` counts
    ``raw_file_hash`` and ``converter_input_hash`` as references).
    """

    def __init__(self, store: PostgresStore, inputs: ConvertJobInputs) -> None:
        self.store = store
        self.inputs = inputs
        self.path = retention_path(inputs.raw_hash)
        self.created = False

    async def ensure(self) -> None:
        if not self.path.exists():
            self.created = True
        await asyncio.to_thread(retain_file, self.inputs.raw_tmp_path, self.path)

    async def release_if_unreferenced(self) -> None:
        if not self.created:
            return
        try:
            if not await self.store.source_hash_in_use(self.inputs.raw_hash, exclude_source_id=""):
                self.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — the job's own error is the one to keep
            logger.exception("could not release retained raw file %s", self.inputs.raw_hash)


async def _create_row(
    store: PostgresStore,
    inputs: ConvertJobInputs,
    sample: Sample | None,
    gen: GeneratedScript | None,
    *,
    name: str,
    version: int,
    status: str = "generating",
) -> tuple[Any, int]:
    """Insert the script row, walking the version forward on a lost race.

    ``next_converter_version`` is a plain read; two concurrent generations that
    propose the same name both compute the same number and the second insert
    hits the ``(case, name, version)`` unique index. Retry with the next free
    version rather than dying — the caller re-prompts if the number moved.
    """
    for _ in range(8):
        try:
            row = await store.create_converter_script(
                case_id=inputs.case_id,
                name=name,
                version=version,
                raw_file_hash=inputs.raw_hash,
                raw_filename=inputs.filename,
                raw_mtime=(
                    datetime.fromtimestamp(inputs.raw_mtime, UTC)
                    if inputs.raw_mtime is not None
                    else None
                ),
                model=gen.model if gen else None,
                provider_endpoint=gen.provider_endpoint if gen else None,
                prompt_hash=gen.prompt_hash if gen else None,
                sample_hash=sample.sha256 if sample else None,
                sample_excerpt=sample.text if sample else None,
                hint=inputs.hint,
                created_by=inputs.user.id,
                parent_id=inputs.parent_id,
                status=status,
            )
            return row, version
        except IntegrityError:
            version = await store.next_converter_version(inputs.case_id, name)
    raise RuntimeError(f"could not allocate a version for converter {name!r}")


async def _generate_loop(
    *,
    store: PostgresStore,
    job_store: JobStore,
    job_id: str,
    inputs: ConvertJobInputs,
    sample: Sample,
    trail: _Trail,
    retention: _Retention,
) -> tuple[str, str, int, str]:
    """Generate → sample-run → validate → repair until a script passes or attempts run out.

    Returns ``(script_id, script, version, name)``; raises ``RuntimeError``
    (with the row already marked ``failed`` and the trail written) when
    exhausted. A model error before any draft arrived is buffered on ``trail``
    and lands on the row once there is one — or on a ``failed`` row named from
    the file if there never is, so the attempts and the audit exist either way.
    """
    settings = get_settings()
    max_attempts = settings.converter_max_attempts
    script_id: str | None = None
    name = inputs.name_hint
    version = await store.next_converter_version(inputs.case_id, name) if name else 1
    script = ""
    report: ValidationReport | None = None
    stderr_tail = ""
    gen: GeneratedScript | None = None
    fresh = True  # next prompt is a generation prompt (not a repair of ``script``)
    sent = False  # has any prompt (with the excerpt) left the host?
    n = 0
    sample_dir = Path(tempfile.mkdtemp(prefix="vestigo-conv-sample-"))
    try:
        sample_file = sample_as_file(sample, sample_dir, inputs.filename)
        sample_sha = hash_file(sample_file)
        while n < max_attempts:
            n += 1
            _phase(
                job_store,
                job_id,
                "generating",
                attempt=n,
                max_attempts=max_attempts,
                converter_script_id=script_id,
            )
            if fresh:
                system, task = render_generation_prompt(
                    **_prompt_kwargs(inputs, sample, version, name)
                )
            else:
                system, task = render_repair_prompt(
                    previous_script=script,
                    report=report.to_dict() if report else {"ok": False, "checks": []},
                    stderr_tail=stderr_tail,
                    **_prompt_kwargs(inputs, sample, version, name),
                )
            try:
                gen = await generate_script(
                    system, task, timeout_s=settings.converter_generation_timeout_seconds
                )
            except GenerationUnavailable as exc:
                # Raised by the availability probe, before anything is sent
                # (``sent`` is deliberately left as it was). The endpoint is
                # gone; retrying is pointless. Still an attempt on the trail —
                # the excerpt may already have left the host on an earlier
                # round — and the row (created below if there is none yet)
                # ends ``failed`` with the reason on record.
                stderr_tail = f"model unavailable: {exc}"
                report = ValidationReport(ok=False, checks=[Check("model", False, stderr_tail)])
                await trail.record("generate", report=report, error=stderr_tail)
                break
            except Exception as exc:  # noqa: BLE001 — a model error is a failed attempt
                sent = True
                # Name the type: a timeout stringifies to "" (asyncio's
                # TimeoutError and httpx's carry no message), and an attempt
                # trail reading "model call failed: " tells an analyst nothing
                # about whether the endpoint refused, stalled or went away.
                stderr_tail = f"model call failed: {type(exc).__name__}: {exc}".rstrip(": ")
                report = ValidationReport(ok=False, checks=[Check("model", False, stderr_tail)])
                await trail.record("generate", report=report, error=stderr_tail)
                continue
            sent = True
            if script_id is None:
                declared = version
                if name is None:
                    name = gen.name
                    version = await store.next_converter_version(inputs.case_id, name)
                await retention.ensure()
                row, version = await _create_row(
                    store, inputs, sample, gen, name=name, version=version
                )
                script_id = row.id
                await trail.bind(script_id, 0)
                job_store.update(job_id, progress={"converter_script_id": script_id})
                if version != declared:
                    # The draft declared a version the harness would reject
                    # (a fresh generation whose proposed name already exists,
                    # or a lost race for the number). Record the draft and ask
                    # again with the real name and version; not counted as an
                    # attempt — the model did nothing wrong.
                    await trail.record(
                        "generate",
                        model=gen.model,
                        script=gen.script,
                        prompt_hash=gen.prompt_hash,
                        error=(
                            f"draft declared version {declared}.0.0 but {name} is at "
                            f"v{version}; regenerating with the real name and version"
                        ),
                    )
                    n -= 1
                    fresh = True
                    continue
            fresh = False
            script = gen.script
            # The row's ``prompt_hash``/``model`` follow the attempt whose draft
            # is the stored ``source_code`` — a repair round's prompt differs
            # from the first draft's, and the provenance header must name the
            # prompt that produced the code it sits on.
            produced = {"source_code": script, "prompt_hash": gen.prompt_hash, "model": gen.model}
            violations = check_script(script)
            if violations:
                report = ValidationReport(
                    ok=False, checks=[Check("static_check", False, "; ".join(violations))]
                )
                stderr_tail = ""
                await trail.record(
                    "sample",
                    model=gen.model,
                    report=report,
                    script=script,
                    prompt_hash=gen.prompt_hash,
                    row_updates=produced,
                )
                continue
            _phase(job_store, job_id, "sample_run", attempt=n, max_attempts=max_attempts)
            result, report, out = await _run_and_validate(
                script,
                sample_file,
                raw_sha256=sample_sha,
                version=version,
                name=name,
                input_name=inputs.filename,
                input_mtime=inputs.raw_mtime,
                timeout_s=min(SAMPLE_RUN_TIMEOUT_S, settings.converter_run_timeout_seconds),
            )
            shutil.rmtree(out.parent, ignore_errors=True)
            stderr_tail = result.stderr_tail
            await trail.record(
                "sample",
                model=gen.model,
                result=result,
                report=report,
                script=script,
                prompt_hash=gen.prompt_hash,
                row_updates=produced,
            )
            if report.ok:
                break
        if report is None or not report.ok:
            if script_id is None and not sent:
                # The endpoint was unreachable before the first prompt went
                # out: nothing left the host, no evidence is retained, no row
                # is worth an analyst's attention — the audit row and the job
                # error say what happened.
                await store.record_audit(
                    action="converter.generate",
                    actor=inputs.user,
                    case_id=inputs.case_id,
                    target_type="case",
                    target_id=inputs.case_id,
                    detail={"outcome": "unavailable", "error": stderr_tail},
                )
                raise _Failed(stderr_tail or "model unavailable")
            if script_id is None:
                # Every attempt died in the model call: no draft, no name. The
                # excerpt did leave the host, so the trail must exist somewhere
                # an analyst can find it — a failed row named from the file,
                # regenerable with a hint.
                name = name or _fallback_name(inputs.filename)
                await retention.ensure()
                row, version = await _create_row(
                    store, inputs, sample, None, name=name, version=version, status="failed"
                )
                script_id = row.id
                await trail.bind(script_id, 0)
                job_store.update(job_id, progress={"converter_script_id": script_id})
            else:
                await store.update_converter_script(script_id, status="failed")
            await store.record_audit(
                action="converter.generate",
                actor=inputs.user,
                case_id=inputs.case_id,
                target_type="converter_script",
                target_id=script_id,
                detail={"outcome": "failed", "attempts": trail.count},
            )
            raise _Failed(
                f"no working converter after {n} attempt{'s' if n != 1 else ''}; "
                f"last report: {_summ(report)}"
            )
    finally:
        shutil.rmtree(sample_dir, ignore_errors=True)
    assert script_id is not None and gen is not None and name  # noqa: S101
    await store.record_audit(
        action="converter.generate",
        actor=inputs.user,
        case_id=inputs.case_id,
        target_type="converter_script",
        target_id=script_id,
        detail={
            "outcome": "working",
            "attempts": trail.count,
            "model": gen.model,
            "prompt_hash": gen.prompt_hash,
            "sample_hash": sample.sha256,
        },
    )
    return script_id, script, version, name


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
    trail = _Trail(store)
    retention = _Retention(store, inputs)
    phase = "generate"  # which attempt phase a failure recorded by the catch-all belongs to
    try:
        _phase(job_store, job_id, "sampling")

        # 1. Script: reuse or generate. A re-run only needs the raw file's
        #    line count (the progress total); a generation builds the excerpt
        #    the model will see. Nothing is retained yet: the raw file is
        #    kept only once a row is about to reference it.
        if inputs.reuse_script_id:
            row = await store.get_converter_script(inputs.case_id, inputs.reuse_script_id)
            if row is None or row.status != "working" or not row.source_code:
                raise RuntimeError("converter script is not reusable (missing or not working)")
            script_id, script, version, name = row.id, row.source_code, row.version, row.name
            # Same script over the same raw file: the endpoint refuses this
            # up front, but the CLI and a concurrent submit reach the job
            # directly, and the source row that would catch it as a duplicate
            # exists only after the conversion — refuse before any work.
            already = await store.get_source_by_converter_input(
                inputs.case_id, script_id, inputs.raw_hash
            )
            if already is not None:
                raise DuplicateConversion(already.id, already.name)
            await trail.bind(script_id, len(row.attempts or []))
            job_store.update(job_id, progress={"converter_script_id": script_id})
            phase = "full"
            line_count = await asyncio.to_thread(count_lines, inputs.raw_tmp_path)
        else:
            sample = await asyncio.to_thread(
                build_sample,
                inputs.raw_tmp_path,
                settings.converter_sample_bytes,
                mtime=inputs.raw_mtime,
            )
            line_count = sample.line_count
            script_id, script, version, name = await _generate_loop(
                store=store,
                job_store=job_store,
                job_id=job_id,
                inputs=inputs,
                sample=sample,
                trail=trail,
                retention=retention,
            )

        # 2. Full run, over the job's private copy of the raw file.
        phase = "full"
        _phase(
            job_store,
            job_id,
            "converting",
            processed=0,
            total=line_count,
            converter_script_id=script_id,
        )

        def on_progress(n: int) -> None:
            job_store.update(job_id, progress={"processed": min(n, line_count)})

        result, report, parquet_out = await _run_and_validate(
            script,
            inputs.raw_tmp_path,
            raw_sha256=inputs.raw_hash,
            version=version,
            name=name,
            input_name=inputs.filename,
            input_mtime=inputs.raw_mtime,
            timeout_s=settings.converter_run_timeout_seconds,
            on_progress=on_progress,
        )
        await trail.record("full", result=result, report=report, script=script)
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
            raise _Failed(f"converter failed on the full file: {_summ(report)}")
        await store.update_converter_script(script_id, status="working", source_code=script)

        # 3. Hand the Parquet to the normal path. From here on the converter
        #    passed; whatever fails is an ``ingest`` attempt on its trail.
        phase = "ingest"
        _phase(job_store, job_id, "ingesting", converter_script_id=script_id)
        pq_hash = await asyncio.to_thread(hash_file, parquet_out)
        pq_size = parquet_out.stat().st_size
        pq_name = Path(inputs.filename).stem + ".parquet"
        await retention.ensure()
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
                converter_input_hash=inputs.raw_hash,
            )
        except HTTPException as exc:
            raise RuntimeError(f"produced Parquet was rejected: {exc.detail}") from exc
        if reg.duplicate_of is not None:
            shutil.rmtree(parquet_out.parent, ignore_errors=True)  # the helper unlinked the file
            parquet_out = None
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
        else:
            # _run_ingestion_job swallowed the failure (marked the job, deleted
            # the source). The converter itself passed — its status stays — but
            # the trail must say its product never landed, or the last attempt
            # would assert a successful conversion whose source does not exist.
            ingest_error = (job.error if job else None) or "ingest failed"
            raise RuntimeError(f"ingest of the produced Parquet failed: {ingest_error}")
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
        # Whatever raised — the model endpoint going away mid-loop, the runner,
        # the validator, the retention volume, the database — the trail says
        # so: an attempt entry for the phase that died and an audit row, unless
        # the raising code already wrote both (``_Failed``). And a row this job
        # created must not stay ``generating``: nothing else will ever finish
        # it, and the panel would show a spinner forever on a script that is
        # neither reusable nor regenerable as a failed draft. A reused script
        # is not this job's row — its status stays; the attempt is still its.
        if known and not isinstance(exc, _Failed):
            await _record_failure(store, trail, inputs, known, phase, str(exc))
        await retention.release_if_unreferenced()
    finally:
        inputs.raw_tmp_path.unlink(missing_ok=True)
        if inputs.raw_tmp_dir is not None:
            shutil.rmtree(inputs.raw_tmp_dir, ignore_errors=True)
        if parquet_out is not None:
            shutil.rmtree(parquet_out.parent, ignore_errors=True)


async def _record_failure(
    store: PostgresStore,
    trail: _Trail,
    inputs: ConvertJobInputs,
    script_id: str,
    phase: str,
    error: str,
) -> None:
    """Attempt entry + audit row + status for a failure the raising code did not record."""
    try:
        if trail.script_id is None:
            row = await store.get_converter_script(inputs.case_id, script_id)
            await trail.bind(script_id, len(row.attempts or []) if row else 0)
        row_updates: dict[str, Any] = {}
        if not inputs.reuse_script_id:
            row = await store.get_converter_script(inputs.case_id, script_id)
            if row is not None and row.status == "generating":
                row_updates["status"] = "failed"
        await trail.record(phase, error=error, row_updates=row_updates)
        await store.record_audit(
            action="converter.generate" if phase == "generate" else "converter.run",
            actor=inputs.user,
            case_id=inputs.case_id,
            target_type="converter_script",
            target_id=script_id,
            detail=(
                {"outcome": "failed", "attempts": trail.count, "error": error}
                if phase == "generate"
                else {"phase": phase, "ok": False, "error": error}
            ),
        )
    except Exception:  # noqa: BLE001 — the job's own error is the one to keep
        logger.exception("could not record the failure of converter script %s", script_id)
