# AI Investigation Agent

Optional, off-by-default AI assistant embedded in the Explorer. It drives the
iterative analysis loop on the analyst's behalf — search, aggregate, run
detectors, refine — and hands results back as **findings**: filter sets the
analyst can apply to the Explorer with one click. Update this document
alongside any agent change, like `ANOMALY_DETECTION.md` for detectors.

Code lives in `src/vestigo/agent/` (runtime, tools, config, context
management) and `api/routers/agent.py` (HTTP/SSE layer).

## Design invariants

- **Sandbox + apply.** The agent queries the backend in its own loop and never
  mutates the analyst's view. Findings render as cards; only an explicit
  analyst click applies filters (through the normal URL-driven filter path,
  `frontend/src/lib/queryParams.ts`).
- **Propose→confirm writes.** The agent itself never writes an annotation.
  `propose_annotation` and `propose_story_block` (available only once a
  conversation is bound) record an
  `AgentProposal` row — it does not touch `annotations` or `story_blocks`. An
  analyst confirms or rejects via `POST .../proposals/{id}/confirm|reject`; the
  decide is an atomic `UPDATE … WHERE status='proposed'`, so a second
  confirm/reject 409s. `AgentProposal.kind` discriminates what gets applied:
  - `annotation` — confirm re-resolves the events against the *current* scope,
    writes one annotation per still-resolving event with
    `origin="agentic-analysis"` and `created_by` set to the confirming analyst,
    and reports `skipped_event_ids`.
  - `story_block` (W7) — confirm creates the block with `origin: agent` in the
    story named by `payload`; an inline `chart_spec` is saved as a SavedChart
    first, so the block references a persisted object like every other embed. A
    story deleted since propose time still decides the proposal and reports
    `applied: false` with a reason. See `docs/STORIES.md`.
  `run_anomaly_detector` is the only other write-shaped tool (it persists a
  `DetectorRun`, same as an analyst-triggered scan).
  - **Origin is provenance, not a visibility class.** A confirmed
    `agentic-analysis` annotation behaves exactly like a manually-typed one in
    tag autocomplete, the annotated/tag Explorer filters, and deletion
    (`USER_VISIBLE_ANNOTATION_ORIGINS = ("user", "agentic-analysis")`); only
    `origin="system"` stays outside that set.
  - Every decision is audited (`agent.annotation_confirm` /
    `agent.annotation_reject`, `agent.story_block_confirm` /
    `agent.story_block_reject`, keyed to `target_type="agent_proposal"`).
  - `propose_annotation`, `propose_story_block` and the decide endpoints are
    **absent from the external `/mcp` transport** — only an in-app conversation
    binds the `conversation_id` that gates the tools' registration. Story
    *read* tools are exposed there like every other read tool.
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
registrations. The two `embeddings_gated` tools are **removed** from the
server when embeddings are unconfigured, not left as error stubs — an
unavailable subsystem costs no schema tokens and gets no calls
(`core/capabilities.py`). `tier="core"` marks the 11-tool lean profile for
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
| `field_scatter` | | Sampled (x, y) numeric pairs for two fields, plus full-data correlation/regression. Samples are drawn in a stable hash order, so an identical query redraws identical points. |
| `compare` | | Two filtered layers of the same timeline (time/terms/numeric). |
| `run_anomaly_detector` | core | Run a statistical detector; persists a `DetectorRun`. Exposes the same tuning surface and bounds as the HTTP endpoint. |
| `propose_finding` | core | Finding card with applicable Explorer filters. |
| `propose_chart` | | Chart card, validated by executing the underlying query. |
| `propose_annotation` | core | Propose tagging/commenting events; conversation-bound only, analyst must confirm. |
| `propose_story_block` | core | Propose adding a block to a story; conversation-bound only, analyst must confirm. |
| `semantic_search` | | Events similar to free text (embeddings-gated: absent when embeddings are unconfigured). |
| `similar_events` | | Events similar to an existing event (embeddings-gated: absent when embeddings are unconfigured). |
| `list_baselines` | | Saved baseline definitions — unlocks the temporal-only detectors. |
| `list_dispositions` | | Analyst verdicts on anomaly findings. |
| `list_saved_views` | | The analyst's saved filter views. |
| `list_stories` | | The case's stories (the analyst's report documents). |
| `read_story` | | One story's ordered blocks — markdown text and embed references. |
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

