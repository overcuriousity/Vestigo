# AI Investigation Agent

Optional, off-by-default AI assistant embedded in the Explorer. It drives the
iterative analysis loop on the analyst's behalf — search, aggregate, run
detectors, refine — and hands results back as **findings**: filter sets the
analyst can apply to the Explorer with one click. Update this document
alongside any agent change, like `ANOMALY_DETECTION.md` for detectors.

Code lives in `src/vestigo/agent/` (runtime, tools, config, context
management) and `api/routers/agent.py` (HTTP/SSE layer). Design records:
`docs/superpowers/specs/2026-07-19-agent-read-parity-mcp-http-design.md`,
`docs/superpowers/specs/2026-07-22-agent-sliding-window-design.md`.

## Design invariants

- **Sandbox + apply.** The agent queries the backend in its own loop and never
  mutates the analyst's view. Findings render as cards; only an explicit
  analyst click applies filters (through the normal URL-driven filter path,
  `frontend/src/lib/queryParams.ts`).
- **Propose→confirm writes.** The agent itself never writes an annotation.
  `propose_annotation` (available only once a conversation is bound) records an
  `AgentProposal` row — it does not touch `annotations`. An analyst confirms or
  rejects via `POST .../proposals/{id}/confirm|reject`; confirm re-resolves the
  events against the *current* scope, writes one annotation per still-resolving
  event with `origin="agentic-analysis"` and `created_by` set to the confirming
  analyst, and reports `skipped_event_ids`. The decide is an atomic
  `UPDATE … WHERE status='proposed'`, so a second confirm/reject 409s.
  `run_anomaly_detector` is the only other write-shaped tool (it persists a
  `DetectorRun`, same as an analyst-triggered scan).
  - **Origin is provenance, not a visibility class.** A confirmed
    `agentic-analysis` annotation behaves exactly like a manually-typed one in
    tag autocomplete, the annotated/tag Explorer filters, and deletion
    (`USER_VISIBLE_ANNOTATION_ORIGINS = ("user", "agentic-analysis")`); only
    `origin="system"` stays outside that set.
  - Every decision is audited (`agent.annotation_confirm` /
    `agent.annotation_reject`, keyed to `target_type="agent_proposal"`).
  - `propose_annotation` and the decide endpoints are **absent from the
    external `/mcp` transport** — only an in-app conversation binds the
    `conversation_id` that gates the tool's registration.
- **Invisible unless configured.** `/api/health` reports `agent_available` only
  when `VESTIGO_AGENT_*` is set **and** the endpoint answered a cached probe
  (`agent/availability.py`, TTL `VESTIGO_AGENT_PROBE_TTL_SECONDS`). Otherwise
  the frontend renders zero agent UI and the API endpoints 503. The cache is
  stale-while-revalidate, so `/api/health` never blocks on a hung LLM endpoint.
- **Forensic reproducibility.** Conversations persist in Postgres
  (`agent_conversations` / `agent_messages`): every tool call with exact
  arguments and a result summary, plus the runtime's replayable pydantic-ai
  history. Each tool call also writes an audit row (`agent.tool_call`), and
  detector runs launched by the agent persist like any other. Conversations
  are per-user (private); the audit trail is the shared record.
- **Scope safety.** Tools are bound to one case + timeline at server-build time
  (`agent/tools.py::AgentScope`); the model never supplies IDs — on both
  transports. The built-in loop derives scope from the conversation; the
  external `/mcp` endpoint derives it from the Bearer token, never from
  anything the model or client sends.
- **Token metering: measured, or null.** Each turn stamps the endpoint's
  reported `input_tokens`/`output_tokens` on the persisted assistant row; a
  `0` (nothing measured) is stored as `NULL` — Vestigo never fabricates or
  estimates a token count. The same fields ride the terminal `done` SSE event.

## Architecture

```
frontend AgentPanel ──POST /messages (SSE)──► api/routers/agent.py
                                                   │ stream_turn()
                                              agent/runtime.py (pydantic-ai Agent)
                                                   │ in-process MCP (fastmcp client)
                                              agent/tools.py (mcp.server.fastmcp FastMCP)
                                                   │ wraps existing services
                                              EventQueryService / StatisticalAnomalyService /
                                              SimilarityService
```

