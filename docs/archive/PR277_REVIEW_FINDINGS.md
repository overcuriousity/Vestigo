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

## Third pass — 2026-08-18

A third `/code-review 277` after the second pass; every finding — including the ones the
report cut for its cap and the cleanups — fixed in session 178.

1. **`check_script` bypassed by dynamic lookup and rebinding.** `pydoc.locate('os.system')`,
   `__builtins__['eval']`, `f = eval; f(…)`, `os.__dict__['system']`, `x = os; x.remove(p)`,
   `def g(m): m.system(…); g(os)`, `f = os.system`, `import _ctypes`, `pickle.loads`,
   `marshal`, `sys.path.insert(0, '.')` (a written file shadowing a stdlib module on the
   next import), `().__class__.__base__.__subclasses__()`, `attrgetter('__subclasses__')`
   all returned `[]`. Now: the deny-list adds `pydoc pkgutil zipimport ensurepip venv site
   gc inspect unittest pickle _pickle marshal shelve dbm _ctypes imaplib poplib nntplib
   telnetlib socketserver wsgiref antigravity` and the submodules `logging.config`
   (imports `()` factory strings) and `unittest.mock`; a name bound by `import` may only be
   the receiver of an attribute access (`x = os`, `f(os)`, `[os]` refused — this is what
   closes the rebinding family without tracking data flow); `exec eval compile __import__
   globals locals vars breakpoint help input __builtins__ __loader__ __spec__` are refused
   wherever *named*; the object-graph dunders (`__dict__ __subclasses__ __bases__ __mro__
   __globals__ __code__ __closure__ __self__ __func__ __wrapped__ __reduce__ …`, frame and
   traceback attributes) are refused on every receiver; `sys.path/meta_path/path_hooks/
   _getframe/settrace/…` join `sys.modules`; `getattr/setattr/delattr/hasattr/attrgetter/
   methodcaller` are refused on a module alias or with a dunder-name string; the
   `os`/`Path` destructive attributes are refused as *reads*, not only calls; `CodeType`/
   `FunctionType`/`get_type_hints` join the any-receiver list. Prompt, `INPUT_FORMATS.md`,
   `DEPLOYMENT.md` and `AGENT.md` now call it a best-effort static guard, which it is.
   Twenty-three new evasion cases in `test_converter_runner.py`; the fixture converter and
   the legitimate-alias case still pass.
2. **Same-script-same-file refusal lived only in the HTTP handler.** The CLI's `--converter`
   and a concurrent submit reached the job directly, and the source row that would read as a
   duplicate exists only after the multi-minute conversion. Now the job checks
   `get_source_by_converter_input` before any work (`DuplicateConversion`),
   `register_source_for_ingest` checks it again as a pre-check and after an `IntegrityError`,
   and migration 0032 makes `ix_sources_converter_input` a partial unique index — the
   backstop under all three.
3. **Raw evidence retained before any row referenced it, never released.** A job failing in
   seconds (endpoint down, mistyped script id) left a plaintext copy under
   `data/sources/<hash>` nothing named; in reuse mode the only reference,
   `sources.converter_input_hash`, was invisible to `source_hash_in_use`. Now `_Retention`
   retains lazily — right before `_create_row` and before `register_source_for_ingest` —
   and on failure unlinks the copy *this job* created when nothing references it;
   `source_hash_in_use` counts `converter_input_hash`. The full run reads the job's private
   copy, not the retention path.
4. **Startup reconciliation could fail a live generation.** It ran in `_startup_recovery`
   after `yield`, queued behind ClickHouse-touching sweeps, and failed *every* `generating`
   row. Moved before `yield` next to `_settle_orphaned_column_recommendations` (one fast
   Postgres statement) — no request can be accepted before it has run. Test:
   `test_generating_rows_are_failed_before_the_app_serves` (a row created while serving is
   left alone).
5. **Concurrent reuse jobs clobbered each other's attempts.** `_Attempts` snapshotted the list
   and wrote it wholesale. New `PostgresStore.append_converter_attempt`: `SELECT … FOR
   UPDATE`, append, `n` assigned from the locked row, row updates in the same transaction.
   The attempt-entry dict is built in one place (`converter_attempt_entry`) for the job and
   for reconciliation. Test: `test_concurrent_reuse_jobs_keep_both_trails`.
6. **Two failure paths left no trail.** (a) After `status='working'`, a failure in
   `hash_file`/`stat`/`register_source_for_ingest` recorded nothing — now the catch-all
   appends an attempt for the phase that died (`generate`/`full`/`ingest`) plus the matching
   audit row unless the raising code already did (`_Failed`). (b) Model errors before a row
   exists are buffered on `_Trail` and flushed onto the row when it appears; if every attempt
   died after the excerpt was sent, a `failed` row named from the file
   (`auth_log2vestigo`) carries them and is regenerable; if the endpoint was unreachable
   before the first prompt, only an audit row (`outcome: unavailable`) — nothing left the
   host, no row is worth an analyst's attention, no blob is retained.
7. **`prompt_hash`/`model` frozen at the first draft.** A repair round's prompt necessarily
   differs, so the download header named a prompt that did not produce the code. Now every
   `generate`/`sample` entry carries its prompt's hash, and the `sample` record of a draft
   sets the row's `source_code`, `prompt_hash` and `model` together.
8. **The "file mtime" was the staging copy's.** The prompt says "missing year: take it from
   the file mtime", and the value was the upload time. Now the browser sends
   `File.lastModified`, the CLI its `stat`, a regeneration the stored
   `converter_scripts.raw_mtime` (migration 0032); the runner `os.utime`s the staged copy;
   with none the header says "mtime unknown" and the prompt says what to fall back to and
   to record it in `timezone_assumption`.
9. **Reuse gated on a reachable model.** `_require_switch` (switch only) for
   `converter_script_id` reuse; `_require_generation_enabled` (switch + model) for
   generation and regenerate. New capability `converter_reuse`; the dialog offers *Use a
   saved converter* alone when only that is on, hides the mode when the case has no working
   script, and the reuse-only select has no "generate a new one" entry.
10. **Mode switch mid-transfer unmounted the progress row.** `SegmentedControl` gained
    `disabled`; the dialog freezes it while a transfer is active, and the progress row's
    state/cancel follow whichever transfer is running rather than the current mode. The AI
    path's `onSuccess` now fires `tourEvent("source-uploaded")`.

Cut-by-the-cap findings and cleanups, all applied: `ScriptDetail`'s query key is
`["converters", caseId, "detail", id]` so the tray's terminal invalidation reaches an
expanded row; the panel tracks regenerate jobs it started (button disabled + spinner until
terminal, list polled every 2 s while one is in flight, no pointless invalidation at submit);
`ParserDownloadsPanel` shows the prompt-load error with a Retry; `jobPhases.ts` drops the
`validating` phase the job never emits; `_take_examples` skips the whole-batch filter when
the mask selects nothing; the CLI copies and hashes the log in one pass (`copy_and_hash`).
