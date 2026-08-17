# PR #277 review findings — generated converters (`feat/generated-converters`)

Point-in-time record of the `/code-review 277` pass on 2026-08-17. Every item below was
fixed in the same session (see `docs/PROGRESS.md` session 176); this file keeps the full
finding set so the reasoning behind each change is recoverable.

## Correctness

1. **`converters/sample.py` — path traversal via the multipart filename.** `sample_as_file`
   wrote `dest_dir/filename` and `regenerate` built `mkdtemp()/row.raw_filename`, both
   unsanitised; a `filename="../../../../home/app/.bashrc"` overwrote that file before any
   model call, and the persisted `raw_filename` let regenerate `unlink()` (or hardlink over)
   an arbitrary path. Fix: `safe_filename` (basename only; `..`/`.`/empty → `input.log`)
   applied in `ConvertJobInputs.__post_init__`, `sample_as_file` and the regenerate route.
2. **`converters/job.py` — sample vs. full run saw different filenames/encodings.** The
   sample was decompressed text under the upload name; the full run staged
   `input/<64-hex-sha>` with raw bytes. A `.gz` upload could only pass if the model sniffed
   magic bytes, and `source_file`/`original_files[0].name` recorded the hash path. Fix:
   `run_converter(input_name=…)` stages under the evidence name; `.gz` samples are
   re-gzipped.
3. **`build_sample` IndexError on single-line files**; tail block unbounded. Fix: every index
   clamped to `line_count`, blocks that would repeat the head dropped, tail capped at its
   budget.
4. **`api/routers/cases.py::register_source_for_ingest` — dropped rollback.** A failure in
   `get_default_timeline`/`add_source_to_timeline` after `create_source` left an orphaned
   `status='ingesting'` row every re-upload saw as a duplicate. Fix: try/except around the
   timeline add deletes the row and an unshared retention copy, then re-raises.
5. **`validate_output` loaded the whole Parquet** into the API process (`pf.read()` plus
   per-row Python lists). Fix: `iter_batches` with vectorised `map_lookup`, per-batch null
   counts, bounded examples, and an `_OffsetOrder` tracker (int64 cast before differencing).
6. **Re-generation call outside the attempt try/except; version race.** Fix: the redraft is a
   loop iteration (recorded as a `generate` attempt, not counted); `_create_row` retries on
   `IntegrityError` with the next free version.
7. **Ingest failure after `status='working'` left a false trail.** Fix: an `ingest` attempt
   and a `converter.run` audit row are recorded and the job fails with the reason.
8. **UI deep link inert / stale list on failure.** Fix: `useEffect` on the search param;
   `JobTray` invalidates on `failed` as well as `completed`.
9. **Runner: child closes stderr → EOF path skipped `killpg`**; `preexec_fn` from a threaded
   process. Fix: on EOF wait out the remaining deadline and kill on `TimeoutExpired`;
   rlimits applied by a `-c` bootstrap in the child that execs the script;
   `start_new_session=True`.
10. **Converter name never told to the model, never enforced.** Fix: `DECLARE
    vestigo.converter_name` in the task header once known; `converter_name` check in
    `validate_output` (`sanitize_name` on the footer value).

## Cleanups (verified, cut by the cap, all applied)

- Temp-dir leaks on the duplicate-Parquet, regenerate and CLI paths →
  `ConvertJobInputs.raw_tmp_dir` removed by the job; duplicate branch rmtrees the out dir.
- CLI `converters`/`convert-ingest` registered after the `__main__` guard → guard moved last.
- `frontend/src/api/converters.ts` five interfaces declared twice → deduplicated.
- `prompt.DENIED_MODULES` hand-copied and drifted → derived from `runner.DENIED_MODULES`;
  `posix`/`_socket`/`_ssl`/`_posixsubprocess`/`_multiprocessing` added; system prompt v2.
- `generator.py` duplicated the advisor's typed call → `agent/oneshot.py::typed_completion`.
- `list_converter_scripts` selected the large Text columns → `defer()`.
- `fmtWhen` local time → `fmtTimestamp` (UTC).
- `_OPTIONAL_STEMS` special case → **declined.** A general "absent stem reads as empty" rule
  lets a truncated archive restore silently; `tests/test_transfer_api.py::
  test_incomplete_archive_fails_the_job` guards exactly that. The set stays explicit, with
  the reason documented on it.
