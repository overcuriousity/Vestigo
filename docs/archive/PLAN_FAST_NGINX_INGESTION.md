# Plan: Fast end-to-end ingestion for massive line-oriented logs (nginx access logs)

Status: **planned, not implemented.** Written 2026-07-08 in response to an evaluation of the
50GiB-nginx-access-log ingestion path. Closes/advances `docs/ROADMAP.md` **M20** and **W8** once
implemented — this file holds the full design; ROADMAP keeps only the condensed pointer.

## Context

Current workflow for a 50GiB nginx access log: run the vendored, single-threaded
`assets/converters/nginx2timesketch.py` to convert it to Timesketch CSV/JSONL (inflates to
150GiB+ because CSV can't exploit the repetitive structure of `user_agent`/`uri`/`referer`
columns), upload that inflated file (web UI or `tsig ingest`), then `IngestionPipeline` re-parses
it single-threaded (`csv.DictReader`) and inserts into ClickHouse row-by-row. Every step in that
chain is an avoidable bottleneck once the format is no longer pinned to CSV/JSONL:

- CSV/JSONL inflate the file 3x before it's even uploaded.
- Upload receive does the copy+hash pass, then **copies the whole file again**
  (`shutil.copy2`) for retention — a second full I/O pass over 150GiB+.
- Ingestion re-parses that inflated file with a single-threaded `csv.DictReader`.
- `ClickHouseStore.insert_events` builds Python row-lists and uses `clickhouse_connect`'s
  row-oriented insert instead of a bulk/columnar path.
- `file_hash`/`byte_offset` (used in `Event._derive_id()` for forensic identity) end up
  pointing at the **converted CSV**, not the original evidence file — a correctness gap
  independent of performance.

None of this is required by the data model: `docs/CONCEPT.md`/`MODEL_REFINEMENT.md` define
`Source` as "one ingested file — the unit of forensic provenance," format-agnostic. The CSV
pre-conversion step is a workflow convention of the current `Parser` implementations, not a
model requirement.

Goal: ingest the raw nginx log directly (no pre-conversion), parse it in parallel across CPU
cores, and bulk-insert into ClickHouse via Arrow — so `Source.file_hash` refers to the real
evidence file and the whole pipeline scales with cores instead of running single-threaded over
an artificially inflated intermediate.

Scope note: `line_number` is dropped entirely for the new parser (not populated at all) — only
line content and `byte_offset` are needed, which removes the only reason a global ordering pass
over the file would ever be required.

## Design

### 1. Bulk/columnar ClickHouse insert (benefits every ingestion path immediately)

- New `src/tracesignal/db/_arrow_schema.py`: `EVENT_ARROW_SCHEMA`, one `pyarrow` field per
  `_EVENT_COLUMNS` entry (`db/clickhouse.py`), dtypes mirroring `_EVENTS_TABLE_DDL` exactly
  (`byte_offset`/`line_number` → `pa.uint64()`, `content_hash`/`file_hash` → `pa.string()`
  against server `FixedString(64)`, `timestamp`/`ingest_time` → `pa.timestamp("ms", tz="UTC")`,
  `tags` → `pa.list_(pa.string())`, `attributes` → `pa.map_(pa.string(), pa.string())`). Kept as
  its own module (not inlined in `clickhouse.py`) so the nginx parser's worker processes (§2) can
  import just the schema without pulling in `clickhouse_connect` client construction.
- `db/clickhouse.py`: add module-level `_events_to_record_batch(events: list[Event]) ->
  pyarrow.RecordBatch`, built from `Event.to_clickhouse_row()` (the single existing place that
  encodes the null-timestamp sentinel and empty-attribute-stripping rules — do not duplicate that
  logic). Rewrite `insert_events()` internals to build a batch and call
  `self.client.insert_arrow(...)` instead of the current row-list `client.insert(...)` —
  **signature unchanged**, every caller keeps working. Add `insert_events_arrow(self, batch) ->
  int` as a thin pass-through for callers (the nginx parser, §2) that already have a batch built
  against `EVENT_ARROW_SCHEMA` and shouldn't round-trip through `Event` objects at all.
- Add `pyarrow` to `pyproject.toml` core `dependencies` (small, pure-C-extension — not the
  `embeddings` optional extra).

### 2. Native parallel nginx parser

New `src/tracesignal/ingestion/nginx.py` (sibling to `parser.py`, not an extension of it —
enough new surface area to warrant its own module):

