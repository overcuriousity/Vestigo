# Vestigo Roadmap — open backlog

Phase 1 (source management, timelines, explorer, anomaly engine, auth/RBAC/audit,
visualization, converters) is complete — see
[`docs/archive/ROADMAP_PHASE1.md`](./archive/ROADMAP_PHASE1.md). Shipped work is
recorded in `docs/PROGRESS.md` and the feature docs (`ANOMALY_DETECTION.md`,
`AGENT.md`), not here: this file holds only open, condensed action items plus the
standing-decisions list at the bottom (triaged 2026-07-20; git history has the old
long form).

Point-in-time PR review findings are archived under `docs/archive/PR{N}_REVIEW_FINDINGS.md`
(full unrestricted finding set, one file per reviewed PR) once triaged into this backlog or
resolved.

## Phase 3 — investigation depth (active, decided 2026-07-19)

Analyst-depth phase; full rationale in
`docs/superpowers/specs/2026-07-19-phase3-investigation-depth-design.md`. Agent stays an
analysis companion — agent-authored stories deferred. Steps 1 (W6 template clustering,
see `docs/ANOMALY_DETECTION.md` §14) and 2 (A9 viz parity, see `docs/AGENT.md`) shipped.

- [ ] **Step 3 — W7 Stories, human-first** (canonical entry; Milestone 5 pointed here).
  Markdown document per case embedding live references to saved Views, charts, and tagged
  events — the report writes itself during the investigation. Building blocks (Views,
  annotations, saved charts, RBAC) all exist; mostly a new Postgres model + frontend
  editor. Timesketch's most-loved feature. Own design round; key tension: live embeds in
  editor vs. point-in-time snapshot on export. Block model leaves room for a later
  `origin` field (agent authorship later, no migration pain).

Parked: D10 (next phase; W6 feeds it), M6 streaming, M7 examination. Standing rule:
when M6 or M7 resumes, S1 and E1 are designed **jointly** in one `MODEL_REFINEMENT.md`
round — the data model migrates once, not twice.

## Milestone 2 — high-leverage improvements

- [ ] **M25 residue — converter follow-ups.** Open from the nginx pilot: benchmark
  converter worker-count/parallel-threshold defaults on a multi-GB log. Porting the
  remaining vendored `*2timesketch` scripts (journal, browser, apache, cowrie, evtx,
  syslog, webhoneypot) to native Parquet converters is **demand-driven, not planned**
  (decided 2026-07-20) — the vendored scripts stay permanently as the minimal-dependency
  (stdlib-only, no pyarrow) alternative, listed side by side in
  `manifest.json`/`/api/converters`. Deferred parallelism work is likewise
  revisit-on-demand: parallel `.gz` parsing (seek-point indexing), pcap intra-file
  record-boundary chunking, CSV intra-file record-boundary chunking (a logical CSV
  record can span physical lines via quoted embedded newlines, unsafe to newline-chunk).

## Milestone 3 — polish

- [ ] Evaluate OpenAPI-generated frontend API types (`openapi-typescript` over `/openapi.json`)
  to replace the hand-mirrored finding/response types in `frontend/src/api/types.ts`
  (~1240 lines, ~90 types) — eliminates the per-detector backend↔frontend type duplication
  wholesale instead of special-casing single detectors (PR109 review follow-up).

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic** and SQL-explainable per the forensic-reproducibility
requirement. D1–D9 plus `proportion_shift` and `sequence_motif` shipped — see
`docs/ANOMALY_DETECTION.md` for every detector's contract. Update that doc in the same
commit as any detector change. Remaining:

- [ ] **D10 — Event correlation rules** (AMiner `EventCorrelationDetector`): mine baseline
  implication rules "value A is followed by value B within Δt", flag violations in the detect
  window. Highest analytical payoff, heaviest lift (rule mining + hypothesis testing) — last.
  Stepping stone shipped: `sequence_motif`'s recurring n-grams are the natural antecedent set.

Skipped deliberately: `TSAArimaDetector` (ARIMA forecasting — z-score `frequency` detector
covers most of it and stays explainable).

## Milestone 5 — post-mortem workflow parity (Timesketch-inspired, 2026-07-07 gap review)

- [ ] **W5 residue — Sigma runner follow-ups.** The runner shipped (session 63:
  `src/vestigo/sigma/`, `docs/ANOMALY_DETECTION.md` §13). Deliberately deferred:
  automatic `logsource` scoping (rules currently run over the full timeline scope,
  logsource stored + displayed for manual selection); end-to-end run test against live
  ClickHouse (covered manually via `/verify`; unit tests cover compiler/loader/router).
- [ ] **W8 — Query-time field extraction (schema-on-read, read half).** Define a virtual
  field as a regex capture over a raw attribute (usually `message`), then facet, histogram,
  and run detectors on it without re-ingest — Splunk `rex` / ES runtime fields, but forensically
  cleaner: the extraction pattern is declared, auditable metadata and raw events stay
  untouched. Natural extension of the field-mappings path (canonical field = regex extraction
  instead of only key rename) via ClickHouse `extractGroups()`; detectors consume it through
  the existing `_col_expr` field-expression mechanism. Prerequisite for making bespoke
  unstructured logs first-class. (The old companion "write half" — server-side raw-log
  parsing per [`docs/archive/PLAN_FAST_NGINX_INGESTION.md`](./archive/PLAN_FAST_NGINX_INGESTION.md)
  — was superseded by the client-side Parquet converter architecture shipped with M20.)

