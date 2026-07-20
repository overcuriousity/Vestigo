# AI Investigation Agent

Optional, off-by-default AI assistant embedded in the Explorer. It drives the
iterative analysis loop on the analyst's behalf — search, aggregate, run
detectors, refine — and hands results back as **findings**: filter sets the
analyst can apply to the Explorer with one click. Update this document
alongside any agent change, like `ANOMALY_DETECTION.md` for detectors.

## Design invariants

- **Sandbox + apply.** The agent queries the backend in its own loop and
  never mutates the analyst's view. Findings render as cards; only an
  explicit analyst click applies filters (through the normal URL-driven
  filter path, `frontend/src/lib/queryParams.ts`).
- **Propose→confirm writes (A1).** The agent itself never writes an
  annotation. `propose_annotation` (available once a conversation is bound,
  `agent/tools.py`) resolves the target events and records an
  `AgentProposal` row (`status="proposed"`) — it does not touch
  `annotations`. An analyst reviews the proposal in the UI and calls
  `POST .../proposals/{id}/confirm` or `.../reject`
  (`api/routers/agent.py`). Confirm re-resolves the events against the
  *current* scope (a source may have left the timeline since propose time),
  writes one `tag`/`comment` annotation per still-resolving event with
  `origin="agentic-analysis"` and `created_by` set to the confirming
  analyst, and reports `skipped_event_ids` for anything that no longer
  resolves. `decide_agent_proposal` is an atomic `UPDATE … WHERE
  status='proposed'`, so a proposal can only ever be decided once — a
  second confirm/reject 409s. Rejecting writes nothing. `run_anomaly_detector`
  remains the other write-shaped tool (it persists a `DetectorRun`, same as
  an analyst-triggered preview scan).
  - **Origin is provenance, not a visibility class.** Once confirmed, an
    `agentic-analysis` annotation is indistinguishable from a manually-typed
    one everywhere that matters: tag autocomplete
    (`list_distinct_tag_contents`), the `annotated`/tag Explorer filter
    (`list_event_ids_by_annotation_type`, which defaults to
    `USER_VISIBLE_ANNOTATION_ORIGINS = ("user", "agentic-analysis")`), and
    manual deletion (`delete_annotation`). Only `origin="system"` (the
    outlier-detection pipeline) stays outside that set — those calls pass
    `origins=("system",)` explicitly.
  - **Audit.** Every decision writes a row: `agent.annotation_confirm`
    (detail: `written`, `skipped_event_ids`, `tag`, `comment_present`) or
    `agent.annotation_reject`, both keyed to `target_type="agent_proposal"`.
  - `propose_annotation`/the confirm/reject endpoints are **not** exposed on
    the external `/mcp` transport — only the in-app agent's tool server
    binds a `conversation_id` (`AgentScope.conversation_id`), which is what
    gates `propose_annotation`'s registration; a bare `/mcp` token scope has
    none, so the tool is simply absent from that server's tool list.
- **Invisible unless configured.** `/api/health` reports `agent_available`
  only when `VESTIGO_AGENT_*` is set **and** the endpoint answered a cached
  probe (`agent/availability.py`, TTL `VESTIGO_AGENT_PROBE_TTL_SECONDS`).
  The frontend renders zero agent UI otherwise; API endpoints 503. The
  cache is stale-while-revalidate: a merely-expired entry answers
  immediately with the last value and re-probes in the background, so
  `/api/health` never blocks on a hung LLM endpoint; only a cold cache or
  a config-fingerprint change probes synchronously.
- **Forensic reproducibility.** Conversations persist in Postgres
  (`agent_conversations` / `agent_messages`, migration 0007): every tool
  call with exact arguments and a result summary, plus the runtime's
  replayable pydantic-ai history. Each tool call is also written to the
  audit trail (`action="agent.tool_call"`), and detector runs launched by
  the agent persist like any other. Conversations are per-user (private);
  the audit trail is the shared record.
- **Scope safety.** Tools are bound to one case + timeline at server-build
  time (`agent/tools.py::AgentScope`); the model never supplies IDs — on
  both transports. The built-in loop derives scope from the conversation's
  case/timeline; the external `/mcp` endpoint derives it from the Bearer
  token (`AgentToken.case_id`/`timeline_id`), never from anything the model
  or client sends.
