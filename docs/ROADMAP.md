# Vestigo Roadmap — Phase 2 (hardening backlog)

Phase 1 (source management, timelines, explorer, anomaly engine, auth/RBAC/audit,
visualization, converters) is complete — see
[`docs/archive/ROADMAP_PHASE1.md`](./archive/ROADMAP_PHASE1.md).

This phase consolidates the remaining findings from the 2026-07-03 repository audit.
The audit's Critical/High items were fixed directly on `fix/audit-critical-high`:

- ✅ **C1** — Dockerfile CMD pointed at a nonexistent `api.main:app`; now `--factory create_app`.
- ✅ **H1** — CSV parser read the whole file into memory (`lines = list(fh)`); now streams with
  incremental byte-offset/line tracking (`ingestion/parser.py::_RecordTrackingIterator`).
- ✅ **H2** — Airgap enforcement: `vestigo-web` no longer runs `npm install` on every start
  (builds only when `dist/` is missing; `VESTIGO_FRONTEND_REBUILD=1` forces); uvicorn reloader is
  development-only; embedding model load forces `HF_HUB_OFFLINE` unless `VESTIGO_ALLOW_ONLINE` and
  fails with an actionable message instead of silently downloading.
- ✅ **H3** — Blocking ClickHouse calls in async handlers (`list_events`, histogram, bulk
  annotate, field/artifact/tag listings, embedding-field recommenders) now go through
  `run_in_threadpool`, matching viz/anomaly endpoints. Convention: **every**
  `EventQueryService` call from an `async def` handler must be threadpool-wrapped.
- ✅ **H4** — Uploads: single-pass copy+hash off the event loop
  (`ingestion/files.py::copy_and_hash`), capped by `VESTIGO_MAX_UPLOAD_BYTES`
  (default 10 GiB, 0 disables) with a 413 mid-stream rejection.

Point-in-time PR review findings are archived under `docs/archive/PR{N}_REVIEW_FINDINGS.md`
(full unrestricted finding set, one file per reviewed PR) once triaged into this backlog or
resolved — this file holds only the condensed, still-open action items.

## Phase 3 — investigation depth (active, decided 2026-07-19)

Analyst-depth phase; full rationale in
`docs/superpowers/specs/2026-07-19-phase3-investigation-depth-design.md`. Agent stays an
analysis companion — agent-authored stories deferred. Ordered:

- [ ] **Step 1 — W6 template clustering** (see Milestone 5 entry). First: independent,
  and template IDs become a facet the later steps consume. Reuse the `routine` collapse
  machinery for "mute template".
- [ ] **Step 2 — A9 viz parity** (see Milestone 8 entry). Own design round; before
  Stories so the chart-spec is exercised once before Stories embeds it.
- [ ] **Step 3 — W7 Stories, human-first** (see Milestone 5 entry). Own design round;
  key tension: live embeds in editor vs. point-in-time snapshot on export. Block model
  leaves room for a later `origin` field (agent authorship later, no migration pain).

Parked: D10 (next phase; W6 feeds it), M6 streaming, M7 examination. Standing rule:
when M6 or M7 resumes, S1 and E1 are designed **jointly** in one `MODEL_REFINEMENT.md`
round — the data model migrates once, not twice.

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
  bulk Arrow insert, the upload hardlink-retention fix, the Vestigo Parquet interchange
  format v1 (`ingestion/parquet_format.py`, `ingestion/parquet_reader.py`), and the
  `nginx2vestigo.py` converter (pilot). This session added native `*2vestigo.py`
  Parquet converters for filterlog, suricata, cloudtrail, and pcap, each with its own
  `tests/test_<name>_converter.py`. Decision (mid-session, user request): the vendored
  `*2timesketch` scripts stay vendored **permanently** as a minimal-dependency (stdlib-only,
  no pyarrow) alternative — `scripts/vendor_converters.py` is not retired, and native/vendored
  converters are listed side by side in `manifest.json`/`/api/converters`. Remaining:
  journal, browser, apache, cowrie, evtx, syslog, webhoneypot (still vendored-only, not yet
  ported to native — re-synced to upstream `d4838eb2`, session 59; webhoneypot is new upstream,
  no native counterpart yet). Follow-ups from the nginx
  pilot, still open: benchmark converter worker-count/parallel-threshold defaults on a
  multi-GB log; parallel `.gz` parsing (seek-point indexing) deferred; pcap intra-file
  parallelism (record-boundary chunking, analogous to nginx's newline chunking) deferred —
  `pcap2vestigo.py` currently parallelizes only across files, one worker process per file.
  Also added `timesketch2parquet.py` — a generic Timesketch-compatible CSV/JSONL converter (any
  column set, no per-source parsing) with no vendored counterpart; column requirements follow
  upstream `google/timesketch`'s own import spec exactly (`message`/`timestamp_desc`/`datetime`
  mandatory, `timestamp` substitutable for `datetime` in CSV, `tag` the only other recognized
  column), not Vestigo's own server-side generic-CSV parser's extra recognized columns. CSV
  parsing is single-process only (a logical record can span multiple physical lines via quoted
  embedded newlines, unsafe to newline-chunk); JSONL gets full nginx-style chunked
  multiprocessing. CSV intra-file parallelism (record-boundary-aware chunking) deferred,
  same treatment as pcap's.

