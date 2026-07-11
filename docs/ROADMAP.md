# TraceSignal Roadmap — Phase 2 (hardening backlog)

Phase 1 (source management, timelines, explorer, anomaly engine, auth/RBAC/audit,
visualization, converters) is complete — see
[`docs/archive/ROADMAP_PHASE1.md`](./archive/ROADMAP_PHASE1.md).

This phase consolidates the remaining findings from the 2026-07-03 repository audit.
The audit's Critical/High items were fixed directly on `fix/audit-critical-high`:

- ✅ **C1** — Dockerfile CMD pointed at a nonexistent `api.main:app`; now `--factory create_app`.
- ✅ **H1** — CSV parser read the whole file into memory (`lines = list(fh)`); now streams with
  incremental byte-offset/line tracking (`ingestion/parser.py::_RecordTrackingIterator`).
- ✅ **H2** — Airgap enforcement: `tsig-web` no longer runs `npm install` on every start
  (builds only when `dist/` is missing; `TS_FRONTEND_REBUILD=1` forces); uvicorn reloader is
  development-only; embedding model load forces `HF_HUB_OFFLINE` unless `TS_ALLOW_ONLINE` and
  fails with an actionable message instead of silently downloading.
- ✅ **H3** — Blocking ClickHouse calls in async handlers (`list_events`, histogram, bulk
  annotate, field/artifact/tag listings, embedding-field recommenders) now go through
  `run_in_threadpool`, matching viz/anomaly endpoints. Convention: **every**
  `EventQueryService` call from an `async def` handler must be threadpool-wrapped.
- ✅ **H4** — Uploads: single-pass copy+hash off the event loop
  (`ingestion/files.py::copy_and_hash`), capped by `TS_MAX_UPLOAD_BYTES`
  (default 10 GiB, 0 disables) with a 413 mid-stream rejection.

Point-in-time PR review findings are archived under `docs/archive/PR{N}_REVIEW_FINDINGS.md`
(full unrestricted finding set, one file per reviewed PR) once triaged into this backlog or
resolved — this file holds only the condensed, still-open action items.

## Milestone 2 — high-leverage improvements

- [ ] **M15 residue — `list_fields_by_artifact` stays live (deliberate).** The per-source
  field-stats cache (`db/field_stats.py`, shipped) converted `field_inventory`,
  `list_fields`, and `field_coverage`; the embedding wizard's `list_fields_by_artifact`
  keeps its live scan because its cost is the randomized per-artifact value sampling that
  feeds content-aware cohesion scoring — caching only its inventory would save little.
  Revisit only if the wizard's latency becomes a complaint. HyperLogLog sketches for exact
  merged `distinct` likewise deferred (max-across-sources approximation documented in the
  module).

- [ ] **M25 — Port remaining converters to the Parquet interchange format.** M20 shipped the
  bulk Arrow insert, the upload hardlink-retention fix, the TraceSignal Parquet interchange
  format v1 (`ingestion/parquet_format.py`, `ingestion/parquet_reader.py`), and the
  `nginx2tracesignal.py` converter (pilot). This session added native `*2tracesignal.py`
  Parquet converters for filterlog, suricata, cloudtrail, and pcap, each with its own
  `tests/test_<name>_converter.py`. Decision (mid-session, user request): the vendored
  `*2timesketch` scripts stay vendored **permanently** as a minimal-dependency (stdlib-only,
  no pyarrow) alternative — `scripts/vendor_converters.py` is not retired, and native/vendored
  converters are listed side by side in `manifest.json`/`/api/converters`. Remaining:
  journal, browser (still vendored-only, not yet ported to native). Follow-ups from the nginx
  pilot, still open: benchmark converter worker-count/parallel-threshold defaults on a
  multi-GB log; parallel `.gz` parsing (seek-point indexing) deferred; pcap intra-file
  parallelism (record-boundary chunking, analogous to nginx's newline chunking) deferred —
  `pcap2tracesignal.py` currently parallelizes only across files, one worker process per file.
  Also added `timesketch2parquet.py` — a generic Timesketch-compatible CSV/JSONL converter (any
  column set, no per-source parsing) with no vendored counterpart; column requirements follow
  upstream `google/timesketch`'s own import spec exactly (`message`/`timestamp_desc`/`datetime`
  mandatory, `timestamp` substitutable for `datetime` in CSV, `tag` the only other recognized
  column), not TraceSignal's own server-side generic-CSV parser's extra recognized columns. CSV
  parsing is single-process only (a logical record can span multiple physical lines via quoted
  embedded newlines, unsafe to newline-chunk); JSONL gets full nginx-style chunked
  multiprocessing. CSV intra-file parallelism (record-boundary-aware chunking) deferred,
  same treatment as pcap's.

