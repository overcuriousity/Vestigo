# Vestigo Roadmap — open backlog

The only open backlog. Shipped work lives in `docs/PROGRESS.md` and the feature docs
(`ANOMALY_DETECTION.md`, `AGENT.md`, `STORIES.md`); Phase 1 is archived in
[`archive/ROADMAP_PHASE1.md`](./archive/ROADMAP_PHASE1.md). Per-PR review findings that
outgrow a couple of lines here go to `archive/PR{N}_REVIEW_FINDINGS.md`. Reported defects
live as GitHub issues; when any are open they get an "Open defects" section here with
issue numbers, and root-cause detail stays in the issue thread.

**State (verified against the codebase 2026-07-29):** no open issues, no open PRs, no code
TODOs. Phase 3 is complete, so the queue is feature-shaped.

**Priority order,** roughly by payoff-per-effort:

1. **A12** local transform tools — no design round needed, no OPSEC gate.
2. **W8** query-time field extraction — makes bespoke unstructured logs first-class.
3. **A8** external MCP toolsets — needs its own design round (policy, not plumbing).
4. **D10** correlation rules — heaviest lift, last of the detector line.

Milestone 2–3 items are polish, picked up opportunistically. Milestones 6 (streaming
ingest) and 7 (forensic examination) are future phases gated on a joint S1+E1 design
round — **standing rule: when either resumes, both are designed together in one
`MODEL_REFINEMENT.md` round, so the data model migrates once, not twice.**

## Milestone 2 — visualization gaps

- [ ] **Choropleth / geographic charts.** The one chart family from the 2026-07-22 viz
  round left unimplemented (correlation matrix, grouped box/violin, waffle and
  distribution statistics all shipped). `geoip2` is already a dependency; what is missing
  is an offline story. Needs a design round covering: vendored basemap geometry with a
  redistributable licence (Natural Earth is public domain), the projection, and the
  count-vs-rate normalization rule — count per country is a choropleth, count per city is
  a proportional-symbol map, and the wrong one misleads by area.

- [ ] **Facetting / small multiples.** Built in the 2026-07-22 viz round and cut in the
  PR #162 review. The client-orchestrated version let every panel request its own
  aggregation, so each got Freedman–Diaconis bin edges from its own subset while the grid
  pinned a shared count axis: equal bar heights meant different densities, the exact
  misreading small multiples exist to prevent. Two revival paths:
  - *Cheap (preferred):* reuse `field_numeric_grouped`, which already bins every group
    over a global range in one scan — at the cost of capping panels at 8 and selecting
    groups by numeric-value count rather than event count.
  - *Full:* add optional `range_min`/`range_max` to `field_numeric_stats` (rejecting a
    range without an explicit `bins`, so edges are pinned rather than re-derived), have
    the page resolve one global range + bin count from the unfacetted query and pass both
    to every panel, plus a shared scale computed only once every panel has settled.

  Either path also needs a caption describing the *grid* rather than the aggregate —
  grouped mode already nulls its per-distribution facts, facet mode did not.

## Milestone 3 — polish

- [ ] **Generate frontend API types from OpenAPI** (`openapi-typescript` over
  `/openapi.json`) to replace the hand-mirrored types in `frontend/src/api/types.ts`.
  The duplication is compounding: 1240 lines when this was filed (PR109 review), 1549
  today. Eliminates the per-detector backend↔frontend drift wholesale instead of
  special-casing single detectors.
- [ ] **Split `api/routers/events.py`.** Previously a standing "only when next touched"
  decision; its own revisit trigger has now fired — 3100 lines on 2026-07-20, 3319 on
  2026-07-29, still growing without anyone touching it deliberately. Split along the
  read/aggregate/export seams.
- [ ] **Extract a `ui/Callout` primitive.** `analysis/EmbeddingStatusBanner`,
  `timelines/UploadDialog`'s duplicate warning and a handful of other sites hand-roll the
  same border/dim-background/icon banner with per-site colour tokens.

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic** and SQL-explainable per the forensic-reproducibility
requirement. D1–D9 plus `proportion_shift` and `sequence_motif` shipped — see
`docs/ANOMALY_DETECTION.md` for every detector's contract, and update it in the same
commit as any detector change. Remaining:

- [ ] **D10 — Event correlation rules** (AMiner `EventCorrelationDetector`): mine baseline
  implication rules "value A is followed by value B within Δt", flag violations in the
  detect window. Highest analytical payoff, heaviest lift (rule mining + hypothesis
  testing). Stepping stone shipped: `sequence_motif`'s recurring n-grams are the natural
  antecedent set.

Skipped deliberately: `TSAArimaDetector` (ARIMA forecasting — the z-score `frequency`
detector covers most of it and stays explainable).

## Milestone 5 — post-mortem workflow parity (Timesketch-inspired)

- [ ] **W5 residue — Sigma `logsource` scoping.** The runner shipped (session 63:
  `src/vestigo/sigma/`, `docs/ANOMALY_DETECTION.md` §13) but rules always run over the
  full timeline scope; `logsource` is parsed, stored and displayed for manual selection
  only. Unscoped rules match log types they were never written for, so this is a
  precision defect, not polish.
- [ ] **W8 — Query-time field extraction (schema-on-read).** Define a virtual field as a
  regex capture over a raw attribute (usually `message`), then facet, histogram and run
  detectors on it without re-ingest — Splunk `rex` / ES runtime fields, but forensically
  cleaner: the extraction pattern is declared, auditable metadata and raw events stay
  untouched. Natural extension of the field-mappings path (canonical field = regex
  extraction, not only key rename) via ClickHouse `extractGroups()`; detectors consume it
  through the existing `_col_expr` field-expression mechanism. Prerequisite for making
  bespoke unstructured logs first-class. The old companion "write half" (server-side raw
  log parsing) was superseded by the client-side Parquet converter architecture shipped
  with M20.

## Milestone 6 — streaming ingest ("live forensic" mode, agentless)

Decided 2026-07-14: no bespoke endpoint agent (see "out of scope"). Vestigo accepts pushed
event batches from *existing* collectors (Velociraptor post-processing, fluent-bit,
winlogbeat, custom scripts) over an authenticated ingest endpoint, and the Explorer follows
the stream by polling. Data-model overhaul explicitly approved by the user.

- [ ] **S1 — Stream-source data model.** New Source kind `stream`: no final file hash;
  instead an append-only per-batch chunk manifest (SHA-256 per received batch,
  hash-chained) preserving the forensic attestation story. Touches `docs/CONCEPT.md` /
  `docs/MODEL_REFINEMENT.md`, Postgres migrations, the `(case_id, file_hash)` dedup
  uniqueness (`db/postgres.py`), and source UI. Design-first — this is the real cost of
  the milestone.
- [ ] **S2 — Push ingest endpoint.** Machine-client auth (per-source ingest token, not
  session cookie), rate limiting/backpressure, batch formats JSONL and Arrow IPC — the
  Arrow record-batch path into ClickHouse already exists
  (`ingestion/pipeline.py::_ingest_file_arrow`, `insert_events_arrow`). Plain HTTP POST
  batches first; Arrow Flight optional later.
- [ ] **S3 — Live Explorer.** TanStack Query `refetchInterval` polling on grid +
  histogram for stream sources; WebSocket push deliberately skipped.
- [ ] **S4 — Detectors on open-ended data.** Periodic detector re-runs over stream
  sources; rethink value-novelty "first seen" and baseline-window semantics for unbounded,
  ever-growing sources.

## Milestone 7 — forensic examination expansion (X-Ways/Autopsy role, decided 2026-07-16)

Expand Vestigo beyond log investigation into a forensic examination tool, with the twist
that artifacts are analyzed as **time-annotated items**. Parsing stays permanently out of
core scope — external Parquet-interchange converters handle it (disk-image extraction,
carving, dfVFS traversal are converter territory).

