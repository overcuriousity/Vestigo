# Vestigo Roadmap — open backlog

The only open backlog. Shipped work lives in `PROGRESS.md`, `CHANGELOG.md` and the feature
docs (`ANOMALY_DETECTION.md`, `AGENT.md`, `STORIES.md`). Reported defects live as GitHub
issues; root-cause detail stays in the issue thread.

**State (verified 2026-08-30, v1.17.0):** one open GitHub issue — #307, converters for AI
agent telemetry — which is tracked there and not duplicated below. Phase 3 is complete and
the queue is feature-shaped. Priority, roughly by payoff-per-effort:

1. **D11** entropy bigram variant — closes a capability gap the docs used to overclaim;
   truth of what we ship outranks new surface.
2. **A12** local transform tools — no design round, no OPSEC gate.
3. **D12** / **D13** / **D15** — cheap detectors reusing existing SQL machinery.
4. **W8** query-time field extraction — makes bespoke unstructured logs first-class.
5. **A8** external MCP toolsets — needs its own design round (policy, not plumbing).
6. **D10** / **D16** — heaviest lifts, last of the detector line.

Milestones 2–3 are polish, picked up opportunistically. Milestone 9 is additive work on
shipped subsystems. Milestones 6 (streaming ingest) and 7 (forensic examination) are future
phases gated on a joint S1+E1 design round — **standing rule: when either resumes, both are
designed together in one `MODEL_REFINEMENT.md` round, so the data model migrates once.**

## Milestone 2 — visualization gaps

- [ ] **Choropleth / geographic charts.** The one family from the 2026-07-22 viz round left
  unimplemented. `geoip2` is already a dependency; what is missing is an offline story.
  Design round must cover vendored basemap geometry with a redistributable licence (Natural
  Earth is public domain), the projection, and the count-vs-rate normalization rule — count
  per country is a choropleth, count per city is a proportional-symbol map, and the wrong
  one misleads by area.
- [ ] **Demo-case chart coverage test.** A sibling of
  `tests/test_demo_detector_coverage_clickhouse.py` asserting `execute_chart_spec` draws every
  figure over the demo case with at least one row/bar/interval — deferred through the
  2026-08-29 Visualize round.
## Milestone 3 — polish

- [ ] **Make `VESTIGO_STAT_SCAN_CONCURRENCY` live.** Every other `stat_scan_*` value now
  resolves per query; concurrency sizes `HEAVY_SCAN_GATE`, a `BoundedSemaphore` imported by
  value into four modules, so rebinding the global would not reach them. Needs the gate
  behind an accessor, plus a decision about what resizing means for held slots.
- [ ] **Surface the scan-budget risk in the admin console.** `/api/health` carries
  `scan_budget` with `risk: ok | over_budget | unbounded` and nothing renders it. Belongs
  next to the Scan guardrails group on Admin → Settings, as a banner naming the remedy.
- [ ] **Show env-pinned settings as pinned in the admin console.** A field set in the
  environment silently wins over the stored override, so an admin can flip a toggle, get a
  200 and see nothing change. Serve the pinned-field set from the settings API and render
  those inputs disabled with a "pinned by `VESTIGO_*`" hint.
- [ ] **Per-source progress on a resumed enrichment run.** `run_resume_job` reports status
  only. Minimal shape: an optional `on_source_done` callback on `_apply_staged_rows`, with
  the route seeding `total` from the staged-source count it already computes.
- [ ] **Semantic search does not survive a server-side resolve.** A saved View's filter
  payload carries `qMode: "semantic"`, but `stories/export.py::_filter_payload_to_spec`
  drops it, so a story export freezes a *different* result set than the block shows. Either
  teach `FilterSpec` the semantic path or refuse the export with a named reason; silently
  degrading the query is the one wrong option.
- [ ] **Generate frontend API types from OpenAPI** (`openapi-typescript`) to replace the
  hand-mirrored `frontend/src/api/types.ts`. The duplication compounds: 1240 lines when
  filed, 2007 on 2026-08-30.
- [ ] **Split `api/routers/events.py`** along the read/aggregate/export seams — 3100 lines
  on 2026-07-20, 3319 on 2026-07-29, 3836 on 2026-08-30, growing without anyone touching it
  deliberately.