- Tools are defined once on a **standard MCP server** (`build_tool_server`);
  the built-in loop consumes it in-process, and the same server is served over
  HTTP for external harnesses — one tool code path, two transports.
- Streaming is SSE over the POST response (`text_delta`, `thinking_delta`,
  `thinking`, `window`, `tool_call`, `tool_result`, `done`, `error`; `error`
  may carry a machine-readable `code`). The frontend reads it via fetch +
  ReadableStream (`frontend/src/api/agent.ts`).
- **Thinking is first-class.** pydantic-ai `ThinkingPart`s stream as
  `thinking_delta` events and persist as `role="thinking"` rows, interleaved
  correctly with tool calls; the chat renders collapsed "Thinking" blocks.
  Thinking *signatures* live only in the replayable `history` blob (and the
  JSON export's `raw_history`), never per-row.
- **One turn at a time per conversation** — a POST while another turn streams
  gets a 409 (`_active_turns`). The reservation is surfaced as `active` on
  every conversation payload (so a reopened panel shows Stop, not a silently
  409ing input), and reservations older than `LLM_TIMEOUT × max_turns` are
  pruned as stranded so a dead ASGI task can't leave a conversation
  permanently "running".
- **Stop is server-side.** `POST .../{id}/cancel` sets the turn's cancel
  event; aborting the client's SSE fetch alone is not enough (with no output
  flowing, Starlette may not notice the disconnect while the turn keeps
  spending tokens). Text streamed before the stop persists tagged
  `[stopped]`; it takes effect at the next streamed event (an in-flight tool
  call completes first), and a real cancel is audited as
  `agent.turn_cancelled`. Idempotent — cancelling an idle conversation
  reports `cancelled: false`.
- The analyst's current Explorer filters ride along with each message and are
  injected as context, so "filter what I'm looking at further" works.

### External MCP endpoint

`/mcp` (`agent/mcp_http.py`) exposes the *identical* tool server over MCP
Streamable HTTP, so any external MCP client (Claude Code, hermes-agent, …)
can investigate a case/timeline with the same tool surface.

- **Gated by `VESTIGO_MCP_ENABLED`** (default off, independent of
  `VESTIGO_AGENT_*` — serving MCP needs no LLM endpoint). When off, the
  endpoint 404s and is invisible; `/api/health` reports `mcp_enabled`.
