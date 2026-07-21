# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.4] — 2026-07-21

### Fixed

- **Agent chart proposals no longer vanish when the model batches tool calls.**
  A model issuing parallel `propose_chart` calls (Kimi does this routinely)
  persists N call rows followed by N result rows, but the agent panel paired
  them through a single buffer that assumed call→result adjacency — so a batch
  of 14 validated charts rendered as one card, and even that one carried the
  wrong title. Tool call and result rows now persist the provider's
  `tool_call_id` (migration `0014`) and the panel pairs by it, with FIFO
  fallback for conversations recorded before the migration. A chart that fails
  validation consumes only its own slot instead of shifting its batch
  siblings, in both the live stream and the reloaded transcript.

### Fixed

- **Muting a template now actually hides its events.** A mute was recorded
  correctly — it appeared under "Muted templates" with its count — but the grid
  kept showing every one of its events, because collapsing them was a separate
  toggle in the top bar that muting never switched on. A mute is a filter, so it
  now applies the moment you make it, which is what the tab always claimed. The
  toggle is now a *reveal*: press it to see the routine events again
  temporarily. The next mute re-applies collapse, so revealing once cannot
  quietly disable every mute you make afterwards.
- **"Select all matching → Tag" no longer tags events you cannot see.** With
  routine events collapsed, bulk-tagging the current filter wrote annotations to
  the muted events as well — records attached to events that were never on
  screen, while the confirmation dialog counted only the visible ones. The bulk
  action now covers exactly the set the grid displays. Exports and histograms
  were already correct.
- **Charts now respect muted templates too.** Every visualization endpoint
  (top values, timeseries, punchcard, pivot, scatter, compare) silently ignored
  the collapse flag the frontend was already sending, so a chart could disagree
  with the grid it sat next to — the histogram modal's top-value list included
  events its own histogram hid. The Visualize page, which cannot inherit the
  flag from the URL, now derives collapse from the mute list itself, shows a
  visible "routine events collapsed" indicator, and offers the same temporary
  reveal as the Explorer.
- **No more flash of muted events on load.** The Explorer and Visualize pages
  fired their first data query before the mute list had loaded, briefly showing
  (and needlessly computing) the uncollapsed event set, then refetching. Both
  now wait for the mute list — one small metadata read — before the first
  fetch.

## [1.4.2] — 2026-07-21

### Added

- **Tool-result detail is now an agent setting (`tool_fidelity`).** How much of
  each event record the agent gets back from searches, similarity lookups and
  anomaly findings — `full` (the whole event), `message` (the one line that
  distinguishes a succeeded login from a failed one), `minimal` (just the
  identity fields), or `auto` (derive it from the configured context window:
  100k and up gets `full`, 32k and up `message`, anything smaller `minimal`, and
  an unconfigured window `message`).
  The default is `full`: an unset context window means the operator has declared
  no constraint, which is assumed to be a cloud model with room. Admins running
  a small local model should set `message` or `auto`. `get_event` always answers
  in full — it is the escape hatch the reduced results point at.
  **Note for `/mcp` users:** the setting applies to the external transport too,
  so setting anything but `full` changes what existing MCP clients receive from
  `search_events`, `semantic_search`, `similar_events` and `run_anomaly_detector`
  — each such result names its tier in a `fidelity` field.
- **An overflow now costs a slower turn, not a shallower one.** When a turn
  overflows the model's context window, the agent first re-runs it handing the
  model less of each event record — no summarizer call, and unlike compaction
  it works on a single broad turn, which has no older turns to fold. It is
  skipped when the turn fetched no event records, since there would be nothing
  to give up; only once it is exhausted does the agent compact. Each such drop
  is recorded the way a compaction is — a message row in the conversation and an
  audit entry — so it survives a reload and reaches the JSON export, and each
  tool result records the detail level that produced it. An exported
  conversation states every degradation that was applied to it.

### Security