**E1 is a go/no-go gate, not a foregone build.** This milestone redefines the product's
scope and forces a vocabulary refactor of already-shipped columns; the design round decides
whether to commit, and S1 is designed in the same round (see standing rule above).

**Vocabulary decision (2026-07-16): Artifact = a file** — both a logfile that gets ingested
and a file on an examined filesystem. This redefines the current per-event
`artifact`/`artifact_long` meaning (Plaso type strings, see `docs/MODEL_REFINEMENT.md`);
those columns need renaming to a type/kind concept as part of E1, with the same discipline
as the 2026-06 Case/Source/Timeline refactor.

Target model:

```
Source (evidence unit, hashed)
  └── Artifact (N)     ← entity: a file — kind, path, content_hash, size, attributes
        └── Event (M)  ← time annotation: timestamp + normalized role (MACB, visited, run, …)
```

- [ ] **E1 — Model design doc.** Amend `docs/MODEL_REFINEMENT.md` before any code:
  Artifact entity, the vocabulary rename, and a closed timestamp-role taxonomy (M/A/C/B,
  `visited`, `run`, …) replacing free-text `timestamp_desc`. Constraints: artifact identity
  must be converter-stamped and deterministic (like `derive_event_id`), never derived by
  query-time grouping; log lines degrade gracefully (artifact-less events or 1:1 artifact).
- [ ] **E2 — Parquet interchange v2.** Separate `artifacts` + `events` streams,
  converter-stamped deterministic artifact IDs, versioned footer; content blobs as a
  content-addressed sha256 sidecar (selective — items of interest, not full images). Keep
  v1 readable. Pilot converter: MFT/`fls` → artifacts + MACB events.
- [ ] **E3 — Storage + query layer.** ClickHouse `artifacts` table; events gain
  `artifact_id`; Explorer pivot (file list with M/A/C/B columns = events pivoted by role
  per artifact). Hierarchy via materialized `path` + `parent_artifact_id` — no graph store,
  no new backing services.
- [ ] **E4 — Artifact detail UI.** Blob store (generalize the existing content-addressed
  source retention) + viewers (hex/text/image).
- [ ] **E5 — Examination extras.** Hashsets (NSRL/known-bad join on `content_hash` via a
  ClickHouse dictionary), image gallery, content keyword search (extracted-text column +
  tokenbf index).

Carries over unchanged: provenance chain (Source `file_hash`, per-event
`content_hash`/`byte_offset`, UUIDv5 identity), detectors/embeddings/Sigma (W5) and
schema-on-read (W8) gain the new domain for free, auth/RBAC/audit as chain-of-custody
baseline. In-memory JobStore stays — heavy work lives in converters.

## Milestone 8 — AI investigation agent expansion