- **Scoped tokens.** `AgentToken` (Postgres) binds a token to exactly one
  case + timeline at creation — leak blast radius is one timeline. Managed via
  `/api/cases/{case_id}/timelines/{timeline_id}/agent-tokens`
  (RBAC-checked; a token never grants more than its creator's case access).
  UI in the timeline list (`AgentTokensDialog.tsx`). Plaintext (`vgo_…`) is
  shown once at creation; only a SHA-256 hash is stored.
- **Bearer auth + per-connect RBAC re-check.** Every request presents
  `Authorization: Bearer vgo_…`; revoked/expired/unknown tokens 401, and the
  endpoint re-checks that the *creating user* still has case access (403) —
  an analyst who lost access can't be impersonated via an old token. Scope
  always comes from the token.
- **Audit.** Each `tools/call` writes an `agent.tool_call` audit row with the
  token id and `transport: "mcp_http"`, sniffed from the request body before
  the MCP session sees it. Bodies cap at 10 MiB (413 above). FastMCP's
  DNS-rebinding host validation is disabled — safe because Bearer auth
  precedes all dispatch; Host handling belongs to the reverse proxy.

## Tools

28 total, defined in `agent/tools.py`; all read-only except the
propose→confirm annotation path and `run_anomaly_detector`'s persisted run.
`TOOL_REGISTRY` is the single source of truth for the catalog (name,
description, `embeddings_gated`, `requires_conversation`, `tier`); a
registry-parity test keeps it in sync with the actual `@server.tool()`
registrations. `tier="core"` marks the 11-tool lean profile for
small-context local models.

| Tool | Tier | Purpose |
|---|---|---|
| `search_events` | core | Search events with Explorer-equivalent filters. |
| `get_event` | core | One event by `event_id`, full attribute set — the escape hatch every reduced payload names. |
| `list_fields` | core | Queryable fields: fixed columns, attribute keys, time parts. |
| `describe_field` | core | Probe one field: coverage, numeric-ness, suggested scale/charts. |
| `list_artifacts` | core | Distinct artifact types in the timeline. |
| `field_terms` | core | Top-N value distribution for a field. |
| `field_numeric_stats` | | Summary stats (incl. skewness) + histogram for a numeric field. |
| `field_numeric_grouped` | | Per-group numeric distributions — one numeric field split by a categorical one. |
| `field_correlation` | | Pairwise Pearson/Spearman across 2–8 numeric fields. |
| `histogram` | core | Time-bucketed event counts. |
| `field_timeseries` | core | Per-value counts bucketed over time. |
| `time_punchcard` | | Counts by day-of-week × hour-of-day (UTC). |
| `field_pivot` | | Top-X × top-Y co-occurrence matrix for two fields. |
| `field_scatter` | | Sampled (x, y) numeric pairs for two fields, plus full-data correlation/regression. |
| `compare` | | Two filtered layers of the same timeline (time/terms/numeric). |
| `run_anomaly_detector` | core | Run a statistical detector; persists a `DetectorRun`. Exposes the same tuning surface and bounds as the HTTP endpoint. |
| `propose_finding` | core | Finding card with applicable Explorer filters. |
| `propose_chart` | | Chart card, validated by executing the underlying query. |
| `propose_annotation` | core | Propose tagging/commenting events; conversation-bound only, analyst must confirm. |
| `semantic_search` | | Events similar to free text (embeddings-gated). |
| `similar_events` | | Events similar to an existing event (embeddings-gated). |
| `list_baselines` | | Saved baseline definitions — unlocks the temporal-only detectors. |
| `list_dispositions` | | Analyst verdicts on anomaly findings. |
| `list_saved_views` | | The analyst's saved filter views. |
| `list_annotations` | | Annotations across the timeline's sources. |
| `get_event_annotations` | | All annotations on one event. |
| `list_sigma_rules` | | Sigma rules available to the case. |
| `get_sigma_rule` | | One rule including full YAML. |
| `list_sigma_runs` | | Past Sigma evaluations. |
| `get_sigma_run` | | One run's full per-rule results. |

All results are budget-capped (row caps, string truncation) because they land
in the model's context window; the viz tools carry tighter caps than the
Visualize page's own bounds (`VIZ_*_MAX_*` constants — every scatter point
counts against the window). The metadata list tools go through `_listing`,
which caps at `MAX_LIST_ROWS` and reports **`returned` alongside `total`** —
a silently partial set the model reasons over as whole is exactly what the
system prompt's evidence rule forbids.

`propose_finding`'s `FilterSpec` uses the exact Explorer filter shape
(including `annotated`, `annotation_tag_value`, `run_id`, `event_ids`,
`collapse_routine`); the backend echoes the current hit count and the
frontend renders an "Apply to Explorer" card (`run_id` maps onto
`EventFilters.anomalyRunId`).

### `propose_chart` — isomorphic with the analyst's `ChartConfig`

`propose_chart(title, description, spec)` mirrors the Visualize page's
`ChartConfig` field for field (`chart_type`, `scale`, `field`, `field_y`,
`fields`, `metric`, `filters`, `compare{mode, filters}`, `facet{field, limit}`,
`options{…}`) — anything an analyst can build by hand the agent can propose.
It replaced a flattened `kind` enum that could address only 7 chart types and
silently rendered a requested pie as a bar.

The field slots are not interchangeable, and the table says which a mark
takes: `field_y` is **required** by pivot/sankey/scatter (`requires_second
_field`) and **optional** on box/violin (`accepts_second_field`), where it is
a categorical grouping variable producing one distribution per group;
`fields` is a 2–8 token list used only by the correlation matrix
(`multi_field`); `facet` draws the same mark once per top value of a
categorical field (`supports_facet`) and is mutually exclusive with
`compare` — one splits the data across panels, the other overlays two layers
in one panel.