- **Path traversal in the frontend catch-all (unauthenticated arbitrary file
  read).** The route that serves the built SPA joined the request path onto
  `frontend/dist` and let the filesystem resolve it, so a request line carrying
  a literal `..` — which neither uvicorn nor Starlette normalizes — returned any
  file readable by the service account, including the deployment's own `.env`.
  The route is unauthenticated by design (the browser needs the app shell before
  login), so this was reachable by anyone who could reach the port. Candidates
  are now resolved and required to sit inside `frontend/dist`, which also stops
  a symlink pointing out of it. **Anyone who exposed vestigo-web to an untrusted
  network should check their proxy access logs for request paths containing `..`
  and rotate the secrets in `.env` if there is any doubt.**

### Fixed

- **Agent turns no longer die on a LiteLLM context overflow.** The overflow
  heuristic did not recognise LiteLLM's "exceeds the available context size"
  phrasing, so an overflow against a proxied local model skipped the
  compact-and-retry escalation entirely and surfaced as a generic model error,
  losing the turn.
- **A single broad investigation turn no longer overflows a small model.** Each
  anomaly finding handed to the agent embedded the full resolved example event
  (~85% of the finding's size); a "find anomalies and visualise" ask that ran
  seven detectors in one turn piled up ~18k tokens of duplicated event bodies
  and overflowed a 64k model — a case compaction cannot fix, since there is only
  one turn to fold. The agent's copy of a finding now carries the example's
  `event_id` and its `message` line — the part that distinguishes a succeeded
  login from a failed one — instead of the whole event, with `get_event` for the
  full record and a note saying so; and the bulk `list_annotations` scan
  truncates long bodies harder than the per-event detail tool. The persisted
  detector run and the Analysis page keep the full data. On the turn that
  failed, this cut the tool payload from ~34k to ~16k tokens.
- **The agent gets more than one attempt to correct a rejected tool call.** Tool
  legality errors name the legal alternative and exist to be acted on, but the
  retry budget was one, so a second wrong guess killed the whole turn. A
  `propose_chart` call asking for a `heatmap` with two fields did exactly that.
  The budget is now three, and that particular rejection names the fix
  (`chart_type="pivot"` is the field × field heatmap; `heatmap` is one field over
  time) rather than only listing the two-field chart types.
- **A turn that ends early says why.** Exhausting a tool's retries or the turn's
  step budget surfaced as "Agent turn failed — see server logs", which does not
  tell the analyst whether to rephrase, narrow the question, or call an admin.
  Both now end with a named error (`tool_retry_exhausted`, `turn_limit_reached`)
  carrying the underlying reason.
- **A reduced tool result no longer claims to have dropped something it kept.**
  An anomaly finding whose example event could not be resolved, or held nothing
  but a short message, still came back with "call get_event for the full
  record" attached — an untruth in an exported conversation. The note now
  appears only when the detail level actually removed something.
- **A degraded turn is legible in the case record.** A turn re-run at a lower
  detail level re-executes its tools, so one analyst question could leave
  several identical detector runs on the Analysis page with nothing to tell them
  apart; re-runs now carry the attempt that produced them. The estimate that
  decides whether to summarize older turns also ignores token counts measured
  before a detail drop, the way it already ignored counts measured before a
  summarization — they describe a request the conversation no longer sends.

## [1.4.1] — 2026-07-20

### Changed

- **The agent fits a small context window again** — tool definitions are resent
  to the model on every request, and they had grown to roughly half of a 32k
  local-model window before the conversation even started. They are now
  advertised in a compact form (~52% smaller) with no loss of guidance: the
  shared filter/chart field documentation moved into the system prompt, where
  it is sent once instead of once per tool. Nothing about what the agent can
  do, or how strictly its arguments are checked, has changed.
- **Tabular tool results are compact** — search hits, value distributions,
  pivots, comparisons, detector findings and time series are handed to the
  model with their column names stated once instead of repeated on every row,
  and a time series no longer repeats its time axis per series (−84% on a full
  one). Every value is preserved exactly; this is a reshaping, not a summary,
  so results stay reproducible. Because results are replayed on every later
  turn, this compounds over a long investigation.
- **The agent's metadata list tools are capped** at 200 rows (baselines, saved
  views, annotations, dispositions, Sigma rules and runs). They were unbounded,
  so a long-running case could push an arbitrarily large payload into the
  conversation history. Each one now reports how many rows it returned
  alongside how many exist, so a capped list can never be mistaken for a
  complete one.
