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

- [ ] **M20 — Ingest-throughput follow-ups (only if needed).** `TS_INGEST_BATCH_SIZE`
  (default 20k, one HTTP insert per batch) should carry a 100 GiB ingest fine (~5k inserts
  for ~100M rows). Revisit only if measured insufficient: ClickHouse native protocol
  (clickhouse-driver, port 9000), `async_insert`, parse/insert pipelining (parser thread
  feeding an insert thread).

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

## Milestone 3 — polish

- [ ] Split `api/routers/events.py` (1500+ lines: query parsing, export streaming, anomaly
  orchestration, bulk annotation) opportunistically when next touched — not proactively.
- [ ] `ClickHouseStore._host/_port` string-splitting breaks on `https://` and creds-in-URL
  forms — use `urllib.parse`.
- [ ] Startup config sanity report: log resolved offline mode, cookie security
  (warn when `environment=production` and `auth_cookie_secure=false`), datastore targets.
- [ ] Large-file ingest regression test: bound peak memory (or assert lazy yielding) over a
  generated ~100 MB CSV, protecting the H1 fix.
- [ ] **M18 — Return `access_level` from the case API.** PR #7 cleanup #9 follow-up:
  `frontend/src/lib/caseAccess.ts` re-implements `resolve_case_access` client-side; the
  backend already computes the level per request. Needs a bulk access-resolution path in
  `list_cases_for_user` first to avoid introducing an N+1 (`docs/archive/PR7_REVIEW_FINDINGS.md`
  cleanup 9).

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic**: they operate on value identity, syntax, and statistics —
never on what a field value *means*. Most follow the existing baseline/detect-window pattern in
`db/anomaly_stats.py` (self-baseline + temporal `baseline_end` modes); a few are mode-less
(e.g. shipped D2 is positional, `method="sequential"`). All must stay SQL-explainable per the
forensic-reproducibility requirement. Update `docs/ANOMALY_DETECTION.md` in the same commit as
each detector.

Prep landed: shared frontend detector scaffolding (`components/analysis/detector-shared.tsx`),
a Radix `Select` detector switcher replacing the flat sub-tab strip, standardized
`["anomalies", caseId, timelineId, ...]` query keys, and an `_col_expr(prefix=...)` param for
multi-field queries. **D1 (value-combo), D2 (timestamp-order), D3 (charset), D4
(numeric-range), and D5 (entropy) shipped.**

High value first:

- [ ] **D6 — Per-value silence** (AMiner `MissingMatchPathValueDetector`): a value that
  appeared regularly in the baseline stops appearing in the detect window (agent killed,
  log source suppressed). Complements the existing `frequency` detector's global silences.
- [ ] **D7 — Interval-periodicity violations** (AMiner `PathValueTimeIntervalDetector`): learn
  the inter-arrival interval distribution per field value in the baseline; flag deviation
  (missed/shifted periodic events) and, inversely, newly *regular* intervals (beaconing).
- [ ] **D8 — Event-sequence novelty** (AMiner `EventSequenceDetector`): n-grams of artifact
  types (or values of one user-chosen grouping field) ordered by time; flag n-grams absent
  from the baseline. `groupArray` + window functions.
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

Easy, high value:

- [ ] **W1 — Context query ("neighbors").** Button on an event (detail panel / grid row) that
  pivots to all events across the timeline within ±N minutes of that event's timestamp,
  regardless of source. One endpoint reusing the existing events-view filter path (time-window
  filter around anchor timestamp) + UI affordance in `EventDetailPanel`. The single most
  common analyst move after finding a hit.
- [ ] **W2 — Per-source clock-skew correction.** Compromised/misconfigured hosts drift; master
  timeline lies without correction. Add `time_offset_seconds` to Source (Postgres), applied at
  **query time** (never mutate ingested events — evidence stays raw; offset is analyst-declared
  metadata and must appear in the audit trail and any export manifest). Applies to explorer,
  histogram, detectors, exports uniformly — route through the shared filter/query path.
- [ ] **W3 — Audit coverage for analyst actions.** `record_audit` currently fires only in
  auth/admin/cases routers. Extend to the actions that matter for "who discovered/extracted
  what": event **exports** (with filter set + row count), **bulk annotation** operations, and
  **anomaly run** launches (detector + params). Deliberately not per-page `list_events` calls —
  query-level logging at browsing granularity is noise, not custody.

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
  unstructured logs first-class; a raw-line ingestion mode (timestamp-only parsing) would be
  the companion write half, deliberately not scheduled yet.
- [ ] **W7 — Stories (investigative notebook).** Markdown document per case embedding live
  references to saved Views, charts, and tagged events — the report writes itself during the
  investigation. Building blocks (Views, annotations, saved charts, RBAC) all exist; this is
  mostly a new Postgres model + frontend editor. Timesketch's most-loved feature.

## Explicitly out of scope (decided during the audit)

- Persistent job store — in-memory is a documented deliberate choice for the single-process
  deployment model.
- CSRF tokens — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- Alembic adoption — hand-rolled additive migration works at the current schema churn;
  revisit at v1.0.
- Proactive router/query-builder splits — churn risk outweighs payoff at current velocity.