- **Legality is enforced from one table.** `agent/chart_meta.py` is the
  source of truth (which scales each mark admits, comparison support, second
  field, options); `frontend/src/components/viz/lib/chartMeta.ts` is
  **generated** from it by `scripts/gen_chart_meta.py` (committed;
  `tests/test_chart_meta.py` asserts regeneration is a no-op). The analyst
  gets the rules as UI affordances; the agent gets them as validation errors
  that name the legal alternatives — the error *is* the dropdown.
- Rejections fire before any query (illegal scale/type pair, missing or
  superfluous fields, unsupported comparison, illegal metric, unknown field
  token with `difflib` near-miss suggestions) and after it for the
  silent-success cases (numeric chart over a non-numeric field, scatter with
  no numeric pairs). Errors the model must recover from say what to do, not
  only what is wrong — e.g. the field × field grid is `pivot`, not
  `heatmap` (one field × time), and the rejection names that fix.
- **Statistics are server-computed, never eyeballed.** ClickHouse natives
  supply the descriptive side (`corr`, `rankCorr`, `simpleLinearRegression`,
  `skewPop`, quantiles) over the **full** filtered data; `vestigo/stats.py`
  (pure Python, no scipy) adds only what ClickHouse has no aggregate for —
  p-values, Kendall's tau-b, Shapiro–Wilk — and the response labels which
  numbers came from a sample. Correlations are **pairwise-complete**: each
  pair reports the `n` it was computed over, so a sparse field cannot
  silently shrink the other pairs.
- **Mark-choice cautions are warnings, not rejections.** A pie past
  `PIE_COMFORTABLE_MAX` slices, or with two slices within 10% of each other,
  still validates — with a warning naming bar/waffle. Refusing would be
  paternalistic; staying silent would let the model ship an unreadable chart.