`read_story` is the one read whose payload is prose rather than records, and
it is budgeted differently for that reason: markdown is capped per block
(`STORY_TEXT_TRUNCATE`) *and* per response (`STORY_TEXT_BUDGET`), spent in
document order so a long story degrades block by block while every block still
returns its id/kind/origin. Any cut carries `truncated: true` and the real
`text_length`, with `truncated_blocks` on the response. The earlier cap
(`ATTR_VALUE_TRUNCATE * 8`, 1600 chars) treated a report the analyst may write
up to 256 KiB of as if it were an attribute value, and cut it unmarked.

`propose_finding`'s `FilterSpec` uses the exact Explorer filter shape
(including `annotated`, `annotation_tag_value`, `run_id`, `event_ids`,
`collapse_routine`); the backend echoes the current hit count and the
frontend renders an "Apply to Explorer" card (`run_id` maps onto
`EventFilters.anomalyRunId`).

### Nested arguments a provider stringified

Not every provider emits a nested object argument as an object; some hand it back
as JSON *text*. pydantic-ai parses only the **top** level of a tool call's
arguments, and the MCP SDK's `pre_parse_json` likewise — so a top-level argument
always arrives parsed and anything nested arrives exactly as the provider sent it.
Rejecting that costs a retry the model has to guess its way out of, on tools it is
otherwise using correctly (`FilterSpec` is a nested argument on 14 of them).

`ObjectArgModel` in `agent/tools.py` is the base for every nested-argument model
and the single place this is handled. It coerces the model itself and every
**field** whose annotation admits a JSON object, so `ChartSpec.options` (a model)
and `FilterSpec.filters` (a plain `dict`) are covered by one mechanism with no
per-field opt-in. The rule is `_admits_json_object`: a field qualifies when some
union member is a mapping or `BaseModel` and **no** member is `str`. That
exclusion is the whole safety argument — `q: str | None` may legitimately hold
`'{"a": 1}'` as a free-text search, and coercing it would rewrite the analyst's
query. Coercion works on a copy: the same mapping is the call's `tool_args`,
persisted verbatim, and a forensic record must not change shape between being
stored and being validated.

Both halves are enforced rather than merely documented, because both fail
*silently*. `test_every_nested_argument_model_derives_from_object_arg_model` walks
the built server's real signatures (transitively through model fields) and fails
if any model in a nested position is a bare `BaseModel`; the same walk feeds
`test_spec_reference_covers_every_nested_argument_model`. And
`_admits_json_object` **raises** on an annotation it cannot decide rather than
answering "no" — quietly dropping a field from coercion is the failure mode this
path exists to make loud.

The frontend needs the mirror image, because `tool_args` is persisted exactly as
emitted — a bad row is permanent and every re-render reads it. `specToChartConfig`
and `specToEventFilters` (`api/agent.ts`) normalize through `parseToolArgObject`
at the translation boundary, so no caller has to remember to; `AgentPanel` uses it
as the render-or-don't decision, and an unusable spec produces no card rather than
a throw.

### `propose_chart` — isomorphic with the analyst's `ChartConfig`

`propose_chart(title, description, spec)` mirrors the Visualize page's
`ChartConfig` field for field (`chart_type`, `scale`, `field`, `field_y`,
`fields`, `metric`, `filters`, `compare{mode, filters}`, `options{…}`) — anything
an analyst can build by hand the agent can propose. It replaced a flattened `kind`
enum that could address only 7 chart types and silently rendered a requested pie
as a bar.

The field slots are not interchangeable: `field_y` is **required** by
pivot/sankey/scatter and **optional** on box/violin, where it is a categorical
grouping variable producing one distribution per group; `fields` is a 2–8 token
list used only by the correlation matrix.