- **The external `/mcp` tool surface changes shape with it.** Clients of the
  `/mcp` endpoint get the same slimmed schemas and the same column-header-once
  results as the built-in agent, rather than a second encoding maintained in
  parallel. The server's MCP `instructions` now carry the filter/chart field
  reference and the result-format legend, so an external client has everything
  it needs to read either. Any client that parsed the old row-per-dict results
  needs updating; Vestigo has no external MCP consumers in the field, so this
  is called out for completeness rather than as a migration.

### Added

- **Core / All presets in the agent tool selector** — "Core" keeps the
  eleven tools an investigation cycle actually needs and turns off the rest,
  cutting the per-request tool overhead to about a fifth of the full catalog.
  Useful when running a small local model. Disabled tools are removed from the
  request entirely, so this reclaims context rather than just tidying the list.
- **Stop a running agent turn** — a turn that is still running when you close
  the panel or navigate away is now visible when you come back, with a Stop
  button that actually cancels it server-side instead of only dropping your
  own stream. Whatever the agent had already written is kept, marked
  `[stopped]`, and who stopped it is recorded in the audit trail.
- **Agent tool selection stays editable** — the tool popover no longer
  disappears once a conversation starts; changing it now adjusts that
  conversation (from the next turn onward) and is written to the audit trail.
- **Resizable agent panel** — drag its left edge, same as the Investigate and
  event-detail panels. The width persists.
- **Model picker in the agent admin settings** — once the API base URL and key
  are set, the model field becomes a dropdown populated from the endpoint's own
  model listing instead of a name typed from memory. Free-text entry remains the
  fallback when an endpoint offers no listing, and stays available for models a
  listing omits.
- **Save an agent finding as a View** — finding cards get a save action
  alongside "Apply to Explorer", so a filter set worth keeping lands in the
  left-hand Views panel instead of dying with the conversation.

## [1.4.0] — 2026-07-20

### Added

- **Log template clustering**: structurally identical log lines (variable
  timestamps/IPs/UUIDs/hex/numbers masked) are grouped into shapes, browsable in
  a new Templates tab (under Patterns) — mute a routine shape to collapse its
  events out of the grid immediately, always behind a visible count. Field is
  filterable in the grid via the new `template_id` facet.
- **Agent chart proposals**: the agent can now explore data through the same
  charts as the Visualize page (per-value time series, punch card, field×field
  pivot, scatter, two-layer compare) and propose one as a live chart card in
  the chat — "Open in Visualize" jumps to the full page with the same chart,
  "Save" writes a saved chart credited to the analyst. The agent never writes
  a chart itself.
- **Agent auto-compaction**: configurable model context window
  (`VESTIGO_AGENT_CONTEXT_WINDOW` / admin UI); long conversations are summarized
  before they overflow, with the summary shown in chat and the exact
  pre-compaction history preserved on an append-only, audited record. Provider
  context-overflow errors now compact-and-retry once, then fail with a specific,
  friendly message instead of a generic one.
- **Per-tool enable/disable, three layers**: admins can hard-disable individual
  agent tools globally (applies to the in-app agent and the external `/mcp`
  endpoint); users can set personal defaults and adjust the tool set per
  conversation.
