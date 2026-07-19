"""In-memory tests for the agent's MCP tools against the SQLite-backed store.

Calls tools exactly like the runtime does — through a fastmcp in-memory
client over the real `build_tool_server` — so tool schemas, serialization,
and scope binding are all exercised.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

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


async def test_list_baselines_returns_timeline_definitions(store):
    await store.init_schema()
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


async def test_list_dispositions_scoped_and_filtered(store):
    await store.init_schema()
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


async def test_list_saved_views(store):
    await store.init_schema()
    await store.create_view(
        "c1", "v1", "failed logins", "status:4625", {"filters": {"status": ["4625"]}}
    )
    server = build_tool_server(_scope("c1", "t1"))
    result = await _call(server, "list_saved_views")
    assert result["total"] == 1
    view = result["views"][0]
    assert view["name"] == "failed logins"
    assert view["query"] == "status:4625"
    assert view["filter"] == {"filters": {"status": ["4625"]}}


async def test_annotations_tools(store):
    await store.init_schema()
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


async def test_sigma_rules_tools(store, monkeypatch):
    await store.init_schema()
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


async def test_sigma_runs_tools(store):
    await store.init_schema()
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