- [ ] **M23 — detector-scan residue (post 300M-row overhaul, session 27).** Two follow-ups
  deliberately deferred: (a) `canonical_inventory` stays a live query — it only runs when a
  timeline has field mappings, which the 300M reference case doesn't; add the planned
  Postgres cache (key = case + sorted sources + mappings + per-source `computed_at`) only if
  a mapped timeline at that scale measures slow. (b) Per-field novelty scans each re-read the
  whole `attributes` map column (~12 GiB / ~23 s per field at 300M rows); batching all
  scanned fields into one pass over `attributes` would amortize that ~7× for a panel open.

- [ ] **M22 residue — tokenbf text-search fast path.** Items (a) typed `IN` for String
  columns, (c) single-round-trip histogram, and (d) novelty auto-field selection via the
  field-stats cache landed 2026-07-06 (session 24). Remaining: broad text search is still
  a full scan per query (~0.4 s/2.8M rows after cleanup) × histogram+count+page per
  interaction — consider a `tokenbf_v1`-indexed fast path via `hasTokenCaseInsensitive`
  when `q` is a plain token (needs index DDL on existing tables).

- [ ] **M24 — Visualize scan-avoidance (deferred from session 33).** Every viz chart
  aggregation (`field_terms`, `field_numeric_stats`, `field_value_timeseries`, all compare
  variants in `db/queries.py`) is a live full-column scan; the `db/field_stats.py` cache
  (distinct/coverage + 3 samples per field) is consumed only by `viz/fields`. Follow-ups:
  (a) seed first-load *unfiltered* top-values from the cache instead of a live `field_terms`
  scan; (b) merge `field_value_timeseries`' ~3 scans (terms + range + bucket) into fewer
  passes; (c) baseline-mode compare re-scans the whole timeline every render — cache or bound
  it. Not `field_numeric_stats`: its two scans are a deliberate fixed-width-bin
  reproducibility choice (documented in the method). Session 33 already removed the first-load
  numeric probe and defaulted Visualize to the single-pass histogram.

## Milestone 3 — polish

- [ ] Split `api/routers/events.py` (1500+ lines: query parsing, export streaming, anomaly
  orchestration, bulk annotation) opportunistically when next touched — not proactively.

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic**: they operate on value identity, syntax, and statistics —
never on what a field value *means*. Most follow the existing baseline/detect-window pattern in
`db/anomaly_stats.py` (self-baseline + temporal `baseline_end` modes); a few are mode-less
(e.g. shipped D2 is positional, `method="sequential"`). All must stay SQL-explainable per the
forensic-reproducibility requirement. Update `docs/ANOMALY_DETECTION.md` in the same commit as
each detector.

Prep landed: shared frontend detector scaffolding (`components/analysis/detector-shared.tsx`),
a `DetectorAccordion` switcher replacing the flat sub-tab strip, standardized
`["anomalies", caseId, timelineId, ...]` query keys, and an `_col_expr(prefix=...)` param for
multi-field queries. **D1 (value-combo), D2 (timestamp-order), D3 (charset), D4
(numeric-range), D5 (entropy), and D6+D7 (interval_periodicity — see below) shipped.** Also
shipped (beyond the AMiner set, no AMiner equivalent): **proportion_shift** — per-(field, value)
2×2 G-test of a value's *share* of events between the baseline and each suspect window, BH-FDR
across the run, rate-ratio effect floor, temporal-only, first-seen excluded (`baseline_cnt ≥ 1`;
value_novelty owns those).

D6+D7 shipped **merged** as the `interval_periodicity` detector (`method="cadence"`): the
re-scope confirmed proportion_shift already owns whole-window vanished values, leaving only the
*periodicity* angle distinct — so per-value silence (D6) is subsumed as the maximal `count = 0`
"missed" case of the cadence-break direction. Two directions, one BH pool: a baseline-regular
value that breaks rhythm (Poisson-rate LRT; missed/accelerated) and a baseline-bursty value that
becomes suspiciously regular (Greenwood spacing statistic; beaconing). Temporal-only, `-log10(p)`
score. See `docs/ANOMALY_DETECTION.md` §8.

D8 shipped as the `sequence_novelty` detector (`method="ngram"`): per source, time-ordered
n-grams (n = 2–5, default 3) of one grouping field (`series_field`, default `artifact`),
assembled entirely in SQL via a `lagInFrame` chain partitioned by (source, window); n-grams
absent from the baseline window are flagged per suspect window, surprise-scored against the
window's own complete-n-gram total. Temporal-only. See `docs/ANOMALY_DETECTION.md` §9
(semantic search renumbered §10).

High value first:

- [ ] **D9 — Value-distribution drift** (AMiner `VariableTypeDetector`, simplified): per
  field, compare baseline vs. detect-window value distributions with ClickHouse's built-in
  `kolmogorovSmirnovTest()` (numeric) or frequency-vector comparison (categorical).
