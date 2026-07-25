# Agent Read Parity + HTTP MCP Exposure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the AI investigation agent full read visibility (baselines, dispositions, annotations, saved views, Sigma, complete FilterSpec, detector tuning) and expose the same MCP tool server over HTTP with scoped personal access tokens.

**Architecture:** All new tools follow the existing `agent/tools.py` pattern — closures over a server-side `AgentScope`, budget-capped dict results, model never supplies IDs. HTTP exposure reuses `build_tool_server()` unchanged: a small ASGI wrapper at `/mcp` authenticates a Bearer token (scoped to one case+timeline at creation), builds the scope from the token, and delegates to a per-request stateless streamable-HTTP MCP app.

**Tech Stack:** Python 3.13, FastAPI, `mcp.server.fastmcp.FastMCP` (official SDK, already a dependency), `fastmcp.client.Client` for in-memory test calls, SQLAlchemy async + Alembic, React 19 + TanStack Query.

**Spec:** `docs/superpowers/specs/2026-07-19-agent-read-parity-mcp-http-design.md`

## Global Constraints

- Ruff: `line-length = 100`, `E501` ignored — don't wrap for length alone. Google-style docstrings.
- Migrations must stay dialect-portable (SQLite in tests): `sa.func.now()`, never `sa.text('now()')`.
- Read-only invariant: no new tool mutates analyst-visible state.
- Scope safety: the model never supplies case/timeline IDs, on either transport.
- Every store method used here is **async** — await directly; only the *sync* query/similarity services go through `run_in_threadpool`.
- Run backend tests with `uv run pytest tests/<file> -v`; lint with `uv run ruff check .` before each commit.
- One deviation from the spec (already agreed): FilterSpec gains `event_ids` but **not** `exclude_event_ids` — the frontend `EventFilters` shape has `ids` but no exclude-ids field, so an exclude-ids finding could never be applied. Update the spec in Task 10.

---

### Task 1: Analysis-context read tools (baselines, dispositions, views, annotations)

**Files:**
- Modify: `src/vestigo/agent/tools.py` (append tools inside `build_tool_server`, before `return server`)
- Test: `tests/test_agent_tools.py` (new file)

**Interfaces:**
- Consumes: `get_store()` from `vestigo.api.deps`; store methods `list_baseline_definitions(case_id, timeline_id)`, `list_dispositions(case_id, timeline_id=, source_ids=, kinds=, detector=)`, `list_views(case_id)`, `list_source_annotations(case_id, source_ids)`, `list_annotations(case_id, source_id, event_id)` — all on `PostgresStore`.
- Produces: MCP tools `list_baselines`, `list_dispositions`, `list_saved_views`, `list_annotations`, `get_event_annotations`. Test helper `_call(server, name, args)` and fixture pattern reused by Tasks 2–4.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_tools.py`:

```python
"""In-memory tests for the agent's MCP tools against the SQLite-backed store.

Calls tools exactly like the runtime does — through a fastmcp in-memory
client over the real `build_tool_server` — so tool schemas, serialization,
and scope binding are all exercised.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastmcp.client import Client as FastMCPClient

from vestigo.agent.tools import AgentScope, build_tool_server
from vestigo.db.postgres import User


def _scope(case_id: str, timeline_id: str, source_ids: list[str] | None = None) -> AgentScope:
    return AgentScope(
        case_id=case_id,
        timeline_id=timeline_id,
        user=User(id="u1", username="tester", is_admin=True, is_active=True),
        source_ids=source_ids or [],
        field_mappings=None,
        source_offsets=None,
    )


async def _call(server, name: str, args: dict[str, Any] | None = None) -> Any:
    """Call one tool over the in-memory transport and return its payload."""
    async with FastMCPClient(server) as client:
        result = await client.call_tool(name, args or {})
    if result.structured_content is not None:
        payload = result.structured_content
        # FastMCP wraps non-dict returns as {"result": ...}.
        if isinstance(payload, dict) and set(payload) == {"result"}:
            return payload["result"]
        return payload
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_list_baselines_returns_timeline_definitions(store):
    await store.create_baseline_definition(
        "c1",
        "t1",
        "normal week",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 8, tzinfo=UTC),
        [
            {
                "id": "w1",
                "label": "incident",
                "start": "2026-01-09T00:00:00+00:00",
                "end": "2026-01-10T00:00:00+00:00",
            }
        ],
    )
    await store.create_baseline_definition(
        "c1",
        "OTHER",
        "foreign",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        [],
    )
    server = build_tool_server(_scope("c1", "t1"))
    result = await _call(server, "list_baselines")
    assert result["total"] == 1
    (b,) = result["baselines"]
    assert b["name"] == "normal week"
    assert b["id"]
    assert b["baseline"]["start"].startswith("2026-01-01")
    assert b["suspect_windows"][0]["label"] == "incident"


@pytest.mark.asyncio
async def test_list_dispositions_scoped_and_filtered(store):
    await store.create_disposition(
        "c1", "normal", detector="value_novelty", timeline_id="t1", field="user", value="svc"
    )
    await store.create_disposition(
        "c1", "dismissed", detector="frequency", timeline_id="t1", field="host", value="a"
    )
    await store.create_disposition(
        "c1", "normal", detector="value_novelty", timeline_id="OTHER", field="x", value="y"
    )
    server = build_tool_server(_scope("c1", "t1"))
    result = await _call(server, "list_dispositions", {"kind": "normal"})
    assert result["total"] == 1
    assert result["dispositions"][0]["field"] == "user"
    everything = await _call(server, "list_dispositions")
    assert everything["total"] == 2


@pytest.mark.asyncio
async def test_list_saved_views(store):
    await store.create_view("c1", "failed logins", "status:4625", {"filters": {"status": ["4625"]}})
    server = build_tool_server(_scope("c1", "t1"))
    result = await _call(server, "list_saved_views")
    assert result["total"] == 1
    view = result["views"][0]
    assert view["name"] == "failed logins"
    assert view["query"] == "status:4625"
    assert view["filter"] == {"filters": {"status": ["4625"]}}


@pytest.mark.asyncio
async def test_annotations_tools(store):
    await store.create_annotation("c1", "s1", "e1", "a1", "tag", "suspicious", created_by="alice")
    await store.create_annotation(
        "c1", "s1", "e2", "a2", "comment", "looks like lateral movement", created_by="bob"
    )
    await store.create_annotation("c1", "sX", "e3", "a3", "tag", "out-of-scope-source")
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    listed = await _call(server, "list_annotations")
    assert listed["total"] == 2
    tags_only = await _call(server, "list_annotations", {"annotation_type": "tag"})
    assert tags_only["total"] == 1
    assert tags_only["annotations"][0]["content"] == "suspicious"
    single = await _call(server, "get_event_annotations", {"source_id": "s1", "event_id": "e2"})
    assert single["total"] == 1
    assert single["annotations"][0]["created_by"] == "bob"
```

Note: `create_view` — confirm the store method name/signature with `grep -n "def create_view" src/vestigo/db/postgres.py` and adjust the call if it differs (it exists; `views` are case-scoped).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL — `Unknown tool: list_baselines` (or fastmcp tool-not-found error) on each test.

- [ ] **Step 3: Implement the four tools**

In `src/vestigo/agent/tools.py`, inside `build_tool_server` (after `similar_events`, before `return server`), add:

```python
@server.tool()
async def list_baselines() -> dict[str, Any]:
    """List saved baseline definitions (baseline range + suspect windows).

    Pass a baseline's id as `baseline_id` to run_anomaly_detector to run
    temporal detection (proportion_shift, interval_periodicity,
    sequence_novelty, frequency, value_distribution_drift) against it.
    """
    from vestigo.api.deps import get_store

    rows = await get_store().list_baseline_definitions(scope.case_id, scope.timeline_id)
    return {
        "total": len(rows),
        "baselines": [
            {
                "id": r.id,
                "name": r.name,
                **r.windows_payload(),
                "created_by": r.created_by,
            }
            for r in rows
        ],
    }


@server.tool()
async def list_dispositions(kind: str | None = None, detector: str | None = None) -> dict[str, Any]:
    """List analyst verdicts on anomaly findings visible from this timeline.

    Kinds: 'normal' (expected behavior, suppresses detection), 'dismissed'
    (noise), 'confirmed' (escalated true positive), 'routine' (recurring
    expected motif). Use these to avoid re-reporting what the analyst has
    already judged.
    """
    from vestigo.api.deps import get_store

    rows = await get_store().list_dispositions(
        scope.case_id,
        timeline_id=scope.timeline_id,
        source_ids=scope.source_ids,
        kinds=[kind] if kind else None,
        detector=detector,
    )
    return {
        "total": len(rows),
        "dispositions": [
            {
                "id": r.id,
                "kind": r.kind,
                "detector": r.detector,
                "field": r.field,
                "value": _truncate(r.value, ATTR_VALUE_TRUNCATE),
                "source_id": r.source_id,
                "event_id": r.event_id,
                "note": _truncate(r.note, MESSAGE_TRUNCATE),
                "created_by": r.created_by,
            }
            for r in rows
        ],
    }


@server.tool()
async def list_saved_views() -> dict[str, Any]:
    """List the analyst's saved filter views for this case (name, query, filter payload)."""
    from vestigo.api.deps import get_store

    rows = await get_store().list_views(scope.case_id)
    return {
        "total": len(rows),
        "views": [
            {"id": r.id, "name": r.name, "query": r.query, "filter": r.view_filter or {}}
            for r in rows
        ],
    }