- **Token metering: measured, or null.** Each streamed turn reads
  `AgentRunResult.usage` (a `RunUsage` with `input_tokens`/`output_tokens`)
  once the run completes and stamps `agent_messages.prompt_tokens` /
  `completion_tokens` on the persisted assistant row (`agent/runtime.py`'s
  `TurnResult`, `api/routers/agent.py::_message_stream`). If the endpoint
  reports `0` (nothing measured), the column is left `NULL` — Vestigo never
  fabricates or estimates a token count. The same two fields ride along on
  the terminal `done` SSE event so the live UI can show a usage chip without
  a refetch.

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
  the built-in loop consumes it in-process. The same server is also served
  over HTTP for external harnesses — see **External MCP endpoint** below.
- Streaming is SSE over the POST response (`text_delta`, `thinking_delta`,
  `thinking`, `compaction`, `tool_call`, `tool_result`, `done`, `error` —
  `error` may carry a machine-readable `code`, currently `context_overflow`
  or `model_error`); the frontend reads it via fetch + ReadableStream
  (`frontend/src/api/agent.ts`).
- **Thinking is first-class.** `stream_turn` maps pydantic-ai
  `ThinkingPart` starts/deltas to `thinking_delta` events and flushes each
  completed segment (on `PartEndEvent`) as a terminal `thinking` event,
  which the router persists as a `role="thinking"` `agent_messages` row —
  interleaved correctly with tool calls. The chat UI renders them as
  collapsed "Thinking" blocks. Thinking *signatures* are never persisted
  per-row; they live only inside the conversation's replayable `history`
  blob (and therefore in the JSON export's `raw_history`).
- One turn at a time per conversation: a POST while another turn is
  streaming gets a 409 (`_active_turns` in `api/routers/agent.py`) —
  concurrent turns would race on the conversation's replayable `history`.
  `_active_turns` maps each conversation to that turn's reservation (a cancel
  `asyncio.Event` plus a start timestamp), and the live state is surfaced as
  `active` on every conversation payload, so a panel that was closed or
  navigated away from mid-turn shows a working Stop instead of an input that
  silently 409s. The reservation is taken before the `StreamingResponse` is
  returned and released by the generator's `finally`; a reservation older than
  `_TURN_STALE_AFTER` (`LLM_TIMEOUT × DEFAULT_MAX_TURNS`, the worst case a turn
  can legitimately take) is treated as stranded and pruned, so an ASGI task
  that died before the generator's first step can't leave a conversation
  permanently "running".
- **Stop is server-side.** `POST .../{id}/cancel` sets the turn's cancel event;
  `_message_stream_inner` checks it as it streams and returns. Aborting the
  client's SSE fetch alone is not enough: with no output flowing (a long tool
  call, a slow model) Starlette may not notice the disconnect for a while and
  the turn keeps spending tokens.
  - **What a stop persists.** The text streamed before the stop is written as
    an assistant message tagged `[stopped]`, exactly as the interrupt paths
    write `[interrupted]`. Like those, it does *not* enter the replayable
    `history` blob — `stream_turn` only hands back `new_messages` on its
    terminal `result` event, so a turn that never got there has no history
    contribution to make.
  - **When it takes effect.** At the next streamed event, and always before
    the next model request. A tool call already in flight runs to completion
    first. The check deliberately lives *inside* the turn generator: breaking
    out of it from the caller would close it with a `GeneratorExit`, which —
    deriving from `BaseException` — no `except Exception` catches, silently
    discarding the streamed text instead of persisting it.
  - Idempotent: cancelling an idle conversation reports `cancelled: false`
    rather than erroring, since the client is always racing the turn's own
    completion. A real cancel is audited as `agent.turn_cancelled` — a stop
    truncates the record, so who did it has to stay recoverable.
- The analyst's current Explorer filters ride along with each message and are
  injected as context, so "filter what I'm looking at further" works.

### External MCP endpoint

`/mcp` (`agent/mcp_http.py`) exposes the *identical* tool server the
built-in agent uses — `build_tool_server(scope)` — over MCP Streamable
HTTP, so any external MCP client (Claude Code, hermes-agent, nib) can
investigate a case/timeline with the same tool surface an analyst's
in-app agent has. One tool code path, two transports.

- **Gated by `VESTIGO_MCP_ENABLED`** (default off, independent of
  `VESTIGO_AGENT_*` — serving MCP needs no LLM endpoint configured). When
  off, the endpoint 404s rather than 405ing off the SPA catch-all, so it is
  invisible. `/api/health` reports `mcp_enabled`.
- **Scoped tokens.** `AgentToken` (Postgres, migration 0008) binds a token
  to exactly one case + timeline at creation time — leak blast radius is
  one timeline. Created/listed/revoked via
  `/api/cases/{case_id}/timelines/{timeline_id}/agent-tokens`
  (`api/routers/agent_tokens.py`), RBAC-checked so a token never grants more
  than its creator's own case access. UI lives in the timeline list
  (`frontend/src/components/timelines/AgentTokensDialog.tsx`), gated on the
  `mcp_enabled` health flag. Plaintext (`vgo_…`) is shown once at creation;
  only a SHA-256 hash is stored.
