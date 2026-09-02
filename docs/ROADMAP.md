# Vestigo Roadmap — open backlog

The only open backlog. Shipped work lives in `PROGRESS.md`, `CHANGELOG.md` and the feature
docs (`ANOMALY_DETECTION.md`, `AGENT.md`, `STORIES.md`). Reported defects live as GitHub
issues; root-cause detail stays in the issue thread.

**State (verified 2026-09-01, v1.18.1):** one open issue (#307, folded into Milestone 10 as
G1). Phase 3 is complete and the queue is feature-shaped. **Milestone 10 — AI agent log
investigation — is the 2.0 thrust and outranks everything below**; the numbered list orders
the remaining 1.x work by payoff-per-effort:

1. **D11** entropy bigram variant — closes a capability gap the docs used to overclaim;
   truth of what we ship outranks new surface.
2. **A12** local transform tools — no design round, no OPSEC gate.
3. **D12** / **D13** / **D15** — cheap detectors reusing existing SQL machinery.
4. **W8** query-time field extraction — makes bespoke unstructured logs first-class.
5. **A8** external MCP toolsets — needs its own design round (policy, not plumbing).
6. **D10** / **D16** — heaviest lifts, last of the detector line.
7. **Milestone 11** external processors — P1 (the protocol doc) gates the rest; the
   Hayabusa engine half lives in `overcuriousity/hayabusa-processor`.

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
- [ ] **A fourth `scan_budget` risk for an unusably small per-query cap.** `risk` only
  compares `total_bytes + cache_bytes` against the ceiling, so a large
  `VESTIGO_STAT_SCAN_CONCURRENCY` reports `ok` while every slice is too small to scan with —
  N=10 on the reference ceiling leaves 409.6 MiB and fails the enrichment partition rewrite
  with `MEMORY_LIMIT_EXCEEDED` (session-223). Needs a floor to compare against (the rewrite
  is the binding query, not a detector GROUP BY) and copy naming the ceiling, not N, as the
  remedy. Documented in `docs/DEPLOYMENT.md` "The N trap" until then.
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
  filed, 1549 today.
- [ ] **Split `api/routers/events.py`** along the read/aggregate/export seams — 3100 lines
  on 2026-07-20, 3319 on 2026-07-29, growing without anyone touching it deliberately.
- [ ] **README screenshot grid.** The README is laid out for a 2×2 grid but ships one
  Explorer shot. Capture at one window size: Analysis with findings and the Method panel,
  a Story with a live view embed, the Agent with an applied finding, a re-shot Explorer.

### Frontend design-system consistency (audit 2026-07-30)

Root cause: **the design system stops at colour.** Colour is tokenized and disciplined;
type, spacing, radius, icon size and surface treatment have no token layer and no
primitive, so every author re-decides at the call site.

The ratchet exists — `frontend/src/test/designSystem.test.ts`. Undefined `var(--…)` is a
hard check at zero; arbitrary `text-[Npx]` and raw `<button>` outside `components/ui/` are
budgeted per file in `designSystemBudget.ts`, seeded at 119 each. The budget only falls:
exceeding an entry fails, and so does *beating* one without lowering it. **Every item below
burns its numbers out of that file**; the migration is done when the file is `{}`.

- [ ] **Type scale in `@theme`, and burn down the 118 arbitrary font sizes.** A correctness
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
- [ ] **`IconButton` primitive / the 119 raw `<button>`s** across 46 files, against 57 that
  import `Button`. Burn down opportunistically, lowering budgets as files are cleaned.
- [ ] **Icon size scale.** Ten distinct values in use; 11 vs 12 vs 13 is drift, not a
  decision. Collapse to three (`inline` 12, `control` 16, `feature` 20) during the
  `Card`/`SectionLabel` passes.
- [ ] **`aria-live` for background work.** Exactly one `aria-live` in the frontend: the job
  tray, toasts and streaming agent output announce nothing. Follow the event grid's
  `aria-rowcount` pattern.