- **Legality is enforced from one table.** `agent/chart_meta.py` is the source of
  truth (admissible scales per mark, comparison support, second field, options, and
  since `ChartConfig` v2 each figure's `inputs`, `derives`, `question` and
  `supports_marks` — `docs/VISUALIZE.md`);
  `viz/lib/chartMeta.ts` is **generated** from it by `scripts/gen_chart_meta.py`
  (committed; `tests/test_chart_meta.py` asserts regeneration is a no-op). The
  analyst gets the rules as UI affordances; the agent gets them as validation
  errors naming the legal alternatives — the error *is* the dropdown. Rejections
  fire before any query (illegal scale/type pair, missing or superfluous fields,
  unsupported comparison, illegal metric, unknown field token with `difflib`
  near-miss suggestions) and after it for silent-success cases (numeric chart over
  a non-numeric field, scatter with no numeric pairs).
- **Derivations are validated from the registry.** `ChartSpec.derive` (the same
  `DeriveSpec` the viz endpoints parse — `docs/VISUALIZE.md` §"Derivations") is
  admitted only by figures whose `derives` lists its kind (bar, heatmap, pivot, sankey
  today; the rejection names the figures that take it), never on a `time:` field
  (already a calendar part), and forces `scale="ordinal"` — an omitted scale resolves
  to ordinal, any other is rejected. Bins that find no numeric value after the scan are
  rejected too, with the categorical alternative. `describe_field` reports
  `derivations` so the model can see which apply before proposing. The resolved echo
  carries `derive`.
- **Statistics are server-computed, never eyeballed.** ClickHouse natives supply
  the descriptive side (`corr`, `rankCorr`, `simpleLinearRegression`, `skewPop`,
  quantiles) over the **full** filtered data; `vestigo/stats.py` (pure Python, no
  scipy) adds only what ClickHouse has no aggregate for — p-values, Kendall's
  tau-b, Shapiro–Wilk — and the response labels which numbers came from a sample.
  Correlations are **pairwise-complete**: each pair reports the `n` it was
  computed over. Where a statistic could not be computed, the response says so
  rather than falling back silently (`recommendation_basis`, `bin_rule`).
- **Mark-choice cautions are warnings, not rejections.** A pie past
  `PIE_COMFORTABLE_MAX` slices, or a grouped box/violin whose grouping field
  exceeds `VIZ_GROUP_CARDINALITY_CAUTION` distinct values, still validates with a
  warning naming the better mark. Refusing would be paternalistic; staying silent
  would let the model ship an unreadable chart.
- **Bad input is refused, not quietly narrowed.** `field_correlation` rejects a
  field list that is too long or repeats a token rather than truncating — a
  silently truncated matrix answers a question the model never asked.