- [ ] **D10 — Event correlation rules** (AMiner `EventCorrelationDetector`): mine baseline
  implication rules "value A is followed by value B within Δt", flag violations in the detect
  window. Highest analytical payoff, heaviest lift (rule mining + hypothesis testing) — last.

Skipped deliberately: `TSAArimaDetector` (ARIMA forecasting — z-score `frequency` detector
covers most of it and stays explainable).

## Milestone 5 — post-mortem workflow parity (Timesketch-inspired, 2026-07-07 gap review)

Gap analysis vs. Timesketch's researcher-loved features and generic forensic-platform
expectations. Ordered: easy+high value first, then easy+low value, then hard+high value.

Easy, low value (deferred, revisit on demand):

- [ ] **W4 — Python client library.** REST API + `tsig` CLI exist; a thin typed client for
  Jupyter/pandas workflows is cheap but no user has asked yet.

Hard, high value:

- [ ] **W5 — Sigma rule runner.** Background job evaluating Sigma rules (offline YAML,
  airgap-friendly) over ClickHouse, writing hits as `Annotation(origin=system)` with the rule
  id/title as tag — lands in the existing annotation/tag filter UI for free. Canonical field
  mappings are the natural hook for Sigma's field taxonomy. Needs a Sigma-to-ClickHouse-SQL
  backend (pySigma has a generic backend model). Strongest single DFIR-adoption lever.
- [ ] **W6 — Log template clustering (Drain-style).** Collapse structurally identical lines
  into templates so 50M repeats of one error can be muted and the one odd line surfaces.
  Deterministic and SQL-explainable beats embeddings here: start with a ClickHouse-side
  normalization pass (digits/hex/UUID masked, group by normalized message), evaluate Drain3
  (offline, pure Python) if that proves too coarse. Complements, not replaces, the embedding
  pipeline.
- [ ] **W8 — Query-time field extraction (schema-on-read, read half).** Define a virtual
  field as a regex capture over a raw attribute (usually `message`), then facet, histogram,
  and run detectors on it without re-ingest — Splunk `rex` / ES runtime fields, but forensically
  cleaner: the extraction pattern is declared, auditable metadata and raw events stay
  untouched. Natural extension of the field-mappings path (canonical field = regex extraction
  instead of only key rename) via ClickHouse `extractGroups()`; detectors consume it through
  the existing `_col_expr` field-expression mechanism. Prerequisite for making bespoke
  unstructured logs first-class. (The old companion "write half" — server-side raw-log
  parsing per [`docs/archive/PLAN_FAST_NGINX_INGESTION.md`](./archive/PLAN_FAST_NGINX_INGESTION.md)
  — was superseded by the client-side Parquet converter architecture shipped with M20;
  see M25 above.)
- [ ] **W7 — Stories (investigative notebook).** Markdown document per case embedding live
  references to saved Views, charts, and tagged events — the report writes itself during the
  investigation. Building blocks (Views, annotations, saved charts, RBAC) all exist; this is
  mostly a new Postgres model + frontend editor. Timesketch's most-loved feature.

## Legacy-removal suspects (flagged 2026-07-08, verify before cutting)

Code kept only for backward compatibility with older runs/clients. Each is a
**candidate** for removal — confirm nothing still depends on it, then delete in
one commit that also updates `docs/ANOMALY_DETECTION.md`.

- [ ] **L1 — Single-`baseline_end` split point + `temporal=true` midpoint fallback.**
  Superseded by explicit baseline definitions (baseline + 1..N suspect windows).
  Still accepted at the API and converted via `windows_from_split`
  (`db/anomaly_stats.py`), so old persisted runs and any pre-window client keep
  working with exactly one internal temporal code path. Remove the `baseline_end`
  / `temporal` request params and `windows_from_split` once no stored `DetectorRun`
  relies on the legacy shape and the CLI/clients all send `baseline_id`.
## Disposition follow-ups (from the 2026-07-10 unified-taxonomy change)

L2 (per-event `normal` annotation) is resolved: dispositions
(`finding_dispositions`, migration `0004`) subsumed the allowlist, the
per-event `normal` annotation, and the `pinned` flag. Remaining polish:

- [ ] **X3 — Event-grid indicator for event-scoped dispositions.** The legacy
  per-event-normal grid indicator was removed with the annotation; an
  indicator driven by event-scoped disposition rows could return if analysts
  miss it.

## Explicitly out of scope (decided during the audit)

- Persistent job store — in-memory is a documented deliberate choice for the single-process
  deployment model.
- CSRF tokens — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- ~~Alembic adoption~~ — **done**: Postgres schema is now Alembic-managed
  (`db/migrations`), with pre-Alembic databases auto-stamped at `0001` on startup.
- Proactive router/query-builder splits — churn risk outweighs payoff at current velocity.