- **Persistent OPSEC notice**: the agent panel always shows where evidence data
  goes — the configured API endpoint URL and model — in its empty state, with
  no dismiss, so it is visible before every first message. Tool selection for a
  new chat sits next to the input as a popover.
- **Thinking content**: the model's reasoning segments are streamed, persisted,
  and rendered as collapsible blocks in the chat.
- **Conversation JSON export**: download any agent thread as JSON — every
  message, tool call with arguments and results, thinking content, token usage,
  compaction records, and the raw provider-wire history.

## [1.3.0] — 2026-07-19

### Added

- **AI investigation agent** (`docs/AGENT.md`) — optional, off-by-default assistant
  embedded in the Explorer. It drives the iterative analysis loop (search, aggregate,
  run detectors, refine) in its own sandbox and hands results back as **findings**:
  filter-set cards the analyst applies with one click — the agent never mutates the
  analyst's view. Conversations, every tool call with exact arguments, and the
  replayable runtime history persist in Postgres; every tool call is audited.
- **Propose→confirm writes**: the agent never writes annotations itself.
  `propose_annotation` records a proposal; an analyst confirms or rejects in the UI.
  Confirming re-resolves events against the current scope and writes annotations with
  `origin="agentic-analysis"`, credited to the confirming analyst and audited.
- **Full read parity**: tools for events, aggregations, histograms, similarity /
  semantic search, all statistical detectors (with tuning parameters), detector
  baselines, dispositions, saved views, annotations, and Sigma rules/runs.
- **External `/mcp` endpoint** (`VESTIGO_MCP_ENABLED`, default off) — the identical
  scoped tool server over streamable HTTP for external MCP clients, authenticated by
  per-timeline scoped tokens (`vgo_…`, shown once at creation). Scope comes from the
  token, never from the client.
- **Admin agent settings page** — DB-backed runtime configuration with per-field
  env-pinning (`VESTIGO_AGENT_*` always wins, pinned fields shown disabled with a
  badge), masked API key, endpoint test button, and per-provider reasoning-effort
  translation (`off`–`max`, incl. an experimental Kimi mapping).
- **Token-usage metering** — measured per turn from the runtime (never estimated;
  `NULL` when the endpoint reports nothing), shown as per-message chips and a running
  conversation total.
- **`VESTIGO_AGENT_SECRET_MODE=env-only`** — refuses DB storage of the LLM API key and
  ignores any previously stored one, making `VESTIGO_AGENT_API_KEY` the only source.
- Explorer: agent-provenance badge on annotations; usernames resolve to display names
  everywhere names render.

### Changed

- `docs/CONCEPT.md` refreshed to match the shipped product: statistical detector suite,
  Sigma, and the agent in the vision; corrected Qdrant collection naming; out-of-scope
  list rewritten (streaming ingest, correlation rules, and Stories are now roadmap
  milestones).

## [1.2.1] — 2026-07-19

### Changed