W7 (Stories) lives in Phase 3 Step 3 above — single canonical entry.

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

## Milestone 8 — AI investigation agent expansion

Agent v1 (read parity + external `/mcp` endpoint) and v2 (compaction, three-layer tool
toggles, OPSEC disclosure, thinking capture, JSON export) shipped 2026-07-19/20 — see
`docs/AGENT.md` and `docs/superpowers/specs/2026-07-19-agent-read-parity-mcp-http-design.md`.

- [ ] **A8 — External MCP toolsets (web research / OSINT / user-pluggable tools).** Do NOT
  build bespoke whois/web tools or a custom plugin API: the runtime is pydantic-ai with MCP
  toolsets, so let the agent consume operator-configured **external MCP servers**
  (users write a whois/VT/web-search/Shodan tool as a tiny MCP server in any language; zero
  Vestigo code per tool; symmetric with our own `/mcp` exposure). Hard requirements:
  (a) OPSEC gate — outbound lookups leak case indicators to third parties (the model
  composes queries from case evidence: an internal hostname or IOC sent to a search
  provider, a victim IP queried on Shodan, can tip off an adversary); gate behind
  `VESTIGO_ALLOW_ONLINE` **and** per-case opt-in, default off; (b) forensic capture —
  audit every external call and persist/hash the raw response with its timestamp
  (external results drift over time; they are OSINT enrichment with provenance, never
  evidence), mark results `origin: external` in the conversation record; (c) governance
  reuse — external tools enter `TOOL_REGISTRY`-equivalent surfacing so the existing three
  deny layers (admin hard-deny, per-user defaults, per-chat opt-in) and the tool-selector
  popover apply uniformly; (d) disclosure — extend the persistent OPSEC panel
  (`AgentPanel.tsx`) to name any enabled network tools and their endpoints; (e) update
  `docs/AGENT.md`'s sandbox invariant ("the agent queries the backend in its own loop"),
  which external tools genuinely widen. Feasibility confirmed 2026-07-20: the
  toggle/audit/disclosure machinery is ready — the work is the policy layer, not plumbing.
  Needs its own design round before implementation.
- [ ] **A12 — Local transform tools (CyberChef-class).** Decode/encode (base64, hex, URL,
  …), hashing, decompression, timestamp conversion as **native tools** in
  `agent/tools.py` — a curated, append-only op set (or recipe-runner over a vetted op
  list), not a call-out to a CyberChef server. Pure local computation: no network, fully
  deterministic, hence reproducible — fits the offline-by-default and forensic
  requirements with no OPSEC gate. Care points: resource caps (decompression bombs,
  output size vs. context budget — reuse the existing `_truncate`/cap conventions) and
  keeping the op set append-only so old conversations stay replayable. Lowest-friction,
  highest-fit agent-tool addition; can ship independently of (and before) A8.

## Explicitly out of scope & standing decisions (with revisit triggers)

Decisions, not work items — each stays as decided unless its trigger fires.

- **Persistent job store** — in-memory is a documented deliberate choice for the
  single-process deployment model.
- **CSRF tokens** — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- **Bespoke endpoint collection agent** (2026-07-14) — building/maintaining a
  cross-platform collector fleet is a whole product (Velociraptor, osquery); Vestigo stays
  agentless and accepts pushes from existing collectors instead (Milestone 6).
- **`api/routers/events.py` split** — opportunistically when next touched, never
  proactively (churn risk outweighs payoff). Now ~3100 lines (2026-07-20), double the
  size when this was decided — if it keeps growing untouched, reconsider as a real item.
- **M15 — `list_fields_by_artifact` stays a live scan.** The per-source field-stats cache
  (`db/field_stats.py`) covers `field_inventory`/`list_fields`/`field_coverage`; the
  embedding wizard's cost is its randomized per-artifact value sampling, which caching
  wouldn't save. HyperLogLog sketches for exact merged `distinct` likewise deferred.
  Trigger: wizard latency complaints.
- **M23 — `canonical_inventory` stays a live query.** It only runs when a timeline has
  field mappings, which the 300M-row reference case doesn't. Trigger: a mapped timeline
  at that scale measures slow — then add the planned Postgres cache (key = case + sorted
  sources + mappings + per-source `computed_at`).
- **M26 — the two time-histogram implementations stay separate.** After session 49 the
  only shared piece is the brush gesture; `TimelineHistogram` carries Explorer-only
  concerns that make a merge high-risk/low-payoff. Trigger: the two drift apart.
- **W4 — Python client library.** REST API + `vestigo` CLI exist; a thin typed client
  for Jupyter/pandas workflows is cheap. Trigger: a user asks.
- **Vendored converter ports** — demand-driven only (see M25); the vendored
  `*2timesketch` scripts are a permanent minimal-dependency alternative, not a porting
  queue.
- **A11 — `/api/auth/users` full-directory listing** (id, username, display name —
  needed to render names on annotations) is fine for the small-team threat model.
  Trigger: multi-tenant / large-org deployments — then add a config flag or scope the
  listing to co-case members (PR137 review follow-up).
- **Confirm-proposal crash-gap** — a crash between the atomic proposal-decide and the
  annotation bulk-write leaves a confirmed proposal with no annotations and no retry
  path. Single-process tradeoff, deliberate. Trigger: it bites in practice.