- **Bearer auth + per-connect RBAC re-check.** Every request presents
  `Authorization: Bearer vgo_…`. The endpoint hashes and looks up the
  token, rejects revoked/expired/unknown tokens (401), then re-checks that
  the *creating user* still has case access (403 if revoked) — so an
  analyst who loses case access can no longer be impersonated via a token
  they minted earlier. Scope for the resulting tool server always comes
  from the token, never from the model or the MCP client.
- **Audit.** Each `tools/call` request writes an `agent.tool_call` audit
  row (same action as the built-in loop) carrying the token id and
  `transport: "mcp_http"` in the detail, sniffed from the request body
  before it's replayed into the MCP session (`_audit_tool_call`). JSON-RPC
  batch arrays audit one row per `tools/call` member — the transport
  rejects batches (2025-06-18 spec), but the custody trail doesn't depend
  on that. Request bodies are capped at 10 MiB (413 above).
- FastMCP's DNS-rebinding host-validation transport security is disabled
  for this endpoint — safe because a universal Bearer-auth check precedes
  all dispatch, unlike the browser-ambient-credential threat that
  protection targets; Host handling is left to the deployment's reverse
  proxy.

## Tools (27 total; all read-only except the propose→confirm annotation path)

Core: `search_events`, `get_event`, `list_fields`, `describe_field`,
`list_artifacts`,
`field_terms`, `field_numeric_stats`, `histogram`, `run_anomaly_detector`,
`propose_finding`, `propose_annotation` (conversation-bound only — see
**Propose→confirm writes** above), and — when embeddings are available —
`semantic_search`, `similar_events`.

Viz tools (A9, viz parity): `field_timeseries`, `time_punchcard`,
`field_pivot`, `field_scatter`, `compare` (kind = `time`/`terms`/`numeric`,
two independent `FilterSpec` layers) wrap the same `db/queries.py` methods
the Visualize page's endpoints call, with tighter per-tool caps than the
page's own UI bounds (e.g. `field_scatter` caps at 1000 points vs. the
page's 20000 — every point counts against the model's context window; see
`VIZ_*_MAX_*` constants in `agent/tools.py`).

### `propose_chart` — isomorphic with the analyst's `ChartConfig`

`propose_chart(title, description, spec)` is the charting analog of
`propose_finding`. `spec` mirrors the Visualize page's `ChartConfig` field for
field — `chart_type`, `scale`, `field`, `field_y`, `metric`, `filters`,
`compare{mode, filters}`, `options{...}` — so anything an analyst can build by
hand the agent can propose, reasoning through the same steps.

This replaced a single flattened `kind` enum whose nine values fused *which
aggregation* + *which mark* + *compare on/off*. That enum could address only
7 of 13 chart types (`pie`, `heatmap`, `box`, `violin`, `ecdf`, `sankey` were
unreachable), could not name a `scale`, hardcoded `metric` to `count`, could
not express `compare.mode="baseline"`, and overloaded one `limit` field.
A pie request therefore returned `ok: true` and rendered a bar — the failure
this design exists to prevent.

**Legality is enforced from one table.** `agent/chart_meta.py` is the source
of truth for which scales each mark admits, which support a comparison layer,
which need a second field, and which options each reads;
`frontend/src/components/viz/lib/chartMeta.ts` is **generated** from it by
`scripts/gen_chart_meta.py` (outputs committed; `tests/test_chart_meta.py`
asserts regeneration is a no-op). The analyst gets these rules as affordances —
a shrinking chart-type dropdown, a disabled Compare control with a reason, a
force-reset metric. The agent gets them as validation errors that name the
legal alternatives, e.g. `chart_type="pie" requires scale in {"nominal"}, got
"ratio". Chart types legal for scale="ratio": …`. The error *is* the dropdown.

Rejections happen before any query: illegal scale/chart_type pair, missing or
superfluous `field`/`field_y`, unsupported comparison, illegal metric, and an
unknown field token (with `difflib` near-miss suggestions — an unknown
attribute key otherwise resolves to an empty Map lookup and returns a cheerful
`ok: true` over zero rows). Two more fire *after* the query, for the same
silent-success reason: a numeric chart whose field yields `count == 0` (the
documented categorical signal) and a scatter with no numeric pairs.