- [ ] **Heading structure.** 39 heading elements across 211 component files. Mostly resolved
  for free by `SectionLabel` — verify after that migration rather than scheduling it.

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic** and SQL-explainable per the forensic-reproducibility
requirement. D1–D9, `proportion_shift` and `sequence_motif` shipped — `ANOMALY_DETECTION.md`
is each detector's contract, updated in the same commit as any detector change.

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
- [ ] **A13 — Agent similarity tools gain reranking.** When V2 (Milestone 10) ships, the
  agent's semantic-similarity tools pass through the same two-stage retrieval as the
  Explorer, so the model and the analyst rank results identically — a divergence there
  would make agent citations unreproducible from the UI. Blocked on V2; near-zero work
  once it lands (`similarity.py` is the shared path).
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

## Milestone 10 — AI agent log investigation (the v2.0 USP)

Decided 2026-09-01. The August 2026 AISI incident report — one anomaly, 122 transcripts,
212,840 messages reviewed *by hand* — names the gap: agent harnesses log in proprietary
formats, the interesting behaviours are cross-log, and no forensic tool owns the problem.
Observability platforms (Langfuse, LangSmith, Arize) monitor *your own* agents in
production; nothing does *forensics on agent logs as evidence* — provenance-hashed,
offline, cross-run. Vestigo already has the skeleton: event-granular provenance, immutable
sources, baselines/dispositions, Sigma, Stories. **v2.0 makes this the primary use case
and aims to be the best tool in the world at it.**

Two tracks. G is the investigation surface; V is the vector subsystem it leans on,
redesigned. Order across tracks: **G1 → G2 ∥ V1 → G3 → V2/V3 → G4 → G5/G6**.

### G — investigation surface