- **The result echoes what will be drawn**: `{ok, resolved{…}, warnings, summary}`.
  `AgentPanel.tsx` gates card creation on `ok`. Warnings carry ignored options and
  clamped validation-query limits (those clamps bound the *tool result*, never the
  analyst's card).
- The chat renders a live chart card fetched fresh through `vizApi` — not the
  tool-result echo — with **Open in Visualize** and **Save** (the analyst's own
  click; the agent never writes a chart). **Save** stores `spec.filters` with the
  chart, `event_ids`/`run_id`/`collapse_routine` included, so a chart the agent
  scoped to one detector run cannot widen to the whole timeline. **Open in
  Visualize** cannot carry those three — the chart is not saved yet, so there is
  no id to name. Saving first is what makes it addressable (`?c_chart=<id>`).

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

## Outside the agent loop: the column advisor

One feature reaches the configured LLM endpoint without being an agent turn:
the event-grid column suggestion (`src/vestigo/columns/advisor.py`, issue
#213). It is documented here because it shares this subsystem's configuration
and its availability gate — not because it shares its machinery. It has no
conversation, no tool server, no history, no proposals, and writes nothing an
analyst has to decide on.

**What it does.** A deterministic sweep over the per-source field-statistics
cache (`db/field_stats.py`) has already scored every candidate column before
the model is involved. The model receives a candidate table — token, how many
sources carry it, fill rate, distinct count, three sample values — and returns
3–5 of those tokens in a useful order, as a pydantic `output_type`
(`ColumnChoice`). That is the whole interaction: one request, no tools.

**How the invariants apply.**

| Invariant | How it holds here |
|---|---|
| Invisible unless configured | Gated on the same cached `agent_available()` probe `/api/health` uses. Unconfigured or unreachable ⇒ never called, and the deterministic answer ships instead. The "Suggest with AI" button is gated on the same `capabilities.agent`, so an instance with no model configured renders no entry point either. |
| Scope safety | The model is given no case, timeline, source or event id, and no event row. It *is* given up to three real sample values per candidate field (40 characters each, ≤20 fields) — evidence-derived strings, which is why the path is opt-in rather than on by default. `tests/test_columns_api.py` asserts the rendered prompt against that promise. |
| Sandbox + apply | The result is a *default*, not a mutation. It lands in `Timeline.recommended_columns`; any analyst's own column choice outranks it, and "Reset to defaults" is one click. |
| Forensic reproducibility | Every run writes a `timeline.recommend_columns` audit row with the method, the model id, the chosen columns and the full candidate set. The heuristic half is deterministic and unit-tested; the LLM half is recorded as `method: "llm"` so a suggestion is never mistaken for a computation. |
| Bounded trust | Every returned token is intersected with the candidate set — the model cannot introduce a field. A response that falls below the minimum after filtering is rejected whole rather than padded. Malformed, timed out (45 s ceiling), or errored is indistinguishable from "not configured". |

**What it deliberately does not do.** It does not go through `build_tool_server`
or `FastMCPClient`. Driving the real tools would mean `describe_field`'s two
live ClickHouse scans per field across ~50 candidates, for data the stats cache
already holds exactly; the tools are also timeline-scoped where the evidence
here is per-source. The tool-deny layers are not bypassed by this, because no
tool is called.

**Opt-in per analyst, per timeline — with the disclosure attached to the
opting.** There is no instance-wide setting, deliberately: whether the model half
is *available* is already answered by whether an agent endpoint is configured, and
a second tri-state on top of it only made the disclosure incoherent.

The job takes `use_llm`, defaulting to False. Every automatic trigger —
post-ingest, timeline creation, the CLI, the demo build — leaves it there, so the
scorer runs locally and **egress is never a side effect of uploading a file**.
Exactly one caller sets it: `POST …/recommend-columns` with `{"use_ai": true}`,
behind a disclosure (`ColumnAdvisorNotice.tsx`) naming exactly what would be sent,
the endpoint URL and the model. Confirming records the answer for that timeline
(`preferences.column_advisor_optin`) and only then runs. Cancelling sends nothing
and records `false` — a declined timeline has to be distinguishable from one
nobody has been asked about, or the offer returns on every visit and people learn
to dismiss it unread. The next timeline asks again, because the sample values sent
are that timeline's evidence.

The disclosure refuses to close while its confirm is in flight (Escape, overlay
and X included): a close is recorded as "no thanks", so a dismissal landing
between the opt-in write and its response would race a `false` against the `true`
the analyst just gave.

One derived run exists and is always local: when an ingest trigger is collapsed
into an already-running job (`columns/jobs.py::_DIRTY`), that job re-runs itself
for the sources it read too early with `use_llm` unset, so a burst can never turn
one opted-in "Suggest with AI" into repeated egress.

Two surfaces open the disclosure, both through `useColumnRecommendation.ts` so
neither can send anything the other would not: the **"Suggest with AI" button** in
the Columns picker, and a **one-time offer** the first time someone with
contribute access opens a timeline holding an unanswered `method: "heuristic"`
suggestion. The offer merely appearing sends nothing — same dialog, same
`capabilities.agent` gate — and it never fires for a read-only member, a timeline
with no local suggestion, or one already ranked by the model.

An explicit run from either surface also clears the requesting browser's stored
column override. The per-user choice still outranks *automatic* recomputes, but
pressing the button is asking for the new answer.

The contribute-access check on the endpoint is the authorization; the stored
opt-in is the UI's memory of having shown the disclosure, not a second gate. The
suggestion is shared with everyone who can see the timeline — the opt-in governs
who *causes* egress, not who may read the result, and the audit row names the
analyst who triggered it.

### The converter generator

The second non-agent use of the endpoint (1.13): `src/vestigo/converters/generator.py`
writes a converter script for a plain-text log the analyst uploaded, and
`converters/job.py` runs, validates, repairs and ingests it —
`docs/INPUT_FORMATS.md` §"Generated converters" is the full description. Same
shape as the column advisor — one typed request per attempt, no tools, no
history — and the same wire call: both go through
`src/vestigo/agent/oneshot.py::typed_completion`. Two things differ. It is gated on its own operator switch
(`converter_generation_enabled`, off by default) *in addition to* the agent
probe, because it executes model-written code on the host; and it does not
degrade silently — a script that could not be written is a failed attempt the
analyst sees, not a heuristic fallback.

| Invariant | How it holds here |
|---|---|
| Invisible unless configured | Capability `converter_generation` = switch **and** `agent_available()`. Off ⇒ no "Let AI write the converter" mode, no regenerate button; generation and `regenerate` answer 503. Re-running a *saved* script sends nothing, so it hangs off `converter_reuse` = switch alone: with the model down or never configured the dialog offers *Use a saved converter* only, and `convert` with a `converter_script_id` still runs. Listing and downloading existing scripts stay available — they are records. |
| Scope safety | The task message carries a bounded head/middle/tail excerpt of the raw file, its name, size, line count, mtime, the version to declare and the analyst's hint. No case/source/timeline/user id, key or host name. `tests/test_converter_prompt.py` asserts it; the dialog names the model, endpoint host and byte count before the analyst confirms. Reusing a saved script sends nothing. |
| Sandbox + apply | The script runs under `python -I` in a private working directory with a scrubbed environment and `RLIMIT_AS`/`CPU`/`FSIZE`/`NOFILE` (set by a bootstrap inside the child, never `preexec_fn`), after a best-effort static guard (`runner.check_script`: import allow-list, module aliases usable only as `module.<attr>`, dynamic-lookup and object-graph dunders refused — `docs/INPUT_FORMATS.md` §"The loop" lists it). Its output is *validated* before anything is ingested, and the ingest itself is the ordinary Parquet path — the model never touches ClickHouse or Postgres. |
| Forensic reproducibility | Every attempt (generation — including model errors before a row exists — sample run, full run, and any failure after the converter passed) is on `converter_scripts.attempts` with the report, stderr tail, script hash and the hash of the prompt that round sent; the row keeps the exact sample text, the evidence file's mtime as stated to the model, and `prompt_hash`/`model` of the attempt that produced its stored code. Audit: `converter.generate`, `converter.run`, `converter.regenerate`. The Parquet carries the usual per-row provenance, so the *ingest* is reproducible from the raw file and the script even though the model's output is not. |
| Bounded trust | Validation is what the harness checks, not what the model claims: schema, footer, the raw file's real hash, non-null provenance, parse and timestamp floors. Attempts are capped (`converter_max_attempts`), each run is time- and memory-limited, and the model's proposed name is sanitized to `^[a-z0-9_]+2vestigo$`. |

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
| `VESTIGO_AGENT_PROBE_TTL_SECONDS` | Availability probe cache (default 60). Edited on `Admin → Settings`, not the Agent tab. |
| `VESTIGO_AGENT_SECRET_MODE` | `db` (default) or `env-only`: refuse DB storage of the API key; `VESTIGO_AGENT_API_KEY` becomes the only source. Edited on `Admin → Settings`. |
| `VESTIGO_MCP_ENABLED` | Serve the external `/mcp` endpoint (default `false`). Independent of `VESTIGO_AGENT_*`. |

Works with any OpenAI-compatible endpoint (ollama, vllm, llama.cpp server,
LocalAI, OpenRouter, `api.moonshot.ai/v1`) or Anthropic-compatible endpoint.
Like the embeddings endpoint and OIDC, agent config is independent of
`VESTIGO_ALLOW_ONLINE` — pointing Vestigo at an endpoint is an explicit
operator decision.

### DB-backed settings, env-wins precedence

Every field above except `VESTIGO_AGENT_PROBE_TTL_SECONDS`,
`VESTIGO_AGENT_SECRET_MODE`, and `VESTIGO_MCP_ENABLED` is editable from
`Admin → Agent`, backed by a singleton `agent_settings` row. Those three live
on `Admin → Settings` instead, in the generic `app_settings` layer
(`core/settings_registry.py`); the agent's own row predates it and keeps its
purpose-built resolver, so the two coexist deliberately — same precedence
rules, same "pinned by environment" semantics, different storage.
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
guard (serialized tool list < 41,000 chars; `ChartSpec.derive` measured 39,382 →
40,213 on 2026-08-29 and the ceiling moved by that delta) — if a change trips it,
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

`agent/window.py` is the one overflow mechanism (1.5.0), replacing the earlier
fidelity overflow-ladder and LLM history compaction (retired: the summarizer
ran on the same possibly-small model and was nondeterministic).

- **Mechanism.** A pydantic-ai `ProcessHistory` capability runs
  `apply_window(messages, budget)` before **every model request — mid-turn
  included**. Three passes, cheapest first:
  1. *Elide*: oldest-first, each `ToolReturnPart`'s content becomes
     `{"elided": true, "note": …}` until the estimate fits. Structure is
     untouched, so tool pairing and role alternation survive on every provider
     protocol.
  2. *Drop turns*: the oldest user turns are replaced with one marker pair; cuts
     land on user-turn boundaries only.
  3. *Truncate the newest returns*: last resort, and the only pass touching the
     request the model is about to reason over — one tool result larger than the
     whole budget is invisible to the first two passes. Content becomes
     `{"truncated": true, "note": …, "head": …}` with a leading slice (never
     below `MIN_KEEP_CHARS`, 500) so the model can narrow the re-run itself.

  Never elided: the first user request (case/timeline context), the most recent
  request's tool returns (pass 3 may still truncate them), the last user turn,
  and all assistant prose.
- **The budget reserves all three things that ship in a request** —
  `context_window × 0.8 − est(system prompt) − est(tool schemas)` (`budget_for`).
  The schema share is not optional: 14 of the 30 tools each carry their own copy
  of the `FilterSpec` definition (~13k tokens), invisible to a window processor
  that only sees `messages`. `schema_chars_for_scope` measures the actual
  advertised schemas for the conversation's tool set (it varies with
  `disabled_tools`); the copies cannot be hoisted into one shared `$defs` (the
  OpenAI function-calling wire gives every tool an independent `parameters`
  schema), so the budget counts the duplication honestly instead.
- **Calibrated estimate, not a fixed divisor.** The default is
  `CHARS_PER_TOKEN_DEFAULT = 3.0` — `chars/4` was measured against prose, while
  real tool payloads (escaped JSON, base64 params, dotted-quad IPs, UUIDs)
  measure closer to 2.35, a 1.7× error `MARGIN` cannot absorb. When a provider
  overflow body names the request's token count, `calibrate_chars_per_token`
  derives the true ratio (clamped 1.5–5.0) and persists it under
  `measured_chars_per_token`. Airgapped-safe — no tokenizer, pure Python.
- **Transparent to the model.** The stubs sit in the replayed history and the
  system prompt explains them, so the model re-runs narrower or fetches via
  `get_event` instead of reasoning over a silent gap.
- **Deterministic, applied at send time.** `apply_window` is a pure function of
  (messages, budget). The stored history blob stays complete — the window
  rewrites the outgoing request, never the record.
- **Reactive backstop.** With `context_window` unset, a provider 400/413 matching
  `_is_context_overflow` (deliberately narrow phrasings) enables the window and
  re-runs the turn **once**. The provider's ground truth wins: when the error
  body names a window, the budget is recomputed from it, **whether or not** one
  already exists — only with no hint does an active window fall back to
  tightening (×0.6), because blindly shrinking a configured budget overshoots
  into turn-dropping. A second overflow surfaces `error{code="context_overflow"}`.
  Learned budget and ratio persist per conversation so the next turn does not
  repeat the failed round trip; a configured `context_window` always wins.
- **Per-request tool guard** (`agent/runtime.py::_RequestGuardToolset`). Wraps the
  MCP toolset and, scoped to one model request (`RunContext.run_step`),
  (a) collapses an identical `(tool, canonical-args)` call to a
  `{"duplicate_of": …}` back-reference, and (b) caps one request's total
  tool-return bytes at `budget × 0.5 × chars_per_token`, returning later results
  reduced with a pointer to `get_event`/narrower filters. The dedupe holds under
  pydantic-ai's parallel tool execution: the first caller plants an in-flight
  marker before its first await, so a concurrent identical call awaits the
  original's outcome. Deterministic (keyed on canonical args, reset when
  `run_step` advances); both actions are counted on the turn's `WindowStats` and
  land on the same `role="window"` row. This is reduction-for-fit, recorded in
  the export — distinct from `fidelity.py`'s static per-conversation tier, which
  must never depend on call order.