- [ ] **README screenshot grid.** The README is laid out for a 2×2 grid but ships one
  Explorer shot. Capture at one window size: Analysis with findings and the Method panel,
  a Story with a live view embed, the Agent with an applied finding, a re-shot Explorer.

### Frontend design-system consistency (audit 2026-07-30)

Root cause: **the design system stops at colour.** Colour is tokenized and disciplined;
type, spacing, radius, icon size and surface treatment have no token layer and no
primitive, so every author re-decides at the call site.

The ratchet exists — `frontend/src/test/designSystem.test.ts`. Undefined `var(--…)` is a
hard check at zero; arbitrary `text-[Npx]` and raw `<button>` outside `components/ui/` are
budgeted per file in `designSystemBudget.ts`, seeded at 119 each on 2026-07-30. The budget
only falls: exceeding an entry fails, and so does *beating* one without lowering it. **Every
item below burns its numbers out of that file**; the migration is done when the file is `{}`.

**The ratchet has not held on font sizes.** Totals on 2026-08-30 are 127 arbitrary font
sizes (up 8 from the seed) and 117 raw `<button>`s (down 2). The check only guards files
already listed, so each new component adds its own entry and the total climbs while every
individual budget is respected — the mechanism is working as written and the written rule is
wrong. Fixing that is part of the two migration items below, not a separate task: seed no
new entries, and require a new component to use the primitives instead.

- [ ] **Type scale in `@theme`, and burn down the 127 arbitrary font sizes.** A correctness
  item: `html[data-density="compact"]` rebases `font-size` to scale the UI, and every
  `text-[10px]`-style escape ignores it — compact density does not do what it claims on
  those sites. Pick five named steps for *this* app (`micro / body / lead / section /
  page`); a dense grid tool legitimately lives at 12–13px, so the point is five named
  decisions replacing ten anonymous pixel values, not larger text.
- [ ] **`Card` and `SectionLabel` primitives.** 141 sites hand-roll the bordered surface, 69
  the uppercase micro-label — which is *why* radius is inconsistent. Precondition for the
  type-scale and radius decisions landing consistently. `SectionLabel` should render real
  heading elements, which absorbs the heading-structure item below for free.
- [ ] **Extract a `ui/Callout` primitive.** `EmbeddingStatusBanner`, `UploadDialog`'s
  duplicate warning and others hand-roll the same banner. Distinct from `Card` — a callout
  interrupts, a card contains; they should not collapse into one primitive.
- [ ] **Just-in-time guidance restructure (Investigate panel).** Needs its own design round;
  the diagnosis is settled, the shape is not. Copy now lives in `lib/guidance.tsx`, but
  placement is the actual complaint: ~120 words of ordered list in the faintest text in the
  app, in a 320px panel, teaching Normal/Dismiss/Confirm at the one moment nothing on
  screen demonstrates it. Proposed inversion: guidance attaches to the control at the moment
  of use. The 2026-08-07 redesign narrowed this but did not close it.
- [ ] **Per-user guidance dismissal.** Collapse state lives in the `vestigo-ui` zustand
  store, so it is per-browser. The backend half exists (`User.preferences`,
  `update_user_preferences`); the work is a preferences passthrough on `PATCH /auth/me`.
- [ ] **`IconButton` primitive / the raw `<button>`s** — 122 occurrences across 46 files on
  2026-08-30 (117 of them budgeted), against 67 files that import `Button`. Burn down
  opportunistically, lowering budgets as files are cleaned.
- [ ] **Icon size scale.** Ten distinct values in use; 11 vs 12 vs 13 is drift, not a
  decision. Collapse to three (`inline` 12, `control` 16, `feature` 20) during the
  `Card`/`SectionLabel` passes.
- [ ] **`aria-live` for background work.** Two `aria-live` regions in the whole frontend
  (`ExplorerPage`, `EmbedWizard`) — and the job tray, which this item used to credit with
  the only one, is not among them. Toasts, the job tray and streaming agent output announce
  nothing. Follow the event grid's `aria-rowcount` pattern.
- [ ] **Heading structure.** 39 heading elements across 211 component files. Mostly resolved
  for free by `SectionLabel` — verify after that migration rather than scheduling it.

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic** and SQL-explainable per the forensic-reproducibility
requirement. D1–D9, `proportion_shift` and `sequence_motif` shipped — `ANOMALY_DETECTION.md`
is each detector's contract, updated in the same commit as any detector change.