@server.tool()
async def list_annotations(annotation_type: str | None = None) -> dict[str, Any]:
    """List annotations (tags/comments/system anomaly marks) across this timeline's sources.

    `annotation_type` filters to 'tag', 'comment', or 'anomaly'. Results
    are capped at 200 rows, oldest first — use get_event_annotations for
    one event's full detail.
    """
    from vestigo.api.deps import get_store

    rows = await get_store().list_source_annotations(scope.case_id, scope.source_ids)
    if annotation_type:
        rows = [r for r in rows if r.annotation_type == annotation_type]
    return {
        "total": len(rows),
        "annotations": [_slim_annotation(r) for r in rows[:200]],
    }


@server.tool()
async def get_event_annotations(source_id: str, event_id: str) -> dict[str, Any]:
    """List all annotations attached to one event (full content, oldest first)."""
    from vestigo.api.deps import get_store

    if source_id not in scope.source_ids:
        return {"error": f"source {source_id} is not part of this timeline"}
    rows = await get_store().list_annotations(scope.case_id, source_id, event_id)
    return {"total": len(rows), "annotations": [_slim_annotation(r) for r in rows]}
```

And add the module-level helper (near `_slim_event`):

```python
def _slim_annotation(row: Any) -> dict[str, Any]:
    """Compact an Annotation row for model consumption."""
    return {
        "event_id": row.event_id,
        "source_id": row.source_id,
        "type": row.annotation_type,
        "content": _truncate(row.content, MESSAGE_TRUNCATE),
        "origin": row.origin,
        "detector": row.detector,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
```

Import note: `get_store` is imported inside the tool bodies (matching the file's existing lazy-import style for router helpers, which avoids import cycles with `api.deps`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format src/vestigo/agent/tools.py tests/test_agent_tools.py
git add src/vestigo/agent/tools.py tests/test_agent_tools.py
git commit -m "feat(agent): read tools for baselines, dispositions, views, annotations"
```

---

### Task 2: Sigma read tools

**Files:**
- Modify: `src/vestigo/agent/tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: store methods `list_sigma_rules(case_id)`, `get_sigma_rule(case_id, rule_id)`, `list_sigma_runs(case_id, limit=50)`, `get_sigma_run(case_id, run_id)`; global rules loader `vestigo.sigma.rules.load_global_rules` via `vestigo.api.routers.sigma._load_global` (reuse the router helper, same pattern as events helpers); test helpers `_scope`/`_call` from Task 1.
- Produces: MCP tools `list_sigma_rules`, `get_sigma_rule`, `list_sigma_runs`, `get_sigma_run`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_tools.py`:

```python
@pytest.mark.asyncio
async def test_sigma_rules_tools(store, monkeypatch):
    import vestigo.api.routers.sigma as sigma_router

    async def no_global():
        return []

    monkeypatch.setattr(sigma_router, "_load_global", no_global)
    from vestigo.db.postgres import SigmaRule, generate_id

    rule = SigmaRule(
        id=generate_id("sigma_rule"),
        case_id="c1",
        rule_key="a" * 32,
        title="Suspicious PowerShell",
        level="high",
        logsource={"product": "windows"},
        yaml_content="title: Suspicious PowerShell\ndetection: {}\n",
        content_hash="b" * 64,
    )
    async with store.session_factory() as session:
        session.add(rule)
        await session.commit()

    server = build_tool_server(_scope("c1", "t1"))
    listed = await _call(server, "list_sigma_rules")
    assert listed["total"] == 1
    meta = listed["rules"][0]
    assert meta["title"] == "Suspicious PowerShell"
    assert "yaml_content" not in meta

    full = await _call(server, "get_sigma_rule", {"rule_id": rule.id})
    assert "Suspicious PowerShell" in full["yaml_content"]

    missing = await _call(server, "get_sigma_rule", {"rule_id": "nope"})
    assert "error" in missing


@pytest.mark.asyncio
async def test_sigma_runs_tools(store):
    run = await store.create_sigma_run("c1", "t1", {"source_ids": ["s1"]}, created_by="alice")
    await store.update_sigma_run(
        run.id,
        status="completed",
        results=[
            {
                "rule_key": "a" * 32,
                "title": "R",
                "match_count": 3,
                "status": "matched",
                "sql": "SELECT 1",
            }
        ],
        completed=True,
    )
    other_timeline = await store.create_sigma_run("c1", "t2", {}, created_by="alice")
    assert other_timeline.id != run.id

    server = build_tool_server(_scope("c1", "t1"))
    listed = await _call(server, "list_sigma_runs")
    assert listed["total"] == 1
    assert listed["runs"][0]["status"] == "completed"
    assert "results" not in listed["runs"][0]

    full = await _call(server, "get_sigma_run", {"run_id": run.id})
    assert full["results"][0]["match_count"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tools.py -k sigma -v`
Expected: FAIL — unknown tool `list_sigma_rules`.

- [ ] **Step 3: Implement the Sigma tools**

In `build_tool_server`, after the Task 1 tools:

```python
@server.tool()
async def list_sigma_rules() -> dict[str, Any]:
    """List Sigma detection rules available to this case (metadata only).

    Covers both the global rule directory and case-uploaded rules. Use
    get_sigma_rule with a case rule's id to read its YAML body.
    """
    from vestigo.api.deps import get_store
    from vestigo.api.routers.sigma import _global_rule_dict, _load_global

    global_rules, case_rows = await asyncio.gather(
        _load_global(), get_store().list_sigma_rules(scope.case_id)
    )
    rules = [_global_rule_dict(r) for r in global_rules]
    for row in case_rows:
        rules.append(
            {
                "origin": "case",
                "id": row.id,
                "rule_key": row.rule_key,
                "title": row.title,
                "level": row.level,
                "logsource": row.logsource,
                "enabled": row.enabled,
            }
        )
    return {"total": len(rules), "rules": rules}


@server.tool()
async def get_sigma_rule(rule_id: str) -> dict[str, Any]:
    """Fetch one case-uploaded Sigma rule including its full YAML content."""
    from vestigo.api.deps import get_store

    row = await get_store().get_sigma_rule(scope.case_id, rule_id)
    if row is None:
        return {"error": f"no case-uploaded sigma rule with id {rule_id}"}
    return row.to_dict()


@server.tool()
async def list_sigma_runs() -> dict[str, Any]:
    """List past Sigma evaluations over this timeline (newest first, no per-rule detail)."""
    from vestigo.api.deps import get_store

    rows = await get_store().list_sigma_runs(scope.case_id)
    rows = [r for r in rows if r.timeline_id == scope.timeline_id]
    return {
        "total": len(rows),
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "rule_count": len(r.results or []),
            }
            for r in rows
        ],
    }


@server.tool()
async def get_sigma_run(run_id: str) -> dict[str, Any]:
    """Fetch one Sigma run's full per-rule results (match counts, statuses, compiled SQL)."""
    from vestigo.api.deps import get_store

    row = await get_store().get_sigma_run(scope.case_id, run_id)
    if row is None or row.timeline_id != scope.timeline_id:
        return {"error": f"no sigma run with id {run_id} in this timeline"}
    return row.to_dict()
```

Add `import asyncio` to the module's imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: all PASS (Tasks 1+2 tests).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format src/vestigo/agent/tools.py tests/test_agent_tools.py
git add src/vestigo/agent/tools.py tests/test_agent_tools.py
git commit -m "feat(agent): sigma rule and run read tools"
```

---

### Task 3: FilterSpec extension (annotation state, run membership, event ids, routine collapse)

**Files:**
- Modify: `src/vestigo/agent/tools.py` (`FilterSpec`, `_build_query`)
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `_resolve_annotated_event_ids(case_id, source_ids, annotated, tag_value, run_id)` (`api/routers/events.py:408`), `_resolve_routine_collapse(case_id, timeline_id, source_ids, collapse_routine)` (`events.py:447`), `_intersect_optional` (`events.py`, near line 372 — verify with grep), `EventQuery.event_ids` / `.exclude_routine_disposition_ids` (`db/queries.py:152,161`). All already take explicit scope arguments — no router refactor needed.
- Produces: `FilterSpec` fields `annotated: list[str] | None`, `annotation_tag_value: str | None`, `run_id: str | None`, `event_ids: list[str] | None`, `collapse_routine: bool` — consumed by every existing tool and `propose_finding` via `_build_query`, and by Task 5's frontend mapping.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_tools.py`:

```python
@pytest.mark.asyncio
async def test_filterspec_annotated_resolves_to_event_ids(store, monkeypatch):
    """annotated=['tag'] resolves tagged event ids into EventQuery.event_ids."""
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.create_annotation("c1", "s1", "e-tagged", "a1", "tag", "bad", origin="user")
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(annotated=["tag"]))
    assert query.event_ids == ["e-tagged"]


@pytest.mark.asyncio
async def test_filterspec_event_ids_intersect_annotated(store):
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.create_annotation("c1", "s1", "e1", "a1", "tag", "bad", origin="user")
    await store.create_annotation("c1", "s1", "e2", "a2", "tag", "bad", origin="user")
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(annotated=["tag"], event_ids=["e2", "e3"]))
    assert query.event_ids == ["e2"]


@pytest.mark.asyncio
async def test_filterspec_event_ids_alone(store):
    from vestigo.agent.tools import FilterSpec, _build_query

    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(event_ids=["e9"]))
    assert query.event_ids == ["e9"]


@pytest.mark.asyncio
async def test_filterspec_collapse_routine(store):
    from vestigo.agent.tools import FilterSpec, _build_query

    row = await store.create_disposition(
        "c1",
        "routine",
        detector="sequence_motif",
        timeline_id="t1",
        field="artifact",
        value="a → b",
    )
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(collapse_routine=True))
    assert query.exclude_routine_disposition_ids == [row.id]
    plain = await _build_query(scope, FilterSpec())
    assert plain.exclude_routine_disposition_ids is None
```

Check `create_disposition`'s exact signature first (`grep -n "async def create_disposition" -A 12 src/vestigo/db/postgres.py`) and adjust keyword names if needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tools.py -k filterspec -v`
Expected: FAIL — `FilterSpec` has no field `annotated` (pydantic ValidationError).

- [ ] **Step 3: Extend FilterSpec and _build_query**

Add to `FilterSpec` (after `tags_exclude`):

```python
annotated: list[str] | None = Field(
    default=None,
    description=(
        'Restrict to annotated events: any of "tag" (user tags, optionally '
        'narrowed by annotation_tag_value) and "anomaly" (system anomaly '
        "marks; unioned with run_id findings when set)."
    ),
)
annotation_tag_value: str | None = Field(
    default=None, description='Narrow annotated=["tag"] to one exact tag value.'
)
run_id: str | None = Field(
    default=None,
    description=(
        "A persisted detector run id (from run_anomaly_detector) — its "
        'finding event ids are unioned into the "anomaly" branch of '
        '`annotated`. Only effective when annotated includes "anomaly".'
    ),
)
event_ids: list[str] | None = Field(
    default=None,
    description="Explicit event_id allowlist, intersected with the other id-based filters.",
)
collapse_routine: bool = Field(
    default=False,
    description="Hide events belonging to analyst-marked routine motifs (kind='routine' dispositions).",
)
```

In `_build_query`, extend the imports and resolution (replace the existing function body's start):

```python
async def _build_query(
    scope: AgentScope,
    spec: FilterSpec | None,
    *,
    limit: int = MAX_EVENTS_PER_SEARCH,
    offset: int = 0,
    order: str = "desc",
) -> EventQuery:
    from vestigo.api.routers.events import (
        _intersect_optional,
        _resolve_annotated_event_ids,
        _resolve_routine_collapse,
        _resolve_tags_filter,
    )

    spec = spec or FilterSpec()
    tags_include: TagFilter | None = None
    tags_exclude: TagFilter | None = None
    if spec.tags_include:
        tags_include = await _resolve_tags_filter(
            scope.case_id, scope.source_ids, spec.tags_include
        )
    if spec.tags_exclude:
        tags_exclude = await _resolve_tags_filter(
            scope.case_id, scope.source_ids, spec.tags_exclude
        )
    annotated_ids = await _resolve_annotated_event_ids(
        scope.case_id,
        scope.source_ids,
        ",".join(spec.annotated) if spec.annotated else None,
        spec.annotation_tag_value,
        spec.run_id,
    )
    event_ids = _intersect_optional(annotated_ids, spec.event_ids)
    routine_ids = await _resolve_routine_collapse(
        scope.case_id, scope.timeline_id, scope.source_ids, spec.collapse_routine
    )
```

and pass the new values into the `EventQuery(...)` constructor:

```python
event_ids = (event_ids,)
exclude_routine_disposition_ids = (routine_ids,)
```

Also update `get_event` (`tools.py:238`) — it currently overwrites `query.event_ids` after building; that still works since the spec there is empty, leave it as is.

`_resolve_run_event_ids` raises `HTTPException(404)` on an unknown run_id; inside the MCP loop that surfaces as a tool error to the model, which is the desired behavior (bad run_id = agent bug worth surfacing). No handling needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tools.py tests/test_agent_api.py -v`
Expected: all PASS (existing agent API tests must stay green — FilterSpec gained only optional fields).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format src/vestigo/agent/tools.py tests/test_agent_tools.py
git add src/vestigo/agent/tools.py tests/test_agent_tools.py
git commit -m "feat(agent): FilterSpec parity — annotation state, run membership, event ids, routine collapse"
```

---

### Task 4: Detector tuning parameters on run_anomaly_detector

**Files:**
- Modify: `src/vestigo/agent/tools.py` (`run_anomaly_detector`)
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `_run_stat_detector` kwargs (`events.py:1439-1459`): `z_threshold, min_skew_seconds, fdr_q, min_ratio, ngram_size, min_support, start, end`; `_persist_detector_run` (unchanged call, but pass the real `z_threshold`).
- Produces: extended tool signature; bounds identical to the HTTP endpoint (`events.py:2247-2296`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_tools.py`:

```python
@pytest.mark.asyncio
async def test_run_anomaly_detector_passes_tuning_params(store, monkeypatch):
    import vestigo.api.routers.events as events_router

    captured: dict[str, Any] = {}

    async def fake_run(case_id, timeline_id, source_ids, **kwargs):
        captured.update(kwargs)

        class R:
            status = "skipped"

        return R(), {}

    def fake_serialize(result):
        return {"status": result.status, "results": []}

    monkeypatch.setattr(events_router, "_run_stat_detector", fake_run)
    monkeypatch.setattr(events_router, "_serialize_stat_result", fake_serialize)

    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "run_anomaly_detector",
        {
            "detector": "proportion_shift",
            "z_threshold": 4.0,
            "fdr_q": 0.05,
            "min_ratio": 2.0,
            "ngram_size": 3,
            "min_support": 5,
            "min_skew_seconds": 1.5,
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
    )
    assert result["status"] == "skipped"
    assert captured["z_threshold"] == 4.0
    assert captured["fdr_q"] == 0.05
    assert captured["min_ratio"] == 2.0
    assert captured["ngram_size"] == 3
    assert captured["min_support"] == 5
    assert captured["min_skew_seconds"] == 1.5
    assert captured["start"] is not None and captured["end"] is not None


@pytest.mark.asyncio
async def test_run_anomaly_detector_rejects_out_of_bounds(store):
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(Exception):
        await _call(
            server, "run_anomaly_detector", {"detector": "sequence_novelty", "ngram_size": 9}
        )
```

Important: `build_tool_server` imports the events-router helpers **into local names at build time** (`tools.py:188-196`), so monkeypatching `events_router._run_stat_detector` after the server is built has no effect. The test above monkeypatches *before* `build_tool_server` is called — keep that order. (If the tool code is changed to late-bind imports, either works.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_tools.py -k tuning -v` (rename `-k` to match)
Expected: FAIL — unexpected keyword `z_threshold` (tool schema rejects unknown arg).

- [ ] **Step 3: Extend the tool**

Replace `run_anomaly_detector` in `tools.py` with:

```python
    @server.tool()
    async def run_anomaly_detector(
        detector: str,
        fields: str | None = None,
        series_field: str = "artifact",
        baseline_id: str | None = None,
        limit: int = 30,
        z_threshold: float | None = Field(default=None, gt=0),
        min_skew_seconds: float | None = Field(default=None, ge=0),
        fdr_q: float | None = Field(default=None, gt=0, le=1),
        min_ratio: float | None = Field(default=None, gt=1),
        ngram_size: int | None = Field(default=None, ge=2, le=5),
        min_support: int | None = Field(default=None, ge=2),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Run a statistical anomaly detector over the timeline.

        Detectors: value_novelty (rare/first-seen values), value_combo,
        frequency (volume spikes/silences), timestamp_order, numeric_range,
        charset, entropy, proportion_shift, interval_periodicity,
        sequence_novelty, sequence_motif, value_distribution_drift.
        `fields` is a comma-separated field list for value detectors (omit to
        auto-recommend); `series_field` groups frequency/sequence detectors.
        Temporal detectors need a `baseline_id` from list_baselines (omit for
        a self-baseline run). Tuning knobs (all optional, server defaults
        otherwise): z_threshold (frequency |z| cutoff), min_skew_seconds
        (timestamp_order), fdr_q (BH false-discovery ceiling), min_ratio
        (effect-size floor), ngram_size (sequence length, 2-5), min_support
        (sequence_motif), start/end (sequence_motif mining window).
        Returns findings plus a persisted run_id the analyst can open.
        """
        result, resolution = await _run_stat_detector(
            scope.case_id,
            scope.timeline_id,
            scope.source_ids,
            detector=detector,
            fields=fields,
            series_field=series_field,
            z_threshold=z_threshold,
            baseline_id=baseline_id,
            limit=min(limit, 100),
            min_skew_seconds=min_skew_seconds,
            fdr_q=fdr_q,
            min_ratio=min_ratio,
            ngram_size=ngram_size,
            min_support=min_support,
            start=start,
            end=end,
            field_mappings=scope.field_mappings,
            source_offsets=scope.source_offsets,
        )
        payload = _serialize_stat_result(result)
        run_id = None
        if result.status == "ok":
            run_id = await _persist_detector_run(
                scope.case_id,
                scope.timeline_id,
                detector=detector,
                fields=fields,
                series_field=series_field,
                z_threshold=z_threshold,
                limit=min(limit, 100),
                payload=payload,
                resolution=resolution,
                source_offsets=scope.source_offsets,
            )
        payload["run_id"] = run_id
        return payload
```

If `Field(...)` defaults in a plain function signature don't produce constraint-enforcing schemas through FastMCP, use `typing.Annotated[float | None, Field(gt=0)] = None` style instead — verify by running the bounds test.

Note for the monkeypatch-order caveat: if the Step 1 test can't pass because the helpers were bound at import of an earlier server, the clean fix is to move the `from vestigo.api.routers.events import ...` block from `build_tool_server`'s top into the individual tool bodies that use them (same lazy style as Task 1's tools). Do that only if needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tools.py tests/test_agent_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format src/vestigo/agent/tools.py tests/test_agent_tools.py
git add src/vestigo/agent/tools.py tests/test_agent_tools.py
git commit -m "feat(agent): expose detector tuning parameters with endpoint-identical bounds"
```

---

### Task 5: Frontend FilterSpec → EventFilters mapping

**Files:**
- Modify: `frontend/src/api/agent.ts` (`AgentFilterSpec`, `specToEventFilters`)
- Test: `frontend/src/test/agent.test.ts`

**Interfaces:**
- Consumes: `EventFilters` fields `annotated?: ("tag"|"anomaly")[]`, `annotationTagValue?`, `runId?`, `ids?: string[]`, `collapseRoutine?: boolean` (`frontend/src/api/types.ts:876-938` — verify exact camelCase names there before writing).
- Produces: extended `AgentFilterSpec` interface + mapping used by the finding-card apply path.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/test/agent.test.ts` (match its existing describe/it style):

```typescript
it("maps annotation-state, run, ids and routine-collapse fields", () => {
  const spec: AgentFilterSpec = {
    annotated: ["tag", "anomaly"],
    annotation_tag_value: "bad",
    run_id: "run-1",
    event_ids: ["e1", "e2"],
    collapse_routine: true,
  };
  const f = specToEventFilters(spec);
  expect(f.annotated).toEqual(["tag", "anomaly"]);
  expect(f.annotationTagValue).toBe("bad");
  expect(f.runId).toBe("run-1");
  expect(f.ids).toEqual(["e1", "e2"]);
  expect(f.collapseRoutine).toBe(true);
});

it("omits the new fields when absent", () => {
  const f = specToEventFilters({});
  expect(f.annotated).toBeUndefined();
  expect(f.runId).toBeUndefined();
  expect(f.ids).toBeUndefined();
  expect(f.collapseRoutine).toBeUndefined();
});
```

Adjust property names to whatever `EventFilters` actually declares (check `types.ts` around lines 916-938; `annotationTagValue` and `runId` names must match exactly).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npm run test`
Expected: FAIL — type error / undefined mapping.

- [ ] **Step 3: Implement**

Extend `AgentFilterSpec`:

```typescript
  annotated?: ("tag" | "anomaly")[] | null;
  annotation_tag_value?: string | null;
  run_id?: string | null;
  event_ids?: string[] | null;
  collapse_routine?: boolean;
```

Extend `specToEventFilters` (before `return f;`):

```typescript
  if (spec.annotated?.length) f.annotated = spec.annotated;
  if (spec.annotation_tag_value) f.annotationTagValue = spec.annotation_tag_value;
  if (spec.run_id) f.runId = spec.run_id;
  if (spec.event_ids?.length) f.ids = spec.event_ids;
  if (spec.collapse_routine) f.collapseRoutine = true;
```

- [ ] **Step 4: Verify**

Run: `cd frontend && npm run test && npm run typecheck && npm run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/agent.ts frontend/src/test/agent.test.ts
git commit -m "feat(agent): map extended FilterSpec fields onto Explorer filters"
```

---

### Task 6: AgentToken model, migration, store methods

**Files:**
- Modify: `src/vestigo/db/postgres.py` (model after `AgentConversation`/`AgentMessage` block; store methods near the agent-conversation section)
- Create: `src/vestigo/db/migrations/versions/0008_agent_tokens.py`
- Test: `tests/test_agent_tokens.py` (new)

**Interfaces:**
- Produces:
  - Model `AgentToken`: `id, token_hash (String(64), unique), case_id, timeline_id, user_id, name, created_at, expires_at (nullable), revoked_at (nullable)`, `to_dict()` (never includes `token_hash`).
  - `PostgresStore.create_agent_token(case_id, timeline_id, user_id, name, token_hash, expires_at=None) -> AgentToken`
  - `PostgresStore.list_agent_tokens(case_id, timeline_id) -> list[AgentToken]`
  - `PostgresStore.get_agent_token_by_hash(token_hash) -> AgentToken | None`
  - `PostgresStore.revoke_agent_token(case_id, token_id) -> bool`
- Consumed by Tasks 7 and 8.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_tokens.py`:

```python
"""AgentToken store + API + MCP auth tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_agent_token_store_roundtrip(store):
    row = await store.create_agent_token("c1", "t1", "u1", "claude-code", "a" * 64)
    assert row.id and row.revoked_at is None

    listed = await store.list_agent_tokens("c1", "t1")
    assert [t.id for t in listed] == [row.id]
    assert "token_hash" not in row.to_dict()

    by_hash = await store.get_agent_token_by_hash("a" * 64)
    assert by_hash is not None and by_hash.id == row.id
    assert await store.get_agent_token_by_hash("b" * 64) is None

    assert await store.revoke_agent_token("c1", row.id) is True
    revoked = await store.get_agent_token_by_hash("a" * 64)
    assert revoked is not None and revoked.revoked_at is not None
    assert await store.revoke_agent_token("c1", "missing") is False


@pytest.mark.asyncio
async def test_agent_token_expiry_field(store):
    exp = datetime.now(UTC) + timedelta(days=30)
    row = await store.create_agent_token("c1", "t1", "u1", "temp", "c" * 64, expires_at=exp)
    assert row.expires_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tokens.py -v`
Expected: FAIL — `AttributeError: 'PostgresStore' object has no attribute 'create_agent_token'`.

- [ ] **Step 3: Add the model**

In `postgres.py`, after the `AgentMessage` model class (find it after `AgentConversation`), add:

```python
class AgentToken(Base):
    """A scoped personal access token for the external MCP endpoint (docs/AGENT.md).

    Bound to exactly one case + timeline at creation — presenting the token
    yields precisely that scope, so the external client (like the built-in
    agent) never supplies IDs. Only the SHA-256 of the token is stored; the
    plaintext is shown once at creation. Access is re-checked against the
    creating user's current case RBAC on every connect, so revoking the user
    or their team membership also cuts off their tokens.
    """

    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict for the token-management API — never the hash."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "timeline_id": self.timeline_id,
            "user_id": self.user_id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }
```

- [ ] **Step 4: Add the store methods**

Near the agent-conversation store section in `postgres.py`:

```python
# ------------------------------------------------------------------
# Agent MCP tokens
# ------------------------------------------------------------------


async def create_agent_token(
    self,
    case_id: str,
    timeline_id: str,
    user_id: str,
    name: str,
    token_hash: str,
    expires_at: datetime | None = None,
) -> AgentToken:
    """Persist a new MCP access token row (hash only, never plaintext)."""
    row = AgentToken(
        id=generate_id(f"agent_token_{name}"),
        token_hash=token_hash,
        case_id=case_id,
        timeline_id=timeline_id,
        user_id=user_id,
        name=name,
        expires_at=expires_at,
    )
    async with self.session_factory() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def list_agent_tokens(self, case_id: str, timeline_id: str) -> list[AgentToken]:
    """Return a timeline's MCP tokens, newest first (revoked ones included)."""
    async with self.session_factory() as session:
        result = await session.execute(
            select(AgentToken)
            .where(AgentToken.case_id == case_id, AgentToken.timeline_id == timeline_id)
            .order_by(AgentToken.created_at.desc())
        )
        return list(result.scalars().all())


async def get_agent_token_by_hash(self, token_hash: str) -> AgentToken | None:
    """Resolve a presented token's SHA-256 to its row (revoked/expired rows included —
    the caller decides how to respond so auth failures stay distinguishable)."""
    async with self.session_factory() as session:
        result = await session.execute(
            select(AgentToken).where(AgentToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()


async def revoke_agent_token(self, case_id: str, token_id: str) -> bool:
    """Stamp revoked_at on a token row. Returns True when the row existed."""
    async with self.session_factory() as session:
        result = await session.execute(
            select(AgentToken).where(AgentToken.case_id == case_id, AgentToken.id == token_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await session.commit()
        return True
```

- [ ] **Step 5: Create the migration**

Do NOT hand-write it — autogenerate, then review:

```bash
uv run alembic revision --autogenerate -m "agent mcp tokens"
```

Review the generated file (rename to `0008_agent_tokens.py`, set `revision = "0008"`, `down_revision = "0007"`, matching the numbering convention of `0007_agent_conversations.py`). It must create `agent_tokens` with the unique index on `token_hash` and plain indexes on `case_id`, `timeline_id`, `user_id`. Keep it dialect-portable (`sa.func.now()` server defaults, as in 0007). If autogenerate misfires against the dev DB, hand-write it following `0007_agent_conversations.py` exactly.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tokens.py -v`
Expected: 2 PASS. (Tests run migrations against SQLite via `init_schema` — a non-portable migration fails here.)

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format src/vestigo/db/postgres.py tests/test_agent_tokens.py src/vestigo/db/migrations/versions/0008_agent_tokens.py
git add src/vestigo/db/postgres.py src/vestigo/db/migrations/versions/0008_agent_tokens.py tests/test_agent_tokens.py
git commit -m "feat(agent): AgentToken model, migration, store methods"
```

---

### Task 7: Token management API

**Files:**
- Create: `src/vestigo/api/routers/agent_tokens.py`
- Modify: `src/vestigo/api/main.py` (import + `app.include_router(agent_tokens.router)`)
- Test: `tests/test_agent_tokens.py`

**Interfaces:**
- Consumes: Task 6 store methods; `require_case_read`, `get_current_user`, `get_store` from `vestigo.api.deps`; `record_audit`.
- Produces: HTTP endpoints
  - `POST /api/cases/{case_id}/timelines/{timeline_id}/agent-tokens` body `{name, expires_in_days?}` → `{token: "<plaintext>", ...to_dict}` (plaintext returned exactly once)
  - `GET  /api/cases/{case_id}/timelines/{timeline_id}/agent-tokens` → `{tokens: [...]}`
  - `DELETE /api/cases/{case_id}/timelines/{timeline_id}/agent-tokens/{token_id}` → `{revoked: true}`
  - Module helpers `hash_token(plaintext) -> str` and `TOKEN_PREFIX = "vgo_"` (reused by Task 8's auth).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_tokens.py`:

```python
from tests.conftest import as_admin


def _case_and_timeline(client) -> tuple[str, str]:
    case = client.post("/api/cases/", json={"name": "token-case"}).json()["case"]
    tl = client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl"}).json()["timeline"]
    return case["id"], tl["id"]


def test_token_api_lifecycle(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _case_and_timeline(client)
    base = f"/api/cases/{case_id}/timelines/{tl_id}/agent-tokens"

    created = client.post(base, json={"name": "claude-code"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["token"].startswith("vgo_")
    assert body["name"] == "claude-code"

    listed = client.get(base).json()["tokens"]
    assert len(listed) == 1
    assert "token" not in listed[0] and "token_hash" not in listed[0]

    revoked = client.delete(f"{base}/{body['id']}")
    assert revoked.status_code == 200
    assert client.get(base).json()["tokens"][0]["revoked_at"] is not None

    assert client.delete(f"{base}/missing").status_code == 404


def test_token_create_rejects_unknown_timeline(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, _ = _case_and_timeline(client)
    resp = client.post(f"/api/cases/{case_id}/timelines/nope/agent-tokens", json={"name": "x"})
    assert resp.status_code == 404


def test_token_create_with_expiry(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _case_and_timeline(client)
    resp = client.post(
        f"/api/cases/{case_id}/timelines/{tl_id}/agent-tokens",
        json={"name": "temp", "expires_in_days": 7},
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tokens.py -v`
Expected: new tests FAIL with 404 (router not mounted).

- [ ] **Step 3: Implement the router**

Create `src/vestigo/api/routers/agent_tokens.py`:

```python
"""Scoped MCP access tokens for the external agent endpoint (docs/AGENT.md).

A token is bound to one case + timeline at creation; presenting it to the
/mcp endpoint yields exactly that scope. Only the SHA-256 is stored — the
plaintext appears once in the creation response. Access is re-validated
against the creating user's live case RBAC on every MCP connect, so these
endpoints only need READ (the token can never do more than read tools).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from vestigo.api.deps import get_current_user, get_store, require_case_read
from vestigo.db.postgres import Case, User

router = APIRouter(prefix="/api/cases", tags=["agent-tokens"])

TOKEN_PREFIX = "vgo_"


def hash_token(plaintext: str) -> str:
    """SHA-256 hex digest of a presented token — the only stored identity."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/{case_id}/timelines/{timeline_id}/agent-tokens")
async def create_agent_token(
    case_id: str,
    timeline_id: str,
    payload: CreateTokenRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a scoped MCP token; the plaintext is returned exactly once."""
    store = get_store()
    if await store.get_timeline(case_id, timeline_id) is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    row = await store.create_agent_token(
        case_id, timeline_id, user.id, payload.name, hash_token(plaintext), expires_at=expires_at
    )
    await store.record_audit(
        action="agent_token.create",
        actor=user,
        case_id=case_id,
        target_type="agent_token",
        target_id=row.id,
        detail={"name": payload.name, "timeline_id": timeline_id},
    )
    return {**row.to_dict(), "token": plaintext}


@router.get("/{case_id}/timelines/{timeline_id}/agent-tokens")
async def list_agent_tokens(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List a timeline's MCP tokens (metadata only, revoked included)."""
    rows = await get_store().list_agent_tokens(case_id, timeline_id)
    return {"tokens": [r.to_dict() for r in rows]}


@router.delete("/{case_id}/timelines/{timeline_id}/agent-tokens/{token_id}")
async def revoke_agent_token(
    case_id: str,
    timeline_id: str,
    token_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Revoke a token immediately (checked on every MCP connect)."""
    revoked = await get_store().revoke_agent_token(case_id, token_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Token not found")
    await get_store().record_audit(
        action="agent_token.revoke",
        actor=user,
        case_id=case_id,
        target_type="agent_token",
        target_id=token_id,
    )
    return {"revoked": True}
```

In `main.py`, add `agent_tokens` to the `from vestigo.api.routers import (...)` block and `app.include_router(agent_tokens.router)` next to `app.include_router(agent.router)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tokens.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format src/vestigo/api/routers/agent_tokens.py tests/test_agent_tokens.py
git add src/vestigo/api/routers/agent_tokens.py src/vestigo/api/main.py tests/test_agent_tokens.py
git commit -m "feat(agent): scoped MCP token management API"
```

---

### Task 8: HTTP MCP endpoint

**Files:**
- Create: `src/vestigo/agent/mcp_http.py`
- Modify: `src/vestigo/core/config.py` (add `mcp_enabled: bool = False` after the agent settings block)
- Modify: `src/vestigo/api/main.py` (mount `/mcp` when enabled; health flag `mcp_enabled`)
- Test: `tests/test_mcp_http.py` (new)

**Interfaces:**
- Consumes: `build_scope`/`build_tool_server` (unchanged), `hash_token`/`TOKEN_PREFIX` from Task 7, `get_agent_token_by_hash`, `has_case_access`/`AccessLevel` from `deps`, `record_audit`.
- Produces: ASGI app `MCPEndpoint` mounted at `/mcp`; health field `"mcp_enabled"`.

**Design notes (verified against the installed `mcp` SDK):**
- `FastMCP(..., stateless_http=True, streamable_http_path="/")` + `server.streamable_http_app()` returns a Starlette app; its `StreamableHTTPSessionManager` must be entered via `async with server.session_manager.run():` around request handling. Building server + session manager **per request** is correct for stateless mode and keeps `build_tool_server(scope)` untouched — scope comes from the token, tools stay closures, invariant intact.
- `/mcp` is outside `/api/`, so `AuthAuditMiddleware`'s session-cookie gate does not apply — Bearer auth below is the sole gate.
- Tool-call audit: the wrapper buffers the (small, JSON-RPC) request body, sniffs `method == "tools/call"`, writes the same `agent.tool_call` audit row the built-in loop writes, plus `token_id`, then replays the body to the inner app.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_http.py`:

```python
"""End-to-end tests for the /mcp streamable-HTTP endpoint with token auth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import as_admin
from vestigo.api.main import create_app
from vestigo.api.routers.agent_tokens import hash_token
from vestigo.core.config import get_settings


@pytest.fixture()
def mcp_client(store, admin_bootstrap, monkeypatch):
    monkeypatch.setenv("VESTIGO_MCP_ENABLED", "1")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _setup_token(client) -> tuple[str, str, str]:
    """Create case + timeline + token via the API. Caller must be logged in (as_admin)."""
    case = client.post("/api/cases/", json={"name": "mcp-case"}).json()["case"]
    tl = client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl"}).json()["timeline"]
    token = client.post(
        f"/api/cases/{case['id']}/timelines/{tl['id']}/agent-tokens", json={"name": "e2e"}
    ).json()["token"]
    return case["id"], tl["id"], token


def _rpc_initialize(client, token: str | None, path: str = "/mcp"):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers=headers,
    )


def test_mcp_absent_when_disabled(client, admin_bootstrap):
    resp = _rpc_initialize(client, token=None)
    assert resp.status_code == 404
    assert client.get("/api/health").json()["mcp_enabled"] is False


def test_mcp_requires_valid_token(mcp_client, admin_bootstrap):
    assert mcp_client.get("/api/health").json()["mcp_enabled"] is True
    assert _rpc_initialize(mcp_client, token=None).status_code == 401
    assert _rpc_initialize(mcp_client, token="vgo_wrong").status_code == 401


def test_mcp_accepts_valid_token(mcp_client, admin_bootstrap):
    as_admin(mcp_client, admin_bootstrap)
    case_id, tl_id, token = _setup_token(mcp_client)
    ok = _rpc_initialize(mcp_client, token)
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_mcp_rejects_revoked_and_expired_rows(store):
    """Auth-decision unit test at the store level (revoked/expired distinguishable)."""
    from vestigo.agent.mcp_http import _token_auth_error

    valid = await store.create_agent_token("c1", "t1", "u1", "ok", hash_token("vgo_a"))
    assert _token_auth_error(valid) is None

    revoked = await store.create_agent_token("c1", "t1", "u1", "rev", hash_token("vgo_b"))
    await store.revoke_agent_token("c1", revoked.id)
    revoked = await store.get_agent_token_by_hash(hash_token("vgo_b"))
    assert _token_auth_error(revoked) == "token revoked"

    expired = await store.create_agent_token(
        "c1",
        "t1",
        "u1",
        "exp",
        hash_token("vgo_c"),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert _token_auth_error(expired) == "token expired"


def test_mcp_end_to_end_tool_call(mcp_client, admin_bootstrap):
    """Full streamable-HTTP round trip: initialize, list tools, call one."""
    as_admin(mcp_client, admin_bootstrap)
    case_id, tl_id, token = _setup_token(mcp_client)

    init = _rpc_initialize(mcp_client, token)
    assert init.status_code == 200, init.text

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    listed = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert "list_baselines" in listed.text

    called = mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_baselines", "arguments": {}},
        },
        headers=headers,
    )
    assert called.status_code == 200, called.text
    assert '"total"' in called.text
```

Note: in stateless streamable HTTP the server may still require the `initialize` request per connection; if `tools/list` without a session id 400s, add `"mcp-session-id"` header from the init response, or set `json_response=True` on the server and re-check — adapt to what the SDK actually returns (the assertion targets, not the transport details, are the contract).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_http.py -v`
Expected: FAIL — `ModuleNotFoundError: vestigo.agent.mcp_http` / health missing `mcp_enabled`.

- [ ] **Step 3: Config + implementation**

`core/config.py`, after `agent_probe_ttl_seconds`:

```python
    # External MCP endpoint (/mcp): serves the same scoped tool server the
    # built-in agent uses over streamable HTTP, authenticated by scoped
    # per-timeline tokens (agent_tokens table). Off by default — invisible
    # unless the operator enables it. Independent of VESTIGO_AGENT_* (serving
    # MCP needs no LLM endpoint).
    mcp_enabled: bool = False
```

Create `src/vestigo/agent/mcp_http.py`:

```python
"""Streamable-HTTP MCP endpoint serving the scoped agent tool server.

External MCP clients (Claude Code, hermes-agent, …) connect with
``Authorization: Bearer vgo_…`` — a scoped token minted per case+timeline
(``api/routers/agent_tokens.py``). The wrapper authenticates, re-checks the
creating user's live case RBAC, builds the exact same tool server the
built-in agent uses (``build_tool_server``), and delegates the request to a
per-request stateless MCP app. Scope comes from the token, never the model —
the scope-safety invariant holds on this transport too.

Tool calls are audited like the built-in loop's (``agent.tool_call``), with
the token id in the detail.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from vestigo.db._dt import ensure_utc

logger = logging.getLogger(__name__)


def _token_auth_error(row: Any | None) -> str | None:
    """Return the auth-failure reason for a token row, or None when usable."""
    if row is None:
        return "unknown token"
    if row.revoked_at is not None:
        return "token revoked"
    if row.expires_at is not None and ensure_utc(row.expires_at) < datetime.now(UTC):
        return "token expired"
    return None


async def _authenticate(headers: dict[bytes, bytes]) -> tuple[Any, Any] | JSONResponse:
    """Resolve the Bearer token to (token_row, user) or an error response."""
    from vestigo.api.deps import AccessLevel, get_store, has_case_access
    from vestigo.api.routers.agent_tokens import TOKEN_PREFIX, hash_token

    auth = headers.get(b"authorization", b"").decode()
    if not auth.startswith("Bearer ") or not auth[7:].startswith(TOKEN_PREFIX):
        return JSONResponse(status_code=401, content={"detail": "Bearer token required"})
    store = get_store()
    row = await store.get_agent_token_by_hash(hash_token(auth[7:]))
    reason = _token_auth_error(row)
    if reason is not None:
        return JSONResponse(status_code=401, content={"detail": reason})
    user = await store.get_user(row.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "token user inactive"})
    case = await store.get_case(row.case_id)
    if not await has_case_access(user, case, AccessLevel.READ):
        return JSONResponse(status_code=403, content={"detail": "case access revoked"})
    return row, user


async def _audit_tool_call(body: bytes, token_row: Any, user: Any) -> None:
    """Best-effort agent.tool_call audit row for tools/call requests."""
    from vestigo.api.deps import get_store

    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return
    params = message.get("params") or {}
    await get_store().record_audit(
        action="agent.tool_call",
        actor=user,
        case_id=token_row.case_id,
        target_type="agent_token",
        target_id=token_row.id,
        detail={
            "tool": params.get("name"),
            "args": params.get("arguments"),
            "transport": "mcp_http",
        },
    )


class MCPEndpoint:
    """ASGI app mounted at /mcp: Bearer auth + per-request scoped MCP dispatch."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        auth = await _authenticate(headers)
        if isinstance(auth, JSONResponse):
            await auth(scope, receive, send)
            return
        token_row, user = auth

        # Buffer the request body once: audit sniffs it, then the inner app
        # re-reads it through a replaying receive.
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await _audit_tool_call(body, token_row, user)

        sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        from vestigo.agent.tools import build_scope, build_tool_server

        try:
            agent_scope = await build_scope(token_row.case_id, token_row.timeline_id, user)
        except HTTPException as exc:
            await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})(
                scope, replay_receive, send
            )
            return

        server = build_tool_server(agent_scope)
        server.settings.stateless_http = True
        server.settings.streamable_http_path = "/"
        app = server.streamable_http_app()
        async with server.session_manager.run():
            await app(scope, replay_receive, send)
```

In `main.py`'s `create_app()`, after the router includes and before the frontend mount:

```python
    if get_settings().mcp_enabled:
        from vestigo.agent.mcp_http import MCPEndpoint

        app.mount("/mcp", MCPEndpoint())
```

And extend the health payload:

```python
            "mcp_enabled": get_settings().mcp_enabled,
```

Path detail: `app.mount("/mcp", ...)` forwards sub-paths with the prefix stripped; the MCP Starlette app's route was configured at `/` via `streamable_http_path="/"`. If the mounted sub-app sees path `""` and 404s, mount via `app.router.routes.append(Mount("/mcp", app=MCPEndpoint()))` or route both `/mcp` and `/mcp/` — adjust based on the failing test, the contract is: `POST /mcp` reaches the wrapper.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_http.py tests/test_agent_tokens.py -v`
Expected: all PASS. Debug transport quirks (session-id header, trailing slash) against the test assertions — the SDK's `StreamableHTTPSessionManager` in stateless mode accepts request-per-POST without a session id.

- [ ] **Step 5: Full backend suite, lint, commit**

```bash
uv run pytest
uv run ruff check . && uv run ruff format src/vestigo/agent/mcp_http.py src/vestigo/core/config.py src/vestigo/api/main.py tests/test_mcp_http.py
git add src/vestigo/agent/mcp_http.py src/vestigo/core/config.py src/vestigo/api/main.py tests/test_mcp_http.py
git commit -m "feat(agent): streamable-HTTP MCP endpoint with scoped token auth"
```

---

### Task 9: Frontend token management UI

**Files:**
- Create: `frontend/src/api/agentTokens.ts`
- Create: `frontend/src/components/timelines/AgentTokensDialog.tsx`
- Modify: `frontend/src/components/timelines/TimelineList.tsx` (add dialog to the per-row actions)
- Modify: `frontend/src/api/types.ts` (add `mcp_enabled: boolean` to `HealthResponse`, near `agent_available` at line ~869)

**Interfaces:**
- Consumes: Task 7 endpoints; `healthApi.check()` (`frontend/src/api/health.ts`); UI primitives `Dialog/DialogContent/DialogTrigger`, `Button`, `Spinner` (`frontend/src/components/ui/`), `toast` (`@/stores/toasts`) — same stack as `EnrichersDialog.tsx`.
- Produces: `agentTokensApi` client + `AgentTokensDialog` rendered per timeline row when `health.mcp_enabled`.

- [ ] **Step 1: API client**

Create `frontend/src/api/agentTokens.ts`:

```typescript
/** Scoped MCP access tokens for the external agent endpoint (docs/AGENT.md). */
import { get, post, del } from "./client";

export interface AgentToken {
  id: string;
  case_id: string;
  timeline_id: string;
  user_id: string;
  name: string;
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export const agentTokensApi = {
  list: (caseId: string, timelineId: string) =>
    get<{ tokens: AgentToken[] }>(`/cases/${caseId}/timelines/${timelineId}/agent-tokens`),
  create: (caseId: string, timelineId: string, body: { name: string; expires_in_days?: number }) =>
    post<AgentToken & { token: string }>(
      `/cases/${caseId}/timelines/${timelineId}/agent-tokens`,
      body,
    ),
  revoke: (caseId: string, timelineId: string, tokenId: string) =>
    del<{ revoked: boolean }>(`/cases/${caseId}/timelines/${timelineId}/agent-tokens/${tokenId}`),
};
```

- [ ] **Step 2: Dialog component**

Create `frontend/src/components/timelines/AgentTokensDialog.tsx` — follow `EnrichersDialog.tsx`'s structure (Dialog + open state + `useQuery` gated on `enabled: open` + mutations invalidating the query). Content:

- Trigger: icon button (lucide `KeyRound`) with title "MCP access tokens".
- Body: list of tokens (`name`, created/expires, revoked badge, revoke button per active row), a create form (`name` text input, optional expiry-days number input), and — after a successful create — a one-time banner showing the plaintext token with a copy button and the note "Shown once — store it now. Connect an MCP client to /mcp with Authorization: Bearer <token>."
- Mutations: `create` (on success: keep plaintext in local state, invalidate list, `toast.success`), `revoke` (invalidate list).

Complete skeleton (adapt imports/classNames to the surrounding components):

```tsx
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Copy, Ban } from "lucide-react";
import { agentTokensApi } from "@/api/agentTokens";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Badge } from "@/components/ui/Badge";
import { toast } from "@/stores/toasts";
import { fmtRelative } from "@/lib/time";
import type { Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timeline: Timeline;
}

/** Manage scoped MCP access tokens for external agents (docs/AGENT.md). */
export function AgentTokensDialog({ caseId, timeline }: Props) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [freshToken, setFreshToken] = useState<string | null>(null);
  const qc = useQueryClient();
  const queryKey = ["agent-tokens", caseId, timeline.id] as const;

  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => agentTokensApi.list(caseId, timeline.id),
    enabled: open,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      agentTokensApi.create(caseId, timeline.id, {
        name: name.trim(),
        ...(expiresDays ? { expires_in_days: Number(expiresDays) } : {}),
      }),
    onSuccess: (created) => {
      setFreshToken(created.token);
      setName("");
      setExpiresDays("");
      void qc.invalidateQueries({ queryKey });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const revokeMutation = useMutation({
    mutationFn: (tokenId: string) => agentTokensApi.revoke(caseId, timeline.id, tokenId),
    onSuccess: () => void qc.invalidateQueries({ queryKey }),
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) setFreshToken(null); }}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" title="MCP access tokens">
          <KeyRound size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <h3 className="text-sm font-semibold mb-2">MCP access tokens — {timeline.name}</h3>
        <p className="text-xs text-[var(--color-fg-muted)] mb-3">
          Tokens let an external MCP client (e.g. Claude Code) investigate this
          timeline read-only via <code>/mcp</code>. Scope is fixed to this
          timeline; revocation is immediate.
        </p>
        {freshToken && (
          <div className="mb-3 rounded border border-[var(--color-warning)] p-2 text-xs">
            <div className="mb-1 font-medium">Shown once — store it now:</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 break-all">{freshToken}</code>
              <Button
                variant="ghost"
                size="icon"
                title="Copy"
                onClick={() => {
                  void navigator.clipboard.writeText(freshToken);
                  toast.success("Token copied");
                }}
              >
                <Copy size={12} />
              </Button>
            </div>
          </div>
        )}
        {isLoading && <Spinner />}
        <div className="space-y-1">
          {data?.tokens.map((t) => (
            <div key={t.id} className="flex items-center gap-2 text-xs">
              <span className="flex-1 truncate">{t.name}</span>
              {t.revoked_at ? (
                <Badge variant="muted">revoked</Badge>
              ) : (
                <>
                  {t.expires_at && <span>expires {fmtRelative(t.expires_at)}</span>}
                  <Button
                    variant="ghost"
                    size="icon"
                    title="Revoke"
                    onClick={() => revokeMutation.mutate(t.id)}
                  >
                    <Ban size={12} />
                  </Button>
                </>
              )}
            </div>
          ))}
          {data && data.tokens.length === 0 && (
            <p className="text-xs text-[var(--color-fg-muted)]">No tokens yet.</p>
          )}
        </div>
        <form
          className="mt-3 flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) createMutation.mutate();
          }}
        >
          <input
            className="flex-1 rounded border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs"
            placeholder="Token name (e.g. claude-code)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="w-24 rounded border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs"
            placeholder="days (opt)"
            inputMode="numeric"
            value={expiresDays}
            onChange={(e) => setExpiresDays(e.target.value.replace(/\D/g, ""))}
          />
          <Button type="submit" size="sm" disabled={!name.trim() || createMutation.isPending}>
            Create
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
```

Check `Button`/`Dialog` prop names against `EnrichersDialog.tsx` usage and adjust (e.g. `size="icon"` variants).

- [ ] **Step 3: Wire into TimelineList + health flag**

In `frontend/src/api/types.ts`, add to `HealthResponse` (next to `agent_available`): `mcp_enabled: boolean;`

In `TimelineList.tsx`:
- import `AgentTokensDialog` and `healthApi` + `useQuery`;
- in `TimelineList`, fetch health once: `const { data: health } = useQuery({ queryKey: ["health"], queryFn: healthApi.check });`
- pass `mcpEnabled={health?.mcp_enabled ?? false}` down to `TimelineRow` (add prop);
- in `TimelineRow`'s actions div (next to `EnrichersDialog`): `{mcpEnabled && <AgentTokensDialog caseId={caseId} timeline={tl} />}`.

- [ ] **Step 4: Verify**

Run: `cd frontend && npm run typecheck && npm run lint && npm run test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/agentTokens.ts frontend/src/api/types.ts frontend/src/components/timelines/AgentTokensDialog.tsx frontend/src/components/timelines/TimelineList.tsx
git commit -m "feat(agent): MCP token management UI"
```

---

### Task 10: Documentation + spec sync

**Files:**
- Modify: `docs/AGENT.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/PROGRESS.md` (new entry on top)
- Modify: `docs/superpowers/specs/2026-07-19-agent-read-parity-mcp-http-design.md`

- [ ] **Step 1: Update AGENT.md**

- Tools section: add the nine new tools and the extended FilterSpec fields + detector tuning params to the read-only tool list.
- Design invariants: "Read-only v1" stands; extend "Scope safety" with "on both transports — the HTTP endpoint derives scope from the token, never the model".
- Architecture: replace the "Exposing the same server over HTTP … is a roadmap item" bullet with a new **External MCP endpoint** section: `/mcp` streamable HTTP, `VESTIGO_MCP_ENABLED`, scoped tokens (create/revoke in the timeline list UI), Bearer auth, per-connect RBAC re-check, `agent.tool_call` audit with token id, independence from `VESTIGO_AGENT_*`.
- Configuration table: add `VESTIGO_MCP_ENABLED`.
- Testing section: add `tests/test_agent_tools.py`, `tests/test_agent_tokens.py`, `tests/test_mcp_http.py`.

- [ ] **Step 2: Update ROADMAP.md**

Delete/adjust any items covered by this work (baseline discovery, agent read gaps, external MCP exposure) per the "delete items once fixed" convention.

- [ ] **Step 3: Update the spec's deviation**

In the spec file, section 2: change `event_ids / exclude_event_ids` to `event_ids` only, with the one-line rationale (frontend `EventFilters` has no exclude-ids shape, so such a finding could never be applied).

- [ ] **Step 4: PROGRESS.md entry**

Prepend a dated entry: what changed (read parity + /mcp) and why (agent-analyst parity, agent-agnostic external access).

- [ ] **Step 5: Final verification + commit**

```bash
uv run pytest
uv run ruff check .
cd frontend && npm run typecheck && npm run lint && npm run test && cd ..
git add docs/
git commit -m "docs: agent read parity + external MCP endpoint"
```

---

## End-to-end verification (after all tasks)

1. `uv run pytest` — full backend suite green.
2. `cd frontend && npm run build` — production build succeeds.
3. Manual (use the `verify` skill's isolated-DB recipe): start `podman compose up -d`, run `VESTIGO_MCP_ENABLED=1 uv run vestigo-web`, create a case+timeline+token in the UI, then from a terminal:
   ```bash
   # Any MCP client works; quickest smoke test with curl:
   curl -s -X POST http://localhost:8080/mcp \
     -H "Authorization: Bearer vgo_..." \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
   ```
   Expect a JSON-RPC result. Then `tools/list` must include all 20 tools; a wrong token must 401; a revoked token must 401.
4. Built-in agent unchanged: with `VESTIGO_AGENT_*` configured, ask the agent "list the saved baselines" in a conversation — it should call `list_baselines`.