- **Config guard-rail.** `fidelity_config_warning` flags an explicit
  `tool_fidelity=full` against a `context_window` below `AUTO_FULL_MIN_WINDOW`
  (100k). Advisory only — logged at turn start, surfaced in the admin
  agent-settings `warnings` array, and rendered on the admin agent page.
- **Forensic trail.** A reduced turn persists one append-only `role="window"`
  message row (reason, attempt, budget, counts, before/after estimates) plus an
  `agent.window` audit row, written on *every* exit including stop and error. The
  chat renders them (SSE `window` event), because the case file must answer "why
  is there less here than there" from itself. Every appended message bumps the
  conversation's `updated_at`, so a conversation whose turns all failed does not
  freeze at the last successful turn. Historical transcripts may carry
  `compaction`/`fidelity` rows from the retired mechanisms; the panel renders
  those read-only.
- **A retry re-runs the tools, and two of them write.** Re-executed
  `run_anomaly_detector` runs are tagged (`params["agent_retry_attempt"]`) so the
  Analysis page's superseded re-runs are distinguishable from an analyst scanning
  twice; the `role="window"` marker delimits the attempts in the message log.

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

### Turn checkpointing and resume

`AgentConversation.history` is the only thing a follow-up turn replays; the
`AgentMessage` rows are the human-readable record and never feed the model. It
used to be written only when a turn reached its result, so a stop, a provider
error or a process restart discarded the whole turn.