Two items (D18, D19) come instead from [sktime](https://github.com/sktime/sktime)'s
`detection` module, read as a second method source only — sktime's estimators are numeric-
series ML and are not a dependency here; what is borrowed is the framing, re-derived as
SQL over already-aggregated bucket counts.

Every item below is incomplete until the frontend half lands with it: a plain-language
method explanation, the SQL/params visible on the finding, disposition + allowlist wiring.
A detector whose reasoning an analyst cannot read does not count as shipped.

**Truth of shipped claims (first):**

- [ ] **D11 — Entropy: add the bigram variant.** The shipped detector measures per-value
  Shannon entropy against a Tukey fence; AMiner's learns a character-**bigram** transition
  table and flags low mean pair probability. Ours misses the case its docs advertise most
  loudly: a lowercase-latin DGA domain among English hostnames has unremarkable Shannon
  entropy. Expressible in SQL via `ngrams(val, 2)` into a frequency map learned from the
  baseline window. Ship as a `method` on the existing detector (`shannon-iqr` | `bigram`),
  not a fifteenth tool.

**Low effort, high value:**

- [ ] **D12 — Time-of-day habit** (`PathValueTimeIntervalDetector`): per value, learn which
  times of day it occurs at in the baseline, flag suspect-window occurrences outside that
  habit. Distinct from `interval_periodicity`, which measures inter-arrival gaps. Bucket by
  `toHour`/`toMinute`, score by distance to the nearest occupied bucket. Needs an explicit
  **timezone** decision stamped into `DetectorRun.params`, or the run is not reproducible.
- [ ] **D13 — Cross-field value correlation** (`VariableCorrelationDetector`): learn which
  field-value pairs co-occur *within the same event*, flag violations. Intra-record, unlike
  D10. Reuses `GROUP BY a, b` plus the G-test and Benjamini–Hochberg pool that
  `proportion_shift` has. Field-pair explosion is the design problem: needs a preselection
  rule and a candidate cap in the `HEAVY_SCAN_SETTINGS` family, honestly reported.
- [ ] **D15 — Impossible-speed transitions** (`MinimalTransitionTimeDetector`): learn the
  minimum observed time between consecutive values of a field per identifier, flag a
  suspect-window transition faster than the baseline ever saw. `find_sequence_novelty`'s
  `lagInFrame` partitions already produce the pairs; this is a `min(dateDiff)` over the
  same shape. Score = `1 − (observed / learned_min)`.
- [ ] **D19 — Point vs. collective anomaly on bucket counts** (framing from sktime's
  `CAPA`): the temporal detectors currently answer "is this bucket unusual" one bucket at a
  time, so a sustained mild elevation across twenty buckets and a single huge spike are
  scored by the same statistic and rank alike. CAPA separates the two — a *point* anomaly
  (one bucket, large deviation) and a *collective* anomaly (a contiguous run whose joint
  cost beats a per-segment penalty). Implementable over the counts `frequency` already
  pulls: keep the existing per-bucket score, add a max-over-segments scan with a length
  penalty, report whichever wins. The finding must say which shape fired and name the
  segment bounds — that distinction is most of the analyst value.

**High effort, high value:**

- [ ] **D10 — Event correlation rules** (`EventCorrelationDetector`): mine baseline
  implication rules "A is followed by B within Δt", flag violations. `sequence_motif`'s
  recurring n-grams are the natural antecedent set. Upstream generates hypotheses randomly;
  the batch re-derivation must mine antecedents deterministically — random generation is
  not reproducible.
- [ ] **D16 — Multivariate window profiles** (`EventCountClusterDetector`): count vector per
  time bucket, compare suspect buckets against baseline by normalized Manhattan distance.
  Catches what `frequency` structurally cannot: a change in the *mix* at constant volume.
  Effort is in explainability — the finding must name the dimensions carrying the distance.
- [ ] **D18 — Proposed baseline windows via change-point detection** (framing from sktime's
  `PELT`/`BinarySegmentation`): the analyst declares the baseline window and the suspect
  windows by hand, which is the single biggest source of a wrong answer — a baseline that
  already contains the intrusion hides it, and nothing in the product says so. Run an exact
  optimal-partition change-point scan over the timeline's bucket counts and *propose*
  boundaries: "activity changes here, here and here — use the first span as baseline?".
  Stays advice, never a lock: the proposal is a prefill on the baseline picker
  (`ToolsSheet` › Scope), the analyst can ignore or edit every boundary, and what they
  finally ran is what `DetectorRun` records. Segment-mean cost with an explicit penalty
  term, both stamped into params; over counts, not events, so it is cheap and the SQL that
  produced the counts is quotable on the proposal.

**Low effort, low value:**

- [ ] **D17 — New field key** (`NewMatchPathDetector`): flag attribute *keys* new to a
  suspect window. Trivial (`arrayJoin` + set difference) and marginal — for most sources a
  new key means a converter update, not an intrusion. Build it when touching field inventory.

**Skipped deliberately:** `TSAArimaDetector`/`PathArimaDetector` (the z-score `frequency`
detector covers most of it and stays explainable); `PCADetector` (output is a reconstruction
error in a rotated space no analyst can trace back to events — it fails the explainability
requirement by construction; revisit if D16 ships and misses correlated multi-field drift);
`HistogramAnalysis`/`ParserCount` (descriptive statistics, already served); every
`learn_mode`/persistence mechanism (replaced wholesale by analyst-declared baseline
definitions — the core adaptation, and why no detector carries hidden state between runs).
From sktime, everything except D18/D19: its segmentation, HMM, foundation-model and PyOD-
adapter estimators all take a chosen numeric series, so they answer neither "which field"
nor "which value" — the two questions a log finding has to answer — and none of them are
field-agnostic in this codebase's sense.

## Milestone 5 — post-mortem workflow depth

- [ ] **W5 residue — Sigma `logsource` scoping.** The runner shipped, but rules always run
  over the full timeline scope; `logsource` is parsed and displayed for manual selection
  only. Unscoped rules match log types they were never written for — a precision defect.
- [ ] **X1 residue — case export/import follow-ups.** From the PR #182 review: a failed
  import records no audit row while a failed export does; `_insert_source_events` lets an
  untrusted Arrow stream size its own record batches, bounded only by the 200 GiB total cap
  and not per batch; the frontend buffers a whole archive in memory instead of navigating to
  the URL; orphan `events/*.arrow` members are skipped silently while orphan blobs warn;
  `ImportCaseDialog` doesn't reset state on reopen; adding an `_IMPORT_SPECS` entity breaks
  reading current-format archives without a `FORMAT_VERSION` bump — decide
  missing-stem-as-empty vs. mandatory bump and write it down.
- [ ] **W8 — Query-time field extraction (schema-on-read).** Define a virtual field as a
  regex capture over a raw attribute, then facet, histogram and run detectors on it without
  re-ingest — Splunk `rex` / ES runtime fields, but forensically cleaner: the pattern is
  declared, auditable metadata and raw events stay untouched. Via ClickHouse
  `extractGroups()`, consumed through the existing `_col_expr` mechanism. Prerequisite for
  making bespoke unstructured logs first-class.

## Milestone 6 — streaming ingest ("live forensic" mode, agentless)

Decided 2026-07-14: no bespoke endpoint agent. Vestigo accepts pushed batches from *existing*
collectors (Velociraptor, fluent-bit, winlogbeat) over an authenticated endpoint, and the
Explorer follows the stream by polling.

- [ ] **S1 — Stream-source data model.** New Source kind `stream`: no final file hash;
  instead an append-only, hash-chained per-batch manifest preserving the attestation story.
  Touches `CONCEPT.md`/`MODEL_REFINEMENT.md`, migrations, the `(case_id, file_hash)` dedup
  uniqueness, and source UI. Design-first — the real cost of the milestone.
- [ ] **S2 — Push ingest endpoint.** Per-source ingest token (not session cookie), rate
  limiting/backpressure, JSONL and Arrow IPC batches — the Arrow path into ClickHouse
  already exists. Plain HTTP POST first; Arrow Flight optional later.
- [ ] **S3 — Live Explorer.** `refetchInterval` polling on grid + histogram for stream
  sources; WebSocket push deliberately skipped.
- [ ] **S4 — Detectors on open-ended data.** Periodic re-runs; rethink value-novelty
  "first seen" and baseline-window semantics for unbounded sources.

## Milestone 7 — forensic examination expansion (decided 2026-07-16)

Expand beyond log investigation into forensic examination, with artifacts analyzed as
**time-annotated items**. Parsing stays permanently out of core scope — external
Parquet-interchange converters handle it.

**E1 is a go/no-go gate, not a foregone build.** It redefines the product's scope and forces
a vocabulary refactor of shipped columns; S1 is designed in the same round.

**Vocabulary decision: Artifact = a file** — both a logfile that gets ingested and a file on
an examined filesystem. This redefines the current per-event `artifact`/`artifact_long`
meaning (Plaso type strings), which need renaming to a type/kind concept as part of E1.

```
Source (evidence unit, hashed)
  └── Artifact (N)     ← entity: a file — kind, path, content_hash, size, attributes
        └── Event (M)  ← time annotation: timestamp + normalized role (MACB, visited, run, …)
```

- [ ] **E1 — Model design doc.** Amend `MODEL_REFINEMENT.md` before any code: the Artifact
  entity, the rename, and a closed timestamp-role taxonomy replacing free-text
  `timestamp_desc`. Artifact identity must be converter-stamped and deterministic, never
  derived by query-time grouping; log lines must degrade gracefully.
- [ ] **E2 — Parquet interchange v2.** Separate `artifacts` + `events` streams, deterministic
  artifact IDs, versioned footer, content blobs as a content-addressed sha256 sidecar
  (selective, not full images). Keep v1 readable. Pilot converter: MFT/`fls` → MACB events.
- [ ] **E3 — Storage + query layer.** ClickHouse `artifacts` table; events gain
  `artifact_id`; Explorer pivot (file list with M/A/C/B columns). Hierarchy via materialized
  `path` + `parent_artifact_id` — no graph store, no new backing services.
- [ ] **E4 — Artifact detail UI.** Blob store (generalizing content-addressed source
  retention) + hex/text/image viewers. Also where N3's extracted HTTP payloads land. Shared
  threat model: RBAC on blob reads, quotas, inclusion in `transfer/` export, and a preview
  endpoint that never serves stored bytes as `text/html`.
- [ ] **E5 — Examination extras.** Hashsets (NSRL join on `content_hash` via a ClickHouse
  dictionary), image gallery, content keyword search (extracted-text column + tokenbf index).

Carries over unchanged: the provenance chain, detectors/embeddings/Sigma and schema-on-read
gaining the new domain for free, auth/RBAC/audit as chain-of-custody baseline. The in-memory
JobStore stays — heavy work lives in converters.

## Milestone 8 — AI investigation agent expansion

Agent v1 and v2 shipped 2026-07-19/20; context management was reworked in 1.5.0 (a sliding
window replaced compaction + fidelity ladder). See `AGENT.md`.

- [ ] **A12 — Local transform tools (CyberChef-class).** Decode/encode, hashing,
  decompression, timestamp conversion as **native tools** in `agent/tools.py` — a curated,
  append-only op set, not a call-out to a CyberChef server. Pure local computation, hence
  reproducible and offline-safe, no OPSEC gate. Care points: decompression bombs, output
  size vs. context budget, and keeping the op set append-only so old conversations replay.
  Ships independently of and before A8.
- [ ] **A8 — External MCP toolsets (web research / OSINT).** Do NOT build bespoke whois/web
  tools or a plugin API: the runtime is pydantic-ai with MCP toolsets, so let the agent
  consume operator-configured **external MCP servers** — zero Vestigo code per tool,
  symmetric with our own `/mcp` exposure. Feasibility confirmed; the work is policy. Needs
  its own design round. Hard requirements:
  - **OPSEC gate.** Outbound lookups leak case indicators to third parties — an internal
    hostname sent to a search provider can tip off an adversary. Gate behind
    `VESTIGO_ALLOW_ONLINE` **and** per-case opt-in, default off.
  - **Forensic capture.** Audit every external call; persist and hash the raw response with
    its timestamp (external results drift — OSINT enrichment with provenance, never
    evidence); mark results `origin: external`.
  - **Governance reuse.** External tools enter the registry surfacing so the three deny
    layers and the tool-selector popover apply uniformly.
  - **Disclosure.** Extend the OPSEC panel to name enabled network tools and endpoints.
  - **Doc.** Update `AGENT.md`'s sandbox invariant, which external tools genuinely widen.

## Milestone 9 — network evidence depth (analyst feedback 2026-08-05)

N1 (ASN enricher) and N2 (`pcap2vestigo --reassemble http`) shipped in 1.10.0. One decision
N3 inherits: the response status is spelled **`status_code`**, not `http_status` — that is
what `nginx2vestigo` emits, and the point is that a pcap timeline and a webserver-log
timeline filter identically.

- [ ] **N3 — HTTP payload: hashes in the timeline, bytes on the analyst's disk.** Three
  deliberately separated tiers:
  1. *Always:* cheap metadata (`http_host`, `http_content_type`, `http_content_length`,
     `http_user_agent`, `http_referer`) plus `http_response_body_sha256`. The full-body hash
     is the forensically valuable part, costs 32 bytes, and allows pivoting against
     known-file lists without the bytes entering the database.
  2. *Opt-in, capped:* `--max-body-bytes`, text-ish media types only, explicit truncation
     marker.
  3. *Opt-in, local:* `--extract-files DIR` writes bodies to the analyst's disk named by
     sha256. No server change, and it keeps malware samples out of ClickHouse and out of
     `.vestigo` exports.

  **Why bodies never go in `attributes`:** `search_blob` is a MATERIALIZED concat of
  `mapValues(attributes)` with an ngram bloom index over it. Base64 bodies would poison that
  index's selectivity for *every* query on the timeline, not only rows carrying bodies. That
  constraint decides the design. **Policy, not only storage:** bodies carry credentials,
  PII and malware, and a `.vestigo` export leaves the box — capture defaults off and
  `--help` says what it captures. The server-side half (inline rendering, per-file download)
  needs E4's blob store and arrives with that milestone.

## Standing decisions (with revisit triggers)

Decisions, not work items — each stays as decided unless its trigger fires.

- **Generated-converter guard stays stdlib-only.** rlimits, `python -I`, a scrubbed
  environment, a private cwd and an AST deny-list — no bwrap/firejail/container, so the
  reference uv and image deployments keep working (2026-08-17). Trigger: a report of a
  script escaping the guard in a way rlimits and the deny-list could not have stopped.
- **`mcp` stays pinned below 2.0** (2026-08-29). The chain is `pydantic-ai-slim[mcp]` ->
  `fastmcp-slim[client]` -> `mcp`, and the `<2.0` cap is fastmcp-slim 3.x's — not
  pydantic-ai's, which already allows `fastmcp-slim<5`. So no amount of rewriting our own
  code makes mcp 2.x resolvable. fastmcp-slim 4.0.0b5 requires `mcp>=2.0`, so **4.0 going
  stable is the trigger**, and it forces the move rather than merely allowing it. Our side is
  small and surveyed: `FastMCP` -> `mcp.server.mcpserver.MCPServer`, and
  `server.settings.{stateless_http,streamable_http_path,transport_security}` become
  `streamable_http_app()` kwargs; every internal `agent/tools.py` reaches for is unchanged.
  `tests/test_dependency_guards.py` fails the moment the cap lifts and names the steps, so
  this does not depend on anyone rereading this file.
- **Persistent job store** — in-memory is deliberate for the single-process deployment
  model, not an oversight. Trigger: multi-process scale-out, which also needs the event bus,
  login backoff and column-recommendation liveness moved to a shared backend.
- **CSRF tokens** — SameSite=Lax is adequate for a self-hosted instance on a trusted
  network. Trigger: exposure to the open internet, or moving off a single app process.
- **Bespoke endpoint collection agent** (2026-07-14) — a collector fleet is a whole product
  (Velociraptor, osquery). Vestigo stays agentless (Milestone 6).
- **Vendored converter ports stay demand-driven.** The `*2timesketch` scripts are a
  permanent minimal-dependency alternative (stdlib-only, no pyarrow), not a porting queue
  (2026-07-20). `evtx2timesketch` stays for *text* exports; binary `.evtx` has a native
  converter — different inputs, not the same one twice.
- **Ship a stock Windows `vestigo-fieldmap.yml`.** `evtx2vestigo` emits Sigma-canonical
  names, so Windows rules resolve correctly but stay flagged in `fallback_fields`. Measured
  over SigmaHQ `rules/windows/builtin` (326 rules): 873 flags → 0, zero SQL and match-count
  differences, from 141 identity entries. The open question is *delivery*, not feasibility —
  the ruleset directory is operator-supplied, so it needs shipping as a downloadable asset
  or a documented snippet.
- **`evtx2vestigo` deferred items.** `.evtx.gz` input (decompressing a hundreds-of-MB log
  costs whole-file RAM); `%%1833`-style message-table resolution (needs the originating
  host's WEVT templates); EvtxECmd PayloadData slot-order parity (we emit each mapped
  property as its own attribute, which is strictly more information). Trigger for the first:
  someone hands us a compressed triage collection.
- **Converter parallelism tuning is revisit-on-demand.** Worker-count defaults, parallel
  `.gz` parsing, and pcap/CSV intra-file chunking (a logical CSV record can span physical
  lines, so newline-chunking is unsafe) are deferred. Trigger: a slow-converter report.
- **Sigma runner has no live-ClickHouse end-to-end test.** Unit tests cover
  compiler/loader/router; the live path is exercised manually. Trigger: a regression escapes.
- **Story artifact upload stays off the progress-reporting transfer path.** It posts
  rendered HTML as a JSON body field capped at 20 MiB, so it was left out of the XHR
  rollout. Trigger: the cap rises, or artifacts carry embedded evidence.
- **`list_fields_by_artifact` stays a live scan.** The field-stats cache covers the other
  inventory paths; the embedding wizard's cost is its randomized per-artifact sampling,
  which caching would not save. Trigger: wizard latency complaints.
- **`canonical_inventory` stays a live query.** It only runs when a timeline has field
  mappings, which the 300M-row reference case does not. Trigger: a mapped timeline at that
  scale measures slow — then add the planned Postgres cache.
- **The two time-histogram implementations stay separate.** Only the brush gesture is
  shared; `TimelineHistogram` carries Explorer-only concerns that make a merge
  high-risk/low-payoff. Trigger: the two drift apart.
- **react-router stays on the 7.x line; GHSA-qwww-vcr4-c8h2 is patched there** (revised
  2026-07-29). The fix shipped in 8.3.0 and was backported to **7.18.2**, which is what
  Vestigo runs. The v8 line dropped `react-router-dom`, so migrating means 41 import changes
  for no security benefit. **Expect the alert to persist** — GitHub and npm still range the
  advisory `>= 7.12.0, < 8.3.0` without carving out 7.18.2. Dismiss as "fix already
  applied", and never `npm audit fix --force`: it *downgrades* to 7.11.0, giving up seven
  minors of fixes to step below the range. Not silenced via `dependabot.yml` either —
  ignoring `< 8.3.0` would suppress real 7.x patches, which is how 7.18.2 would have been
  missed. Independently, the vulnerable surface is an unstable RSC API and Vestigo is a SPA
  with zero `unstable_*` imports. Triggers: we migrate imports for another reason, or RSC
  APIs are adopted — re-evaluate immediately in that second case.
- **`diskcache` GHSA-w8v5-vhqr-4h9v / CVE-2025-69872 needs no action** (2026-07-29). Unsafe
  pickle deserialization, no patch exists. It reaches us as `pysigma → diskcache`, used by
  exactly two pysigma modules Vestigo never imports — verified empirically: constructing the
  app leaves `diskcache` and both modules absent from `sys.modules`. The attack also needs
  write access to `~/.cache/pysigma/`, which implies code execution as that user. Dismiss as
  "vulnerable code not used". Separately: those two modules `urlopen` MITRE data from
  GitHub, so pulling them in would be an airgap violation — a second reason to keep them out.
- **Python client library.** REST API + CLI exist; a thin typed client for Jupyter/pandas
  workflows is cheap. Trigger: a user asks.
- **`/api/auth/users` full-directory listing** assumes every authenticated user may know who
  else has an account. Trigger: a deployment where the directory is itself sensitive —
  compartmented investigations, or several groups sharing an instance — then add a config
  flag or scope the listing to co-case members.
- **Confirm-proposal crash gap** — a crash between the atomic proposal-decide and the
  annotation bulk-write leaves a confirmed proposal with no annotations and no retry path.
  A deliberate single-process tradeoff. Trigger: it bites in practice.