**The result echoes what will be drawn.** `{ok, resolved{chart_type, scale,
metric, compare_mode, data_kind, field, field_y, options}, warnings, summary}`.
`ok` stays top-level — `AgentPanel.tsx` gates card creation on it. `resolved`
is the channel the model reads to confirm its chart is the one it asked for;
the system prompt requires checking it. `warnings` carries non-fatal issues:
options this chart type ignores (inert, never fatal) and any limit clamped for
the validation query — those clamps bound the *tool result* for context
budget, never the analyst's card.

`describe_field(field, filters)` is the agent's equivalent of the page's
numeric auto-probe: coverage, distinct count, numeric stats, a suggested
`scale` (`numeric.count > 0 → ratio`, else `nominal` — the same test
`VisualizePage` uses) and the chart types legal for it. Two scans per real
field, free for virtual `time:` fields.

The frontend maps the spec onto `ChartConfig` (`specToChartConfig`,
`frontend/src/api/agent.ts`); `specToChartConfigLegacy` beside it is a frozen
translation of the retired `kind` shape, since persisted `tool_args` from old
conversations still re-render through it. `resolveChartOptions`
(`viz/lib/chartOptions.ts`) is shared with `VisualizePage`, so a proposed
chart and a hand-built one resolve defaults identically. The chat panel renders
a live chart card (`ChartProposalCard.tsx`) fetched fresh through `vizApi` (not
the tool_result echo, so it stays consistent with current data/dispositions),
keyed on `chart_type` rather than on the aggregation that fed it — several
marks share one `dataKind`, and switching on the fetch result is what turned
the pie into a bar. It offers **Open in Visualize** (a route link carrying the
mapped `ChartConfig` + filters as URL params) and **Save** (the analyst's own
click against `savedChartsApi.create` — the only write in this flow, credited
to the analyst; the agent never writes a chart).

Read-parity tools (analyst-visible state the agent previously couldn't see):
`list_baselines` (saved baseline definitions — unlocks the temporal-only
detectors: `proportion_shift`, `interval_periodicity`, `sequence_novelty`,
`value_distribution_drift`), `list_dispositions` (normal/dismissed/
confirmed/routine marks), `list_saved_views`, `list_annotations` /
`get_event_annotations` (tag/comment annotations across the timeline),
`list_sigma_rules` / `get_sigma_rule` (case + global Sigma rules, the
latter including full YAML), `list_sigma_runs` / `get_sigma_run` (past
Sigma evaluations and their per-rule results). All defined in
`agent/tools.py` alongside the core tools; same scope-bound-closure
pattern, no IDs from the model.

All results are budget-capped (row caps, string truncation) because they
land in the model's context window.

`propose_finding(title, description, filters)` is the findings channel: the
filter spec uses the exact Explorer filter shape, the backend echoes the
current hit count, and the frontend renders an "Apply to Explorer" card.
`FilterSpec` (`agent/tools.py`) also carries `annotated` (subset of
`{"tag","anomaly"}`), `annotation_tag_value`, `run_id` (detector-run
finding membership, unioned into the `"anomaly"` branch of `annotated`),
`event_ids` (explicit allowlist — no `exclude_event_ids`: the frontend
`EventFilters` shape has no exclude-ids field, so such a finding could
never be applied), and `collapse_routine` (hide `kind="routine"`-disposed
motif events). The frontend maps `run_id` onto `EventFilters.anomalyRunId`
(`frontend/src/api/agent.ts`).

`run_anomaly_detector` exposes the same tuning surface the HTTP endpoint
validates, with identical bounds: `z_threshold` (>0), `min_skew_seconds`
(≥0), `fdr_q` (>0, ≤1), `min_ratio` (>1), `ngram_size` (2–5), `min_support`
(≥2), plus `start`/`end` for the mining window. All optional, defaulting
to server behavior.

### Per-tool enable/disable (three layers)

`TOOL_REGISTRY` (`agent/tools.py`) is the single source of truth for the
tool catalog (name, one-line description, `embeddings_gated`,
`requires_conversation`); a registry-parity test keeps it in sync with the
actual `@server.tool()` registrations. Every tool is toggleable — none are
hard-wired on. Three deny layers compose (a tool is available only if *no*
layer denies it):

1. **Admin hard-deny** — `agent_settings.disabled_tools` /
   `VESTIGO_AGENT_DISABLED_TOOLS` (JSON array). Applies to the in-app agent
   **and** the external `/mcp` transport; users cannot re-enable these.
   Edited as a checkbox list on `Admin → Agent`.
2. **Per-user defaults** — `users.preferences["agent_disabled_tools"]`,
   edited via `PUT /api/agent/preferences` ("Save as my defaults" in the
   tool-selector popover). Only a default for the popover's checkboxes.
3. **Per-chat choice** — `agent_conversations.disabled_tools`, frozen at
   creation from whatever the popover held at send time. Later
   preference/admin edits never mutate an existing conversation's list (the
   admin layer still applies at turn time, so an admin deny takes effect
   everywhere immediately).