- **The result echoes what will be drawn**: `{ok, resolved{…}, warnings,
  summary}`. `AgentPanel.tsx` gates card creation on `ok`; the system prompt
  requires the model to check `resolved`. Warnings carry ignored options and
  clamped validation-query limits (those clamps bound the *tool result*,
  never the analyst's card).
- The chat renders a live chart card (`ChartProposalCard.tsx`) fetched fresh
  through `vizApi` — not the tool-result echo — with **Open in Visualize**
  and **Save** (the analyst's own click; the agent never writes a chart).
  `specToChartConfig` maps the spec; `specToChartConfigLegacy` is a frozen
  translation for persisted old-shape `tool_args`. Virtual `time:` fields are
  analyst-facing too (`viz/lib/fieldDisplay.ts` labels tokens and values;
  canonical values, not labels, round-trip into filters/URLs/saved charts).

### Per-tool enable/disable (three layers)

Every tool is toggleable — none are hard-wired on. A tool is available only
if *no* layer denies it:

1. **Admin hard-deny** — `agent_settings.disabled_tools` /
   `VESTIGO_AGENT_DISABLED_TOOLS`. Applies to the in-app agent **and**
   `/mcp`; users cannot re-enable. Edited on `Admin → Agent`.
2. **Per-user defaults** — `users.preferences["agent_disabled_tools"]`
   ("Save as my defaults" in the tool-selector popover).
3. **Per-chat choice** — `agent_conversations.disabled_tools`, frozen at
   creation. Later preference/admin edits never mutate an existing
   conversation's list (the admin layer still applies at turn time).

`AgentScope.disabled_tools` carries the union of layers 1+3 (`/mcp`: layer 1
only) and `build_tool_server` removes those tools after registration — a
disabled tool is *absent* from the tool list and the model's prompt, not an
error-returning stub. Disabling the propose tools degrades the sandbox+apply
workflow to prose-only; the popover warns but does not prevent it. The
popover's **Core / All** presets are just deny-list generators over layer 2/3.

Tool-selection changes apply from the **next turn**, are genuine partial
updates (omitting `disabled_tools` in a PATCH leaves it alone — `[]` means
"re-enable everything"), and are audited as
`agent.conversation_tools_changed` with before/after lists.

### OPSEC disclosure

The OPSEC notice ("Evidence leaves Vestigo…", with the *actual* configured
endpoint URL and model name) is a persistent element of `AgentPanel.tsx`,
shown in the empty state above the input — deliberately no "don't show
again". Both it and the tool selector draw from `GET /api/agent/info`:
model, provider, `api_base_url`, `context_window`, the tool catalog with
`admin_disabled` flags, and the user's saved defaults. Model + base URL are
disclosed to **all authenticated users** (that disclosure *is* the OPSEC
feature); the API key is never included.

### Conversation JSON export

`GET /api/cases/{case_id}/agent/conversations/{id}/export` (owner-only,
audited, *not* gated on agent availability — the record must stay exportable
while the LLM endpoint is down) returns the whole thread: `export_version`,
the conversation row (incl. `model_id`, `disabled_tools`), every message row
(user/assistant/tool/thinking/window, with tool args/results and measured
token usage), the proposals, and `raw_history` — the provider-wire
pydantic-ai history blob (the only place thinking signatures live).

## Configuration

| Variable | Meaning |
|---|---|
| `VESTIGO_AGENT_MODEL` | Model name (e.g. `qwen3:32b`, `kimi-for-coding`). Required. |
| `VESTIGO_AGENT_PROVIDER` | Wire protocol: `openai` (default) or `anthropic`. |
| `VESTIGO_AGENT_API_BASE_URL` | Endpoint base URL. Required for `openai`; defaults to Anthropic's API for `anthropic`. |
| `VESTIGO_AGENT_API_KEY` | API key, if the endpoint needs one. |
| `VESTIGO_AGENT_USER_AGENT` | UA header for endpoints that gate on client identity. |
| `VESTIGO_AGENT_EXTRA_HEADERS` | JSON object of extra HTTP headers. |
| `VESTIGO_AGENT_MAX_TURNS` | Model round-trip cap per user message (default 15). |
| `VESTIGO_AGENT_REASONING_EFFORT` | `off` (default) / `low` / `medium` / `high` / `max`. See **Reasoning effort**. |
| `VESTIGO_AGENT_CONTEXT_WINDOW` | Model context window in tokens (≥1024). Unset = the sliding window engages only reactively after an overflow. |
| `VESTIGO_AGENT_TOOL_FIDELITY` | How much of an example record tool results carry: `full` (default) / `message` / `minimal` / `auto`. |
| `VESTIGO_AGENT_DISABLED_TOOLS` | JSON array of tool names to hard-deny everywhere (in-app + `/mcp`). |
| `VESTIGO_AGENT_PROBE_TTL_SECONDS` | Availability probe cache (default 60). |
| `VESTIGO_AGENT_SECRET_MODE` | `db` (default) or `env-only`: refuse DB storage of the API key; `VESTIGO_AGENT_API_KEY` becomes the only source. Env-only, not admin-editable. |
| `VESTIGO_MCP_ENABLED` | Serve the external `/mcp` endpoint (default `false`). Independent of `VESTIGO_AGENT_*`. |

Works with any OpenAI-compatible endpoint (ollama, vllm, llama.cpp server,
LocalAI, OpenRouter, `api.moonshot.ai/v1`) or Anthropic-compatible endpoint.
Like the embeddings endpoint and OIDC, agent config is independent of
`VESTIGO_ALLOW_ONLINE` — pointing Vestigo at an endpoint is an explicit
operator decision.

### DB-backed settings, env-wins precedence

Every field above except `VESTIGO_AGENT_PROBE_TTL_SECONDS`,
`VESTIGO_AGENT_SECRET_MODE`, and `VESTIGO_MCP_ENABLED` is also editable from
`Admin → Agent`, backed by a singleton `agent_settings` row.
`resolve_agent_config()` (`agent/config.py`) resolves **per field**: env var
if set, else DB value, else hardcoded default — so an operator can pin
`VESTIGO_AGENT_API_KEY` while leaving `model` admin-editable. The resolved
`AgentConfig.sources` dict records which layer won each field; the admin UI
renders env-pinned fields disabled with a `pinned by VESTIGO_AGENT_<FIELD>`
badge. The API key is never round-tripped in plaintext (only `api_key_set`);
the DB-stored key is plaintext at rest, which `VESTIGO_AGENT_SECRET_MODE=
env-only` avoids entirely. Resolved configs are cached per-fingerprint, so
admin edits take effect on the next call without a restart, and a `PUT`
resets the availability-probe cache.

`POST /api/admin/agent-settings/models` (admin-only) lists the model ids the
configured endpoint advertises, so the model field can be a dropdown. It
accepts the **unsaved** form credentials (omitted fields fall back to the
resolved config), but env-pinned fields are not overridable per request —
otherwise overriding `api_base_url` while the key stays pinned would ship
the operator's key to a host the caller chose. It always returns 200
(failures yield `[]` and the UI falls back to free-text entry), persists
nothing, and reaches only the operator's own configured endpoint.

## Context management

Three mechanisms keep the agent inside a model's context window. All are
deterministic: a tier or window decision is a function of static
configuration and the message list alone — never of what already ran —
because replaying a conversation's tool calls under the same configuration
must produce byte-identical results.

### Per-request fixed overhead (A13)

Tool schemas and the system prompt are resent with every model request.
Three levers (all in 1.4.1) cut the fixed overhead from ~17.3k tokens to
~11.2k for all tools / ~6.8k for the core profile:

- **Schema slimming + prose relocation** (`agent/schema_slim.py`): drop
  pydantic's generated `title`/null-arms/`default: null`, strip per-field
  prose from the repeated `$defs`, and render that prose **once** into the
  system prompt (`spec_reference_block`, generated from the models' own
  `Field(description=…)` values so it cannot drift). We advertise slim and
  validate full (`Tool.parameters` only, never `Tool.fn_metadata`). Null arms
  survive on required fields — dropping them would advertise a narrower
  contract than pydantic validates.
- **Tool profiles**: the Core preset denies everything `tier="extended"`,
  reclaiming ~7.7k chars of schema directly.
- **Compact tool-result encoding** (`agent/encoding.py`): columnar
  row-encoding states keys once instead of per row; timeseries hoist the
  shared time axis. Byte-identical values — a reshaping, not a
  summarisation (30–84% smaller at cap). Each result carries its own
  `columns` legend, because replayed history can legitimately mix old
  dict-shaped and new columnar results. Re-encoding happens at the agent
  boundary (`_columnize`), never inside `db/queries.py`, whose shapes the
  Explorer/Visualize frontends depend on.

Both the slim schemas and the encoding notes reach the external `/mcp`
surface too: `SPEC_REFERENCE` and `RESULT_FORMAT_NOTE` are appended to
`FastMCP(instructions=…)`, sharing the exact strings the in-app
`SYSTEM_PROMPT` composes from. `tests/test_agent_schema.py` holds a budget
guard (serialized tool list < 40,000 chars) — if a change trips it,
re-measure rather than raising the ceiling.

Detector findings additionally reduce their inline example event in the
**model's copy** to `event_id` + truncated `message`
(`_deflate_findings` — on the turn that motivated it: 33.7k → 15.7k tokens);
the persisted `DetectorRun` keeps the full event. The `message` survives
deliberately: for value-shaped detectors the message *is* the finding, and
dropping it would force `get_event` follow-ups that cost more than it saves.
Reduced payloads carry a `note` naming `get_event` as the way back.

### Tool-result fidelity

`agent/fidelity.py` expresses *how far* to reduce as three named tiers:

| tier | an event record carries | for |
|---|---|---|
| `full` (default) | the whole event inline — message at 500, attributes | large windows |
| `message` | identity fields + `message` at 200; no attributes | ~64k local models |
| `minimal` | identity fields alone | last resort |

A fourth value `auto` derives the tier from `context_window`: ≥100k →
`full`, ≥32k → `message`, below → `minimal`, unset → `message` (an admin who
picked `auto` asked to be kept inside a window, not assumed to have room).
The default `full` assumes an unconstrained deployment; the tradeoff is that
a broad turn on an unconfigured small model overflows on attempt 0 and
succeeds on the sliding-window retry.

It applies to both transports and only to the tools that return *many* event
records (`FIDELITY_TIERED_TOOLS`: `search_events`, `semantic_search`,
`similar_events`, `run_anomaly_detector`). Deliberately exempt: `get_event`
and `get_event_annotations` (the escape hatches — tiering them would leave
the model looping on a reduction it cannot undo) and `list_annotations`
(annotation bodies are analyst-written evidence, not illustrative records).
Every tiered result carries `fidelity`, at `full` too; the `note` appears
only when the tier actually dropped something. Per-event attribute caps
(`MAX_ATTRS_PER_EVENT`, `ATTR_VALUE_TRUNCATE`) are not tiered — they guard
against a single pathological event, not the window.

### Sliding context window

`agent/window.py` is the one overflow mechanism (1.5.0), replacing the
earlier fidelity overflow-ladder and LLM history compaction (retired: the
summarizer ran on the same possibly-small model and was nondeterministic).

- **Mechanism.** A pydantic-ai `ProcessHistory` capability runs
  `apply_window(messages, budget)` before **every model request — mid-turn
  included** (the failing case overflowed twice inside its first turn, where
  compaction had nothing to fold). Three passes, cheapest first:
  1. *Elide*: oldest-first, each `ToolReturnPart`'s content becomes
     `{"elided": true, "note": …}` until the estimate fits. Structure is
     untouched, so tool pairing and role alternation survive on every
     provider protocol.
  2. *Drop turns*: the oldest user turns are replaced with one marker pair;
     cuts land on user-turn boundaries only.
  3. *Truncate the newest returns*: last resort, the only pass touching the
     request the model is about to reason over — one tool result larger than
     the whole budget is invisible to the first two passes. Content becomes
     `{"truncated": true, "note": …, "head": …}` with a leading slice (never
     below `MIN_KEEP_CHARS`, 500) so the model can narrow the re-run itself.

  Never elided: the first user request (case/timeline context), the most
  recent request's tool returns (pass 3 may still truncate them), the last
  user turn, and all assistant prose. The budget is `context_window × 0.8 −
  est(system prompt)`; estimates are chars/4.
- **Transparent to the model.** The stubs sit in the replayed history and the
  system prompt explains them, so the model re-runs narrower or fetches via
  `get_event` instead of reasoning over a silent gap.
- **Deterministic, applied at send time.** `apply_window` is a pure function
  of (messages, budget). The stored history blob stays complete — the window
  rewrites the outgoing request, never the record.
- **Reactive backstop.** With `context_window` unset, a provider 400/413
  matching `_is_context_overflow` (deliberately narrow phrasings) enables the
  window and re-runs the turn **once**. The budget comes from the error
  body's window hint when the provider names one, else from the estimated
  pre-turn size (×0.8); an already-active window tightens (×0.6) instead. A
  second overflow surfaces `error{code="context_overflow"}`. A learned
  budget persists per conversation (`get_last_window_budget`) so the next
  turn doesn't repeat the failed round trip; configured `context_window`
  always wins.
- **Forensic trail.** A reduced turn persists one append-only `role="window"`
  message row (reason, attempt, budget, counts, before/after estimates — the
  turn's single largest reduction) plus an `agent.window` audit row, written
  on *every* exit including stop and error. The chat renders them (SSE
  `window` event), because the case file must answer "why is there less here
  than there" from itself. Historical transcripts may still carry
  `compaction`/`fidelity` marker rows from the retired mechanisms; the panel
  renders those read-only.
- **A retry re-runs the tools, and two of them write.** Re-executed
  `run_anomaly_detector` runs are tagged (`params["agent_retry_attempt"]`)
  so the Analysis page's superseded re-runs are distinguishable from an
  analyst scanning twice; the `role="window"` overflow marker delimits the
  attempts in the message log.

### How a turn can end early

Every terminal SSE `error` carries a `code`:

| `code` | Raised by | Means |
|---|---|---|
| `context_overflow` | overflow persisting after the windowed retry | Start a new conversation |
| `model_error` | any other 4xx/5xx from the endpoint | Endpoint/config problem — server logs |
| `tool_retry_exhausted` | `UnexpectedModelBehavior` | The model couldn't call one tool correctly within its retry budget |
| `turn_limit_reached` | `UsageLimitExceeded` | The turn spent all `max_turns` requests — narrow the ask or raise the setting |

The tool retry budget is `Agent(..., retries=3)`: tool-legality errors name
the legal alternative and are *meant* to be acted on, so pydantic-ai's
default single correction attempt was too tight — but every retry is also a
model request counted against the turn limit, so not more than three.
Whatever streamed before an early end persists with an ` [interrupted]`
marker.

## Provider notes

### Reasoning effort

`reasoning_effort` (closed enum `off`/`low`/`medium`/`high`/`max`; `off`
sends nothing at all) is translated by `runtime.py::effort_model_settings`
into the wire shape the endpoint expects:

| `reasoning_effort` | OpenAI-protocol | Anthropic-protocol (non-Kimi) | Kimi `/coding` |
|---|---|---|---|
| `low` | `openai_reasoning_effort="low"` | `anthropic_thinking budget_tokens=2048` | `reasoning_effort="low"` |
| `medium` | `="medium"` | `budget_tokens=8192` | `reasoning_effort="high"` |
| `high` | `="high"` | `budget_tokens=24576` | `reasoning_effort="high"` |
| `max` | `="max"` | `budget_tokens=32768` | `reasoning_effort="max"` |

Anthropic-protocol endpoints have no discrete effort enum on the wire, only
a thinking-token budget (`_ANTHROPIC_THINKING_BUDGETS`). Kimi's `/coding`
endpoint uses its own coarser tiers via a top-level `reasoning_effort`
field in the JSON body (`extra_body`), not the Anthropic `thinking` object
(`_KIMI_EFFORT`; mapping per Kimi's third-party coding-agent docs — treat as
experimental pending a raw request capture against the `/v1/messages`
route).

### Kimi coding plan

- `https://api.kimi.com/coding` speaks the **Anthropic Messages protocol**
  (`sk-kimi-*` keys). The pay-per-token platform is separate:
  `api.moonshot.ai/v1`, OpenAI protocol.
- The `/coding` endpoint **403s unless the User-Agent identifies a coding
  agent** — set `VESTIGO_AGENT_USER_AGENT=claude-code/0.1.0`. Vestigo
  deliberately does not hardcode a spoofed UA; the operator sets it.
- The availability probe uses `{base}/v1/models` (an OpenAI-compatible model
  list Kimi serves on the coding endpoint).
- With server-side thinking active, Kimi requires replayed assistant
  tool-call messages to carry an (unsigned) thinking block; stock
  pydantic-ai replays only *signed* blocks, so `runtime.KimiAnthropicModel`
  injects unsigned ones for `api.kimi.com/coding` base URLs.

## Testing

- `tests/test_agent_api.py` — availability gate, 503 gating, conversation
  CRUD + per-user privacy, the full streamed loop over a stubbed MCP tool
  server with pydantic-ai's `FunctionModel` (no real LLM), thinking-event
  mapping + persisted rows, the Kimi replay shim, `effort_model_settings`,
  the proposal lifecycle over HTTP (confirm writes + audits, idempotent
  409 on redecide, owner-only), admin settings round-trips, `/api/agent/info`
  shape + key-never-leaks, the JSON export, mid-turn elision end-to-end, and
  the reactive overflow retry.
- `tests/test_agent_window.py` — elision order, protected regions,
  turn-dropping never orphans tool returns, purity and determinism.
- `tests/test_agent_tools.py` — registry parity, read-parity tools, the
  extended `FilterSpec`/detector-tuning surface, `propose_annotation`,
  disabled tools absent from `list_tools` and erroring on call.
- `tests/test_agent_schema.py` — schema slimming + the serialized-size
  budget guard.
- `tests/test_agent_tokens.py` / `tests/test_mcp_http.py` — token model +
  management API (create/list/revoke, RBAC), token lifecycle over HTTP,
  scope binding, an end-to-end tool call, off-by-default 404, admin deny
  list on the external `tools/list`, `propose_annotation` absent from `/mcp`.
- `tests/test_chart_meta.py` — chart-meta legality table + generated
  frontend copy is regeneration-stable.
- Frontend: `frontend/src/test/agent.test.ts` — FilterSpec → EventFilters
  mapping.