`stream_turn` drives `agent.iter` and updates a caller-owned `TurnRecorder` after
every tool result and at every node boundary, yielding a router-internal
`checkpoint` event. Each snapshot passes through
`agent/resume.py::repair_partial`, and any checkpoint is replayable on its own.

pydantic-ai already normalizes a history before every request, so
`repair_partial` is not there to make the blob sendable — it is there to make it
*faithful*, and to keep it that way if a future version normalizes differently:

- **Unpaired calls are answered with the part the tool actually produced.** The
  library's own repair substitutes a generic stub; replaying the real
  `ToolReturnPart` — or `RetryPromptPart`, so a rejection is never replayed as a
  success — keeps the resumed history in agreement with the run. Only a call that
  genuinely never returned gets `INTERRUPTED_RESULT`, stamped
  `outcome="interrupted"` so a reader of an export can tell synthesized answers
  from real ones without parsing prose. Answers are inserted immediately
  following the response that made the call, because the Anthropic protocol
  requires that adjacency.
- **A truncated trailing call is kept, not dropped.** A dead model stream can
  leave a `ToolCallPart` with half-written JSON arguments. Removing it would
  rewrite the shape of a `ModelResponse` whose thinking signature was computed
  over the turn that included the call, and this blob is the only place those
  signatures live. Malformed arguments are already sendable (`args_as_dict`
  degrades them), so the call is replayed and answered like any other.