- [ ] **M23 — detector-scan residue (post 300M-row overhaul, session 27).** Remaining:
  (a) `canonical_inventory` stays a live query — it only runs when a timeline has field
  mappings, which the 300M reference case doesn't; add the planned Postgres cache (key =
  case + sorted sources + mappings + per-source `computed_at`) only if a mapped timeline at
  that scale measures slow. ((b) — batching per-field novelty scans into one `attributes`
  pass — landed 2026-07-11, session 50.)

- [ ] **M26 — Unify the two time-histogram implementations (deferred, session 49).** The
  Explorer's `TimelineHistogram` (div-bars, markers/baseline bands/scroll indicator/mark
  mode) and the Visualize page's `TimeHistogram`/`CompareHistogram` (d3-SVG, now with its
  own brush-zoom) duplicate bucket math and brush gestures. Deliberately deferred: after
  session 49 the shared piece is only the brush gesture, and TimelineHistogram carries
  Explorer-only concerns that make a merge high-risk/low-payoff. Revisit if the two drift.

## Milestone 3 — polish

- [ ] Split `api/routers/events.py` (1500+ lines: query parsing, export streaming, anomaly
  orchestration, bulk annotation) opportunistically when next touched — not proactively.
- [ ] Evaluate OpenAPI-generated frontend API types (`openapi-typescript` over `/openapi.json`)
  to replace the hand-mirrored finding/response types in `frontend/src/api/types.ts` —
  eliminates the per-detector backend↔frontend type duplication wholesale instead of
  special-casing single detectors (PR109 review follow-up).

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

D9 shipped as the `value_distribution_drift` detector (`method="drift"`): per field, one
whole-distribution test per suspect window — ClickHouse `kolmogorovSmirnovTestIf` for
numeric fields, a k-category G-test (top-50 + exact `__other__`, pure-math df=k−1 chi²)
for categorical; one BH pool across both branches, `-log10(p)` score, effect floors on
KS D / total-variation distance, field-level `(field, "*")` allowlist key. See
`docs/ANOMALY_DETECTION.md` §10 (semantic search renumbered §11).

Shipped beyond the AMiner set: **sequence_motif** (`method="motif"`) — the mining complement
of D8: same per-source `lagInFrame` n-gram assembly (shared `_ngram_inner_sql` helper), but
mode-less and ranking *recurring* n-grams by support × cadence regularity (CV of
inter-occurrence gaps + auditable Greenwood z/p, per-source breakdown). Comes with the
`kind="routine"` disposition (presentation-only, materialized `motif_occurrences` ClickHouse
table, `collapse_routine` grid filter with an always-visible `routine_collapsed_count`).
Mined motifs are candidate rule antecedents for D10. See `docs/ANOMALY_DETECTION.md` §12.

Remaining:

- [ ] **D10 — Event correlation rules** (AMiner `EventCorrelationDetector`): mine baseline
  implication rules "value A is followed by value B within Δt", flag violations in the detect
  window. Highest analytical payoff, heaviest lift (rule mining + hypothesis testing) — last.
  Stepping stone shipped: `sequence_motif`'s recurring n-grams are the natural antecedent set.

Skipped deliberately: `TSAArimaDetector` (ARIMA forecasting — z-score `frequency` detector
covers most of it and stays explainable).

## Milestone 5 — post-mortem workflow parity (Timesketch-inspired, 2026-07-07 gap review)

Gap analysis vs. Timesketch's researcher-loved features and generic forensic-platform
expectations. Ordered: easy+high value first, then easy+low value, then hard+high value.

Easy, low value (deferred, revisit on demand):

- [ ] **W4 — Python client library.** REST API + `vestigo` CLI exist; a thin typed client for
  Jupyter/pandas workflows is cheap but no user has asked yet.