Mechanically, `AgentScope.disabled_tools` carries the union of layers 1+3
(`/mcp`: layer 1 only) and `build_tool_server` removes those tools
(`FastMCP.remove_tool`) after registration — a disabled tool is *absent*
from the tool list and the model's prompt, not an error-returning stub.
Disabling `propose_finding`/`propose_annotation` degrades the sandbox+apply
workflow to prose-only; the popover warns about that but does not prevent it.

### OPSEC disclosure + tool selector

The OPSEC notice ("Evidence leaves Vestigo…", with the *actual* configured
endpoint URL and model name) is a persistent panel element in
`frontend/src/components/agent/AgentPanel.tsx`, shown in the empty state
above the input — not a one-time dialog, so it's visible before every first
message in a fresh chat rather than gated behind a "new conversation"
click. There is deliberately no "don't show again".

Per-chat tool selection is a separate concern, in
`frontend/src/components/agent/ToolSelector.tsx`: a popover reachable from a
toolbar button above the input, always available, with "Save as my defaults"
writing the current selection back to the user record.

The popover's behavior depends on whether a conversation exists yet:

- **No conversation**: it seeds from the user's saved defaults and the
  selection is passed to `create_conversation`.
- **Active conversation**: it seeds from *that conversation's*
  `disabled_tools` and each change `PATCH`es it. `seedFromDefaults={false}`
  is load-bearing here — without it the mount-time seeding would replace the
  analyst's actual restriction with their defaults and persist that.

