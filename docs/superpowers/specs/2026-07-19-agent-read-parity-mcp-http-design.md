# AI Agent Read Parity + HTTP MCP Exposure — Design

Date: 2026-07-19
Status: approved in brainstorm, pending spec review

## Goal

Give the AI investigation agent the same *read* visibility a human analyst has in the
Explorer, and expose the same tool server over HTTP so any external MCP client (Claude
Code, hermes-agent, …) can investigate a timeline with identical capabilities. Writes
stay out of scope (read-only v1 invariant holds).

## Motivation

Current agent (11 tools, `src/vestigo/agent/tools.py`) cannot: discover baselines
(temporal detectors unusable), see prior analyst work (annotations, dispositions, saved
views, Sigma rules/runs), filter or propose findings by annotation state / detector-run
membership / event-id lists / routine collapse, or tune detector parameters. And only
the built-in loop can consume the tools — no external agent access.

## Design invariants (unchanged)

- Read-only: no tool mutates analyst-visible state (sole nuance: `run_anomaly_detector`
  persists a `DetectorRun`, as before).
- Scope safety: the model never supplies case/timeline IDs, on either transport.
- Sandbox + apply: findings are cards; only an analyst click changes the Explorer view.
- Invisible unless configured: MCP endpoint off by default.
- Forensic reproducibility: every tool call audited (`agent.tool_call`), external calls
  additionally carry the token id.

## 1. New read tools (`src/vestigo/agent/tools.py`)

Same pattern as existing tools: closures over `AgentScope`, no IDs from the model,
compact truncated dict results. Store methods are async — await directly (existing
tools threadpool-wrap *sync* services; leave a code note).

| Tool | Wraps | Returns |
|---|---|---|
| `list_baselines()` | `PostgresStore.list_baseline_definitions` (`db/postgres.py:2517`) | id, name, baseline window, suspect windows |
| `list_dispositions(kind?, detector?)` | `list_dispositions` (`db/postgres.py:2635`) | normal/dismissed/confirmed/routine marks with scope fields |
| `list_annotations(annotation_type?)` | `list_source_annotations` (`db/postgres.py:3213`) | tag/comment annotations across the timeline's sources |
| `get_event_annotations(source_id, event_id)` | `list_annotations` (`db/postgres.py:3192`) | full annotation content for one event |
| `list_saved_views()` | `list_views` (`db/postgres.py:2370`) | name, query, filter payload |
| `list_sigma_rules()` | sigma case+global listing (`api/routers/sigma.py:71`) | rule metadata (no YAML body) |
| `get_sigma_rule(rule_key)` | `get_sigma_rule` (`db/postgres.py`) | full rule incl. `yaml_content` |
| `list_sigma_runs()` / `get_sigma_run(run_id)` | `list_sigma_runs`/`get_sigma_run` (`db/postgres.py:3169`) | run list; full per-rule results (SQL, match counts, statuses) |

`list_baselines` is the priority item — it unlocks the five temporal-only detectors
(`proportion_shift`, `interval_periodicity`, `sequence_novelty`, drift detectors).

## 2. FilterSpec extension (`tools.py:39-80`)

New fields, mirroring the events router:

- `annotated` — subset of `{"tag","anomaly"}` (router param, `api/routers/events.py:579`)
- `annotation_tag_value`
- `run_id` — detector-run finding membership (`events.py:587`)
- `event_ids` / `exclude_event_ids` (`db/queries.py:152,155`)
- `collapse_routine` (`events.py:614`, `queries.py:156-161`)

Resolution reuses the events-router helpers `_resolve_annotated_event_ids`
(`events.py:408-444`) and `_resolve_event_id_filters` (`events.py:472`); refactor them
as needed to accept explicit scope arguments instead of request-bound params.

`propose_finding` inherits the new fields automatically → findings of these shapes
become one-click applicable. Extend the frontend `FilterSpec → EventFilters` mapping
(`frontend/src/api/agent.ts`, tested in `frontend/src/test/agent.test.ts`).

Out of scope: semantic `qMode` in FilterSpec. Semantic search stays a separate tool; a
semantic finding is not a reproducible filter set (embedding-config-dependent) and fits
the findings model badly. Keyset pagination (`after`/`before`) also stays out — the
agent aggregates instead of deep-paging.

## 3. Detector tuning (`run_anomaly_detector`, `tools.py:288`)

Expose the full `_run_stat_detector` surface (`events.py:1439-1459`) with the same
bounds the HTTP endpoint validates (`events.py:2227-2331`):

`z_threshold (>0)`, `min_skew_seconds (≥0)`, `fdr_q (>0, ≤1)`, `min_ratio (>1)`,
`ngram_size (2–5)`, `min_support (≥2)`, `start`, `end`, `include_dismissed`.

Pydantic field constraints mirror the FastAPI `Query` bounds so validation is identical
on both paths. All optional, defaulting to server defaults (current behavior).

## 4. HTTP MCP exposure + scoped PAT

**Token model.** New Postgres model `AgentToken` + Alembic migration: `id`,
`token_hash` (SHA-256; plaintext shown once at creation), `case_id`, `timeline_id`,
`user_id` (creator), `name`, `created_at`, `expires_at?`, `revoked_at?`. A token is
scoped to exactly one case + timeline — leak blast radius is one timeline, and the
scope-safety invariant holds because scope comes from the token, never the model.

**Management API + UI.** Create/list/revoke under
`/api/cases/{case_id}/timelines/{timeline_id}/agent-tokens`, RBAC-checked (token grants
no more than the creator's case access). UI in case settings. Revocation is instant
(checked per connect).

**Endpoint.** FastMCP Streamable HTTP app mounted at `/mcp`, auth via
`Authorization: Bearer <token>`. On connect: hash lookup → expiry/revocation check →
re-check creator still has case access → `build_scope(case, timeline, user)` → the
*identical* tool server the built-in agent uses (`build_tool_server(scope)`). One tool
code path, two transports.

**Gating.** `VESTIGO_MCP_ENABLED` setting, default off. Independent of
`VESTIGO_AGENT_*` (serving MCP needs no LLM config). `/api/health` reports
`mcp_enabled`.

**Audit.** External tool calls log `agent.tool_call` like built-in ones, plus the token
id in the audit detail.

## 5. Testing

- `tests/test_agent_api.py`: each new tool over the stubbed store; FilterSpec
  round-trip incl. new fields; detector param passthrough and bound violations.
- New `tests/test_mcp_http.py`: token lifecycle (valid / expired / revoked /
  creator-lost-access), scope binding (tools only see the token's timeline), one
  end-to-end tool call over the HTTP transport, 404/off behavior when
  `VESTIGO_MCP_ENABLED` unset.
- Frontend: `agent.test.ts` extended for new FilterSpec fields.

## 6. Documentation

- `docs/AGENT.md`: new tool list, token/HTTP section, invariant wording updated
  ("model never supplies IDs" holds for both transports; read-only unchanged).
- `docs/ROADMAP.md`: close the corresponding items (external MCP exposure was already
  listed as a roadmap item in `AGENT.md`).