Agent v1 (read parity + external `/mcp` endpoint) and v2 (three-layer tool toggles, OPSEC
disclosure, thinking capture, JSON export) shipped 2026-07-19/20; context management was
reworked in 1.5.0 — a sliding context window replaced compaction + fidelity ladder
(PR #152). See `docs/AGENT.md` and
`docs/superpowers/specs/2026-07-22-agent-sliding-window-design.md`.

- [ ] **A12 — Local transform tools (CyberChef-class).** Decode/encode (base64, hex, URL,
  …), hashing, decompression, timestamp conversion as **native tools** in `agent/tools.py`
  — a curated, append-only op set (or a recipe runner over a vetted op list), not a
  call-out to a CyberChef server. Pure local computation: no network, deterministic, hence
  reproducible, so it fits offline-by-default with no OPSEC gate. Care points: resource
  caps (decompression bombs; output size vs. context budget — reuse the existing
  `_truncate`/cap conventions) and keeping the op set append-only so old conversations stay
  replayable. Ships independently of and before A8.
- [ ] **A8 — External MCP toolsets (web research / OSINT / user-pluggable tools).** Do NOT
  build bespoke whois/web tools or a custom plugin API: the runtime is pydantic-ai with MCP
  toolsets, so let the agent consume operator-configured **external MCP servers** (a user
  writes a whois/VT/web-search/Shodan tool as a tiny MCP server in any language; zero
  Vestigo code per tool; symmetric with our own `/mcp` exposure). Feasibility confirmed
  2026-07-20 — the toggle/audit/disclosure machinery is ready, the work is the policy
  layer. Needs its own design round. Hard requirements:
  - **OPSEC gate.** Outbound lookups leak case indicators to third parties (the model
    composes queries from case evidence: an internal hostname sent to a search provider, a
    victim IP queried on Shodan, can tip off an adversary). Gate behind
    `VESTIGO_ALLOW_ONLINE` **and** per-case opt-in, default off.
  - **Forensic capture.** Audit every external call; persist and hash the raw response with
    its timestamp (external results drift — they are OSINT enrichment with provenance,
    never evidence); mark results `origin: external` in the conversation record.
  - **Governance reuse.** External tools enter `TOOL_REGISTRY`-equivalent surfacing so the
    three existing deny layers (admin hard-deny, per-user defaults, per-chat opt-in) and
    the tool-selector popover apply uniformly.
  - **Disclosure.** Extend the persistent OPSEC panel (`AgentPanel.tsx`) to name enabled
    network tools and their endpoints.
  - **Doc.** Update `docs/AGENT.md`'s sandbox invariant ("the agent queries the backend in
    its own loop"), which external tools genuinely widen.

## Explicitly out of scope & standing decisions (with revisit triggers)

Decisions, not work items — each stays as decided unless its trigger fires.

- **Persistent job store** — in-memory is a deliberate choice for the single-process
  deployment model ([Operational scale](./DEPLOYMENT.md#operational-scale)), not an
  oversight. Trigger: multi-process scale-out, which needs it moved to a shared backend
  along with the event bus and login backoff.
- **CSRF tokens** — SameSite=Lax cookies are adequate for a self-hosted instance on a
  trusted network. Trigger: exposing Vestigo to the open internet, or moving off a single
  trusted app process (see [Operational scale](./DEPLOYMENT.md#operational-scale)).
- **Bespoke endpoint collection agent** (2026-07-14) — a cross-platform collector fleet is
  a whole product (Velociraptor, osquery). Vestigo stays agentless and accepts pushes from
  existing collectors instead (Milestone 6).
- **Vendored converter ports stay demand-driven.** The vendored `*2timesketch` scripts
  (journal, browser, apache, cowrie, evtx, syslog, webhoneypot) are a permanent
  minimal-dependency alternative (stdlib-only, no pyarrow), listed side by side with native
  converters in `manifest.json` / `/api/converters` — not a porting queue (decided
  2026-07-20).
- **Converter parallelism tuning is revisit-on-demand.** Benchmarking worker-count and
  parallel-threshold defaults on a multi-GB log, parallel `.gz` parsing (seek-point
  indexing), and pcap/CSV intra-file record-boundary chunking (a logical CSV record can
  span physical lines via quoted embedded newlines, so newline-chunking is unsafe) are all
  deferred. Trigger: someone reports a slow converter run.
- **Sigma runner has no live-ClickHouse end-to-end test.** Unit tests cover
  compiler/loader/router; the live path is exercised manually via `/verify`. Trigger: a
  regression escapes that split.
- **Story artifact upload stays off the progress-reporting transfer path.**
  `api/stories.ts::uploadArtifact` posts rendered HTML as a JSON body field rather than
  `FormData`, capped at `story_max_artifact_bytes` (20 MiB), so it was left out of the
  session-108 rollout that moved every other file-bearing request onto `client.ts`'s XHR
  core. Trigger: the cap rises, or artifacts start carrying embedded evidence.
- **M15 — `list_fields_by_artifact` stays a live scan.** The per-source field-stats cache
  (`db/field_stats.py`) covers `field_inventory`/`list_fields`/`field_coverage`; the
  embedding wizard's cost is its randomized per-artifact value sampling, which caching
  would not save. HyperLogLog sketches for exact merged `distinct` likewise deferred.
  Trigger: wizard latency complaints.
- **M23 — `canonical_inventory` stays a live query.** It only runs when a timeline has
  field mappings, which the 300M-row reference case does not. Trigger: a mapped timeline at
  that scale measures slow — then add the planned Postgres cache (key = case + sorted
  sources + mappings + per-source `computed_at`).
- **M26 — the two time-histogram implementations stay separate.** After session 49 the only
  shared piece is the brush gesture; `TimelineHistogram` carries Explorer-only concerns
  that make a merge high-risk/low-payoff. Trigger: the two drift apart.
- **react-router stays on the 7.x line; GHSA-qwww-vcr4-c8h2 is patched there** (revised
  2026-07-29). The advisory (high, CSRF in RSC mode) is fixed by PR #15311, shipped in
  `react-router` 8.3.0 (2026-07-22) and **backported to 7.18.2** as PR #15353, same title,
  published 2026-07-28. Vestigo is on `react-router-dom@7.18.2` → `react-router@7.18.2`, so
  the fix is in. Staying on 7.x rather than migrating to v8 is the standing part of this
  decision: the v8 line dropped `react-router-dom` and moved every export to
  `react-router`, so taking it means migrating 41 imports for no security benefit.
  **Expect the alert to persist**: GitHub and npm both still range the advisory
  `>= 7.12.0, < 8.3.0` and have not amended it to carve out 7.18.2, so tooling keeps
  flagging a version that carries the fix. Dismiss as "fix already applied" rather than
  acting on it, and never run `npm audit fix --force` — it *downgrades* to 7.11.0, giving
  up seven minors of fixes to step below the range. Deliberately not silenced via a
  `dependabot.yml` ignore either: ignoring `< 8.3.0` would also suppress real 7.x patches,
  which is exactly how 7.18.2 would have been missed. Independently, the vulnerable surface
  is unreachable here — it is an unstable RSC API (upstream files the fix under "unstable
  features, not recommended for production"), and Vestigo is a SPA (`createBrowserRouter` +
  `RouterProvider`, zero `unstable_*` imports, FastAPI backend). Triggers: we migrate
  imports to `react-router` for another reason, or RSC APIs are ever adopted — re-evaluate
  immediately in that second case.
- **`diskcache` GHSA-w8v5-vhqr-4h9v / CVE-2025-69872 needs no action** (2026-07-29). Unsafe
  pickle deserialization, medium, *no patch exists* (affects "through 5.6.3",
  `first_patched_version: null`). It reaches us transitively as `pysigma → diskcache` and is
  used by exactly two pysigma modules, `sigma/data/mitre_attack.py` and
  `sigma/data/mitre_d3fend.py`, which Vestigo never imports — verified empirically:
  constructing the app plus `vestigo.sigma.backend`/`rules` leaves `diskcache` and both
  modules absent from `sys.modules`. The attack also requires write access to
  `~/.cache/pysigma/`, which already implies code execution as that user. Dismiss as
  "vulnerable code not used". Separately worth knowing: those two modules `urlopen` MITRE
  data from GitHub, so pulling them in would be an unconditional network call and an airgap
  violation — a second reason to keep them out.
- **W4 — Python client library.** REST API + `vestigo` CLI exist; a thin typed client for
  Jupyter/pandas workflows is cheap. Trigger: a user asks.
- **A11 — `/api/auth/users` full-directory listing** (id, username, display name — needed
  to render names on annotations) assumes every authenticated user of an instance may know
  who else has an account. Trigger: a deployment where the user directory is itself
  sensitive — compartmented investigations, or several groups sharing one instance without
  being meant to see each other — then add a config flag or scope the listing to co-case
  members (PR137 review follow-up).
- **Confirm-proposal crash gap** — a crash between the atomic proposal-decide and the
  annotation bulk-write leaves a confirmed proposal with no annotations and no retry path.
  Single-process tradeoff, deliberate. Trigger: it bites in practice.