- [ ] **G1 — Agent-telemetry converters** (= issue #307, the contract for this item).
  Tier 1: OTel GenAI semantic conventions (OTLP/JSON spans + message log events) and
  OpenInference exports. Tier 2 (demand-driven): Claude Code session JSONL, OpenAI Agents
  SDK traces, Wintermute audit export. One event per span with start/end preserved,
  prompt/completion messages as their own rows, `trace_id`/`span_id`/`parent_span_id`
  retained so call trees survive, token counts and durations as typed numerics. Raw
  payloads hashed, parser config hashed into identity — same provenance as every
  converter. No embeddings involved: statistical detectors and Sigma get purchase from
  day one. The field-naming decisions made here (span kind, tool name, role, session id)
  are load-bearing for G2–G5 — write them into `INPUT_FORMATS.md` as the agent-telemetry
  vocabulary, the way `status_code` was decided once for N-track.
- [ ] **G2 — Session and trace surface in the Explorer.** Supersedes #307's "no dedicated
  UI mode" — a generic event grid cannot be the world's best way to read a conversation.
  Two views, reached from any agent-telemetry event row: a **conversation reader**
  (turn-by-turn, role-distinguished, tool calls collapsible, long content expandable
  in place) and a **call-tree pivot** (orchestrator → sub-agent → tool from parent
  linkage, with per-node duration and token rollups). Both are projections of the event
  grid's data — same filters, same provenance chips, no second query system. Needs G1
  only, and it is the USP demo moment; design round decides whether it lives as a third
  Explorer mode or a sheet, and must handle the 200k-message session without loading it.
- [ ] **G3 — Detection pack for agent telemetry.** Mostly reuse, verified against the
  shipped detectors: tool-call novelty and bursts are `value_novelty` + `frequency` over
  the G1 fields; token/duration outliers are the numeric detectors; impossible tool
  sequences fit `sequence_motif`/D15 shapes partitioned by session id. Genuinely new:
  **cross-run joins** — the same external artifact (URL, account, repo) appearing in
  independent traces, the AISI coordination signature, buildable as SQL over typed
  attributes — and a **curated Sigma ruleset** for prompt-injection patterns in inbound
  tool results and credential-shaped strings in outbound messages, routed via the
  existing `logsource` machinery once G1 fixes the field names. Ships with detector docs
  in `ANOMALY_DETECTION.md` per the standing rule.
- [ ] **G4 — Semantic investigation flow** (needs V1; better with V2). Where embeddings
  earn their keep for the first time: agent messages are natural language, and the
  questions are paraphrase-shaped ("everywhere the agent discussed credentials",
  injection families that mutate wording). Turn-level embedding defaults that embed
  message *content* and never the JSON envelope — verify `field_recommend.py`'s cohesion
  scoring picks the G1 content fields and pin it; semantic search + rerank as the primary
  triage gesture on agent timelines; session-level clustering to group recurring
  injection families across runs. All of it is a *search and triage* surface under the
  standing decision below — results name their neighbors, findings stay statistical.
- [ ] **G5 — Agent-incident demo case.** A fabricated AISI-style scenario through
  `demo/`: a multi-session agent trace with an injection, a credential egress and a
  cross-run coordination artifact. Every G3 detection must fire on it, enforced by the
  existing demo-coverage test; the demo continues to require no embeddings (semantic
  stays optional), which keeps the coverage test honest about what the stock install
  detects.
- [ ] **G6 — Positioning.** README and `CONCEPT.md` §8 gain the third comparison axis:
  observability platforms do monitoring of your own agents; Vestigo does forensics on
  evidence. Tone rule applies in full — no claims about their current feature sets
  nobody verified, and AMiner stays uninvolved.

### V — vector subsystem redesign

- [ ] **V1 — Endpoint-only embeddings; EmbeddingGemma as the reference default.** Remove
  the local inference path entirely: the `embeddings` extra (torch +
  sentence-transformers), the local branch of `models/embeddings.py`,
  `embedding_device`, the `INSTALL_EMBEDDINGS` build arg, the airgap `--embeddings`
  flag, and the weights probe PR #339 added (that PR documents its own retirement here).
  The OpenAI-compatible `/embeddings` endpoint becomes the only path; capability =
  switch + configured endpoint + one-token probe, one arm gone. Docs name
  **EmbeddingGemma-300M served via llama.cpp/Ollama/vLLM** as the reference deployment —
  the airgap story becomes "run the endpoint on your own network", which is strictly
  simpler than shipping weights, and MiniLM remains servable the same way so nobody is
  stranded. No data migration: collections are keyed by `embedding_config_hash`, so
  existing local-embedded collections go stale and re-embed against the endpoint like
  any config change.
- [ ] **V2 — Reranking.** Two-stage retrieval: Qdrant top-k (~50) → a `/rerank` endpoint
  → top-n. There is no OpenAI-standard rerank API; adopt the de-facto
  Cohere/TEI/Infinity-compatible shape and probe it the way the agent endpoint is
  probed (TTL cache, fingerprint, stale-while-revalidate). New optional settings +
  capability; wired once into `db/similarity.py` so the Explorer semantic mode and the
  agent tools (A13) inherit it together. Degrades to vector order when unconfigured —
  reranking is an upgrade, never a requirement.
- [ ] **V3 — Qdrant modernization.** Matryoshka output dimension (768/512/256/128) as an
  `EmbeddingConfig` field — hash-covered, so dimension changes keep the identity
  semantics; scalar quantization for large cases; payload indexes on session id and
  role so semantic search filters *within* a session or to one side of the
  conversation. One design question flagged honestly for the round rather than assumed:
  hybrid sparse+dense (Qdrant-native BM25/SPLADE) versus leaving keyword search to
  ClickHouse `search_blob` — the wrong answer duplicates a query system.

## Milestone 11 — external enrichment processors, first consumer Hayabusa (designed 2026-09-02)

Windows EVTX signature detection with [Hayabusa](https://github.com/Yamato-Security/hayabusa)
arrives as an **enricher whose columns feed the statistical detectors** — not a fifteenth
detector, not a Timesketch-style imported timeline (standing decision below). The Hayabusa
half is a **separate, optional component in its own repository**,
[`overcuriousity/hayabusa-processor`](https://github.com/overcuriousity/hayabusa-processor).
Vestigo core gains only the generic, processor-agnostic wiring: an HTTP protocol any external
processor can implement, and one enricher class built from a processor's manifest. No
Hayabusa-specific code lands in this repository.

Flow: the analyst opens the enrichers dialog on the timeline screen, picks the processor,
uploads the `.evtx` files, and runs it. Vestigo forwards the files to the processor, the
processor runs the engine, Vestigo joins the result rows onto the timeline's events and
writes the columns back through the existing staging and partition rewrite.

Verified on 2026-09-02, and load-bearing for every item:

- **Registration exists and stays a module-level list** (`enrichers/registry.py`). What is
  missing is the contract: `Enricher` is value-shaped (regex over attribute *values*,
  `enrich_value(raw_value)`, memoized per value); a processor is record-shaped (one verdict
  per `(file, record)` from an external input). Everything from staging on is reusable
  unchanged — rows are staged per `event_id` with a `fields` map, applied by `mapUpdate`
  over a LEFT JOIN in one `REPLACE PARTITION`, `SourceEnrichment` records the config hash.
- **Vestigo does not hold the `.evtx`.** `evtx2vestigo` runs on the analyst's machine and
  the retained source is its Parquet, so the engine input is a new upload. Every event row
  carries `attributes['evtx_record_id']`, `events.source_file` (the `.evtx` basename) and
  `events.file_hash` (sha256 of that `.evtx`), plus `Channel` and `Computer` — the join key.
- **Rebuilding the engine's JSON input from ClickHouse rows is lossy** (Hayabusa
  `eventkey_alias.txt` against the converter): System and EventData fields round-trip,
  UserData does not — the converter flattens to `UserData_<Container>_<Leaf>` with `_` and
  Hayabusa's container names contain `_` themselves. Hence the raw `.evtx` upload.
- **Hayabusa correlation rules** (`event_count`, `value_count`, `temporal`,
  `temporal_ordered`) emit `RecordID` `-` and cannot join to a row.

- [ ] **P1 — Processor protocol `v1`.** Written down in a new `docs/ENRICHERS.md` before code
  (the enrichers subsystem has no reference doc today; this becomes it). HTTP, bearer token
  shared per processor, versioned path `/v1/`:
  - `GET /v1/manifest` → processor `key`, `display_name`, `description`, processor
    `version`, `engine` (`name`, `version`, `rules_hash`), `config_hash`, `input`
    (`accepted_extensions`, `multiple`), `join` (ordered list of result column → event
    column or attribute key; Hayabusa: `source_file` → `events.source_file`, `record_id` →
    `attributes['evtx_record_id']`, `channel` → `attributes['Channel']` as a check),
    `output_fields` (each with `name`, `kind` scalar|set|numeric, `sentinel`, `series`
    bool), optional `findings` (`level_field`, `rule_field`, `rule_id_field`).
  - `POST /v1/runs` multipart files → `202 {run_id}`; `GET /v1/runs/{id}` →
    `status` queued|running|completed|failed, `progress`, `warnings[]`, `error`;
    `GET /v1/runs/{id}/result` JSONL, one row `{"join": {...}, "fields": {...}}`, rows with
    `"join": null` carry `"window": {start, end}` and `fields` (non-record hits);
    `DELETE /v1/runs/{id}`.
  - Reachability is operator-configured (localhost or LAN) and independent of
    `VESTIGO_ALLOW_ONLINE`, like PostgreSQL — document that in `TECH_STACK.md` §6.
- [ ] **P2 — `ExternalProcessorEnricher`.** One generic class in `enrichers/external.py`
  built from a manifest; registered at startup from a new `external_processors` setting
  (list of `{url, token}`, env-only for the token, `SettingSpec` in the registry). Availability
  = manifest reachable and protocol version supported, re-checked through
  `refresh_availability` like an asset upload. Eligibility = every join attribute key
  present on at least one event (`mapContains` existence scan, same 3 s fail-open cap as
  the regex scan). `config_hash` = the manifest's `config_hash` plus the input file hashes.
  `output_fields` from the manifest; derived keys keep the `<attr_key>:<field>` contract
  with the first join attribute as parent (`evtx_record_id:hayabusa_rule`), so
  `derived_suffixes`, Explorer sorting and the re-run skip rule need no change.
  `finalize_enrichment_apply`'s suffix-uniqueness rule applies: refuse to register a
  processor whose suffixes collide with another enricher's.
- [ ] **P3 — Input upload and retention.** The enrichers dialog renders a file picker when
  the manifest declares `input`; the run route becomes multipart. Files are retained
  content-addressed under `source_retention_path/enricher-inputs/<sha256>`, size-capped by
  `max_upload_bytes`, audited (`enricher.input`), included in `transfer/` export and
  import. They are inputs, not Sources: no events derive from them directly, the Parquet
  did that. Re-running with the same files and the same manifest `config_hash` is a no-op
  by provenance, exactly as GeoIP today.
- [ ] **P4 — The join and its failure modes.** Load the result rows into a dict keyed by the
  join tuple (hits only, small), stream the source's events through `iter_source_events`,
  emit staged rows on match. Every outcome counted per source and written to the job
  result and `SourceEnrichment.detail`: `joined`, `ambiguous` (two events for one key —
  a triage directory can hold two `Security.evtx`, so basename alone can collide; the
  check column decides, else neither is applied), `unmatched` (result row with no
  event), `windows` (rows with `join: null`). Zero joins fails the run: the files were
  made over different evidence. `SourceEnrichment` gains a `detail` JSON column (engine
  version, rules hash, input hashes, counts) — the engine version and ruleset are what
  make a rerun in March the same run as one in January.
- [ ] **P5 — Sentinel and series declaration.** `finalize_enrichment_apply` takes a
  `defaults` map from the manifest's sentinels and applies it where the LEFT JOIN misses;
  the rewrite reads the whole partition anyway, so unmatched rows cost no staging. With
  `no_detection` on every unmatched row the column is 100 % covered, classifies
  `categorical`, and proportion shift gets a correct denominator — an empty value would
  land it under the 5 % sparse rule and out of every auto-selection. Fields declared
  `series: false` (the `;`-joined sets) are skipped by value-novelty and sequence
  auto-selection through a registry-fed exclusion beside `_SYNTHETIC_FIELDS`, not inside
  it (that set means "stamped by the pipeline"); explicit `fields=` still takes them.
  Document both in `ANOMALY_DETECTION.md`'s field-classification section.
- [ ] **P6 — Findings projection, second phase.** When the manifest carries `findings`, the
  rail gets an entry under *Named techniques* that reads the columns back (one finding per
  `(rule, source)`, representative event at the highest level, score from the level) plus
  the window rows from P4 as window findings with no event anchor and a disclosed count.
  Dispositions reuse the Sigma runner's pattern (system annotations, `detector` = rule id,
  `list_confirmed_keys` preserved on re-apply). MITRE tactics land as `origin: system`
  tags for Explorer filtering — a projection of the columns, never the record of truth.
  Plan gate: `not_applicable` unless a processor is registered and a source in scope
  carries the join keys; `reason_facts` names which is missing.
- [ ] **P7 — Tests.** A fake processor (FastAPI app in `tests/`, no engine) serving a
  manifest and a canned result over the demo case or an EVTX-shaped fixture: join counts,
  ambiguity refusal, sentinel coverage, series exclusion, provenance detail, export/import
  round-trip of the retained inputs, and the capability predicate. No Hayabusa binary in
  this repository's CI, ever.

**Not planned here:** importing Hayabusa CSV/JSONL as a Source (standing decision);
`metrics`/`logon-summary`/`computer-metrics` (already computed over ClickHouse);
`pivot-keywords-list`; automatic de-duplication against the Sigma runner — community rules
exist in both engines, so the same rule can surface twice on an EVTX timeline until W5
logsource scoping lands. A CLI path: enrichment runs through the web interface only.

## Standing decisions (with revisit triggers)

Decisions, not work items — each stays as decided unless its trigger fires.

- **Engine output is enrichment, never a Source and never a detector of its own**
  (2026-09-02, Milestone 11). Timesketch's Hayabusa integration is a CSV profile uploaded
  as a timeline: rows that are verdicts, with no `content_hash`/`byte_offset` into
  evidence. Ingesting that here would give derived data the provenance columns of
  evidence. As a standalone detector it would produce a flat alert list; as columns, the
  primary-rule field becomes an n-gram, cadence and proportion-shift series for free.
  Engines live in their own repositories behind the processor protocol; Vestigo ships the
  wiring, not the engine. Trigger: a user whose only artifact is an engine export from a
  pipeline (Velociraptor, LimaCharlie) with no `.evtx` reachable — then design a clearly
  labelled derived-source kind, not a quiet import.
- **Embeddings are a triage/relevance layer, never a scored detector** (2026-09-01, with
  Milestone 10). The reason PCA was skipped applies with full force: a cosine distance is
  not an explanation an analyst can defend. Semantic surfaces return neighbors and name
  their exemplars; anything that produces a *finding* with a disposition stays SQL-
  explainable. Trigger: an embedding-native signal proves itself with an explanation
  story as strong as the statistical detectors' — then design it as its own round, not
  as a quiet promotion of search results to findings.
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