- **The trailing request/response pair is closed** with a `RESUME_MARKER`
  response. Belt-and-braces on 2.17.0, whose private `_merge_consecutive_messages`
  already folds the unmerged shape — but that is private API under a `>=` pin, and
  a checkpoint shaped like a completed turn is one less case for the window and
  the exporter to reason about.
  `tests/test_agent_resume.py::test_the_library_still_merges_adjacent_requests`
  fails loudly if a bump changes it.

No checkpoint is taken before the model commits a response: there would be
nothing to save, and closing the pair on a lone prompt would put a reply to the
analyst in the model's mouth.

Checkpointing is deliberately cheap. The node-boundary checkpoint is skipped when
the node produced nothing mapped, and when its last mapped event was a
`tool_result`, so a 125-tool-call turn takes ~125 snapshots rather than 250.
Reaching the database is throttled separately, because each write is a full
`dump_history` plus a whole-column JSON UPDATE of a growing blob on the event loop
— writing every snapshot costs bytes quadratic in the turn's length.
`_persist_partial` skips a write whose `recorder.revision` has not advanced, and a
periodic one taken less than `_CHECKPOINT_MIN_INTERVAL` (3s) after the last. Every
terminal exit forces the write regardless, so worst-case loss is a few seconds of
tool work, never an analyst's turn.

