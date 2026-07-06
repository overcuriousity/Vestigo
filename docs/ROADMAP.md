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

## Milestone 1 — correctness & forensic integrity (Medium severity)


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
multi-field queries. **D1 (value-combo), D2 (timestamp-order), and D4 (numeric-range)
shipped.**

High value first:

- [ ] **D3 — Charset novelty** (AMiner `CharsetDetector`): per field, learn the baseline
  character set of values; flag values in the detect window containing never-seen characters
  (null bytes, unicode homoglyphs, injection metacharacters — detected syntactically, not by
  meaning).
- [ ] **D5 — Value entropy outliers** (AMiner `EntropyDetector`): per field, Shannon
  character-entropy of each value vs. the field's baseline entropy distribution; flags
  random-looking strings (DGA domains, encoded payloads) without interpreting them.
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

## Explicitly out of scope (decided during the audit)

- Persistent job store — in-memory is a documented deliberate choice for the single-process
  deployment model.
- CSRF tokens — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- Alembic adoption — hand-rolled additive migration works at the current schema churn;
  revisit at v1.0.
- Proactive router/query-builder splits — churn risk outweighs payoff at current velocity.
