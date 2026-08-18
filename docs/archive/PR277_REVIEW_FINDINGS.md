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

## Second pass — 2026-08-18

A second `/code-review 277` after the fixes above; every finding fixed in session 177.

1. **`runner.py` — the staged input was a hardlink to the retention copy.** `os.link` shared
   the evidence's inode, so the `chmod(0o400)` landed on the retained file and a script that
   reopened `-i` for append rewrote the evidence under its own hash (verified with a `pass`
   script leaving the blob at 0o400, and a `Path.chmod` + `open(..., 'a')` script altering
   it). Now a private `shutil.copyfile` — a large log is copied once per run, which is the
   price. Test: `test_staged_input_is_a_private_copy`.
2. **`check_script` bypassed by one-token rewrites.** `import os as o; o.system(...)`,
   `from os import *`, `builtins.__import__`, `sys.modules['os']`, `runpy`, `Path.unlink` /
   `Path.chmod` all passed. Now: an import **allow-list** (`sys.stdlib_module_names` minus
   the deny-list, plus `pyarrow`/`numpy` — the prompt's own contract), alias resolution,
   star imports refused, `sys.modules` and `getattr()`-on-a-module refused, and
   `unlink/rmdir/chmod/chown/kill/fork/exec*/spawn*/system/popen` refused as a method on any
   receiver (`remove/rename/replace` stay `os`-only — strings and lists have them). Prompt,
   `INPUT_FORMATS.md` and `DEPLOYMENT.md` restate the guard as it is.
3. **Retention in-use check ignored `converter_scripts.raw_file_hash`.** Every unlink guard
   (`register_source_for_ingest` rollback, ingest rollback, startup reconciliation) asked
   `source_hash_in_use`, which knew only sources, so a converter's raw input could be
   unlinked by an unrelated failed upload of the same bytes and regenerate would 409 "no
   longer retained". The check now unions the converter rows.
4. **A `generating` row could stay so forever.** `GenerationUnavailable` mid-loop, any
   unexpected exception, or a restart left the row's status untouched. The job's catch-all
   now flips a row it created to `failed`; `_reconcile_stale_converter_generations` on
   startup does the same for rows a restart orphaned, appending an attempt that says why.
5. **`.gz`-named upload that is not gzip → 500 and a leaked temp file.** `gzip.BadGzipFile`
   (an `OSError`) / `EOFError` are caught next to `NotTextError`: unlink, 400.
6. **Unbounded stderr partial-line buffer.** `RLIMIT_FSIZE` does not cover pipes; a script
   writing without newlines grew `buf` in the API process for the whole timeout. Capped at
   `_STDERR_TAIL`. Test: `test_stderr_without_newlines_is_bounded`.
7. **`build_sample` kept one int per line of the whole file** — ~4 GB for a file at the
   upload cap, and the reuse path paid it for a line count. Rewritten as a chunked scan that
   keeps the length, the line count and two `(index, offset)` pairs; `count_lines` for the
   reuse path. `test_streaming_scan_matches_reference` pins the block output against the old
   algorithm over plain/gz/CRLF/no-trailing-newline/huge-line/multibyte inputs.
8. **`delete_case` did not cascade `converter_scripts`** — rows with `sample_excerpt` (a
   verbatim slice of the evidence) outlived the case. Added to the cascade list; test.
9. **Egress disclosure said "the first N bytes"** while the sample is head + middle + tail.
   Copy now says an excerpt "from its beginning, its middle and its end".
10. **Same script + same raw file was silently converted twice**, and a duplicate outcome was
    invisible in the UI. New `sources.converter_input_hash` (migration 0031); the convert
    endpoint 409s a repeat of the same saved script over the same raw file naming the
    existing source (a fresh generation or another script stays allowed); the tray and the
    case jobs panel render `result.duplicate` (`jobOutcomeNote`).
