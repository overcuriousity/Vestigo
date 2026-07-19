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
- **Read-only v1.** Tools cannot annotate, tag, or change anything (the one
  nuance: `run_anomaly_detector` persists a `DetectorRun`, same as an
  analyst-triggered preview scan). Agent-written annotations
  (`origin: agentic-analysis`) are a roadmap item.
- **Invisible unless configured.** `/api/health` reports `agent_available`
  only when `VESTIGO_AGENT_*` is set **and** the endpoint answered a cached
  probe (`agent/availability.py`, TTL `VESTIGO_AGENT_PROBE_TTL_SECONDS`).
  The frontend renders zero agent UI otherwise; API endpoints 503.
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
  before it's replayed into the MCP session (`_audit_tool_call`).
- FastMCP's DNS-rebinding host-validation transport security is disabled
  for this endpoint — safe because a universal Bearer-auth check precedes
  all dispatch, unlike the browser-ambient-credential threat that
  protection targets; Host handling is left to the deployment's reverse
  proxy.

## Tools (read-only, 20 total)

Core: `search_events`, `get_event`, `list_fields`, `list_artifacts`,
`field_terms`, `field_numeric_stats`, `histogram`, `run_anomaly_detector`,
`propose_finding`, and — when embeddings are available — `semantic_search`,
`similar_events`.

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
| `VESTIGO_AGENT_MODEL` | Model name (e.g. `qwen3:32b`, `kimi-k2.5`). Required. |
| `VESTIGO_AGENT_PROVIDER` | Wire protocol: `openai` (default) or `anthropic`. |
| `VESTIGO_AGENT_API_BASE_URL` | Endpoint base URL. Required for `openai`; defaults to Anthropic's API for `anthropic`. |
| `VESTIGO_AGENT_API_KEY` | API key, if the endpoint needs one. |
| `VESTIGO_AGENT_USER_AGENT` | UA header for endpoints that gate on client identity. |
| `VESTIGO_AGENT_EXTRA_HEADERS` | JSON object of extra HTTP headers. |
| `VESTIGO_AGENT_MAX_TURNS` | Model round-trip cap per user message (default 15). |
| `VESTIGO_AGENT_PROBE_TTL_SECONDS` | Availability probe cache (default 60). |
| `VESTIGO_MCP_ENABLED` | Serve the external `/mcp` streamable-HTTP endpoint (default `false`). Independent of `VESTIGO_AGENT_*`. |

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
  `api.kimi.com/coding` base URLs; the Anthropic `thinking` request
  parameter is never sent for this endpoint.

## Testing

`tests/test_agent_api.py`: availability gate (probe + cache), health flag,
router 503 gating, conversation CRUD + per-user privacy, the full streamed
loop over a stubbed MCP tool server with pydantic-ai's `FunctionModel` (no
real LLM), and the Kimi replay shim. `tests/test_agent_tools.py`: the nine
read-parity tools and the extended `FilterSpec`/detector-tuning surface
against a stubbed store. `tests/test_agent_tokens.py`: `AgentToken` model +
store methods + the token-management API (create/list/revoke, RBAC).
`tests/test_mcp_http.py`: token lifecycle (valid/expired/revoked/
creator-lost-access), scope binding, an end-to-end tool call over the HTTP
transport, and 404/off behavior when `VESTIGO_MCP_ENABLED` is unset.
Frontend: `frontend/src/test/agent.test.ts` (FilterSpec → EventFilters
mapping, including the new fields).
