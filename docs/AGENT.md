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
  time (`agent/tools.py::AgentScope`); the model never supplies IDs.

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

- Tools are defined once on a **standard MCP server**; the built-in loop
  consumes it in-process. Exposing the same server over HTTP for external
  harnesses (Claude Code, hermes-agent, nib) is a roadmap item (needs PAT
  auth — session cookies don't fit headless clients).
- Streaming is SSE over the POST response (`text_delta`, `tool_call`,
  `tool_result`, `done`, `error`); the frontend reads it via fetch +
  ReadableStream (`frontend/src/api/agent.ts`).
- The analyst's current Explorer filters ride along with each message and are
  injected as context, so "filter what I'm looking at further" works.

## Tools (read-only)

`search_events`, `get_event`, `list_fields`, `list_artifacts`, `field_terms`,
`field_numeric_stats`, `histogram`, `run_anomaly_detector`,
`propose_finding`, and — when embeddings are available — `semantic_search`,
`similar_events`. All results are budget-capped (row caps, string
truncation) because they land in the model's context window.

`propose_finding(title, description, filters)` is the findings channel: the
filter spec uses the exact Explorer filter shape, the backend echoes the
current hit count, and the frontend renders an "Apply to Explorer" card.

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
real LLM), and the Kimi replay shim. Frontend:
`frontend/src/test/agent.test.ts` (FilterSpec → EventFilters mapping).
