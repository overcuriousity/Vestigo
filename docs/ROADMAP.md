# Vestigo Roadmap — open backlog

The only open backlog. Shipped work lives in `docs/PROGRESS.md` and the feature docs
(`ANOMALY_DETECTION.md`, `AGENT.md`, `STORIES.md`); Phase 1 is archived in
[`archive/ROADMAP_PHASE1.md`](./archive/ROADMAP_PHASE1.md). Per-PR review findings that
outgrow a couple of lines here go to `archive/PR{N}_REVIEW_FINDINGS.md`. Reported defects
live as GitHub issues; when any are open they get an "Open defects" section here with
issue numbers, and root-cause detail stays in the issue thread.

**State (verified against the codebase 2026-07-30):** open issues are the #206 1.8.6
umbrella and its sub-issues; D14 shipped on `release/1.8.6`. Phase 3 is complete, so the
queue is feature-shaped.

**Priority order,** roughly by payoff-per-effort:

1. **D11** entropy bigram variant — closes a capability gap the shipped docs
   used to overclaim; truth of what we ship outranks new surface. (D14, the other
   truth-of-claims item, shipped in 1.8.6.)
2. **A12** local transform tools — no design round needed, no OPSEC gate.
3. **D12** time-of-day habit, **D13** cross-field correlation, **D15** impossible-speed
   transitions — cheap detectors reusing existing SQL machinery, high forensic payoff.
4. **W8** query-time field extraction — makes bespoke unstructured logs first-class.
5. **A8** external MCP toolsets — needs its own design round (policy, not plumbing).
6. **D10** correlation rules, **D16** multivariate window profiles — heaviest lifts, last of
   the detector line.

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

- [ ] **Show env-pinned settings as pinned in the admin console.** A field the operator
  set in the environment silently wins over the stored override (`core/config.py::
  env_pinned`), so an admin can flip a toggle, get a 200, and see nothing change —
  `VESTIGO_OIDC_ENABLED=false` left in a `.env` is the case that actually bit someone
  (session 138). `runtime_settings._usable_overrides` already logs the drop; surface it:
  serve the pinned-field set from the settings API and render those inputs disabled with
  a "pinned by VESTIGO_*" hint.