- Port `_ACCESS_LOG_RE`/`_ERROR_LOG_RE` and the access/error line-parsing logic from
  `assets/converters/nginx2timesketch.py` (lines ~898-1066) as an independent copy — that file
  is a generated, stdlib-only download artifact (`scripts/vendor_converters.py`), not an
  importable module. Also port `_detect_log_type`/`_sniff_log_type` (file-level access-vs-error
  detection) and gzip-transparent opening (`_open_log`).
- `_find_chunk_boundaries(path, target_chunks, min_chunk_bytes)`: cheap newline-aligned byte-range
  splitting — seek to `i * (size // target_chunks)` for each candidate boundary, read forward in
  small growing windows until a `\n`, without scanning the whole file. Only applies to **plain**
  files; `.gz` isn't seekable this way.
- Parallel execution: `multiprocessing` with `get_context("spawn")` (not `fork` — avoid forking a
  running FastAPI/uvicorn process), a work-queue of chunk descriptors (more chunks than workers,
  e.g. `workers * 4`, for natural load-balancing) consumed by `TS_INGEST_PARSER_WORKERS` (default
  `os.process_cpu_count() or 4`) long-lived worker processes. Each worker parses its chunk with the
  same line-parsing logic as the sequential path, builds `RecordBatch`es via
  `_events_to_record_batch` directly (skip building `Event` objects across the process boundary —
  serialize as Arrow IPC bytes onto a capped results queue for backpressure/bounded memory).
  Main process drains the results queue, deserializes each batch, and yields it to the pipeline.
  Out-of-order emission across chunks is fine by construction: `Event._derive_id()` never depends
  on global ordering, and ClickHouse's `MergeTree ORDER BY` re-sorts at merge time regardless of
  insertion order.
- `.gz` input: sequential fallback only (no parallel chunking) — falls back to the single-stream
  `parse()` generator, still grouped into Arrow batches so the bulk-insert benefit (§1) still
  applies. `byte_offset` for `.gz` sources refers to the **decompressed** content stream (the only
  actionable convention — cite in a code comment and in `docs/CONCEPT.md`); `file_hash`/retention
  still hash/retain the exact uploaded `.gz` bytes. Document this scope limit in `docs/ROADMAP.md`
  as a deliberate follow-up (parallel gzip via seek-point indexing is future work), not an
  oversight.
- `line_number` is not populated by this parser (left at the default/unset — only line content +
  `byte_offset` matter; no prefix-counting pass is needed at all, simplifying the parallel design
  further than a naive design would require).
- `class NginxLogParser(Parser)`: `parse()` (sequential generator, single source of truth for
  line-parsing correctness, used for `.gz` and small plain files) plus `parse_arrow_batches()`
  (the bulk/parallel entry point), dispatching between sequential and parallel modes based on
  file size/type.

### 3. `Parser`/`IngestionPipeline` integration (small, additive)

- `Parser` ABC (`ingestion/parser.py`) gains an **optional** duck-typed method
  `parse_arrow_batches(self, path, on_progress=None) -> Iterator[RecordBatch] | None`, default
  returns `None`. Existing `TimesketchCsvParser`/`JsonlParser` need zero changes.
- `IngestionPipeline._ingest_file()` (`pipeline.py:164`) branches: if
  `parser.parse_arrow_batches(...)` returns non-`None`, drain it via a new `_ingest_file_arrow()`
  helper calling `insert_events_arrow` per batch; otherwise fall through to the existing
  `Event`-based loop unchanged.
- Progress reporting: since chunks emit out of order, track `bytes_consumed` per chunk id
  (monotonic within a chunk) and sum across chunks for the existing
  `progress_callback(total=, processed=)` contract — no changes needed above `_ingest_file`.