Hard, high value:

- [ ] **W5 residue — Sigma runner follow-ups.** The runner shipped (session 63:
  `src/vestigo/sigma/`, `docs/ANOMALY_DETECTION.md` §13). Deliberately deferred:
  automatic `logsource` scoping (rules currently run over the full timeline scope,
  logsource stored + displayed for manual selection); end-to-end run test against live
  ClickHouse (covered manually via `/verify`; unit tests cover compiler/loader/router).
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

## Milestone 6 — streaming ingest ("live forensic" mode, agentless)

Decided 2026-07-14: no bespoke endpoint agent (Velociraptor/osquery-class effort, out of
scope permanently — see "Explicitly out of scope"). Instead: Vestigo accepts pushed event
batches from *existing* collectors (Velociraptor post-processing, fluent-bit, winlogbeat,
custom scripts) over an authenticated ingest endpoint, and the Explorer follows the stream
via polling. Data-model overhaul explicitly approved by the user.

- [ ] **S1 — Stream-source data model.** New Source kind `stream`: no final file hash;
  instead an append-only per-batch chunk manifest (SHA-256 per received batch, hash-chained)
  preserving the forensic attestation story. Touches `docs/CONCEPT.md` /
  `docs/MODEL_REFINEMENT.md`, Postgres migrations, the `(case_id, file_hash)` dedup
  uniqueness (`db/postgres.py`), and source UI. Design-first — this is the real cost of the
  milestone; do it before any endpoint work.
- [ ] **S2 — Push ingest endpoint.** Machine-client auth (per-source ingest token, not
  session cookie), rate limiting/backpressure, batch formats: JSONL and Arrow IPC — the
  Arrow record-batch path into ClickHouse already exists
  (`ingestion/pipeline.py::_ingest_file_arrow`, `insert_events_arrow`). Arrow Flight
  optional later; plain HTTP POST batches first.
- [ ] **S3 — Live Explorer.** TanStack Query `refetchInterval` polling on grid + histogram
  for stream sources; WebSocket push deliberately skipped.
- [ ] **S4 — Detectors on open-ended data.** Periodic detector re-runs over stream sources;
  rethink value-novelty "first seen" and baseline-window semantics for unbounded,
  ever-growing sources.

## Milestone 7 — forensic examination expansion (X-Ways/Autopsy role, decided 2026-07-16)

Expand Vestigo beyond log investigation into a forensic examination tool, with the twist
that artifacts are analyzed as **time-annotated items**. Parsing stays permanently out of
core scope — external Parquet-interchange converters handle it (disk-image extraction,
carving, dfVFS traversal are converter territory).

**Vocabulary decision (2026-07-16): Artifact = a file** — both a logfile that gets
ingested and a file on an examined filesystem. This redefines the current per-event
`artifact`/`artifact_long` meaning (Plaso type strings, see `docs/MODEL_REFINEMENT.md`);
those columns need renaming to a type/kind concept as part of E1 — a deliberate
vocabulary refactor with the same discipline as the 2026-06 Case/Source/Timeline one.

Target model:

```
Source (evidence unit, hashed)
  └── Artifact (N)    ← entity: a file — kind, path, content_hash, size, attributes
        └── Event (M)  ← time annotation: timestamp + normalized role (MACB, visited, run, …)
```

- [ ] **E1 — Model design doc.** Amend `docs/MODEL_REFINEMENT.md` before any code:
  Artifact entity, the vocabulary rename (per-event type column loses the "artifact"
  name), and a closed timestamp-role taxonomy (M/A/C/B, `visited`, `run`, …) replacing
  free-text `timestamp_desc`. Constraints: artifact identity must be converter-stamped
  and deterministic (like `derive_event_id`), never derived by query-time grouping;
  log lines degrade gracefully (artifact-less events or 1:1 artifact).
- [ ] **E2 — Parquet interchange v2.** Separate `artifacts` + `events` streams,
  converter-stamped deterministic artifact IDs, versioned footer; content blobs as a
  content-addressed sha256 sidecar (selective — items of interest, not full images).
  Keep v1 readable. Pilot converter: MFT/`fls` → artifacts + MACB events (richest test
  of the model).
- [ ] **E3 — Storage + query layer.** ClickHouse `artifacts` table; events gain
  `artifact_id`; Explorer pivot (file list with M/A/C/B columns = events pivoted by
  role per artifact). Hierarchy via materialized `path` + `parent_artifact_id` — no
  graph store, no new backing services.
