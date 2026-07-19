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
- Streaming is SSE over the POST response (`text_delta`, `tool_call`,
  `tool_result`, `done`, `error`); the frontend reads it via fetch +
  ReadableStream (`frontend/src/api/agent.ts`).
- One turn at a time per conversation: a POST while another turn is
  streaming gets a 409 (`_active_turns` in `api/routers/agent.py`) —
  concurrent turns would race on the conversation's replayable `history`.
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

## Tools (21 total; all read-only except the propose→confirm annotation path)

Core: `search_events`, `get_event`, `list_fields`, `list_artifacts`,
`field_terms`, `field_numeric_stats`, `histogram`, `run_anomaly_detector`,
`propose_finding`, `propose_annotation` (conversation-bound only — see
**Propose→confirm writes** above), and — when embeddings are available —
`semantic_search`, `similar_events`.

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
Resolved configs are cached per-fingerprint (hash of the resolved values) so admin edits
take effect on the next call without a process restart, and `PUT` resets the availability
probe cache so a following health check re-probes immediately.

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