- `get_parser()` adds `"nginx"` → `NginxLogParser`. `detect_format()` adds filename-pattern
  recognition (`access.log`, `error.log`, `redirect-access.log`, with optional `.gz`/rotation
  suffixes, redirect checked before access since it's a substring) ahead of the extension-based
  CSV/JSONL rules; unmatched files fall through to today's behavior unchanged.

### 4. Upload-receive double-copy fix (`api/routers/cases.py`)

`_run_ingestion_job` ingests from `tmp_path` (not `retention_path`) and only unlinks it when done
— so the fix can't simply move `tmp_path` away at receive time. Replace the `shutil.copy2` call
(lines ~662-664) with a new `_retain_file(tmp_path, retention_path)` helper: hardlink
(`os.link`) when possible (metadata-only, no data copy), falling back to `shutil.copy2` on
`OSError(errno.EXDEV)` (cross-filesystem, e.g. OS temp dir vs. `TS_SOURCE_RETENTION_PATH` on a
different mount), and short-circuiting entirely if `retention_path` already exists (content-
addressed by hash — an existing file there is guaranteed byte-identical). Document in deployment
docs that the fast path requires `TMPDIR` and `TS_SOURCE_RETENTION_PATH` to share a filesystem.

## Files to touch (when implemented)

- `src/tracesignal/db/_arrow_schema.py` — new
- `src/tracesignal/db/clickhouse.py` — `_events_to_record_batch`, `insert_events` rewrite,
  `insert_events_arrow`
- `src/tracesignal/ingestion/nginx.py` — new
- `src/tracesignal/ingestion/parser.py` — optional `parse_arrow_batches` hook, `get_parser`/
  `detect_format` wiring
- `src/tracesignal/ingestion/pipeline.py` — `_ingest_file` branch, `_ingest_file_arrow`
- `src/tracesignal/api/routers/cases.py` — `_retain_file` helper replacing `shutil.copy2`
- `src/tracesignal/core/config.py` — `TS_INGEST_PARSER_WORKERS`, `TS_INGEST_PARALLEL_MIN_BYTES`
- `pyproject.toml` — add `pyarrow`
- `docs/ROADMAP.md` — close M20/W8, add gzip-parallelism follow-up item
- `docs/PROGRESS.md` — append session entry (per its append-only convention)
- `docs/CONCEPT.md` / `docs/MODEL_REFINEMENT.md` — note the `.gz` decompressed-byte_offset
  convention and native-vs-CSV recommended upload path per format

## Reused utilities (don't reinvent)

- `Event.to_clickhouse_row()` (`models/event.py`) — the only place row-encoding rules live;
  Arrow batch-building must go through it, not a parallel re-implementation.
- `Parser._make_event()` / byte-offset tracking pattern from `JsonlParser.parse()`
  (`ingestion/parser.py`) for the sequential `NginxLogParser.parse()`.
- Existing `_retention_path()` content-addressing convention in `cases.py`.

## Staged rollout (for whoever implements this)

1. Bulk Arrow insert + double-copy fix (§1, §4) — small, low-risk, benefits CSV/JSONL/CLI too.
   Close ROADMAP M20 here.
2. `Parser`/`IngestionPipeline` optional-hook plumbing (§3) with a trivial test-only fake parser.
3. Sequential `NginxLogParser.parse()` + format detection (§2 minus parallelism) — already fixes
   the CSV-inflation and forensic byte_offset/file_hash problems end-to-end.
4. Parallel chunked path (`parse_arrow_batches`, multiprocessing) on top of proven sequential
   correctness. Close ROADMAP W8 here.

## Verification (once implemented)

- `uv run pytest` full suite after each stage.
- New/updated tests: `tests/test_nginx_parser.py` (line-parsing, chunk-boundary math, format
  detection, `parse()` vs `parse_arrow_batches()` equivalence via row-set comparison since
  parallel order isn't guaranteed), `tests/test_pipeline.py` (fake parser exercising the new
  Arrow branch), `tests/test_clickhouse_store.py` (Arrow batch schema/row-count assertions against
  the existing recording-client fake), a new live-ClickHouse-guarded test alongside
  `tests/test_field_mappings_clickhouse.py`'s skip-if-unreachable pattern for real
  `insert_arrow` round-tripping, `tests/test_uploads.py` (hardlink happy path, `EXDEV` fallback,
  already-exists short-circuit).
- Manual: ingest a real (or synthetic multi-GB) nginx access log end-to-end via both `tsig
  ingest` and the web upload, compare wall-clock and peak memory against the current
  CSV-pre-conversion path; verify `Source.file_hash` matches the raw log's own SHA-256 and that
  event `byte_offset`s resolve to the correct line when spot-checked against the raw file.

## Open questions deferred to implementation time

- `TS_INGEST_PARSER_WORKERS`/`TS_INGEST_PARALLEL_MIN_BYTES` defaults need real benchmarking, not
  a guess.
- Worker sub-batch/flush size: reuse `TS_INGEST_BATCH_SIZE` or a separate IPC-tuned knob.
- `parser` form-value naming: single `"nginx"` auto-sniffing access/error vs. separate values —
  user/API-facing, worth confirming before it becomes a compatibility surface.
- Parallel `.gz` support (indexed-gzip / seek-point indexing) explicitly deferred past this plan.