- **Dependency roundup** — all 20 open Dependabot PRs merged and lockfiles fully
  refreshed. Backend: fastapi 0.139.2, clickhouse-connect 1.5.0, typer 0.27.0,
  geoip2 5.3.0, ruff 0.15.22, plus all transitive updates via `uv lock --upgrade`.
  Frontend: vite 8.1.5, tailwindcss 4.3.3, oxlint 1.74.0, @types/node 26,
  Radix UI patch releases, @tanstack/react-virtual 3.14.6, lucide-react 1.25.0,
  @fontsource/inter + jetbrains-mono 5.3.0. CI: docker/* actions and
  actions/setup-node major bumps. Full backend + frontend suites green on the
  upgraded set.
- Frontend `package.json` version now tracks the app version (was stale at 1.1.2).

## [1.2.0] — 2026-07-19

### Added

- **Sigma rule runner** (`docs/ANOMALY_DETECTION.md` §13) — deterministic signature
  matching of community-standard [Sigma](https://github.com/SigmaHQ/sigma) YAML rules
  over ClickHouse, deliberately separate from the statistical detectors. Rules come
  from an admin-managed offline directory (`VESTIGO_SIGMA_RULES_PATH`, a file drop —
  no restart needed, unchanged files reuse a per-file parse cache) and per-case
  uploads. Every hit is written as `Annotation(origin=system, annotation_type="sigma")`
  whose `sigma: <rule title>` label joins the unified tag filter panel.
- **Custom pySigma → ClickHouse backend**: one boolean SQL expression per rule.
  Sigma-spec case-insensitive matching (`ILIKE` with `*`/`?` wildcards), `|cased`,
  `|re` (RE2), `|cidr` (guarded `isIPAddressInRange`), numeric comparisons, null/missing
  semantics, field-less keywords over `search_blob`. Field names resolve through
  ruleset `vestigo-fieldmap.yml` → timeline canonical mappings → raw-attribute
  fallback (tracked and flagged in the UI). All values pass through an audited,
  adversarially-tested literal-quoting boundary.
- **Streamed, reproducible runs**: background job per timeline; per rule, hits stream
  under the shared heavy-scan gate through a bounded queue (no hit cap, no in-memory
  hit list) into batched annotation writes; re-runs are idempotent per rule and
  preserve confirmed findings. Persistent `sigma_runs` records (Alembic `0006`)
  snapshot each rule's YAML content hash, exact compiled SQL, match count, and status.
- **Sigma tab** in the Investigate panel: rule picker with level/logsource badges,
  YAML upload, run launch into the job tray, run history with per-rule status,
  compiled-SQL view, fallback-field warnings, and filter-grid-by-rule.
- Config: `VESTIGO_SIGMA_RULES_PATH`, `VESTIGO_SIGMA_ANNOTATION_BATCH_SIZE`.
  Deps: `pysigma`, explicit `pyyaml` (offline — no Sigma code path touches the network).

## [1.1.0] — 2026-07-13

### Added

- **Repeating-sequence (motif) mining** — new `sequence_motif` detector
  (`docs/ANOMALY_DETECTION.md` §12): per source, time-ordered n-grams of one field's
  values that *recur* are ranked by support × cadence regularity (median gap, CV,
  Greenwood spacing test). Mode-less — needs no baseline, runs right after ingestion;
  optional `start`/`end` scope. Tunables: `VESTIGO_STAT_MOTIF_MIN_SUPPORT`,
  `VESTIGO_STAT_MOTIF_MAX_CANDIDATES`, `VESTIGO_STAT_MOTIF_CADENCE_TOP_K`.
- **Routine suppression** — new disposition `kind="routine"`: a motif marked routine has
  its occurrences materialized (ClickHouse `motif_occurrences` table, auto-created) so the
  event grid, histogram, and export can collapse them via `collapse_routine`. The response
  always reports `routine_collapsed_count` — collapse is explicit, never silent. Routine is
  presentation-only: detectors keep scoring and it never enters the reproducibility hash.
- **Patterns tab** in the Investigate panel: motif list with support, period, regularity
  bar and per-source cadence; Mark routine / unmark; Explorer collapse toggle with an
  always-visible collapsed-count banner.
- **Unified findings feed** — the Anomalies tab now opens with one cross-detector ranked
  inbox (per-detector rank interleave, raw score with its unit per row, detector chips as
  filters), built from the detector sweep the count badges already paid for.

### Changed

- The 11 per-detector views moved under a collapsed **Advanced** expander, grouped
  Values / Volume & timing / Sequences. The dense baseline/suspect-window builder moved
  from the inline flow into an overlay drawer (FrameBar → *Manage baselines*; histogram
  mark-mode opens it automatically).

## [1.0.0] — 2026-07-12

First stable release. Everything below is new in 1.0.0.

### Renamed

The project was renamed **TraceSignal → Vestigo** ahead of 1.0 (*vestigo*, Latin:
"I follow the tracks"). For anyone upgrading a pre-release deployment:

- CLI entry points: `tsig` → `vestigo`, `tsig-web` → `vestigo-web`.
- Environment variables: `TS_*` → `VESTIGO_*` (e.g. `TS_POSTGRES_URL` →
  `VESTIGO_POSTGRES_URL`).
- Default backing-store names changed to `vestigo` (PostgreSQL database/user, ClickHouse
  database, Qdrant collection prefix). Existing deployments keep their data by pinning the
  old names via `VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_DATABASE`, and
  `VESTIGO_QDRANT_COLLECTION_PREFIX`.
- Converter scripts: `*2tracesignal.py` → `*2vestigo.py`. Parquet footer metadata keys
  moved from `tracesignal.*` to `vestigo.*`; the server still reads files produced by
  pre-rename converters.

### Ingestion

- Streaming parsers for Plaso CSV/JSONL and generic Timesketch-compatible CSV/JSONL —
  constant-memory, tens-of-GB capable, with per-record byte offsets and content hashes.
- Every ingested file (Source) is SHA-256 hashed and retained content-addressed.
- Vestigo Parquet interchange format v1: downloadable client-side converter scripts
  (nginx, filterlog, suricata, cloudtrail, pcap — plus vendored stdlib-only Timesketch
  converters for apache, browser, cowrie, evtx, journal, syslog) emit typed columnar
  Parquet that the server bulk-inserts via Arrow record batches, with forensic provenance
  anchored to the original raw evidence file.
- CLI ingestion (`vestigo ingest`) streams straight from disk with progress/ETA and
  per-user attribution; upload size cap (`VESTIGO_MAX_UPLOAD_BYTES`) with mid-stream 413.
- Optional per-source enrichers with recorded provenance, force re-run recovery, and
  upgrade guards.

### Explorer

- Virtualized ELK-like event grid over ClickHouse: resizable/pickable columns, density
  modes, light/dark themes, keyset pagination.
- Full filter model (field, value, time range, tags, annotations), saved Views per
  timeline, indexed full-text search, time histogram with brush zoom and event markers.
- Context query around any event; per-source clock-skew correction; column stats and
  field inventory backed by a per-source field-stats cache.

### Anomaly detection

- Statistical detectors run directly against ClickHouse, all SQL-explainable, each with
  self-baseline and temporal (baseline/suspect window) modes where applicable:
  value novelty, frequency (z-score spikes/silences), value combinations,
  timestamp order, charset, numeric range, entropy, interval periodicity
  (cadence breaks + beaconing), sequence novelty (n-grams), proportion shift
  (G-test with BH-FDR), and value distribution drift (KS / G-test).
- Embedding pipeline: user-triggered jobs embed events into Qdrant (local models,
  offline-capable); semantic search and nearest-neighbor similarity; embedding wizard
  with content-aware field recommendation.
- Triage workflow: unified disposition taxonomy, dismissals, Investigate panel bundling
  detectors with shared baseline configuration.

### Visualization

- Visualize page: time histogram, comparison histogram, punch card, pivot, Sankey and
  scatter charts, click-to-filter, saved charts — with scan guardrails at 300M-row scale.

### Platform

- Session-cookie auth with optional OIDC SSO, case-level RBAC, teams, audit trail.
- Alembic-managed PostgreSQL schema with automatic migration on startup (pre-Alembic
  databases are auto-adopted).
- Airgapped/offline by default (`VESTIGO_ALLOW_ONLINE` gates all network paths except
  the deliberately independent OIDC).
- Typer CLI mirroring the API for scriptable/offline use; reference `docker-compose.yml`
  for the three backing services (PostgreSQL, ClickHouse, Qdrant).
- Container images published to `ghcr.io/overcuriousity/vestigo`.

[1.0.0]: https://github.com/overcuriousity/Vestigo/releases/tag/v1.0.0