- [ ] **Semantic search does not survive a server-side resolve.** A saved View's (and now a
  saved chart's) filter payload carries `qMode: "semantic"`, but `stories/export.py::
  _filter_payload_to_spec` drops it — `FilterSpec` has no semantic mode — so a story export
  re-runs the query as a keyword search and freezes a *different* result set than the block
  shows on screen. Either teach `FilterSpec`/`_build_query` the semantic path, or refuse the
  export with a named reason; silently degrading the query is the one option that is wrong.
- [ ] **Generate frontend API types from OpenAPI** (`openapi-typescript` over
  `/openapi.json`) to replace the hand-mirrored types in `frontend/src/api/types.ts`.
  The duplication is compounding: 1240 lines when this was filed (PR109 review), 1549
  today. Eliminates the per-detector backend↔frontend drift wholesale instead of
  special-casing single detectors.
- [ ] **Split `api/routers/events.py`.** Previously a standing "only when next touched"
  decision; its own revisit trigger has now fired — 3100 lines on 2026-07-20, 3319 on
  2026-07-29, still growing without anyone touching it deliberately. Split along the
  read/aggregate/export seams.
- [ ] **README screenshot grid.** The README is laid out for a 2×2 image grid but ships one
  Explorer shot. Capture, at one consistent window size: the Analysis tab with a detector
  run's findings and the Method panel open, a Story with a live view embed and a chart
  block, the Agent with an applied finding, and a re-shot Explorer to match. The
  placeholder comment in `README.md` marks where they go.
- [ ] **Extract a `ui/Callout` primitive.** `analysis/EmbeddingStatusBanner`,
  `timelines/UploadDialog`'s duplicate warning and a handful of other sites hand-roll the
  same border/dim-background/icon banner with per-site colour tokens. Distinct from the
  `Card` item below — a callout is a banner that interrupts, a card is a surface that
  contains; they should not collapse into one primitive.

### Frontend design-system consistency (audit 2026-07-30)

Root cause, established by the audit: **the design system stops at colour.** Colour is
tokenized and disciplined (tiered backgrounds, CVD-validated viz palette, documented
diverging-ramp rationale); type, spacing, radius, icon size and surface treatment have no
token layer and no primitive, so every author re-decides at the call site. Absent a token,
`text-[10px]` is the *reasonable* thing to write — 118 reasonable moments is the drift.

Seven dead-token references found and fixed the same day (`--color-error` in 12 files,
`--color-border-focus` in 7, `--color-bg-subtle` in 2) plus the off-palette brand mark; all
of it compiled, typechecked, linted and passed tests, because nothing checked.

**The ratchet now exists** — `frontend/src/test/designSystem.test.ts`, shipped with the
tier-1 work. Undefined `var(--…)` is a hard check at zero, over every `.ts`/`.tsx` under
`src/` outside `src/test/` — a dead token is not a JSX-only problem, since
`viz/lib/colors.ts` builds `var(--viz-*)` strings for the chart export path. Arbitrary
`text-[Npx]` and raw `<button>` outside `components/ui/` are budgeted per file in
`designSystemBudget.ts`, seeded at 119 and 119, and stay scoped to components and pages
because only JSX has them. The budget only falls: exceeding an entry fails, and so does
*beating* one without lowering it, which is what keeps the list shrinking. **Every item
below burns its numbers out of that file** — closing one means deleting its budget entries,
and the migration is done when the file is `{}`.

- [ ] **Type scale in `@theme`, and burn down the 118 arbitrary font sizes.** Filed as a
  correctness item, not an aesthetic one: `html[data-density="compact"]` (`index.css:205`)
  rebases `font-size` to scale the whole UI, and every `text-[10px]`/`[11px]`/`[9px]`/`[8px]`
  ignores it — **compact density does not do what it claims** on those 118 sites. Current
  usage is `text-xs` ×565, `text-sm` ×107, arbitrary-px ×118, against `base`/`lg`/`xl` ×15
  total: one size with ad-hoc escapes *downward*, so nothing has hierarchy and the smallest
  text is below any legibility floor. Pick five named steps for *this* app — a dense grid
  tool legitimately lives at 12–13px, so the scale is `micro / body / lead / section / page`,
  not a generic 12/14/16/20/24. The point is five named decisions replacing ten anonymous
  pixel values, not larger text. Related: 69 `uppercase` + 68 `tracking-wide/wider` + 232
  `font-medium/semibold` are currently doing the job type size should, which is why every
  section label reads as the same texture and the panel cannot be skimmed.

- [ ] **`Card` and `SectionLabel` primitives.** 141 sites hand-roll
  `border border-[var(--color-border)]` + `rounded` + padding as a surface; 69 hand-roll the
  uppercase micro-label. That duplication is *why* radius is inconsistent (`rounded` ×267,
  `-lg` ×22, `-md` ×12, `-sm` ×6, against three defined radius tokens that go largely
  unused). Precondition for the type-scale and radius decisions landing consistently —
  afterwards those live in one place instead of 141. Extraction is small; migration is a
  ratcheted long tail, not a big-bang refactor. `SectionLabel` should render real heading
  elements, which absorbs most of the heading-structure item below for free.

- [ ] **Just-in-time guidance restructure (Investigate panel).** Needs its own design round;
  the diagnosis is settled, the shape is not. The tier-1 work fixed the *plumbing* — copy is
  now in `lib/guidance.tsx` and inlining it is a type error, dismissal is restorable — but
  not the placement, which is the actual complaint. `guidance["investigate-anomalies"]` is
  still ~120 words of three-step ordered list rendered in `text-xs`/`--color-fg-muted`, the
  faintest text in the app, into a panel 320px wide by default: sized like documentation,
  styled like a footnote, too long to skim and too faint to read. Worse, step 3 teaches
  Normal/Dismiss/Confirm — the single most important concept in the product — at the one
  moment nothing on screen demonstrates it, since finding cards have not rendered on first
  run. Proposed inversion: guidance attaches to the control at the moment of use. Panel top
  keeps one or two sentences of orientation (what this panel is *for*); scope guidance moves
  onto `FrameBar` where the choice is made; disposition guidance moves onto the first finding
  card, which is its referent. The registry makes this cheap to try — the copy is one file
  and the panel is one component.

- [ ] **Per-user guidance dismissal.** Collapse state lives in the `vestigo-ui` zustand store,
  so it is per-browser: an analyst who folds a panel away at their desk meets it again on a
  second machine, and vice versa. The backend half already exists — `User.preferences` (JSON,
  `db/postgres.py:1515`) and `update_user_preferences` (`:4938`, where a `None` value deletes
  the key, so reset comes free) — and `agent_disabled_tools` is the precedent for a namespaced
  key. The work is a preferences passthrough (`PATCH /auth/me` takes only username, display
  name and `onboarding_completed` today) plus deciding whether the frontend `User` type gains
  `preferences` or the agent's derived-endpoint pattern is repeated. Deliberately deferred
  from tier 1, which was scoped frontend-only.

- [ ] **`IconButton` primitive / the 119 raw `<button>`s.** Across 46 files, against 57 that
  import `Button` — roughly half the interactive surface skips the variant system, the focus
  ring and the disabled treatment. Individually small, collectively wide. The ratchet has
  stopped the bleeding; burn these down opportunistically rather than as one task, lowering
  the `rawButton` budgets as files are cleaned.

- [ ] **Icon size scale.** Ten distinct values in use (9, 10, 11, 12, 13, 14, 16, 18, 20,
  24; `size={12}` ×110, `size={13}` ×102, `size={11}` ×75). 11 vs 12 vs 13 is not a decision
  anyone can perceive — it is drift. Collapse to three (`inline` 12, `control` 16, `feature`
  20). Nothing breaks today, so fold it into the `Card`/`SectionLabel` migration passes
  rather than scheduling it separately.

- [ ] **`aria-live` for background work.** 35 `aria-label` but exactly one `aria-live` in the
  whole frontend: the job tray, toasts and streaming agent output announce nothing, so a
  screen-reader user gets no signal that an ingest finished. Narrow audience, total failure
  for that audience, small well-defined fix. The event grid's `aria-rowcount`/`aria-rowindex`
  is the pattern to follow.

- [ ] **Heading structure.** 39 heading elements (`h1` ×8, `h2` ×20, `h3` ×5, `h4` ×6)
  across 211 component files; hierarchy is carried entirely by the uppercase micro-label
  convention, so screen-reader navigation is effectively absent. Mostly resolved for free by
  `SectionLabel` above — do not schedule separately, verify after that migration.

## Milestone 4 — anomaly detector expansion (AMiner-inspired, field-agnostic)

Detectors adapted from [ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner),
constrained to be **field-agnostic** and SQL-explainable per the forensic-reproducibility
requirement. D1–D9 plus `proportion_shift` and `sequence_motif` shipped — see
`docs/ANOMALY_DETECTION.md` for every detector's contract, and update it in the same
commit as any detector change. Remaining:

Gap audit against the upstream `aminer/analysis/` catalogue, 2026-07-29 — the items below
are ordered by the standing priority rule: **truth of shipped claims first**, then
low-effort/high-value, high-effort/high-value, low-effort/low-value, high-effort/low-value.

Every detector item below is incomplete until the frontend half lands with it: a Method-tab
explanation in the same plain-language register as the existing fourteen, the SQL/params
visible on the finding, and disposition + allowlist wiring. A detector an analyst cannot
read the reasoning of does not meet the reproducibility bar and does not count as shipped.

### Truth of shipped claims (do first)

- [ ] **D11 — Entropy: add the bigram variant.** The shipped `entropy` detector measures
  per-value Shannon character entropy against a Tukey fence; AMiner's `EntropyDetector`
  learns a character-**bigram** transition table and flags low mean pair probability. These
  answer different questions, and ours misses the case its own docs advertise most loudly:
  a lowercase-latin DGA domain among English hostnames has unremarkable Shannon entropy.
  The false "adapted from" claim is already corrected in `docs/ANOMALY_DETECTION.md` §6 and
  the `find_entropy_outliers` docstring — this item closes the capability gap rather than
  the wording. Expressible in SQL: learn `P(c₂|c₁)` from the baseline window's distinct
  values via `arrayMap`/`ngrams(val, 2)` into a frequency map, score detect-window values
  by mean pair probability, flag below `prob_thresh` (AMiner default 0.05). Ship as a
  `method` on the existing entropy detector (`shannon-iqr` | `bigram`), not a fifteenth
  tool — same field selection, same findings shape, one more radio in the UI.
- [x] **D14 — Close the two documented scope narrowings.** Shipped in 1.8.6: charset
  gains `group_field` (one learned alphabet per value of a second field; suppressions
  stay `(field, value)`-keyed and apply across groups), and both sequence detectors gain
  `max_gap_seconds` (the n-gram assembly partitions on a running count of over-gap
  boundaries, so sequences no longer span quiet gaps). Both caveats in
  `docs/ANOMALY_DETECTION.md` are rewritten to describe the opt-in. Review fixes on the
  same branch: one scan per field rather than per group, a fallback reference for groups
  absent from the baseline window (`details.group_basis`), per-field warning attribution
  plus a warning when the grouped scan hits its row ceiling, and `age` rather than
  `dateDiff` for the gap so the bound measures elapsed seconds, not boundaries crossed.

### Low effort, high value

- [ ] **D12 — Time-of-day habit** (AMiner `PathValueTimeIntervalDetector`): per value, learn
  which times of day it occurs at in the baseline window, flag suspect-window occurrences
  outside that habit. "This service account only ever authenticates 08:00–18:00" is a
  first-order forensic signal and nothing currently covers it — `interval_periodicity`
  measures *inter-arrival gaps*, a different question (a beacon every 300s is regular but
  has no time-of-day habit; a 09:00 batch job has a habit but wildly irregular gaps).
  Cheap in SQL: bucket by `toHour`/`toMinute` per value over the baseline, flag detect-window
  events whose bucket has no baseline mass, score by distance to the nearest occupied
  bucket (AMiner's `max_time_diff`, default 360s). Needs an explicit **timezone** decision
  in the design — habits are local-time facts and the corpus is UTC; whatever is chosen has
  to be stamped into `DetectorRun.params` or the run is not reproducible.
- [ ] **D13 — Cross-field value correlation** (AMiner `VariableCorrelationDetector`): learn
  which field-value pairs co-occur *within the same event* in the baseline, flag
  suspect-window events that violate an established association. "This user is always on
  this subnet", "this process always has this parent". Distinct from D10, which is temporal
  (A *then* B); this is intra-record. Reuses machinery wholesale — `GROUP BY a, b` plus the
  G-test and Benjamini–Hochberg pool that `proportion_shift` already has, and AMiner's own
  "Rel" method (deterministic one-to-one mapping violated) is the trivially explainable
  case worth building first. Field-pair explosion is the real design problem: needs a
  preselection rule (AMiner uses distribution matching) and a candidate cap in the
  `HEAVY_SCAN_SETTINGS` family, honestly reported like the other caps.
- [ ] **D15 — Impossible-speed transitions** (AMiner `MinimalTransitionTimeDetector`): learn
  the minimum observed time between consecutive values of a field for a given identifier,
  flag a suspect-window transition faster than the baseline ever saw. Impossible travel,
  automation posing as a human. `find_sequence_novelty`'s ordered `lagInFrame` partitions
  already produce consecutive pairs with their timestamps — this is a `min(dateDiff)`
  aggregate over the same shape, so the incremental cost is small. Score = AMiner's
  `1 − (observed / learned_min)`, which is already a 0–1 confidence.

### High effort, high value

- [ ] **D10 — Event correlation rules** (AMiner `EventCorrelationDetector`): mine baseline
  implication rules "value A is followed by value B within Δt", flag violations in the
  detect window. Highest analytical payoff, heaviest lift (rule mining + hypothesis
  testing). Stepping stone shipped: `sequence_motif`'s recurring n-grams are the natural
  antecedent set. AMiner's online form generates hypotheses randomly and confirms them with
  a binomial test (`p0` 0.9, `alpha` 0.05) — the batch re-derivation should mine candidate
  antecedents deterministically instead, since random hypothesis generation is not
  reproducible and reproducibility is non-negotiable here.
- [ ] **D16 — Multivariate window profiles** (AMiner `EventCountClusterDetector`): build a
  count vector per time bucket (one dimension per value of the series field), compare each
  suspect bucket against the baseline buckets by normalized Manhattan distance, flag beyond
  `confidence_factor` (AMiner default 0.33). Catches what `frequency` structurally cannot: a
  change in the *mix* of event types at constant total volume, which is what a compromised
  host looks like when the attacker keeps the noise floor steady. Effort is in making it
  explainable — a distance is not a p-value, so the finding must name the dimensions that
  contributed most of the distance, or it fails the explainability bar. Optional IDF
  weighting is upstream's answer to rare-event dominance and worth carrying.

### Low effort, low value

- [ ] **D17 — New field key** (AMiner `NewMatchPathDetector`): flag *attribute keys* that
  appear in a suspect window but never in the baseline — a new field, not a new value.
  Nothing covers it today. Genuinely trivial (`arrayJoin` over attribute keys, set
  difference), and genuinely marginal: for most sources a new key means a format change or
  a converter update, not an intrusion. Build it when touching field inventory anyway.

Skipped deliberately:

- `TSAArimaDetector` / `PathArimaDetector` — ARIMA forecasting; the z-score `frequency`
  detector covers most of it and stays explainable.
- `PCADetector` — principal-component analysis over event count vectors. High effort, and
  the output is a reconstruction error in a rotated space that no analyst can trace back to
  events. It fails the explainability requirement by construction, which is the whole reason
  the field-agnostic/SQL-explainable constraint exists. D16 is the same signal at a fraction
  of the cost with findings you can read. Revisit trigger: D16 ships and demonstrably misses
  correlated multi-field drift that only a rotation exposes.
- `HistogramAnalysis` / `ParserCount` — descriptive statistics, not detection; the Explorer
  histogram and field inventory already serve this.
- Every `learn_mode` / persistence / `stop_learning_*` mechanism — replaced wholesale by
  analyst-declared baseline definitions. This is the core adaptation, not a gap: it is what
  turns an online detector into a reproducible forensic one, and it is why no Vestigo
  detector has hidden state carried between runs.

## Milestone 5 — post-mortem workflow depth (Timesketch-inspired, then past it)

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
  2026-07-20). `evtx2timesketch` stays for *text* exports, but binary `.evtx` now has a
  native converter (`evtx2vestigo`, 2026-07-30) — the two cover different inputs, not the
  same one twice.
- **Ship a stock Windows `vestigo-fieldmap.yml`.** `evtx2vestigo` emits Sigma-canonical
  names, so Windows rules resolve correctly but are permanently flagged in
  `fallback_fields` — nothing vouches for a name that is right by construction, and a
  timeline field mapping cannot vouch for it (identity mappings are rejected as shadowing
  the raw key). A ruleset-root fieldmap of identity entries clears the flag with identical
  SQL — measured over SigmaHQ `rules/windows/builtin` (326 rules): 873 fallback flags → 0,
  zero SQL differences, zero match-count differences, from 141 identity entries generated
  straight from the rules' own field names. The open question is *delivery*, not
  feasibility: the ruleset directory is operator-supplied via `VESTIGO_SIGMA_RULES_PATH`,
  so the repo cannot drop a file into it — it needs shipping as a downloadable asset (like
  the converters) or a documented snippet. See `docs/ANOMALY_DETECTION.md` §Sigma.
- **`evtx2vestigo` deferred items.** `.evtx.gz` input (the `evtx` wheel's `PyEvtxParser`
  accepts a `BytesIO`, but decompressing a routinely-hundreds-of-MB log costs whole-file
  RAM); `%%1833`-style message-table resolution (needs the originating host's WEVT
  templates, which no converter-side library has); EvtxECmd PayloadData slot-order parity
  (we emit each mapped
  property as its own attribute instead, which is strictly more information). Trigger for
  the first: someone hands us a compressed triage collection.
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