- [ ] **E4 — Artifact detail UI.** Blob store (generalize the existing content-addressed
  source retention) + viewers (hex/text/image).
- [ ] **E5 — Examination extras.** Hashsets (NSRL/known-bad join on `content_hash` via
  ClickHouse dictionary), image gallery, content keyword search (extracted-text column
  + tokenbf index).

Carries over unchanged: provenance chain (Source `file_hash`, per-event
`content_hash`/`byte_offset`, UUIDv5 identity), detectors/embeddings/Sigma (W5) and
schema-on-read (W8) gain the new domain for free, auth/RBAC/audit as chain-of-custody
baseline. In-memory JobStore stays — heavy work lives in converters.

## Milestone 8 — AI investigation agent expansion (v1 shipped 2026-07-19, see docs/AGENT.md)

Read parity + external MCP endpoint shipped 2026-07-19 (session 66): nine new read tools
(baselines, dispositions, saved views, annotations, Sigma rules/runs), FilterSpec gained
`annotated`/`annotation_tag_value`/`run_id`/`event_ids`/`collapse_routine`, detector tuning
params on `run_anomaly_detector`, `AgentToken` scoped PATs, and `/mcp` streamable-HTTP
exposure of the identical tool server (`VESTIGO_MCP_ENABLED`, default off). See
`docs/superpowers/specs/2026-07-19-agent-read-parity-mcp-http-design.md` for the full design.

- [ ] **A11 — `/api/auth/users` directory scope.** Any signed-in user can list the full
  user directory (id, username, display name — needed to render names on annotations).
  Fine for the small-team threat model; add a config flag or scope the listing to co-case
  members if multi-tenant / large-org deployments emerge (PR137 review follow-up).
- [ ] **A8 — External MCP toolsets (web research / user-pluggable tools).** Do NOT build
  bespoke whois/web tools or a custom plugin API: the runtime is pydantic-ai with MCP
  toolsets, so let the agent consume operator-configured **external MCP servers**
  (users write a whois/VT/web-search tool as a tiny MCP server in any language; zero
  Vestigo code per tool; symmetric with our own `/mcp` exposure). Hard requirements:
  (a) OPSEC gate — outbound lookups leak case indicators to third parties; gate behind
  `VESTIGO_ALLOW_ONLINE` **and** per-case opt-in, default off; (b) forensic capture —
  audit every external call and persist/hash the raw response (external evidence must
  stay replayable), mark results `origin: external` in the conversation record. Needs
  its own design round before implementation.
- [ ] **A9 — Agent-created visualizations (viz parity).** Give the agent the same charting
  a human has on the Visualize page, in two symmetric halves: (a) **read tools** wrapping
  the existing viz queries (`field_timeseries`, `time_punchcard`, `field_pivot`,
  `field_scatter`, `compare_layers` — field-terms/numeric/histogram already exist)
  returning budget-capped compact series so the model can *find* a pattern before showing
  it; (b) a **`propose_chart` tool** carrying the exact chart spec (chart type + the same
  validated params the viz endpoints take + FilterSpec); the backend validates by
  executing the query and echoing summary stats, the panel renders a **live chart card**
  reusing the Visualize page's chart components, with "Open in Visualize" (applies the
  spec through the normal URL path) and "Save" (the analyst's click writes a saved chart
  via the existing endpoint, credited to the analyst — no proposal lifecycle needed since
  the analyst executes the write). Sandbox+apply invariant holds: the agent never mutates
  the analyst's view or writes anything itself. Result caps matter: viz series are dense —
  per-tool row/bucket budgets like `search_events`. Needs its own design round
  (chart-spec schema shared backend↔frontend, card rendering reuse vs. simplified
  renderer) before implementation.
- [ ] **Confirm-proposal crash-gap.** A crash between the atomic proposal-decide and the
  annotation bulk-write leaves a confirmed proposal with no annotations and no retry path.
  Single-process tradeoff, deliberate; revisit if it bites.

## Explicitly out of scope (decided during the audit)

- Persistent job store — in-memory is a documented deliberate choice for the single-process
  deployment model.
- CSRF tokens — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- ~~Alembic adoption~~ — **done**: Postgres schema is now Alembic-managed
  (`db/migrations`), with pre-Alembic databases auto-stamped at `0001` on startup.
- Proactive router/query-builder splits — churn risk outweighs payoff at current velocity.
- Bespoke endpoint collection agent (2026-07-14) — building/maintaining a cross-platform
  collector fleet is a whole product (Velociraptor, osquery); Vestigo stays agentless and
  accepts pushes from existing collectors instead (Milestone 6).