The router writes each checkpoint and stamps `history_partial_at`. Only a
completed turn clears it; a stop is an interruption like any other. While it is
set, the next turn runs with `RESUME_NOTE` as pydantic-ai `instructions` — telling
the model the previous turn ended early and to build on the history rather than
re-orient, without being folded into the prompt: the prompt is persisted verbatim
as the analyst's `UserPromptPart`, so a note there would claim words the analyst
never wrote and stack one stale copy per interruption. Instructions come from the
current run only, so a note on an older `ModelRequest` is never resent. The
recorder resets per attempt, so the reactive overflow re-run replays from the same
pre-turn base rather than concatenating the failed attempt's messages.

`history_partial_at` is on the conversation payload, so it reaches the list and
detail responses and the JSON export alongside `raw_history` — a reader can tell a
replayable turn boundary from a mid-turn checkpoint without inspecting the blob.

No extra truncation logic: a resumed history is ordinary `message_history` and the
sliding window still sizes every request. Because the learned budget and
calibrated `chars_per_token` persist per conversation, a resumed turn starts with
the budget the interrupted turn paid to learn.

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

No test reaches a real LLM — the streamed loop runs over a stubbed MCP tool server
with pydantic-ai's `FunctionModel`.

- `tests/test_agent_api.py` — availability and 503 gating, conversation CRUD and
  per-user privacy, the full streamed loop, thinking-event mapping, the Kimi
  replay shim, `effort_model_settings`, the proposal lifecycle over HTTP (confirm
  writes + audits, idempotent 409, owner-only), admin settings round-trips,
  `/api/agent/info` shape and key-never-leaks, the JSON export, mid-turn elision
  end-to-end, and the reactive overflow retry.
- `tests/test_agent_window.py` / `test_agent_resume.py` — elision order, protected
  regions, turn-dropping never orphaning tool returns, purity, checkpoint repair.
- `tests/test_agent_tools.py` / `test_agent_schema.py` — registry parity, the
  extended `FilterSpec`/detector surface, `propose_annotation`, disabled tools,
  schema slimming and the serialized-size budget guard.
- `tests/test_agent_tokens.py` / `test_mcp_http.py` — token model and management
  API, scope binding, an end-to-end tool call, off-by-default 404, admin deny list
  on the external `tools/list`, and the propose-* tools' absence from `/mcp`.
- `tests/test_chart_meta.py` — chart-meta legality table and regeneration
  stability. Frontend: `frontend/src/test/agent.test.ts`.