Two details keep one conversation's restriction from leaking onto another:
the panel syncs local state with `conversationTools ?? []` (an unrestricted
conversation reports `null`, and skipping those would strand the previous
conversation's set, which the next toggle would then `PATCH` onto this one),
and the popover is keyed on the conversation id so it remounts — its
`seededRef` is mount-scoped, so without that a new chat would never re-seed
from the user's defaults.

A change applies from the **next turn** (the turn reads
`conversation.disabled_tools` fresh on every send); it never rewrites what
earlier turns were allowed to do. `PATCH` is a genuine partial update —
omitting `disabled_tools` leaves it alone, since `[]` already means
"re-enable everything" and a silent widening of the agent's reach would be
the worst possible default. It is audited as
`agent.conversation_tools_changed` with before/after lists: the row carries
only the current restriction, so who narrowed the agent's reach and when has
to live in the audit trail for the record to stay readable afterwards.

Both draw from `GET /api/agent/info` (`info_router` in
`api/routers/agent.py`): model, provider, `api_base_url`,
`context_window`, `compact_threshold`, the tool catalog with
`admin_disabled` flags, and the user's saved `user_disabled_tools`. This
deliberately discloses model + base URL to **all authenticated users**
(that disclosure *is* the OPSEC feature); the API key is never included.

### Conversation JSON export

`GET /api/cases/{case_id}/agent/conversations/{id}/export` (owner-only,
audited as `agent.conversation_export`, *not* gated on agent availability —
the record must stay exportable while the LLM endpoint is down) returns the
whole thread as a JSON attachment: `export_version`, exporter/timestamps,
the conversation row (incl. `model_id` and `disabled_tools`), every
`agent_messages` row (user/assistant/tool/thinking/compaction, with tool
args/results and measured token usage), the proposals, and `raw_history` —
the provider-wire pydantic-ai history blob (the only place thinking
signatures and provider quirks live). Download button in the AgentPanel
header.

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
| `VESTIGO_AGENT_REASONING_EFFORT` | Reasoning-effort enum: `off` (default), `low`, `medium`, `high`, `max`. Admin-editable; see **Reasoning effort** below. |
| `VESTIGO_AGENT_CONTEXT_WINDOW` | Model context window in tokens (≥1024). Unset (default) = auto-compaction off. See **Auto-compaction** below. |
| `VESTIGO_AGENT_COMPACT_THRESHOLD` | Fraction of the window that triggers compaction (0.1–1 exclusive, default 0.85). |
| `VESTIGO_AGENT_DISABLED_TOOLS` | JSON array of tool names to hard-deny everywhere (in-app + `/mcp`), e.g. `["semantic_search"]`. |
| `VESTIGO_AGENT_PROBE_TTL_SECONDS` | Availability probe cache (default 60). |
| `VESTIGO_AGENT_SECRET_MODE` | `db` (default) or `env-only`: refuse DB storage of the API key and ignore any previously stored one — `VESTIGO_AGENT_API_KEY` becomes the only source (A10). Env-only, not admin-editable. |
| `VESTIGO_MCP_ENABLED` | Serve the external `/mcp` streamable-HTTP endpoint (default `false`). Independent of `VESTIGO_AGENT_*`. |

### DB-backed settings and env-wins precedence (A7)

Every field above except `VESTIGO_AGENT_PROBE_TTL_SECONDS` and `VESTIGO_MCP_ENABLED`
can also be set from the admin UI (`Admin -> Agent`, `frontend/src/pages/admin/AdminAgentPage.tsx`),
backed by a singleton `agent_settings` row (migration `0011`, `db/postgres.py`).
`resolve_agent_config()` (`agent/config.py`) resolves each field independently,
**per field**, in this order:

1. `VESTIGO_AGENT_<FIELD>` env var, if set — wins unconditionally.
2. The DB-stored value, if set.
3. The hardcoded default (e.g. `provider=openai`, `max_turns=15`, `reasoning_effort=off`).

This is deliberately per-field, not per-config: an operator can pin `VESTIGO_AGENT_API_KEY`
while leaving `model` and `reasoning_effort` admin-editable in the DB. The resolved
`AgentConfig.sources` dict records which layer won each field (`"env"|"db"|"default"`),
which the admin API (`GET/PUT /api/admin/agent-settings`) surfaces so the UI can render
env-pinned fields as disabled with a `pinned by VESTIGO_AGENT_<FIELD>` badge instead of
silently accepting edits that would never take effect. `api_key` is never round-tripped
in plaintext through this API — only an `api_key_set` boolean; the UI's password field
treats an empty submit as "unchanged" and requires an explicit clear action to null it out.
The DB-stored key is plaintext at rest; `VESTIGO_AGENT_SECRET_MODE=env-only` (A10) makes the
PUT refuse key storage (400) and the resolver ignore any previously stored key, so
`VESTIGO_AGENT_API_KEY` is the only source — clearing a leftover stored key stays allowed.
The response's `secret_mode` field drives the UI's disabled key input in that mode.

#### Model picker

`POST /api/admin/agent-settings/models` (admin-only) returns the model ids the
configured endpoint advertises, so the model field can be a dropdown rather than a
name typed from memory. It reuses the availability probe's `GET /models` request —
same per-provider URL and Kimi auth quirks (`agent/availability.py::list_models`),
parsing the `{"data": [{"id": ...}]}` shape both protocols return.

- It takes the **unsaved** form credentials, because the point is seeing an endpoint's
  models before committing them. Omitted fields fall back to the resolved config, which
  is how it works at all for a key that is env-pinned or already stored — the browser
  never holds those.
- **Env-pinned fields are not overridable per request.** Beyond matching the PUT
  endpoint, this closes a path the pin would otherwise leave open: overriding
  `api_base_url` while the key stays env-pinned would ship the operator's key — which
  this API never discloses — to a host the caller chose.
- It always returns 200. Unreachable, auth-rejected, unparseable, and listing-free
  endpoints all yield `[]`, and the UI falls back to free-text entry — which also stays
  reachable via "Enter manually" for a model the listing omits, and preserves a saved
  model the endpoint no longer lists. Nothing is persisted and the probe cache is
  untouched.
- Like the availability probe, this reaches the network only on an admin's action and
  only to the operator's own configured endpoint (`TECH_STACK.md` §6). The frontend
  debounces it so typing a base URL doesn't fire a request per keystroke.
Resolved configs are cached per-fingerprint (hash of the resolved values) so admin edits
take effect on the next call without a process restart, and `PUT` resets the availability
probe cache so a following health check re-probes immediately.

### Auto-compaction (context-window awareness)

The runtime replays the full conversation `history` every turn, so long
investigations eventually overflow the model's context window and the
provider answers 400. When the operator sets `context_window` (env or admin
UI — explicit opt-in, since the right number is model-specific),
`agent/compaction.py` keeps conversations under it:

- **Pre-turn check.** Before each turn the router estimates the next prompt
  from the last measured usage (`prompt + completion + new input`; falls
  back to serialized-history-chars/4 when usage was never reported). At
  `compact_threshold × context_window` it compacts first.
- **Overflow backstop.** Tool-output sizes make the estimate lag one turn,
  so a provider 400/413 whose body matches a known overflow phrasing
  (`_is_context_overflow` — deliberately narrow patterns like "maximum
  context", "prompt is too long", so unrelated 400s such as "invalid token"
  never trigger it) compacts and retries — this path works even without a
  configured window. The retry escalates: the first compaction keeps 2
  recent turns verbatim, a repeat overflow folds down to 1, a third
  overflow (or nothing left to fold) yields a friendly
  `error{code="context_overflow"}` instead of the generic failure. Tool
  calls re-executed by a retry carry an `attempt` field on their
  `agent.tool_call` audit rows so the custody trail distinguishes re-runs
  from duplicates.
- **What compaction does.** `split_history` cuts at *user-turn boundaries
  only* (never between a tool_use and its tool_result), keeping the last
  `KEEP_RECENT_TURNS=2` turns verbatim (1 on the escalated retry); the
  older head is summarized by a toolset-less agent run against the same
  configured model (forensic summary prompt: goals, findings with exact
  event_ids/filters, open hypotheses, failed approaches). The new history =
  a stub user/assistant message *pair* carrying the summary (pair, so
  strict user/assistant alternation survives Anthropic-protocol replay) +
  the kept tail. Usage measured before a compaction is ignored by the next
  turn's estimate — it describes the pre-compaction size and would
  otherwise re-trigger compaction on the already-compacted history.
- **Forensic trail.** Compaction never destroys the record: an append-only
  `role="compaction"` message row stores the summary as content and
  `{reason, keep_turns, messages_summarized, estimated_tokens_before,
  pre_compaction_history}` (the exact pre-compaction wire blob) in
  `tool_result`, plus an `agent.compaction` audit row. The chat shows a
  visible "older turns were summarized" item (SSE `compaction` event), and
  the JSON export carries everything.

### Reasoning effort

`AgentConfig.reasoning_effort` (`agent/config.py`, `EFFORT_VALUES`) is a closed
five-value enum — `off`/`low`/`medium`/`high`/`max` — resolved through the same
env-wins-per-field layering as every other agent setting. `off` is the default
and reproduces pre-A7 behavior exactly: no reasoning-effort field is sent at
all. `runtime.py::effort_model_settings(config)` translates a non-`off` value
into the wire shape the configured endpoint expects and is passed as
`Agent(..., model_settings=...)` in `stream_turn`:

| `reasoning_effort` | OpenAI-protocol | Anthropic-protocol (non-Kimi) | Kimi `/coding` |
|---|---|---|---|
| `off` | nothing sent | nothing sent | nothing sent |
| `low` | `openai_reasoning_effort="low"` | `anthropic_thinking={"type":"enabled","budget_tokens":2048}` | `reasoning_effort="low"` |
| `medium` | `openai_reasoning_effort="medium"` | `budget_tokens=8192` | `reasoning_effort="high"` |
| `high` | `openai_reasoning_effort="high"` | `budget_tokens=24576` | `reasoning_effort="high"` |
| `max` | `openai_reasoning_effort="max"` | `budget_tokens=32768` | `reasoning_effort="max"` |

- **OpenAI-protocol** endpoints get the value passed straight through as
  `OpenAIChatModelSettings(openai_reasoning_effort=effort)`.
- **Anthropic-protocol, non-Kimi** endpoints have no discrete effort enum on
  the wire, only a thinking-token budget, so effort is translated to
  `AnthropicModelSettings(anthropic_thinking={"type": "enabled",
  "budget_tokens": ...})` using a fixed budget table
  (`_ANTHROPIC_THINKING_BUDGETS` in `runtime.py`).
- **Kimi's `https://api.kimi.com/coding` endpoint** (Anthropic protocol on
  the wire, see below) uses its own coarser `low`/`high`/`max` tiers via a
  **top-level `reasoning_effort` field in the JSON request body**
  (`ModelSettings(extra_body={"reasoning_effort": ...})`), not the Anthropic
  `thinking` object — Vestigo's `medium` and `high` both collapse to Kimi's
  `high` tier (`_KIMI_EFFORT` in `runtime.py`).
  - **Verified against:** platform.kimi.ai's "Thinking Effort" guide
    (`https://platform.kimi.ai/docs/guide/use-thinking-effort`), which shows
    `reasoning_effort` as a top-level field on Kimi's chat-completions API
    for `kimi-k3` and documents it as OpenAI-`reasoning_effort`-compatible;
    and Kimi Code's "Using in Third-Party Coding Agents" docs
    (`https://www.kimi.com/code/docs/en/third-party-tools/other-coding-agents.html`),
    which give the exact effort-tier mapping (Claude-Code-level -> K3 level:
    `low`->`low`, `medium`->`high`, `high`->`high`, `xhigh`->`max`,
    `max`->`max`) used by `_KIMI_EFFORT` (Vestigo has no `xhigh` tier). Neither
    source shows a captured raw request against `/coding`'s specific
    Anthropic-protocol (`/v1/messages`) route, so treat the Kimi branch as
    **experimental** pending a direct request/response capture; `extra_body`
    is the safe construction regardless of exact route, since pydantic-ai
    merges it into the JSON body unconditionally.

Works with any OpenAI-compatible endpoint (ollama, vllm, LocalAI,
OpenRouter, `api.moonshot.ai/v1`) or Anthropic-compatible endpoint. Like the
embeddings endpoint and OIDC, agent config is independent of
`VESTIGO_ALLOW_ONLINE` — pointing Vestigo at an endpoint is an explicit
operator decision.

### Kimi coding plan

Verified against the hermes-agent source (`agent/anthropic_adapter.py`,
`plugins/model-providers/kimi-coding/`) and Kimi CLI docs:

- `https://api.kimi.com/coding` speaks the **Anthropic Messages protocol**
  (`sk-kimi-*` keys). The pay-per-token platform is separate:
  `api.moonshot.ai/v1`, OpenAI protocol.
- The `/coding` endpoint **403s unless the User-Agent identifies a coding
  agent** — set `VESTIGO_AGENT_USER_AGENT=claude-code/0.1.0` (what hermes
  sends). Vestigo deliberately does not hardcode a spoofed UA; the operator
  sets it.
- The availability probe uses `{base}/v1/models` (an OpenAI-compatible model
  list Kimi serves on the coding endpoint).
- With server-side thinking active, Kimi requires replayed assistant
  tool-call messages to carry an (unsigned) thinking block
  (hermes-agent#13848). Stock pydantic-ai replays only *signed* thinking
  blocks, so `runtime.KimiAnthropicModel` injects unsigned ones for
  `api.kimi.com/coding` base URLs. The Anthropic `thinking` request
  parameter itself is still never sent for this endpoint; reasoning effort
  (when `VESTIGO_AGENT_REASONING_EFFORT` != `off`) instead rides a top-level
  `reasoning_effort` field via `extra_body` — see **Reasoning effort** above.

## Testing

`tests/test_agent_api.py`: availability gate (probe + cache), health flag,
router 503 gating, conversation CRUD + per-user privacy, the full streamed
loop over a stubbed MCP tool server with pydantic-ai's `FunctionModel` (no
real LLM), the Kimi replay shim, `effort_model_settings` (off/openai/
anthropic-budget/Kimi-mapping, pure function, no network), and the proposal
lifecycle over HTTP
(confirm writes annotations + audits, confirm is idempotent/409s on
redecide, reject writes nothing, only the conversation's owner can decide).
`tests/test_agent_tools.py`: the read-parity tools, the extended
`FilterSpec`/detector-tuning surface, and `propose_annotation` (records the
proposal, requires tag or comment, rejects unknown event ids) against a
stubbed store. `tests/test_agent_tokens.py`: `AgentToken` model + store
methods + the token-management API (create/list/revoke, RBAC).
`tests/test_mcp_http.py`: token lifecycle (valid/expired/revoked/
creator-lost-access), scope binding, an end-to-end tool call over the HTTP
transport, 404/off behavior when `VESTIGO_MCP_ENABLED` is unset, and that
`propose_annotation` is absent from the `/mcp` tool list (no
`conversation_id` in that scope). `tests/test_annotations.py`:
`agentic-analysis`-origin annotations are user-visible in tag autocomplete,
the annotated-event filter, and deletion. Frontend:
`frontend/src/test/agent.test.ts` (FilterSpec → EventFilters mapping,
including the new fields).

Agent-v2 additions: `tests/test_agent_compaction.py` (turn-boundary
splitting never orphans tool returns, threshold math, compacted-history
shape with an injected `FunctionModel`); `tests/test_agent_api.py` (the
three new resolver fields, admin toggles round-trip + 422 on unknown tool
names, `/api/agent/info` shape + key-never-leaks, preference round-trip,
thinking-event mapping via `DeltaThinkingPart` + persisted `thinking` rows,
the export endpoint incl. owner-only 404 + audit, threshold- and
overflow-triggered compaction end-to-end, friendly `context_overflow`
error); `tests/test_agent_tools.py` (registry parity, disabled tools absent
from `list_tools` and erroring on call); `tests/test_mcp_http.py` (admin
deny list applies to the external `tools/list`).
