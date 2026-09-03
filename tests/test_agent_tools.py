"""In-memory tests for the agent's MCP tools against the SQLite-backed store.

Calls tools exactly like the runtime does — through a fastmcp in-memory
client over the real `build_tool_server` — so tool schemas, serialization,
and scope binding are all exercised.
"""

from __future__ import annotations

import json
import typing
from datetime import UTC, datetime
from typing import Any

import pytest
from fastmcp.client import Client as FastMCPClient
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from vestigo.agent.fidelity import Fidelity
from vestigo.agent.schema_slim import SHARED_SPEC_NAMES
from vestigo.agent.tools import (
    STORY_TEXT_BUDGET,
    STORY_TEXT_TRUNCATE,
    AgentScope,
    ChartSpec,
    FilterSpec,
    ObjectArgModel,
    _admits_json_object,
    build_tool_server,
    schema_chars_for_scope,
)
from vestigo.db._time_fields import resolve_time_field
from vestigo.db.postgres import User


def _scope(
    case_id: str,
    timeline_id: str,
    source_ids: list[str] | None = None,
    fidelity: Fidelity = Fidelity.MESSAGE,
) -> AgentScope:
    # MESSAGE rather than the deployment default (FULL): these tests assert the
    # reshaping the agent boundary applies, and FULL is the tier that applies
    # none. Tier selection itself lives in tests/test_agent_fidelity.py.
    return AgentScope(
        case_id=case_id,
        timeline_id=timeline_id,
        user=User(id="u1", username="tester", is_admin=True, is_active=True),
        source_ids=source_ids or [],
        field_mappings=None,
        source_offsets=None,
        fidelity=fidelity,
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


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Decode a columnar tool payload back to dict rows (see agent/encoding.py).

    Tabular results are sent to the model column-header-once (A13); tests
    assert against the decoded rows, which also exercises the round trip.
    """
    return [dict(zip(payload["columns"], row, strict=True)) for row in payload["rows"]]


async def test_list_configured_detectors_reads_the_timeline_list(store):
    await store.init_schema()
    await store.create_case(case_id="c1", name="c", owner_id="u1")
    await store.create_timeline(case_id="c1", timeline_id="t1", name="t")
    await store.set_timeline_detector(
        "c1",
        "t1",
        {
            "method": "value_novelty",
            "params": {"fields": ["user"]},
            "frame": "self",
            "baseline_id": None,
            "added_by": "u1",
            "added_at": "2026-09-03T00:00:00+00:00",
        },
    )
    server = build_tool_server(_scope("c1", "t1"))
    result = await _call(server, "list_configured_detectors")
    assert result["total"] == 1
    assert [d["method"] for d in result["detectors"]] == ["value_novelty"]
    assert result["detectors"][0]["params"] == {"fields": ["user"]}


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
    (b,) = _rows(result["baselines"])
    assert b["name"] == "normal week"
    assert b["id"]
    assert b["baseline"]["start"].startswith("2026-01-01")
    assert b["suspect_windows"][0]["label"] == "incident"


async def test_list_tools_report_returned_against_total(store, monkeypatch):
    """A capped list must be distinguishable from a complete one, or the model
    reasons over a silently partial set — the evidence rule's whole point."""
    import vestigo.agent.tools as agent_tools

    await store.init_schema()
    for i in range(3):
        await store.create_view("c1", f"v{i}", f"view {i}", f"q{i}", {})

    server = build_tool_server(_scope("c1", "t1"))
    full = await _call(server, "list_saved_views")
    assert full["total"] == full["returned"] == 3

    monkeypatch.setattr(agent_tools, "MAX_LIST_ROWS", 2)
    capped = await _call(server, "list_saved_views")
    assert capped["total"] == 3 and capped["returned"] == 2
    assert len(_rows(capped["views"])) == 2


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
    assert _rows(result["dispositions"])[0]["field"] == "user"
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
    view = _rows(result["views"])[0]
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
    assert _rows(tags_only["annotations"])[0]["content"] == "suspicious"
    single = await _call(server, "get_event_annotations", {"source_id": "s1", "event_id": "e2"})
    assert single["total"] == 1
    assert _rows(single["annotations"])[0]["created_by"] == "bob"


async def test_list_annotations_truncates_content_harder_than_detail_tool(store):
    """The bulk list is a scan resent every turn (200 rows of long CVE bodies
    was ~7k tokens); it truncates the body tighter than get_event_annotations,
    which is the one-event detail tool and keeps the fuller text."""
    from vestigo.agent.tools import ANNOTATION_LIST_CONTENT_TRUNCATE, MESSAGE_TRUNCATE

    await store.init_schema()
    body = "CVE-2024-4577 " + "detail " * 100  # ~700 chars, over both caps
    await store.create_annotation("c1", "s1", "e1", "a1", "comment", body, created_by="alice")
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))

    listed = _rows((await _call(server, "list_annotations"))["annotations"])[0]
    assert len(listed["content"]) <= ANNOTATION_LIST_CONTENT_TRUNCATE + 1  # +1 for the ellipsis
    detail = _rows(
        (await _call(server, "get_event_annotations", {"source_id": "s1", "event_id": "e1"}))[
            "annotations"
        ]
    )[0]
    assert len(detail["content"]) > ANNOTATION_LIST_CONTENT_TRUNCATE
    assert len(detail["content"]) <= MESSAGE_TRUNCATE + 1


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
    meta = _rows(listed["rules"])[0]
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
    assert _rows(listed["runs"])[0]["status"] == "completed"
    assert "results" not in _rows(listed["runs"])[0]

    full = await _call(server, "get_sigma_run", {"run_id": run.id})
    assert full["results"][0]["match_count"] == 3


async def test_filterspec_annotated_resolves_to_event_ids(store, monkeypatch):
    """annotated=['tag'] resolves tagged event ids into EventQuery.event_ids."""
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.init_schema()
    await store.create_annotation("c1", "s1", "e-tagged", "a1", "tag", "bad", origin="user")
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(annotated=["tag"]))
    assert query.event_ids == ["e-tagged"]


async def test_filterspec_event_ids_intersect_annotated(store):
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.init_schema()
    await store.create_annotation("c1", "s1", "e1", "a1", "tag", "bad", origin="user")
    await store.create_annotation("c1", "s1", "e2", "a2", "tag", "bad", origin="user")
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(annotated=["tag"], event_ids=["e2", "e3"]))
    assert query.event_ids == ["e2"]


async def test_build_query_clamps_limit_and_offset(store):
    """Model-supplied paging is clamped — a negative LIMIT/OFFSET would be a
    ClickHouse error."""
    from vestigo.agent.tools import MAX_EVENTS_PER_SEARCH, FilterSpec, _build_query

    await store.init_schema()
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(), limit=-5, offset=-10)
    assert query.limit == 1
    assert query.offset == 0
    query = await _build_query(scope, FilterSpec(), limit=10_000)
    assert query.limit == MAX_EVENTS_PER_SEARCH


async def test_filterspec_event_ids_alone(store):
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.init_schema()
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(event_ids=["e9"]))
    assert query.event_ids == ["e9"]


async def test_filterspec_collapse_routine(store):
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.init_schema()
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


async def test_filterspec_collapse_routine_log_template(store):
    """W6: agent-side search/grid parity — a log_template routine
    disposition resolves to exclude_template_hashes, not the motif
    disposition-id anti-join path."""
    from vestigo.agent.tools import FilterSpec, _build_query

    await store.init_schema()
    await store.create_disposition(
        "c1",
        "routine",
        detector="log_template",
        timeline_id="t1",
        field="template_id",
        value="987654321",
        details={"template": "Allow TCP <IP>", "template_version": 1},
    )
    scope = _scope("c1", "t1", source_ids=["s1"])
    query = await _build_query(scope, FilterSpec(collapse_routine=True))
    assert query.exclude_template_hashes == [987654321]
    assert query.exclude_routine_disposition_ids is None
    plain = await _build_query(scope, FilterSpec())
    assert plain.exclude_template_hashes is None


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
async def test_run_anomaly_detector_findings_are_columnar_and_deflated(store, monkeypatch):
    """One run is one detector, so its findings share their keys — the same
    header-once win as the other tabular results. The model's copy also reduces
    each finding's heavy inline `event` to its `message` line, which was ~85% of
    a finding's size and overflowed a 64k model across a seven-detector sweep.
    The *persisted* payload keeps its full dict rows; only the model's copy is
    reshaped."""
    import vestigo.api.routers.events as events_router

    big_event = {"event_id": "e1", "message": "login as svc-a", "attr": {"k": "y" * 8000}}
    findings = [
        {
            "type": "value_novelty",
            "field": "user",
            "value": "svc-a",
            "count": 1,
            "event_id": "e1",
            "event": big_event,
            "details": {"surprise": 12.7},
        },
        {
            "type": "value_novelty",
            "field": "user",
            "value": "svc-b",
            "count": 2,
            "event_id": "e2",
            "event": {"event_id": "e2", "message": "login as svc-b"},
            "details": {"surprise": 9.1},
        },
    ]
    persisted: dict[str, Any] = {}

    async def fake_run(case_id, timeline_id, source_ids, **kwargs):
        class R:
            status = "skipped"  # keeps the persistence path out of this test

        return R(), {}

    monkeypatch.setattr(events_router, "_run_stat_detector", fake_run)
    monkeypatch.setattr(
        events_router,
        "_serialize_stat_result",
        lambda result: persisted.setdefault(
            "payload", {"status": result.status, "results": findings}
        ),
    )

    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "run_anomaly_detector", {"detector": "value_novelty"})
    model_rows = _rows(result["results"])
    # event_id + message + details survive; the fat attribute bag is gone.
    assert [r["event_id"] for r in model_rows] == ["e1", "e2"]
    assert [r["message"] for r in model_rows] == ["login as svc-a", "login as svc-b"]
    assert all("event" not in r for r in model_rows)
    assert model_rows[0]["details"] == {"surprise": 12.7}
    assert "get_event" in result["note"]
    assert "columns" in result["results"] and "event" not in result["results"]["columns"]
    # The persisted record is untouched — the full event stays reproducible.
    assert persisted["payload"]["results"][0]["event"] == big_event

    # ...and at the deployment default the model gets the whole event, since
    # an operator who declared no context constraint is assumed to have room.
    full_server = build_tool_server(_scope("c1", "t1", ["s1"], fidelity=Fidelity.FULL))
    full = await _call(full_server, "run_anomaly_detector", {"detector": "value_novelty"})
    assert _rows(full["results"])[0]["event"] == big_event
    # Nothing was dropped, so no note — but the tier is still on the record,
    # or an export could not tell "ran at full" from "predates the setting".
    assert full["fidelity"] == "full"
    assert "note" not in full


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [0, 2])
async def test_a_detector_run_from_a_retried_turn_says_so(store, monkeypatch, attempt):
    """The overflow ladder re-runs a turn by re-executing its tools, so one
    analyst question can persist the same scan more than once. The runs are not
    suppressed — they really happened, and hiding a re-execution is what the
    marker rows exist to prevent — but a superseded re-run must be
    distinguishable from an analyst scanning twice."""
    import vestigo.api.routers.events as events_router

    await store.init_schema()
    await store.create_case("c1", "Case 1")

    async def fake_run(case_id, timeline_id, source_ids, **kwargs):
        class R:
            status = "ok"

        return R(), {}

    monkeypatch.setattr(events_router, "_run_stat_detector", fake_run)
    monkeypatch.setattr(
        events_router,
        "_serialize_stat_result",
        lambda result: {"status": result.status, "results": []},
    )

    scope = _scope("c1", "t1", ["s1"])
    scope.attempt = attempt
    result = await _call(
        build_tool_server(scope), "run_anomaly_detector", {"detector": "frequency"}
    )

    run = await store.get_detector_run("c1", result["run_id"])
    assert run is not None
    if attempt:
        assert run.params["agent_retry_attempt"] == attempt
    else:
        # Every non-agent run keeps its existing params shape.
        assert "agent_retry_attempt" not in run.params


def _fat_event() -> dict[str, Any]:
    return {
        "event_id": "e1",
        "timestamp": "2026-07-20T10:00:00Z",
        "source_id": "s1",
        "artifact": "auth",
        "message": "login attempt [svc-a/rock] succeeded",
        "attributes": {"user": "svc-a", "ip": "10.0.0.9"},
    }


def _one_event_service(monkeypatch) -> None:
    """Point every query tool at a single fat event."""
    import vestigo.api.routers.events as events_router

    class _Page:
        total = 1
        events = [_fat_event()]

    class _Service:
        def query(self, query):
            return _Page()

    monkeypatch.setattr(events_router, "_get_query_service", lambda: _Service())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "has_attributes", "has_message"),
    [
        (Fidelity.FULL, True, True),
        (Fidelity.MESSAGE, False, True),
        (Fidelity.MINIMAL, False, False),
    ],
)
async def test_search_events_honours_the_deployment_tier(
    store, monkeypatch, tier, has_attributes, has_message
):
    """A broad search is as capable of overflowing a small window as a
    detector sweep, so it spends the same tier."""
    _one_event_service(monkeypatch)

    server = build_tool_server(_scope("c1", "t1", ["s1"], fidelity=tier))
    result = await _call(server, "search_events", {})
    row = _rows(result["events"])[0]

    assert result["fidelity"] == tier.value
    assert ("attributes" in row) is has_attributes
    assert ("message" in row) is has_message
    # The handles back to the full record survive every reduction.
    assert row["event_id"] == "e1" and row["source_id"] == "s1"
    if tier is Fidelity.FULL:
        assert "note" not in result
    else:
        assert "get_event" in result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["semantic_search", "similar_events"])
async def test_similarity_tools_honour_the_deployment_tier(store, monkeypatch, tool):
    import vestigo.api.routers.events as events_router

    class _Hit:
        event_id = "e1"
        score = 0.93
        event = _fat_event()

    class _Result:
        status = "ok"
        results = [_Hit()]

    class _Similarity:
        def find_similar_by_text(self, *a, **kw):
            return _Result()

        def find_similar(self, *a, **kw):
            return _Result()

    monkeypatch.setattr(events_router, "_get_similarity_service", lambda: _Similarity())
    monkeypatch.setattr("vestigo.agent.tools.embeddings_available", lambda: True)

    server = build_tool_server(_scope("c1", "t1", ["s1"], fidelity=Fidelity.MINIMAL))
    args = {"q": "brute force"} if tool == "semantic_search" else {"event_id": "e1"}
    result = await _call(server, tool, args)
    hit = result["results"][0]

    assert result["fidelity"] == "minimal"
    assert hit["event"] == {
        "event_id": "e1",
        "timestamp": "2026-07-20T10:00:00Z",
        "source_id": "s1",
        "artifact": "auth",
    }


@pytest.mark.asyncio
async def test_the_escape_hatches_are_exempt_from_the_tier(store, monkeypatch):
    """`get_event` and `get_event_annotations` are what every reduced payload
    tells the model to call. Tiering them would leave it looping on a
    reduction it has no way to undo, so they answer in full at every tier."""
    _one_event_service(monkeypatch)
    await store.init_schema()
    await store.create_annotation("c1", "s1", "e1", "a-fid", "comment", "looked at")

    server = build_tool_server(_scope("c1", "t1", ["s1"], fidelity=Fidelity.MINIMAL))

    event = await _call(server, "get_event", {"event_id": "e1"})
    assert event["attributes"] == {"user": "svc-a", "ip": "10.0.0.9"}
    assert event["message"] == "login attempt [svc-a/rock] succeeded"
    assert "fidelity" not in event

    annotations = await _call(
        server, "get_event_annotations", {"source_id": "s1", "event_id": "e1"}
    )
    assert _rows(annotations["annotations"])[0]["content"] == "looked at"


@pytest.mark.asyncio
async def test_run_anomaly_detector_rejects_out_of_bounds(store):
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError):
        await _call(
            server, "run_anomaly_detector", {"detector": "sequence_novelty", "ngram_size": 9}
        )


# ---------------------------------------------------------------------------
# A9 viz read tools: field_timeseries, time_punchcard, field_pivot,
# field_scatter, compare — pass-through + cap clamping. Monkeypatches
# _get_query_service (same seam build_tool_server resolves at build time)
# with a fake recording service, so these run without live ClickHouse — the
# existing detector/query tools in this file take the same approach.
# ---------------------------------------------------------------------------


class _FakeVizService:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def field_value_timeseries(self, query, field, buckets, series_limit):
        self.calls.append(("field_value_timeseries", (field, buckets, series_limit), {}))
        return {
            "field": field,
            "series": [],
            "interval_seconds": 3600,
            "min": None,
            "max": None,
            "distinct": 0,
            "other_count": 0,
            "series_truncated": False,
        }

    def time_punchcard(self, query):
        self.calls.append(("time_punchcard", (), {}))
        return {"kind": "punchcard", "cells": [], "total": 0, "max_count": 0}

    def field_pivot(self, query, field_x, field_y, limit_x, limit_y):
        self.calls.append(("field_pivot", (field_x, field_y, limit_x, limit_y), {}))

        # x_values/y_values are part of the real response — the axes the
        # matrix actually resolved to, which a bounded time axis fills from
        # its domain rather than from a top-N scan.
        # `*_bounded` is derived the same way the real service derives it, so
        # the fake can't drift into claiming a measured distinct count for an
        # axis that was charted from a static domain.
        def _bounded(token: str) -> bool:
            spec = resolve_time_field(token)
            return spec is not None and spec.domain is not None

        return {
            "kind": "pivot",
            "cells": [],
            "total": 0,
            "x_values": [],
            "y_values": [],
            "x_distinct": 0,
            "y_distinct": 0,
            "x_bounded": _bounded(field_x),
            "y_bounded": _bounded(field_y),
        }

    def field_correlation(self, query, fields):
        self.calls.append(("field_correlation", (tuple(fields),), {}))
        pairs = [
            {
                "x": a,
                "y": b,
                "n": 100,
                "pearson": 0.5,
                "p_pearson": 0.001,
                "spearman": 0.4,
                "p_spearman": 0.01,
            }
            for i, a in enumerate(fields)
            for b in fields[i + 1 :]
        ]
        return {
            "kind": "corr",
            "fields": list(fields),
            "total": 100,
            "numeric_counts": dict.fromkeys(fields, 100),
            "pairs": pairs,
            "dropped_fields": self.corr_dropped,
        }

    def field_scatter(self, query, field_x, field_y, limit):
        self.calls.append(("field_scatter", (field_x, field_y, limit), {}))
        return {"kind": "scatter", "points": [], "total": 0, "sampled": 0}

    def compare_time_histogram(self, primary, comparison, buckets):
        self.calls.append(("compare_time_histogram", (buckets,), {}))
        return {"kind": "time", "buckets": [], "primary_total": 0, "comparison_total": 0}

    def compare_field_terms(self, primary, comparison, field, limit):
        self.calls.append(("compare_field_terms", (field, limit), {}))
        return {"kind": "terms", "field": field, "primary_total": 0, "comparison_total": 0}

    def compare_field_numeric(self, primary, comparison, field, bins):
        self.calls.append(("compare_field_numeric", (field, bins), {}))
        return {"kind": "numeric", "field": field, "primary_total": 0, "comparison_total": 0}


def _patch_viz_service(monkeypatch) -> _FakeVizService:
    import vestigo.api.routers.events as events_router

    fake = _FakeVizService()
    monkeypatch.setattr(events_router, "_get_query_service", lambda: fake)
    return fake


async def test_field_timeseries_clamps_buckets_and_series_limit(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _call(
        server, "field_timeseries", {"field": "attr:status", "buckets": 500, "series_limit": 50}
    )
    name, args, _ = fake.calls[0]
    assert name == "field_value_timeseries"
    assert args == ("attr:status", 60, 8)  # clamped to VIZ_TIMESERIES_MAX_BUCKETS/SERIES


async def test_time_punchcard_passes_through(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "time_punchcard")
    assert result["kind"] == "punchcard"
    assert fake.calls[0][0] == "time_punchcard"


async def test_field_pivot_clamps_limits(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _call(
        server,
        "field_pivot",
        {"field_x": "attr:user", "field_y": "attr:host", "limit_x": 100, "limit_y": 100},
    )
    name, args, _ = fake.calls[0]
    assert name == "field_pivot"
    assert args == ("attr:user", "attr:host", 12, 12)  # clamped to VIZ_PIVOT_MAX_LIMIT


async def test_field_scatter_clamps_limit(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _call(
        server,
        "field_scatter",
        {"field_x": "attr:bytes", "field_y": "attr:latency", "limit": 20000},
    )
    name, args, _ = fake.calls[0]
    assert name == "field_scatter"
    assert args == ("attr:bytes", "attr:latency", 1000)  # clamped to VIZ_SCATTER_MAX_POINTS


async def test_compare_time_dispatches_and_clamps_buckets(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "compare", {"kind": "time", "buckets": 500})
    assert result["kind"] == "time"
    name, args, _ = fake.calls[0]
    assert name == "compare_time_histogram"
    assert args == (60,)  # clamped to VIZ_MAX_BUCKETS


async def test_compare_terms_requires_field(store):
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError):
        await _call(server, "compare", {"kind": "terms"})


async def test_compare_terms_dispatches_and_clamps_limit(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "compare", {"kind": "terms", "field": "attr:status", "limit": 999})
    assert result["kind"] == "terms"
    name, args, _ = fake.calls[0]
    assert name == "compare_field_terms"
    assert args == ("attr:status", 30)  # clamped to VIZ_MAX_TERMS


async def test_compare_numeric_dispatches_and_clamps_bins(store, monkeypatch):
    fake = _patch_viz_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "compare", {"kind": "numeric", "field": "attr:bytes", "limit": 999}
    )
    assert result["kind"] == "numeric"
    name, args, _ = fake.calls[0]
    assert name == "compare_field_numeric"
    assert args == ("attr:bytes", 30)  # clamped to VIZ_MAX_BINS


@pytest.mark.parametrize(
    ("args", "key", "row"),
    [
        ({"kind": "time"}, "buckets", {"start": "T0", "primary": 1, "comparison": 2}),
        (
            {"kind": "terms", "field": "attr:status"},
            "values",
            {"value": "404", "primary": 1, "comparison": 2},
        ),
        (
            {"kind": "numeric", "field": "attr:bytes"},
            "bins",
            {"x0": 0.0, "x1": 1.0, "primary": 1, "comparison": 2},
        ),
    ],
)
async def test_compare_rows_are_columnar(store, monkeypatch, args, key, row):
    """All three compare kinds are dict-per-row and among the heaviest results
    the agent can request — each one's rows go to the model header-once."""
    fake = _patch_viz_service(monkeypatch)
    for name in ("compare_time_histogram", "compare_field_terms", "compare_field_numeric"):
        setattr(
            fake,
            name,
            lambda *a, _k=key, **kw: {"kind": args["kind"], _k: [row, row]},
        )
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "compare", args)
    assert _rows(result[key]) == [row, row]


async def test_compare_rejects_unknown_kind(store):
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError):
        await _call(server, "compare", {"kind": "bogus"})


# ---------------------------------------------------------------------------
# A9 propose_chart: validate-by-execute, summary stats echoed, no proposal
# row (unlike propose_annotation — the analyst's Save click is the only
# write, mirroring propose_finding's no-write contract).
# ---------------------------------------------------------------------------


class _FakeChartService(_FakeVizService):
    #: Field vocabulary `propose_chart`/`describe_field` validate against.
    FIELDS = [
        "bytes",
        "latency",
        "status",
        "user",
        "country",
        # Extra numeric-ish tokens so the >8-field correlation rejection can
        # be exercised with a vocabulary the field check accepts.
        "duration",
        "retries",
        "size",
        "score",
        "rank",
    ]

    #: Set to 0 to exercise the "field is not numeric" rejection.
    numeric_count = 100
    #: Set to 0 to exercise the "no numeric pairs" scatter rejection.
    scatter_sampled = 25
    #: Non-numeric fields the correlation scan reports back.
    corr_dropped: list[dict[str, str]] = []
    #: Distinct grouping values a grouped box/violin scan reports back —
    #: raise it past VIZ_GROUP_CARDINALITY_CAUTION to exercise the caution.
    grouped_distinct_groups = 5

    def list_fields(self, case_id, source_ids, field_mappings=None):
        self.calls.append(("list_fields", (), {}))
        return {"top_level": ["artifact", "message"], "attributes": list(self.FIELDS), "mapped": []}

    #: Overridable so pie-readability cases can shape the value distribution.
    terms_values: list[dict[str, Any]] | None = None
    terms_other = 5

    def field_terms(self, query, field, limit, **kw):
        self.calls.append(("field_terms", (field, limit), kw))
        values = self.terms_values or [
            {"value": "a", "count": 60},
            {"value": "b", "count": 40},
        ]
        return {
            "field": field,
            "total": 100,
            "distinct": 4,
            "other_count": self.terms_other,
            "values": values,
        }

    def field_numeric_stats(self, query, field, bins=None):
        self.calls.append(("field_numeric_stats", (field, bins), {}))
        resolved = bins if bins is not None else 30
        return {
            "field": field,
            "count": self.numeric_count,
            "min": 0,
            "max": 99,
            "mean": 50,
            "stddev": 10,
            "skewness": 0.1,
            "quantiles": {"0.25": 25, "0.5": 50, "0.75": 75},
            "bins": [{"x0": i, "x1": i + 1, "count": 1} for i in range(resolved)],
            "bin_rule": "manual" if bins is not None else "fd",
            "bin_count_clamped": False,
            "bin_width": 1.0,
        }

    def field_numeric_grouped(
        self, query, field, group_field, groups=8, bins=30, points=False, points_limit=1000
    ):
        self.calls.append(("field_numeric_grouped", (field, group_field, groups, bins, points), {}))
        return {
            "kind": "numeric_grouped",
            "field": field,
            "group_field": group_field,
            "total": self.numeric_count,
            "min": 0,
            "max": 99,
            "distinct_groups": self.grouped_distinct_groups,
            "omitted_groups": 3,
            "omitted_count": 7,
            "groups": [
                {
                    "value": "alice",
                    "count": 40,
                    "quantiles": {"0.5": 20},
                    "bins": [],
                },
                {"value": "bob", "count": 30, "quantiles": {"0.5": 50}, "bins": []},
            ],
            "points": None,
        }

    def mark_instants(self, query, limit):
        self.calls.append(("mark_instants", (limit,), {"q": query.q}))
        return {
            "instants": [{"event_id": "e1", "source_id": "s1", "at": "2026-07-20T01:00:00+00:00"}],
            "dated": 1,
            "undated": 0,
            "overflow": False,
        }

    def cumulative(self, query, *, field=None, quantity="events", buckets=60):
        self.calls.append(("cumulative", (field, quantity, buckets), {}))
        return {
            "kind": "cumulative",
            "quantity": quantity,
            "field": field,
            "interval_seconds": 3600,
            "min": "2026-07-20T00:00:00+00:00",
            "max": "2026-07-20T03:00:00+00:00",
            "buckets": [{"start": "2026-07-20T00:00:00+00:00", "delta": 3, "value": 3}],
            "total": 3,
            "events": 4,
            "unparsed": 1,
        }

    def field_lanes(self, primary, field, *, pairing, start=None, end=None, limit_y, rows_cap):
        self.calls.append(
            (
                "field_lanes",
                (field, pairing, limit_y, rows_cap),
                {"layers": (start is not None, end is not None)},
            )
        )
        return {
            "kind": "lanes",
            "field": field,
            "pairing": pairing,
            "lanes": [
                {
                    "key": "h2",
                    "count": 4,
                    "intervals": [
                        {
                            "start": "2026-07-20T09:00:00+00:00",
                            "end": None,
                            "start_event_id": "e2",
                            "end_event_id": None,
                        },
                        {
                            "start": "2026-07-20T10:00:00+00:00",
                            "end": "2026-07-20T11:00:00+00:00",
                            "start_event_id": "e4",
                            "end_event_id": "e6",
                        },
                    ],
                },
                {
                    "key": "h1",
                    "count": 3,
                    "intervals": [
                        {
                            "start": "2026-07-20T09:00:00+00:00",
                            "end": "2026-07-20T10:00:00+00:00",
                            "start_event_id": "e1",
                            "end_event_id": "e3",
                        },
                    ],
                },
            ],
            "lane_cap": limit_y,
            "lanes_total": 3,
            "lane_cap_hit": True,
            "other_lanes": 1,
            "starts": 4,
            "ends": 3,
            "unpaired_starts": 1,
            "orphan_ends": 1,
            "rows_cap": rows_cap,
            "rows_truncated": False,
            "rows_paired": 7,
            "undated": 1,
            "slice_start": "2026-07-20T08:00:00+00:00",
            "slice_end": "2026-07-20T14:00:00+00:00",
        }

    def field_change(self, primary, comparison, field, limit, *, union_cap, derive=None):
        self.calls.append(("field_change", (field, limit, union_cap), {"derive": derive}))
        return {
            "kind": "change",
            "field": field,
            "derive": None,
            "top_n": limit,
            "primary_total": 20,
            "comparison_total": 10,
            "rows": [
                {
                    "value": "alice",
                    "primary": 4,
                    "comparison": 6,
                    "primary_share": 0.2,
                    "comparison_share": 0.6,
                    "delta_share": -0.4,
                    "status": "fell",
                },
                {
                    "value": "bob",
                    "primary": 12,
                    "comparison": 3,
                    "primary_share": 0.6,
                    "comparison_share": 0.3,
                    "delta_share": 0.3,
                    "status": "rose",
                },
                {
                    "value": "dave",
                    "primary": 3,
                    "comparison": 0,
                    "primary_share": 0.15,
                    "comparison_share": 0.0,
                    "delta_share": 0.15,
                    "status": "new",
                },
            ],
            "union_size": 4,
            "rows_shown": 3,
            "union_cap": union_cap,
            "truncated": True,
            "omitted": 1,
        }

    def calendar(self, query, *, field=None, max_weeks=53):
        self.calls.append(("calendar", (field, max_weeks), {}))
        return {
            "kind": "calendar",
            "field": field,
            "timezone": "UTC",
            "start": "2026-07-20",
            "end": "2026-07-22",
            "days": [{"date": "2026-07-20", "count": 2}],
            "total": 2,
            "max_count": 2,
            "weeks": 1,
            "weeks_total": 1,
            "truncated": False,
            "dropped": 0,
        }

    def field_table(self, query, field, limit, **kw):
        self.calls.append(("field_table", (field, limit), kw))
        return {
            "kind": "table",
            "field": field,
            "second_field": kw.get("second_field"),
            "total": 100,
            "distinct": 4,
            "rows": [
                {
                    "value": "a",
                    "count": 60,
                    "share": 0.6,
                    "first_seen": None,
                    "last_seen": None,
                    "distinct_second": None,
                },
                {
                    "value": "b",
                    "count": 30,
                    "share": 0.3,
                    "first_seen": None,
                    "last_seen": None,
                    "distinct_second": None,
                },
            ],
            "remainder": {"count": 10, "share": 0.1, "distinct_values": 2},
            "sort": {"by": kw.get("sort_by", "count"), "dir": kw.get("sort_dir", "desc")},
            "derive": None,
        }

    def field_correlation(self, query, fields):
        self.calls.append(("field_correlation", (tuple(fields),), {}))
        pairs = [
            {
                "x": a,
                "y": b,
                "n": 100,
                "pearson": 0.5,
                "p_pearson": 0.001,
                "spearman": 0.4,
                "p_spearman": 0.01,
            }
            for i, a in enumerate(fields)
            for b in fields[i + 1 :]
        ]
        return {
            "kind": "corr",
            "fields": list(fields),
            "total": 100,
            "numeric_counts": dict.fromkeys(fields, 100),
            "pairs": pairs,
            "dropped_fields": self.corr_dropped,
        }

    def field_scatter(self, query, field_x, field_y, limit):
        self.calls.append(("field_scatter", (field_x, field_y, limit), {}))
        return {
            "kind": "scatter",
            "points": [],
            "total": 0,
            "sampled": self.scatter_sampled,
            "stats": {
                "n": 100,
                "basis": "full",
                "pearson": {"r": 0.5, "p": 0.001},
                "spearman": {"rho": 0.4, "p": 0.01},
                "kendall": None,
                "regression": {"slope": 1.0, "intercept": 2.0, "r_squared": 0.25},
                "shapiro": {"x": None, "y": None, "basis": "sample", "n": 0},
                "recommendation": "spearman",
                "recommendation_basis": "default",
            },
        }

    def compare_field_terms(self, primary, comparison, field, limit):
        self.calls.append(("compare_field_terms", (field, limit), {}))
        return {
            "kind": "terms",
            "field": field,
            "primary_total": 0,
            "comparison_total": 0,
            "distinct": 3,
        }

    def histogram(self, query, buckets):
        self.calls.append(("histogram", (buckets,), {}))
        return {"buckets": [], "interval_seconds": 3600}


def _patch_chart_service(monkeypatch) -> _FakeChartService:
    import vestigo.api.routers.events as events_router

    fake = _FakeChartService()
    monkeypatch.setattr(events_router, "_get_query_service", lambda: fake)
    return fake


def _chart(spec: dict) -> dict:
    return {"title": "t", "description": "", "spec": spec}


def _called(fake: _FakeChartService, name: str) -> tuple:
    """Args of the first call to *name* — skips the field-vocabulary lookup."""
    for called_name, args, _ in fake.calls:
        if called_name == name:
            return args
    raise AssertionError(f"{name} was not called; got {[c[0] for c in fake.calls]}")


# ── every chart type is reachable ───────────────────────────────────────────
# The bug this contract replaced: `kind` addressed 7 marks, so a pie request
# silently rendered a bar. `pie`/`heatmap`/`box`/`violin`/`ecdf`/`sankey` here
# are the ones that were unreachable; `waffle` postdates the enum entirely.

_CHART_TYPE_CASES = [
    ("time", {}, "histogram"),
    ("bar", {"field": "attr:status"}, "field_terms"),
    ("pie", {"field": "attr:status"}, "field_terms"),
    ("waffle", {"field": "attr:status"}, "field_terms"),
    ("heatmap", {"field": "attr:status"}, "field_value_timeseries"),
    ("line", {"field": "attr:bytes", "scale": "ratio"}, "field_value_timeseries"),
    ("histogram", {"field": "attr:bytes"}, "field_numeric_stats"),
    ("box", {"field": "attr:bytes"}, "field_numeric_stats"),
    ("violin", {"field": "attr:bytes"}, "field_numeric_stats"),
    ("ecdf", {"field": "attr:bytes"}, "field_numeric_stats"),
    ("punchcard", {}, "time_punchcard"),
    ("pivot", {"field": "attr:user", "field_y": "attr:status"}, "field_pivot"),
    ("sankey", {"field": "attr:user", "field_y": "attr:status"}, "field_pivot"),
    ("scatter", {"field": "attr:bytes", "field_y": "attr:latency"}, "field_scatter"),
    ("corr", {"fields": ["attr:bytes", "attr:latency"]}, "field_correlation"),
    ("table", {"field": "attr:user"}, "field_table"),
    ("cumulative", {}, "cumulative"),
    ("calendar", {}, "calendar"),
    ("change", {"field": "attr:status", "compare": {"mode": "baseline"}}, "field_change"),
    ("lanes", {"field": "attr:status"}, "field_lanes"),
]


@pytest.mark.parametrize(("chart_type", "extra", "expected_call"), _CHART_TYPE_CASES)
async def test_propose_chart_reaches_every_chart_type(
    store, monkeypatch, chart_type, extra, expected_call
):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "propose_chart", _chart({"chart_type": chart_type, **extra}))
    assert result["ok"] is True
    assert result["resolved"]["chart_type"] == chart_type
    _called(fake, expected_call)


async def test_propose_chart_covers_every_chart_type():
    """Guard against the parametrization drifting behind the table."""
    from vestigo.agent.chart_meta import CHART_TYPES

    assert {c for c, _, _ in _CHART_TYPE_CASES} == set(CHART_TYPES)


# ── the resolved echo ───────────────────────────────────────────────────────


async def test_propose_chart_echoes_what_will_be_drawn(store, monkeypatch):
    """The model asked for a pie and was told `ok: true` while a bar rendered.
    `resolved` is the channel that makes that impossible to miss."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "propose_chart", _chart({"chart_type": "pie", "field": "country"}))
    assert result["resolved"] == {
        "chart_type": "pie",
        "scale": "nominal",  # the chart type's default, since none was given
        "metric": "count",
        "compare_mode": "off",
        "data_kind": "terms",
        "field": "country",
        "field_y": None,
        "fields": None,
        "options": {"top_n": 30},
        "derive": None,
        "inputs": None,
        "marks": None,
    }
    assert result["warnings"] == []


async def test_clamped_option_is_reported_as_a_warning(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "bar", "field": "country", "options": {"top_n": 500}}),
    )
    assert result["resolved"]["options"]["top_n"] == 30
    assert any("clamped" in w and "not capped" in w for w in result["warnings"])
    assert _called(fake, "field_terms") == ("country", 30)


async def test_option_the_chart_ignores_warns_but_still_succeeds(store, monkeypatch):
    """A stray cosmetic option must not cost the analyst a chart — but silence
    would leave the model believing it had set something."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "bar", "field": "country", "options": {"bins": 12}}),
    )
    assert result["ok"] is True
    assert any("bins" in w and "ignored" in w for w in result["warnings"])


async def test_presentation_options_reach_the_resolved_echo(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "bar",
                "field": "country",
                "options": {"orientation": "vertical", "sort": "value", "log_scale": True},
            }
        ),
    )
    assert result["resolved"]["options"]["orientation"] == "vertical"
    assert result["resolved"]["options"]["sort"] == "value"
    assert result["resolved"]["options"]["log_scale"] is True


# ── legality rules; each error must name the legal alternative ──────────────


async def _reject(server, spec: dict) -> str:
    with pytest.raises(ToolError) as excinfo:
        await _call(server, "propose_chart", _chart(spec))
    return str(excinfo.value)


async def test_scale_illegal_for_chart_type_lists_the_alternatives(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "pie", "field": "country", "scale": "ratio"})
    assert '"nominal"' in message
    # ...and names what *is* legal at that scale, so the model can retry.
    assert "histogram" in message


async def test_chart_tool_reports_a_busy_lane_as_a_tool_error(store, monkeypatch):
    """A full foreground lane (#300) is an answer the model can relay, not a 500."""
    from vestigo.db._scan import ScanBusy

    fake = _patch_chart_service(monkeypatch)

    def busy(query, field, limit):
        raise ScanBusy(ahead=2, wait=30.0)

    monkeypatch.setattr(fake, "field_terms", busy)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "bar", "field": "country"})
    assert "busy" in message
    assert "2 waiting ahead" in message


async def test_aggregation_tools_report_a_busy_lane_as_a_tool_error(store, monkeypatch):
    """Not only `propose_chart`: every tool that calls a gated aggregation.

    The bounded foreground wait raises `ScanBusy`, a bare `RuntimeError`. Out
    of a tool that is an unhandled crash — a 500 on the MCP surface — where
    the model could have relayed "busy, retry" instead (#305).
    """
    from vestigo.db._scan import ScanBusy

    fake = _patch_chart_service(monkeypatch)

    def busy(*args, **kwargs):
        raise ScanBusy(ahead=2, wait=30.0)

    monkeypatch.setattr(fake, "field_terms", busy)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError) as excinfo:
        await _call(server, "field_terms", {"field": "country"})
    message = str(excinfo.value)
    assert "2 waiting ahead" in message
    # The guidance `run_gated_scan` appends — its absence means the bare
    # RuntimeError escaped and only happened to carry a readable message.
    assert "try again" in message


async def test_missing_field_names_the_field_free_charts(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "bar"})
    assert "requires field" in message
    assert "punchcard" in message


async def test_missing_field_y_says_why_it_is_needed(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "sankey", "field": "user"})
    assert "field_y" in message
    # ...and names the one-field alternative, in case that was the intent.
    assert '"heatmap"' in message


async def test_heatmap_with_field_y_is_pointed_at_pivot(store, monkeypatch):
    """The naming trap that cost a real turn (2026-07-20): our `heatmap` is one
    field x time, and the field x field grid an analyst also calls a heatmap is
    `pivot`. Enumerating the two-field types was not enough — the model spent
    both retries on the same rejection, so the message names the fix."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server, {"chart_type": "heatmap", "field": "country", "field_y": "time:hour_of_day"}
    )
    assert "takes no field_y" in message
    assert 'chart_type="pivot"' in message


async def test_field_y_on_a_one_field_chart_is_rejected_not_dropped(store, monkeypatch):
    """Silently ignoring it would teach the model nothing."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "bar", "field": "user", "field_y": "status"})
    assert "takes no field_y" in message
    assert "pivot, sankey, scatter" in message


async def test_compare_on_an_unsupported_chart_lists_the_capable_ones(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server, {"chart_type": "pie", "field": "country", "compare": {"mode": "baseline"}}
    )
    assert "time, bar, histogram" in message


async def test_custom_compare_without_filters_points_at_baseline(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "time", "compare": {"mode": "custom"}})
    assert "baseline" in message


async def test_time_bucketed_metric_outside_the_time_chart_is_rejected(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "bar", "field": "country", "metric": "rate"})
    assert 'chart_type="time"' in message
    # The formula is quoted so the model learns what the metric means.
    assert "bucket_interval_seconds" in message


async def test_ratio_metric_without_a_comparison_layer_is_rejected(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "time", "metric": "ratio"})
    assert "comparison layer" in message


async def test_rate_metric_on_the_time_chart_is_accepted(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "propose_chart", _chart({"chart_type": "time", "metric": "rate"}))
    assert result["resolved"]["metric"] == "rate"


async def test_unknown_field_is_rejected_with_near_misses(store, monkeypatch):
    """An unknown attribute key resolves to an empty Map lookup, so without
    this check a typo returns a cheerful `ok: true` over zero rows."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "bar", "field": "attr:countyr"})
    assert "not a field in this timeline" in message
    assert "country" in message


async def test_numeric_chart_over_a_categorical_field_is_rejected(store, monkeypatch):
    """`count == 0` is the documented categorical signal. It used to return
    `ok: true` — a validated-looking success for an unrenderable chart."""
    fake = _patch_chart_service(monkeypatch)
    fake.numeric_count = 0
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "histogram", "field": "user"})
    assert "no numeric values" in message
    assert '"bar"' in message


async def test_scatter_with_no_numeric_pairs_is_rejected(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    fake.scatter_sampled = 0
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "scatter", "field": "user", "field_y": "status"})
    assert "describe_field" in message


# ── option routing, incl. the bugs the old overloaded `limit` caused ────────


async def test_bins_reach_the_numeric_scan(store, monkeypatch):
    """`propose_chart` used to drop the bin count entirely."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "histogram", "field": "bytes", "options": {"bins": 12}}),
    )
    assert _called(fake, "field_numeric_stats") == ("bytes", 12)


async def test_top_n_and_buckets_no_longer_collide_on_timeseries(store, monkeypatch):
    """Both used to land on `topN`, so whichever was written last won."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "line",
                "field": "bytes",
                "scale": "ratio",
                "options": {"top_n": 5, "buckets": 20},
            }
        ),
    )
    assert _called(fake, "field_value_timeseries") == ("bytes", 20, 5)


async def test_compare_baseline_is_reachable(store, monkeypatch):
    """Unreachable under the old contract, though the viz endpoint supported it."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "time", "compare": {"mode": "baseline"}})
    )
    assert result["resolved"]["compare_mode"] == "baseline"
    _called(fake, "compare_time_histogram")


async def test_compare_on_a_bar_chart_uses_the_compare_terms_scan(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "bar", "field": "country", "compare": {"mode": "baseline"}}),
    )
    assert result["summary"]["comparison_total"] == 0
    _called(fake, "compare_field_terms")


async def test_unknown_option_key_is_rejected(store, monkeypatch):
    """`ChartOptionsSpec` is a small closed set, so a typo should error rather
    than vanish — the warning path only covers *known* but inert keys."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _reject(server, {"chart_type": "bar", "field": "country", "options": {"topn": 5}})


# ── virtual time fields as chart axes ───────────────────────────────────────


async def test_country_by_hour_of_day_pivot(store, monkeypatch):
    """The chart the temporal-heatmap work exists for."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "pivot", "field": "country", "field_y": "time:hour_of_day"}),
    )
    assert result["ok"] is True
    assert _called(fake, "field_pivot")[:2] == ("country", "time:hour_of_day")


async def test_time_field_passes_field_validation(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "bar", "field": "time:day_of_week"})
    )
    assert result["resolved"]["field"] == "time:day_of_week"


async def test_time_field_validation_matches_the_query_layer_spelling(store, monkeypatch):
    """`resolve_time_field` normalises case/whitespace, so a token the SQL
    layer resolves must not be rejected one layer above it."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "bar", "field": "Time:Hour_Of_Day"})
    )
    assert result["ok"] is True


async def test_bounded_time_axis_warns_that_its_limit_did_not_apply(store, monkeypatch):
    """A bounded time axis is charted as its whole domain — an hour with no
    events is a finding. Accepting `limit_x` silently would leave the model
    believing it had bounded a matrix it had not."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "pivot",
                "field": "time:hour_of_day",
                "field_y": "country",
                "options": {"limit_x": 3, "limit_y": 5},
            }
        ),
    )
    assert any("limit_x does not apply" in w for w in result["warnings"])
    # ...and it stops claiming a limit that had no effect.
    assert "limit_x" not in result["resolved"]["options"]
    # The unbounded axis keeps its limit.
    assert result["resolved"]["options"]["limit_y"] == 5
    assert not any("limit_y does not apply" in w for w in result["warnings"])


async def test_clamped_buckets_warn_like_every_other_clamped_option(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "time", "options": {"buckets": 100_000}}),
    )
    assert any("buckets" in w and "clamped" in w for w in result["warnings"])
    assert result["resolved"]["options"]["buckets"] < 100_000


async def test_pivot_summary_marks_which_distinct_counts_are_domain_sizes(store, monkeypatch):
    """`x_distinct` carries two units — a measured count the axis may have been
    truncated against, or a bounded domain charted whole. Without the
    `*_bounded` flags the model reads "12 of 12" and "12 of 400" alike."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {"chart_type": "pivot", "field": "time:hour_of_day", "field_y": "country"},
        ),
    )
    assert result["summary"]["x_bounded"] is True
    assert result["summary"]["y_bounded"] is False


# ── virtual time fields are chart/filter-only, never detector fields ────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fields": "time:hour_of_day"},
        {"fields": "artifact,time:day_of_week"},
        {"series_field": "time:month"},
    ],
)
async def test_run_anomaly_detector_rejects_virtual_time_fields(store, kwargs):
    """`anomaly_stats._col_expr` has no `time:` branch, so such a token falls
    through to `attributes['time:hour_of_day']` — empty for every row. The
    detector would finish cleanly with zero findings, which reads as "nothing
    anomalous" rather than "that field was never scanned"."""
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError) as excinfo:
        await _call(server, "run_anomaly_detector", {"detector": "value_novelty", **kwargs})
    assert "virtual time field" in str(excinfo.value)


# ── back-compat: persisted conversations still resolve ──────────────────────
# The retired `kind` enum is absent from the model-facing schema but still
# understood, for a conversation in flight across a server restart.


async def test_propose_chart_legacy_kind_terms(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"kind": "terms", "field": "artifact", "limit": 500})
    )
    assert result["ok"] is True
    assert result["resolved"]["chart_type"] == "bar"
    assert result["summary"]["total"] == 100
    assert len(result["summary"]["top_values"]) == 2
    assert _called(fake, "field_terms") == ("artifact", 30)


async def test_propose_chart_legacy_kind_numeric(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"kind": "numeric", "field": "attr:bytes"})
    )
    assert result["resolved"]["chart_type"] == "histogram"
    assert result["summary"]["mean"] == 50


async def test_propose_chart_legacy_kind_timeseries(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"kind": "timeseries", "field": "attr:status", "buckets": 999}),
    )
    assert result["resolved"]["chart_type"] == "line"
    assert _called(fake, "field_value_timeseries") == ("attr:status", 60, 6)


async def test_propose_chart_legacy_kind_scatter_demuxes_limit(store, monkeypatch):
    """`limit` meant a different option per kind — here, the point cap."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _call(
        server,
        "propose_chart",
        _chart(
            {
                "kind": "scatter",
                "field": "attr:bytes",
                "field_y": "attr:latency",
                "limit": 50000,
            }
        ),
    )
    assert _called(fake, "field_scatter") == ("attr:bytes", "attr:latency", 1000)


async def test_propose_chart_legacy_kind_compare_time_with_filters(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "kind": "compare_time",
                "buckets": 999,
                "comparison_filters": {"source_id": "s2"},
            }
        ),
    )
    assert result["resolved"]["chart_type"] == "time"
    assert result["resolved"]["compare_mode"] == "custom"
    assert _called(fake, "compare_time_histogram") == (60,)


async def test_propose_chart_legacy_compare_without_filters_matches_the_old_card(
    store, monkeypatch
):
    """`specToChartConfig` emitted `{mode: "off"}` when `comparison_filters`
    was absent, so the analyst's card drew a single-layer histogram even though
    the old backend validated it as a comparison. The card is the artifact, so
    the translation follows the card."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "propose_chart", _chart({"kind": "compare_time"}))
    assert result["resolved"]["compare_mode"] == "off"
    _called(fake, "histogram")


async def test_propose_chart_legacy_kind_pivot_requires_field_y(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _reject(server, {"kind": "pivot", "field": "user"})


async def test_propose_chart_unknown_chart_type(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _reject(server, {"chart_type": "bogus"})


async def test_propose_chart_unknown_legacy_kind(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    await _reject(server, {"kind": "bogus"})


# ── describe_field: the agent's equivalent of the page's auto-probe ─────────


async def test_describe_field_suggests_ratio_for_a_numeric_field(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "describe_field", {"field": "bytes"})
    assert result["exists"] is True
    assert result["suggested_scale"] == "ratio"
    assert "histogram" in result["suggested_chart_types"]
    assert result["numeric"]["mean"] == 50


async def test_describe_field_suggests_nominal_when_values_are_not_numeric(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    fake.numeric_count = 0
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "describe_field", {"field": "user"})
    assert result["suggested_scale"] == "nominal"
    assert result["numeric"] is None
    assert "bar" in result["suggested_chart_types"]
    assert any("do not parse as numbers" in n for n in result["notes"])


async def test_describe_field_reports_an_unknown_field_with_suggestions(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "describe_field", {"field": "countyr"})
    assert result["exists"] is False
    assert "country" in result["suggestions"]


async def test_describe_field_answers_time_fields_without_scanning(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "describe_field", {"field": "time:hour_of_day"})
    assert result["virtual"] is True
    assert result["suggested_scale"] == "ordinal"
    assert result["distinct"] == 24
    assert result["top_values"][:3] == ["00", "01", "02"]
    assert not [c for c in fake.calls if c[0] in {"field_terms", "field_numeric_stats"}]


async def test_describe_field_reports_a_count_under_a_count_name(store, monkeypatch):
    """`coverage` means a 0-1 fraction everywhere else in the API.

    Reporting a raw event count under that name left the model unable to
    compare a field described here against the same field in the viz field
    list — so the count is named as one.
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "describe_field", {"field": "user"})
    assert result["non_empty_total"] == 100
    assert "coverage" not in result


async def test_describe_field_claims_no_coverage_for_a_virtual_field(store, monkeypatch):
    """A time part is undefined for an undated event, so full coverage would
    be a claim about data this tool never scanned."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(server, "describe_field", {"field": "time:date"})
    assert "coverage" not in result
    assert "non_empty_total" not in result
    # Unbounded domain — no honest distinct count either.
    assert result["distinct"] is None


MAX_PROPOSAL_EVENTS_TEST = 500  # mirror of tools.MAX_PROPOSAL_EVENTS


def _scope_with_conversation(case_id: str, timeline_id: str, conversation_id: str) -> AgentScope:
    s = _scope(case_id, timeline_id)
    s.conversation_id = conversation_id
    return s


async def test_propose_annotation_records_proposal(store, monkeypatch):
    await store.init_schema()
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    # ClickHouse resolution is monkeypatched: pretend both ids exist in scope.
    from vestigo.agent import tools as tools_mod

    async def fake_resolve(scope, event_ids):
        return {"e1": "s1", "e2": "s1"}, []

    monkeypatch.setattr(tools_mod, "_resolve_event_sources", fake_resolve)
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))
    result = await _call(
        server,
        "propose_annotation",
        {"event_ids": ["e1", "e2"], "tag": "suspicious", "rationale": "clustered"},
    )
    assert result["status"] == "proposed" and result["event_count"] == 2
    # AgentPanel reads exactly `proposal_id` off this result (and `ok` off
    # propose_chart's) to render the proposal cards — the only two tool-result
    # keys the frontend touches, so the columnar encoding (A13) must never
    # reshape these two.
    assert isinstance(result["proposal_id"], str) and result["proposal_id"]
    (p,) = await store.list_agent_proposals(conv.id)
    assert p.tag == "suspicious" and len(p.events) == 2


async def test_propose_annotation_requires_tag_or_comment(store, monkeypatch):
    await store.init_schema()
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    from vestigo.agent import tools as tools_mod

    async def fake_resolve(scope, event_ids):
        return {"e1": "s1", "e2": "s1"}, []

    monkeypatch.setattr(tools_mod, "_resolve_event_sources", fake_resolve)
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))
    result = await _call(
        server, "propose_annotation", {"event_ids": ["e1", "e2"], "rationale": "clustered"}
    )
    assert "error" in result


async def test_propose_annotation_rejects_unknown_ids(store, monkeypatch):
    await store.init_schema()
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    from vestigo.agent import tools as tools_mod

    async def fake_resolve(scope, event_ids):
        return {"e1": "s1"}, ["eX"]

    monkeypatch.setattr(tools_mod, "_resolve_event_sources", fake_resolve)
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))
    result = await _call(
        server,
        "propose_annotation",
        {"event_ids": ["e1", "eX"], "tag": "suspicious", "rationale": "clustered"},
    )
    assert "error" in result
    assert "eX" in result["error"]


async def test_propose_annotation_absent_without_conversation(store):
    await store.init_schema()
    server = build_tool_server(_scope("c1", "t1"))  # no conversation_id
    async with FastMCPClient(server) as client:
        names = [t.name for t in await client.list_tools()]
    assert "propose_annotation" not in names


async def test_list_sigma_runs_not_starved_by_other_timelines(store):
    await store.init_schema()
    # Create t1 run first (oldest)
    await store.create_sigma_run("c1", "t1", params={}, created_by="alice")
    # Then create 55 OTHER timeline runs (newer)
    for _ in range(55):
        await store.create_sigma_run("c1", "OTHER", params={}, created_by="alice")
    server = build_tool_server(_scope("c1", "t1"))
    result = await _call(server, "list_sigma_runs")
    assert result["total"] == 1


# ---------------------------------------------------------------------------
# Tool registry + per-tool disable (scope.disabled_tools)
# ---------------------------------------------------------------------------


async def test_tool_registry_matches_registered_tools(store, monkeypatch):
    """TOOL_REGISTRY is the single source of truth for toggle UIs — it must
    exactly mirror what build_tool_server registers (with a conversation
    scope and embeddings configured, where every tool exists)."""
    import vestigo.agent.tools as tools_module
    from vestigo.agent.tools import TOOL_NAMES

    monkeypatch.setattr(tools_module, "embeddings_available", lambda: True)
    await store.init_schema()
    server = build_tool_server(_scope_with_conversation("c1", "t1", "conv1"))
    async with FastMCPClient(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == TOOL_NAMES


async def test_embeddings_tools_absent_when_embeddings_unconfigured(store, monkeypatch):
    """An unconfigured subsystem is invisible to the model, not an error stub:
    without embeddings the two vector tools are never advertised."""
    import vestigo.agent.tools as tools_module

    monkeypatch.setattr(tools_module, "embeddings_available", lambda: False)
    await store.init_schema()
    server = build_tool_server(_scope_with_conversation("c1", "t1", "conv1"))
    async with FastMCPClient(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert "semantic_search" not in names
    assert "similar_events" not in names
    assert "search_events" in names


def test_tool_tiers_are_valid_and_core_is_workable():
    """The "core" tier is the lean profile offered to small-context models —
    it has to be able to run the investigation cycle on its own, so guard the
    tools that cycle depends on rather than just counting them."""
    from vestigo.agent.tools import TOOL_NAMES, TOOL_REGISTRY

    assert {t.tier for t in TOOL_REGISTRY} <= {"core", "extended"}
    core = {t.name for t in TOOL_REGISTRY if t.tier == "core"}
    # Terrain, aggregation, search, and both proposal paths must survive.
    assert {"list_fields", "field_terms", "search_events", "propose_finding"} <= core
    assert core < set(TOOL_NAMES), "core must be a strict subset, else it saves nothing"


async def test_core_profile_is_a_real_context_saving(store):
    """Disabling the extended tier must actually shrink what is advertised."""
    import json

    from vestigo.agent.tools import TOOL_REGISTRY

    await store.init_schema()

    async def advertised(disabled: frozenset[str]) -> int:
        scope = _scope_with_conversation("c1", "t1", "conv1")
        scope.disabled_tools = disabled
        async with FastMCPClient(build_tool_server(scope)) as client:
            tools = await client.list_tools()
        return sum(len(json.dumps({"n": t.name, "s": t.inputSchema})) for t in tools)

    extended = frozenset(t.name for t in TOOL_REGISTRY if t.tier != "core")
    assert await advertised(extended) < await advertised(frozenset()) / 2


async def test_disabled_tool_removed_from_server(store):
    await store.init_schema()
    scope = _scope("c1", "t1")
    scope.disabled_tools = frozenset({"search_events", "list_baselines"})
    server = build_tool_server(scope)
    async with FastMCPClient(server) as client:
        names = {t.name for t in await client.list_tools()}
        assert "search_events" not in names
        assert "list_baselines" not in names
        assert "list_fields" in names
        with pytest.raises(ToolError):
            await client.call_tool("search_events", {})


async def test_disabling_unregistered_tool_is_harmless(store):
    """Disabling propose_annotation on a conversation-less scope (where it was
    never registered) must not crash the remove pass."""
    await store.init_schema()
    scope = _scope("c1", "t1")
    scope.disabled_tools = frozenset({"propose_annotation"})
    server = build_tool_server(scope)
    async with FastMCPClient(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert "propose_annotation" not in names


def test_schema_chars_for_scope_measures_the_advertised_tool_list():
    """The number budget_for reserves for the tool list — measured from the
    scope's actual advertised set: a conversation adds propose_annotation,
    disabled_tools takes tools back out. For the default shape it stays in the
    tens of kilobytes (the ~38.8k whose omission sank the budget on 2026-07-23).
    """
    full = schema_chars_for_scope(_scope_with_conversation("c1", "t1", "conv1"))
    assert 30_000 < full < 60_000

    # propose_annotation is only registered for a conversation scope.
    assert schema_chars_for_scope(_scope("c1", "t1")) < full

    # Every disabled tool takes its schema with it.
    trimmed = _scope_with_conversation("c1", "t1", "conv1")
    trimmed.disabled_tools = frozenset({"search_events", "list_fields"})
    assert schema_chars_for_scope(trimmed) < full


# ── grouped distributions, pie readability (lecture-driven additions) ────────


async def test_box_with_a_group_field_uses_the_grouped_aggregation(store, monkeypatch):
    """box/violin accept an OPTIONAL categorical field_y — the grouped
    aggregation, not the single-distribution one."""
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "box",
                "field": "attr:bytes",
                "field_y": "attr:user",
                "options": {"groups": 3, "show_points": True},
            }
        ),
    )
    assert result["ok"] is True
    field, group_field, groups, _bins, points = _called(fake, "field_numeric_grouped")
    assert (field, group_field, groups, points) == ("attr:bytes", "attr:user", 3, True)
    # The omission is reported, never silently rolled into an "Other" group.
    assert result["summary"]["omitted_groups"] == 3
    assert result["summary"]["omitted_count"] == 7
    assert [g["value"] for g in result["summary"]["groups"]] == ["alice", "bob"]


async def test_grouped_group_count_is_clamped(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "violin",
                "field": "attr:bytes",
                "field_y": "attr:user",
                "options": {"groups": 50},
            }
        ),
    )
    assert _called(fake, "field_numeric_grouped")[2] == 8
    assert any("groups=50 clamped to 8" in w for w in result["warnings"])


async def test_group_field_is_rejected_for_charts_that_take_no_second_field(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError, match="takes no field_y"):
        await _call(
            server,
            "propose_chart",
            _chart({"chart_type": "ecdf", "field": "attr:bytes", "field_y": "attr:user"}),
        )


async def test_a_cut_series_list_is_disclosed_to_the_model(store, monkeypatch):
    """One series per value, and the rest simply not drawn.

    ``field_value_timeseries`` pays an extra aggregate to report the cut, and
    a model reasoning over twelve drawn series has to know whether that was
    all of them or the visible slice of fifty-three — a derived axis makes
    that routine (53 ISO weeks against a default cap of 12), and ``derive``
    echoes all 53 labels either way (#332).
    """
    fake = _patch_chart_service(monkeypatch)

    def _truncated(query, field, buckets, series_limit):
        return {
            "field": field,
            "series": [{"value": str(i), "buckets": []} for i in range(12)],
            "interval_seconds": 3600,
            "min": None,
            "max": None,
            "distinct": 53,
            "other_count": 4120,
            "series_truncated": True,
        }

    fake.field_value_timeseries = _truncated
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "heatmap", "field": "attr:status"}),
    )
    assert result["summary"]["distinct"] == 53
    assert result["summary"]["other_count"] == 4120
    assert any("top 12 of 53" in w and "4120 events" in w for w in result["warnings"])


async def test_a_second_field_that_repeats_the_first_is_rejected(store, monkeypatch):
    """One field named twice is not a two-field chart.

    Every HTTP endpoint that takes a second field refuses it with a 422, and
    the rail refuses it at the picker — but ``execute_chart_spec`` calls the
    query service directly, so nothing stopped a spec that names one field
    twice. It does not fail: a table's "distinct field_y" column is 1 on every
    row, a pivot is diagonal, a scatter is y=x — each presented as a real
    answer (#332).
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    for chart_type in ("table", "pivot", "violin"):
        with pytest.raises(ToolError, match="field_y must differ from field"):
            await _call(
                server,
                "propose_chart",
                _chart({"chart_type": chart_type, "field": "attr:user", "field_y": "attr:user"}),
            )


async def test_pie_with_too_many_slices_is_warned_not_rejected(store, monkeypatch):
    """Lecture rule: past a handful of slices, angle comparison stops working.
    Advisory — the chart still validates, the model just learns a better mark."""
    fake = _patch_chart_service(monkeypatch)
    fake.terms_values = [{"value": f"v{i}", "count": 100 - i * 7} for i in range(6)]
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "pie", "field": "attr:status"})
    )
    assert result["ok"] is True
    assert any("slices" in w and "waffle" in w for w in result["warnings"])


async def test_pie_with_near_equal_slices_is_warned(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    fake.terms_values = [{"value": "a", "count": 100}, {"value": "b", "count": 96}]
    fake.terms_other = 0
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "pie", "field": "attr:status"})
    )
    assert any("less than 10%" in w for w in result["warnings"])


async def test_waffle_and_bar_never_get_a_readability_warning(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    fake.terms_values = [{"value": f"v{i}", "count": 100 - i} for i in range(8)]
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    for chart_type in ("waffle", "bar"):
        result = await _call(
            server, "propose_chart", _chart({"chart_type": chart_type, "field": "attr:status"})
        )
        assert not [w for w in result["warnings"] if "slices" in w]


async def test_corr_requires_a_field_list(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError, match="needs `fields`"):
        await _call(server, "propose_chart", _chart({"chart_type": "corr", "field": "attr:bytes"}))
    with pytest.raises(ToolError, match="must not repeat"):
        await _call(
            server,
            "propose_chart",
            _chart({"chart_type": "corr", "fields": ["attr:bytes", "attr:bytes"]}),
        )


async def test_fields_list_is_rejected_for_single_field_charts(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError, match="takes no `fields` list"):
        await _call(
            server,
            "propose_chart",
            _chart({"chart_type": "bar", "field": "attr:status", "fields": ["attr:bytes"]}),
        )


async def test_corr_reports_non_numeric_fields_as_a_warning(store, monkeypatch):
    """An empty row/column is a fact about the data, not a reason to refuse."""
    fake = _patch_chart_service(monkeypatch)
    fake.corr_dropped = [{"field": "attr:user", "reason": "non_numeric"}]
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "corr", "fields": ["attr:bytes", "attr:user"]}),
    )
    fake.corr_dropped = []
    assert result["ok"] is True
    assert any("no numeric values for attr:user" in w for w in result["warnings"])
    assert result["summary"]["pairs"][0]["n"] == 100


async def test_field_correlation_tool_refuses_rather_than_truncates(store, monkeypatch):
    """The data tool must not quietly chart a subset of what was asked for.

    Truncating to the first eight fields (or de-duplicating in silence)
    answers a different question and labels it as the answer to this one.
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))

    with pytest.raises(ToolError) as too_many:
        await _call(
            server,
            "field_correlation",
            {"fields": [f"attr:f{i}" for i in range(9)]},
        )
    assert "between 2 and 8 fields" in str(too_many.value)

    with pytest.raises(ToolError) as duplicated:
        await _call(
            server,
            "field_correlation",
            {"fields": ["attr:bytes", "attr:bytes"]},
        )
    assert "repeat" in str(duplicated.value)

    with pytest.raises(ToolError) as too_few:
        await _call(server, "field_correlation", {"fields": ["attr:bytes"]})
    assert "between 2 and 8 fields" in str(too_few.value)


async def test_grouped_chart_warns_about_omission_and_identifier_like_groups(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    fake.grouped_distinct_groups = 400
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "box", "field": "attr:bytes", "field_y": "attr:user"}),
    )
    assert result["ok"] is True
    # Omission is a warning, not just a number buried in the summary.
    assert any("omitted" in w for w in result["warnings"])
    assert any("identifier" in w for w in result["warnings"])


async def test_corr_field_list_refuses_rather_than_truncates(store, monkeypatch):
    """propose_chart rejects >8 correlation fields, matching field_correlation.

    Both agent entry points must apply the identical rule — silently charting
    the first eight answers a question the model never asked.
    """
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    with pytest.raises(ToolError) as too_many:
        await _call(
            server,
            "propose_chart",
            _chart(
                {
                    "chart_type": "corr",
                    "fields": [f"attr:{f}" for f in _FakeChartService.FIELDS],
                }
            ),
        )
    assert "between 2 and 8 fields" in str(too_many.value)
    # Rejected before any scan — the service is never called with a subset.
    assert not any(name == "field_correlation" for name, _, _ in fake.calls)


# ── provider-stringified spec ───────────────────────────────────────────────


async def test_spec_handed_over_as_a_json_string_is_parsed(store, monkeypatch):
    """Some providers emit a nested object argument as a JSON string.

    Rejecting it costs the model a retry it has to guess its way out of, and
    the stringified args are persisted on the tool-call row either way — so
    parse it here and keep the stored row renderable.
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart('{"chart_type": "bar", "field": "attr:status", "scale": "nominal"}'),  # type: ignore[arg-type]
    )
    assert result["ok"] is True
    assert result["resolved"]["chart_type"] == "bar"


async def test_a_stringified_filter_spec_reaches_the_query_end_to_end(store, monkeypatch):
    """`FilterSpec` is a nested argument on 14 tools, not a chart-only shape.

    The same provider that stringifies a chart spec stringifies these, so the
    tolerance belongs to the position (nested argument) — `ObjectArgModel` —
    rather than to any one spec. One tool end to end here, because the point
    of this case is that the coercion survives the whole MCP argument-parsing
    path and not just `model_validate`; that every nested-argument model is
    covered at all is
    `test_every_nested_argument_model_derives_from_object_arg_model`'s job.
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "histogram",
        {"filters": '{"q": "failed login", "artifacts": ["auth"]}'},
    )
    # Reaching the aggregation at all is the assertion: an unparsed string
    # never gets past argument validation.
    assert "error" not in result
    assert "buckets" in result
    spec = FilterSpec.model_validate('{"q": "failed login", "artifacts": ["auth"]}')
    assert spec.q == "failed login"
    assert spec.artifacts == ["auth"]


def test_empty_mode_needs_no_values_and_still_reaches_the_where_clause():
    """ "Events with no user agent" is a valueless question with a real answer.

    ``_build_where`` only visits keys present in the filter map, so a mode
    entry whose key is absent would be dropped and the tool would answer with
    the whole timeline — while naming the key with an empty list trips
    ``_reject_empty_selections``. Neither is a way for the model to ask, so
    the placeholder the predicate ignores is injected for it.
    """
    spec = FilterSpec.model_validate({"filter_modes": {"attr:user_agent": "empty"}})
    assert spec.filters == {"attr:user_agent": [""]}

    exclusion = FilterSpec.model_validate({"exclusion_modes": {"attr:user_agent": "empty"}})
    assert exclusion.exclusions == {"attr:user_agent": [""]}


def test_an_empty_value_list_is_still_rejected_in_every_other_mode():
    with pytest.raises(ValidationError, match="filters nothing"):
        FilterSpec.model_validate(
            {"filters": {"attr:status": []}, "filter_modes": {"attr:status": "wildcard"}}
        )


def test_a_stringified_spec_still_reaches_the_legacy_kind_translation():
    """Both before-validators run, in whichever order pydantic picks."""
    spec = ChartSpec.model_validate('{"kind": "terms", "field": "attr:status", "limit": 5}')
    assert spec.chart_type == "bar"
    assert spec.options.top_n == 5


def test_unparseable_object_arg_falls_through_to_the_normal_error():
    with pytest.raises(ValidationError):
        ChartSpec.model_validate("not json at all")


def test_a_stringified_value_inside_a_spec_is_parsed_too():
    """A provider that stringifies one level tends to stringify the next.

    ``FilterSpec.filters`` is a plain ``dict`` field, not a nested model, so
    nothing about it being a spec would have covered it — `ObjectArgModel`
    coerces it because its *annotation* admits an object.
    """
    spec = FilterSpec.model_validate({"filters": '{"attr:status": ["500"]}', "q": "boom"})
    assert spec.filters == {"attr:status": ["500"]}
    assert spec.q == "boom"

    chart = ChartSpec.model_validate(
        {
            "chart_type": "bar",
            "field": "attr:status",
            "options": '{"top_n": 7}',
            "compare": '{"mode": "custom", "filters": {"q": "baseline"}}',
        }
    )
    assert chart.options.top_n == 7
    assert chart.compare is not None
    assert chart.compare.mode == "custom"
    assert chart.compare.filters is not None
    assert chart.compare.filters.q == "baseline"


def test_a_string_field_is_never_coerced_even_when_it_holds_json():
    """The safety rule behind `_admits_json_object`.

    ``q`` is free text an analyst may well have typed as JSON. Coercing any
    string that happens to parse would rewrite the query into a dict and fail
    validation on a search that is perfectly legal.
    """
    spec = FilterSpec.model_validate({"q": '{"chart_type": "bar"}'})
    assert spec.q == '{"chart_type": "bar"}'


def test_object_field_coverage_is_derived_not_hand_maintained():
    """Pins which fields the coercion covers, so a new one is a decision.

    Adding a dict-typed field to one of these specs silently widens the set;
    adding a differently-shaped one silently doesn't. Either way this test
    says so at the moment of the change rather than in production.
    """
    assert FilterSpec._object_fields() == {
        "filters",
        "exclusions",
        "filter_modes",
        "exclusion_modes",
    }
    assert {"filters", "compare", "options"} <= ChartSpec._object_fields()
    # `field`/`fields`/`metric` are str/list/enum — an object is not what a
    # string there could have meant.
    assert not ({"field", "fields", "metric", "scale"} & ChartSpec._object_fields())


def _nested_arg_models() -> dict[str, set[type[BaseModel]]]:
    """Every pydantic model reachable as a *nested* tool argument, by tool name.

    Walks the built server's real signatures rather than a hand-kept list: the
    invariant is about the position an argument occupies, so it has to be read
    off the positions that actually exist.
    """

    def models_in(annotation: Any) -> set[type[BaseModel]]:
        found: set[type[BaseModel]] = set()
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.add(annotation)
        for arg in typing.get_args(annotation):
            found |= models_in(arg)
        return found

    scope = _scope("c1", "t1", source_ids=["s1"], fidelity=Fidelity.FULL)
    by_tool: dict[str, set[type[BaseModel]]] = {}
    for tool in build_tool_server(scope)._tool_manager.list_tools():
        hints = typing.get_type_hints(tool.fn)
        hints.pop("return", None)
        direct: set[type[BaseModel]] = set()
        for annotation in hints.values():
            direct |= models_in(annotation)
        # Transitively: a model reached through another model's field is just
        # as nested, and `FilterSpec` inside `ChartSpec.compare` is exactly the
        # position that broke.
        reachable = set(direct)
        queue = list(direct)
        while queue:
            for field in queue.pop().model_fields.values():
                for model in models_in(field.annotation):
                    if model not in reachable:
                        reachable.add(model)
                        queue.append(model)
        if reachable:
            by_tool[tool.name] = reachable
    return by_tool


def test_every_nested_argument_model_derives_from_object_arg_model():
    """The invariant `ObjectArgModel` states about itself, enforced.

    `docs/AGENT.md` and the class docstring both call it "the base for every
    nested-argument model", but inheritance is a thing a future spec can
    simply not do — and the failure mode is not a test failure, it is one
    provider retry-looping in production on a tool it is using correctly.
    Derived from the real signatures so a model added tomorrow is covered.
    """
    by_tool = _nested_arg_models()
    assert by_tool, "no nested model arguments found — the walk is broken, not the invariant"
    offenders = {
        (tool, model.__name__)
        for tool, models in by_tool.items()
        for model in models
        if not issubclass(model, ObjectArgModel)
    }
    assert not offenders, f"nested tool-argument models not based on ObjectArgModel: {offenders}"
    # The walk must be seeing the real thing, not an empty set that trivially
    # passes: FilterSpec is a nested argument on most of the toolset, and
    # ChartSpec reaches FilterSpec transitively through `compare`.
    assert sum(FilterSpec in m for m in by_tool.values()) > 10
    assert FilterSpec in by_tool["propose_chart"]


def test_every_nested_argument_model_has_inspectable_annotations():
    """`_admits_json_object` raises on an annotation it cannot decide.

    An unresolved forward reference would otherwise be silently read as "does
    not admit an object" and drop that field from coercion. This asserts the
    computation succeeds for every model actually in a nested position, which
    is the same thing as asserting every one of them is rebuilt.
    """
    for models in _nested_arg_models().values():
        for model in models:
            assert isinstance(model._object_fields(), frozenset)


def test_admits_json_object_refuses_an_unresolved_annotation():
    with pytest.raises(TypeError, match="model_rebuild"):
        _admits_json_object(typing.ForwardRef("SomeLaterSpec") | None)


def test_spec_reference_covers_every_nested_argument_model():
    """`SPEC_REFERENCE` renders per-field prose once, for the models slimmed
    out of the repeated `$defs` (A13).

    Its input tuple is hand-kept, so a nested-argument model added later would
    have its schema slimmed and its prose never rendered — the model would see
    field names with no descriptions and nothing would fail. Same walk, same
    invariant, one layer up.
    """
    reachable = {model for models in _nested_arg_models().values() for model in models}
    missing = {m.__name__ for m in reachable} - set(SHARED_SPEC_NAMES)
    assert not missing, f"nested-argument models absent from SHARED_SPEC_NAMES: {missing}"


def test_coercion_does_not_rewrite_the_caller_s_mapping():
    """`tool_args` is persisted verbatim as the model emitted it.

    The validator normalizes a copy: a forensic record that changed shape
    between being stored and being validated is no longer the record.
    """
    raw = {"filters": '{"attr:status": ["500"]}'}
    FilterSpec.model_validate(raw)
    assert raw == {"filters": '{"attr:status": ["500"]}'}


# ── retired facet spec ──────────────────────────────────────────────────────


async def test_stale_facet_key_is_ignored_and_absent_from_the_resolved_echo(store, monkeypatch):
    """A conversation holding the pre-removal tool schema must not hard-fail.

    `ChartSpec` is `extra="ignore"` on purpose (same reason as
    `_accept_legacy_kind`): a model whose context still carries the old schema
    keeps working. What it must NOT do is quietly promise panels — `resolved`
    is the contract, and it no longer mentions facetting at all, so the model
    reading it learns the chart is unfacetted.
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "bar",
                "field": "attr:status",
                "facet": {"field": "attr:user", "limit": 4},
            }
        ),
    )
    assert result["ok"] is True
    assert "facet" not in result["resolved"]
    assert "facet" not in result["summary"]


# ---------------------------------------------------------------------------
# No-op filter rejection (2026-07-23 context overflow)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"filters": {"src_ip": []}},
        {"exclusions": {"status_code": []}},
        {"artifacts": []},
        {"tags_include": []},
        {"event_ids": []},
    ],
)
def test_empty_selections_are_rejected(payload):
    """An empty value list is an absent filter, so the tool would answer with
    the whole unfiltered timeline — a full-size result for a question the model
    did not mean to ask. Three such calls in one turn (`{"src_ip": []}`,
    `{"user_agent": []}`, `{"remote_user": []}`) returned byte-identical 34 KB
    payloads and consumed two thirds of a 65k context window.
    """
    from pydantic import ValidationError

    from vestigo.agent.tools import FilterSpec

    with pytest.raises(ValidationError) as exc:
        FilterSpec(**payload)
    message = str(exc.value)
    # The error has to name a way forward, not just refuse.
    assert "Omit" in message
    assert "field_terms" in message or "unconstrained" in message


def test_populated_and_absent_filters_still_validate():
    """The rejection must not catch either legitimate shape."""
    from vestigo.agent.tools import FilterSpec

    assert FilterSpec().filters == {}
    assert FilterSpec(filters={"src_ip": ["203.0.113.1"]}).filters == {"src_ip": ["203.0.113.1"]}


# ---------------------------------------------------------------------------
# Stories read tools (W7)
# ---------------------------------------------------------------------------


async def test_list_stories_scoped_to_case(store):
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_case("c2", "Case Two")
    s1 = await store.create_story("c1", "s1", "Ours", "notes", user="alice")
    await store.create_story_block(s1.id, "b1", "markdown", {"text": "x"}, user="alice")
    await store.create_story("c2", "s2", "Foreign", None, user="bob")

    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "list_stories")
    rows = _rows(payload["stories"])
    assert payload["total"] == 1
    assert rows[0]["id"] == "s1"
    assert rows[0]["title"] == "Ours"
    assert rows[0]["block_count"] == 1


async def test_read_story_returns_ordered_blocks(store):
    await store.init_schema()
    await store.create_case("c1", "Case One")
    story = await store.create_story("c1", "s1", "Report", None, user="alice")
    # The view_ref block below is created under the referent's row lock, so the
    # view it points at has to exist first.
    await store.create_view("c1", "v1", "My View", "", {})
    await store.create_story_block(story.id, "b1", "markdown", {"text": "first"}, user="alice")
    await store.create_story_block(
        story.id,
        "b2",
        "view_ref",
        {"view_id": "v1", "timeline_id": "t1", "display": {"limit": 200, "columns": None}},
        user="alice",
    )

    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "read_story", {"story_id": "s1"})
    assert payload["story"]["title"] == "Report"
    kinds = [b["kind"] for b in payload["blocks"]]
    assert kinds == ["markdown", "view_ref"]
    assert payload["blocks"][0]["content"]["text"] == "first"
    # Embed blocks carry their reference, not inline data.
    assert payload["blocks"][1]["content"]["view_id"] == "v1"


async def test_read_story_returns_ordinary_prose_whole(store):
    """A story block is the document the agent reasons about, not incidental
    string data. The old cap (1600 chars) cut ordinary prose — a few
    paragraphs of narrative — while a write accepts 256 KiB."""
    await store.init_schema()
    await store.create_case("c1", "Case One")
    story = await store.create_story("c1", "s1", "Report", None, user="alice")
    prose = "The lateral movement began at 02:14. " * 150  # ~5.5k chars
    await store.create_story_block(story.id, "b1", "markdown", {"text": prose}, user="alice")

    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "read_story", {"story_id": "s1"})
    block = payload["blocks"][0]
    assert block["content"]["text"] == prose
    assert "truncated" not in block["content"]
    assert "truncated_blocks" not in payload


async def test_read_story_charges_the_budget_only_for_text_taken(store):
    """A short block costs its own length, not the per-block cap.

    Charging every block ``STORY_TEXT_TRUNCATE`` regardless of how much it
    actually holds exhausts the response budget after three paragraphs and
    hands back later blocks as empty and ``truncated`` — the model is then told
    to treat a complete block as unread, which is the exact failure the marker
    exists to prevent.
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    story = await store.create_story("c1", "s1", "Report", None, user="alice")
    para = "The lateral movement began at 02:14. " * 5  # ~185 chars
    for i in range(10):
        await store.create_story_block(story.id, f"b{i}", "markdown", {"text": para}, user="alice")

    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "read_story", {"story_id": "s1"})
    assert len(payload["blocks"]) == 10
    for block in payload["blocks"]:
        assert block["content"]["text"] == para
        assert "truncated" not in block["content"]
    assert "truncated_blocks" not in payload


async def test_read_story_stamps_every_cut(store):
    """An unmarked cut is the failure that matters: the model summarizes half
    a paragraph believing it read the block. Cuts carry `truncated` and the
    real `text_length`, and the response says how many blocks were cut."""
    await store.init_schema()
    await store.create_case("c1", "Case One")
    story = await store.create_story("c1", "s1", "Report", None, user="alice")
    huge = "x" * (STORY_TEXT_TRUNCATE + 500)
    await store.create_story_block(story.id, "b1", "markdown", {"text": huge}, user="alice")

    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "read_story", {"story_id": "s1"})
    content = payload["blocks"][0]["content"]
    assert len(content["text"]) == STORY_TEXT_TRUNCATE
    assert content["truncated"] is True
    assert content["text_length"] == len(huge)
    assert payload["truncated_blocks"] == 1


async def test_read_story_spends_one_budget_across_blocks(store):
    """One enormous block cannot eat the whole response.

    Text is spent in document order, so a long story degrades block by block;
    every block still returns its id/kind/origin, because structure the model
    can act on beats a list that stops early.
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    story = await store.create_story("c1", "s1", "Report", None, user="alice")
    block_count = (STORY_TEXT_BUDGET // STORY_TEXT_TRUNCATE) + 2
    for i in range(block_count):
        await store.create_story_block(
            story.id, f"b{i}", "markdown", {"text": "y" * STORY_TEXT_TRUNCATE}, user="alice"
        )

    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "read_story", {"story_id": "s1"})
    assert payload["returned"] == block_count
    assert len(payload["blocks"]) == block_count
    total_text = sum(len(b["content"]["text"]) for b in payload["blocks"])
    assert total_text <= STORY_TEXT_BUDGET
    # The block past the budget is returned empty *and* marked, never as an
    # empty block that reads like an empty block.
    last = payload["blocks"][-1]["content"]
    assert last["text"] == ""
    assert last["truncated"] is True
    assert last["text_length"] == STORY_TEXT_TRUNCATE


async def test_read_story_unknown_id(store):
    await store.init_schema()
    await store.create_case("c1", "Case One")
    server = build_tool_server(_scope("c1", "t1"))
    payload = await _call(server, "read_story", {"story_id": "ghost"})
    assert "not found" in payload["error"]


async def test_propose_story_block_records_proposal(store):
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_story("c1", "s1", "Report", None, user="alice")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))
    result = await _call(
        server,
        "propose_story_block",
        {
            "story_id": "s1",
            "block_kind": "markdown",
            "content": {"text": "## agent finding"},
            "rationale": "summarizes the brute-force window",
        },
    )
    assert result["status"] == "proposed"
    assert isinstance(result["proposal_id"], str) and result["proposal_id"]
    (p,) = await store.list_agent_proposals(conv.id)
    assert p.kind == "story_block"
    assert p.payload["story_id"] == "s1"
    assert p.payload["content"] == {"text": "## agent finding"}


async def test_propose_story_block_validates(store):
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_story("c1", "s1", "Report", None, user="alice")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    unknown_story = await _call(
        server,
        "propose_story_block",
        {"story_id": "ghost", "block_kind": "markdown", "content": {"text": "x"}},
    )
    assert "not found" in unknown_story["error"]

    bad_kind = await _call(
        server,
        "propose_story_block",
        {"story_id": "s1", "block_kind": "gif", "content": {}},
    )
    assert "unknown block kind" in bad_kind["error"]

    bad_anchor = await _call(
        server,
        "propose_story_block",
        {
            "story_id": "s1",
            "block_kind": "markdown",
            "content": {"text": "x"},
            "after_block_id": "ghost",
        },
    )
    assert "after_block_id" in bad_anchor["error"]


async def test_a_stringified_top_level_argument_is_parsed_before_the_tool_body(store):
    """A *well-formed* stringified top-level argument never reaches the body.

    Two independent layers parse them: pydantic-ai's ``args_as_dict`` on the
    way in, and the MCP SDK's ``pre_parse_json``
    (``mcp/server/fastmcp/utilities/func_metadata.py``) for any parameter
    whose annotation is not ``str`` — the same "an object is the only thing a
    string could have meant" rule `_admits_json_object` applies one level
    down. Both give up on a string that is not valid JSON, which is why
    ``propose_story_block`` accepts ``str`` and parses it itself; see
    `test_propose_story_block_names_the_defect_in_malformed_json`.
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_story("c1", "s1", "Report", None, user="alice")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    result = await _call(
        server,
        "propose_story_block",
        {"story_id": "s1", "block_kind": "markdown", "content": '{"text": "## parsed"}'},
    )
    assert result["status"] == "proposed"
    (p,) = await store.list_agent_proposals(conv.id)
    assert p.payload["content"] == {"text": "## parsed"}


async def test_propose_story_records_a_story_proposal(store):
    """The agent can propose the document it was asked to write into.

    Without this a case with no stories was a dead end: `propose_story_block`
    needs a `story_id` and the agent had no way to make one.
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    result = await _call(
        server,
        "propose_story",
        {
            "title": "  Verifikation der Ergebnisse  ",
            "description": " Abschluss ",
            "rationale": "no report yet",
        },
    )
    assert result["status"] == "proposed"
    # The note is the whole reason this does not chain: a story has no id
    # until the analyst confirms, and a model that invents one wastes a turn.
    assert "list_stories" in result["note"]

    (p,) = await store.list_agent_proposals(conv.id)
    assert p.kind == "story"
    assert p.payload == {"title": "Verifikation der Ergebnisse", "description": "Abschluss"}
    assert p.rationale == "no report yet"
    # Proposing writes nothing.
    assert await store.list_stories("c1") == []


async def test_propose_story_refuses_a_name_already_taken_or_pending(store):
    """Both "it exists" and "you already asked" are named, and differently.

    A proposal is not a story, so a model that cannot tell the two apart
    re-proposes the same document every turn — the same blindness that made it
    probe `propose_story_block` with throwaway test blocks (2026-09-03).
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    await store.create_story("c1", "s1", "Verifikation", None, user="alice")
    taken = await _call(server, "propose_story", {"title": "  verifikation "})
    assert "already exists" in taken["error"]
    assert "s1" in taken["error"]

    first = await _call(server, "propose_story", {"title": "Zweiter Bericht"})
    assert first["status"] == "proposed"
    again = await _call(server, "propose_story", {"title": "zweiter bericht"})
    assert "already proposed" in again["error"]
    assert first["proposal_id"] in again["error"]

    # A decided proposal is no longer pending — the analyst rejected it, so
    # proposing it again is a legitimate move rather than a duplicate.
    await store.decide_agent_proposal(first["proposal_id"], status="rejected", decided_by="alice")
    retry = await _call(server, "propose_story", {"title": "Zweiter Bericht"})
    assert retry["status"] == "proposed"


async def test_propose_story_validates_its_title(store):
    await store.init_schema()
    await store.create_case("c1", "Case One")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    assert "title is required" in (await _call(server, "propose_story", {"title": "   "}))["error"]
    # The column is String(255); an over-long title would otherwise surface as
    # a driver error at confirm time, i.e. a 500 on the analyst's click.
    long = await _call(server, "propose_story", {"title": "x" * 256})
    assert "255" in long["error"]
    assert await store.list_agent_proposals(conv.id) == []


async def test_propose_story_block_points_an_empty_case_at_propose_story(store):
    """ "Not found — list_stories" is a dead end when list_stories is empty."""
    await store.init_schema()
    await store.create_case("c1", "Case One")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    empty = await _call(
        server,
        "propose_story_block",
        {"story_id": "whatever", "block_kind": "markdown", "content": {"text": "x"}},
    )
    assert "propose_story" in empty["error"]

    # With a story in the case, a wrong id is just a wrong id.
    await store.create_story("c1", "s1", "Report", None, user="alice")
    wrong = await _call(
        server,
        "propose_story_block",
        {"story_id": "ghost", "block_kind": "markdown", "content": {"text": "x"}},
    )
    assert "list_stories" in wrong["error"]
    assert "propose_story" not in wrong["error"]


async def test_propose_story_block_names_the_defect_in_malformed_json(store):
    """A model that closes an object with `")` gets told what is wrong.

    Neither parsing layer can recover such a string, so before the tool took
    ``str`` the model saw pydantic's "Input should be a valid dictionary" —
    which names neither the malformed JSON nor the shape a markdown block
    needs. A real turn spent six retries on it (2026-09-03).
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_story("c1", "s1", "Report", None, user="alice")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    broken = await _call(
        server,
        "propose_story_block",
        {"story_id": "s1", "block_kind": "markdown", "content": '{"text": "## Bewertung")'},
    )
    assert "not valid JSON" in broken["error"]
    assert '{"text": "..."}' in broken["error"]

    # Bare prose is a plausible guess at an opaque `content`; it must teach
    # the shape too rather than fall through as a substring-matchable string.
    prose = await _call(
        server,
        "propose_story_block",
        {"story_id": "s1", "block_kind": "markdown", "content": "## Bewertung"},
    )
    assert '{"text": "..."}' in prose["error"]

    # The other plausible guess: right kind, wrong key.
    wrong_key = await _call(
        server,
        "propose_story_block",
        {"story_id": "s1", "block_kind": "markdown", "content": {"markdown": "## Bewertung"}},
    )
    assert '{"text": "..."}' in wrong_key["error"]

    assert await store.list_agent_proposals(conv.id) == []


async def test_read_story_reports_this_conversation_s_pending_proposals(store):
    """A proposal is invisible in `blocks`/`block_count` until confirmed.

    A model that cannot see its own pending work reads its proposal as a
    no-op and probes the API with throwaway "test" blocks, which land in the
    analyst's confirmation queue as junk (observed 2026-09-03).
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_story("c1", "s1", "Report", None, user="alice")
    await store.create_story("c1", "s2", "Other", None, user="alice")
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    other = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    proposed = await _call(
        server,
        "propose_story_block",
        {
            "story_id": "s1",
            "block_kind": "markdown",
            "content": {"text": "## Abschließende Bewertung"},
            "rationale": "conclusion",
        },
    )
    # Another story of the same conversation, and another conversation's work
    # on this one: neither belongs in this story's answer.
    await _call(
        server,
        "propose_story_block",
        {"story_id": "s2", "block_kind": "markdown", "content": {"text": "elsewhere"}},
    )
    await store.create_agent_proposal(
        case_id="c1",
        timeline_id="t1",
        conversation_id=other.id,
        rationale="",
        kind="story_block",
        payload={"story_id": "s1", "block_kind": "markdown", "content": {"text": "theirs"}},
    )

    read = await _call(server, "read_story", {"story_id": "s1"})
    assert read["block_count"] == 0
    assert read["blocks"] == []
    assert read["pending_proposals"] == [
        {
            "proposal_id": proposed["proposal_id"],
            "block_kind": "markdown",
            "after_block_id": None,
            "rationale": "conclusion",
        }
    ]

    # A decided proposal stops being pending — it is a block or it is gone.
    await store.decide_agent_proposal(
        proposed["proposal_id"], status="rejected", decided_by="alice"
    )
    settled = await _call(server, "read_story", {"story_id": "s1"})
    assert "pending_proposals" not in settled


async def test_propose_story_block_checks_referent_scope(store):
    """A referent outside the case is an error the model can correct.

    Without this the analyst gets a proposal card that confirms into a block
    which only reveals itself as broken at export time, as a frozen
    ``resolution.error``.
    """
    await store.init_schema()
    await store.create_case("c1", "Case One")
    await store.create_case("c2", "Case Two")
    await store.create_story("c1", "s1", "Report", None, user="alice")
    await store.create_timeline("c1", "t1", "Timeline One")
    foreign = await store.create_view("c2", "v-foreign", "Theirs", "ssh", {})
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    server = build_tool_server(_scope_with_conversation("c1", "t1", conv.id))

    result = await _call(
        server,
        "propose_story_block",
        {
            "story_id": "s1",
            "block_kind": "view_ref",
            "content": {"view_id": foreign.id, "timeline_id": "t1"},
        },
    )
    assert result["error"] == "view 'v-foreign' is not in this case"
    assert await store.list_agent_proposals(conv.id) == []


async def test_propose_story_block_absent_without_conversation(store):
    await store.init_schema()
    server = build_tool_server(_scope("c1", "t1"))  # no conversation_id
    async with FastMCPClient(server) as client:
        names = [t.name for t in await client.list_tools()]
    assert "propose_story_block" not in names


# ── derivations ──────────────────────────────────────────────────────────────


async def test_derive_is_passed_to_the_terms_scan_and_echoed(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "bar",
                "field": "bytes",
                "derive": {"kind": "bins", "mode": "log", "count": 8},
            }
        ),
    )
    assert result["ok"] is True
    kw = next(kw for name, _, kw in fake.calls if name == "field_terms")
    assert kw["derive"].mode == "log"
    assert result["resolved"]["scale"] == "ordinal"
    assert result["resolved"]["derive"] == {"kind": "bins", "mode": "log", "count": 8}


async def test_derive_rejected_on_a_figure_that_admits_none(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {
            "chart_type": "pie",
            "field": "bytes",
            "derive": {"kind": "bins", "mode": "width", "count": 4},
        },
    )
    assert "admits no derivation" in message
    assert "bar" in message and "heatmap" in message


async def test_derive_rejected_on_a_virtual_time_field(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {
            "chart_type": "bar",
            "field": "time:hour_of_day",
            "derive": {"kind": "time_part", "part": "hour"},
        },
    )
    assert "already a calendar part" in message


async def test_derive_with_a_categorical_scale_is_rejected(store, monkeypatch):
    """`scale` is what the field is treated as *before* the derivation — the
    page's treat-as — so categories cannot be binned, and a calendar part is
    taken from a number-or-time field, not a measure."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {
            "chart_type": "bar",
            "field": "bytes",
            "scale": "nominal",
            "derive": {"kind": "bins", "mode": "width", "count": 4},
        },
    )
    assert "ratio" in message and "interval" in message
    message = await _reject(
        server,
        {
            "chart_type": "bar",
            "field": "ts",
            "scale": "ratio",
            "derive": {"kind": "time_part", "part": "hour"},
        },
    )
    assert "interval" in message


@pytest.mark.parametrize("scale", ["ratio", "interval", "ordinal", None])
async def test_derive_accepts_the_treat_as_scale_and_resolves_to_ordinal(store, monkeypatch, scale):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    spec = {
        "chart_type": "bar",
        "field": "bytes",
        "derive": {"kind": "bins", "mode": "width", "count": 4},
    }
    if scale is not None:
        spec["scale"] = scale
    result = await _call(server, "propose_chart", _chart(spec))
    assert result["ok"] is True and result["resolved"]["scale"] == "ordinal"


async def test_describe_field_lists_the_derivations_that_make_sense(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    numeric = await _call(server, "describe_field", {"field": "bytes"})
    assert numeric["derivations"] == ["bins", "time_part"]
    fake.numeric_count = 0
    categorical = await _call(server, "describe_field", {"field": "status"})
    assert categorical["derivations"] == ["time_part"]
    virtual = await _call(server, "describe_field", {"field": "time:hour_of_day"})
    assert virtual["derivations"] == []


# ── table figure ─────────────────────────────────────────────────────────────


async def test_table_dispatches_with_sort_second_field_and_columns(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "table",
                "field": "user",
                "field_y": "country",
                "inputs": {"columns": ["count", "share", "distinct_second"]},
                "options": {
                    "top_n": 100,
                    "table_sort_by": "distinct_second",
                    "table_sort_dir": "asc",
                },
            }
        ),
    )
    assert result["ok"] is True
    name, args, kw = next(c for c in fake.calls if c[0] == "field_table")
    assert args == ("user", 30)  # clamped to AGENT_CHART_LIMITS.table_rows
    assert kw == {"second_field": "country", "sort_by": "distinct_second", "sort_dir": "asc"}
    assert result["resolved"]["inputs"] == {"columns": ["count", "share", "distinct_second"]}
    assert result["resolved"]["options"]["top_n"] == 30
    assert result["summary"]["remainder"] == {"count": 10, "share": 0.1, "distinct_values": 2}
    assert [r["value"] for r in result["summary"]["rows"]] == ["a", "b"]


async def test_table_columns_on_another_figure_are_refused(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server, {"chart_type": "bar", "field": "user", "inputs": {"columns": ["count"]}}
    )
    assert "only the table figure has columns" in message


async def test_table_distinct_second_needs_field_y(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {"chart_type": "table", "field": "user", "inputs": {"columns": ["distinct_second"]}},
    )
    assert "distinct_second needs field_y" in message
    message = await _reject(
        server,
        {"chart_type": "table", "field": "user", "options": {"table_sort_by": "distinct_second"}},
    )
    assert "distinct_second needs field_y" in message


async def test_table_takes_a_derivation(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "table",
                "field": "bytes",
                "derive": {"kind": "bins", "mode": "log", "count": 4},
            }
        ),
    )
    assert result["ok"] is True
    kw = next(kw for name, _, kw in fake.calls if name == "field_table")
    assert kw["derive"].count == 4 and result["resolved"]["scale"] == "ordinal"


# ── marks: the spec ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mark", "needle"),
    [
        ({"kind": "events"}, 'kind="events" needs filters'),
        ({"kind": "baseline"}, 'kind="baseline" needs definition_id'),
        ({"kind": "view"}, 'kind="view" needs view_id'),
        ({"kind": "instant", "at": "2026-03-13T09:41:00Z"}, 'kind="instant" needs at and label'),
        (
            {
                "kind": "range",
                "start": "2026-03-13T09:41:00Z",
                "end": "2026-03-13T08:00:00Z",
                "label": "w",
            },
            "start must be before end",
        ),
        (
            {"kind": "instant", "at": "2026-03-13T09:41:00Z", "label": "x", "view_id": "v1"},
            'kind="instant" does not take view_id',
        ),
    ],
)
def test_chart_mark_spec_refuses_a_kind_without_its_fields(mark, needle):
    from vestigo.agent.tools import ChartMarkSpec

    with pytest.raises(ValidationError) as excinfo:
        ChartMarkSpec.model_validate(mark)
    assert needle in str(excinfo.value)


def test_chart_mark_spec_accepts_each_kind():
    from vestigo.agent.tools import ChartMarkSpec

    events = ChartMarkSpec.model_validate(
        {"kind": "events", "filters": {"tags_include": ["exfil"]}}
    )
    assert events.filters.tags_include == ["exfil"]
    assert (
        ChartMarkSpec.model_validate({"kind": "baseline", "definition_id": "bd1"}).definition_id
        == "bd1"
    )
    assert ChartMarkSpec.model_validate({"kind": "view", "view_id": "v1"}).view_id == "v1"
    instant = ChartMarkSpec.model_validate(
        {"kind": "instant", "at": "2026-03-13T09:41:00Z", "label": "beacon"}
    )
    assert instant.at.year == 2026
    r = ChartMarkSpec.model_validate(
        {
            "kind": "range",
            "start": "2026-03-13T09:00:00Z",
            "end": "2026-03-13T10:00:00Z",
            "label": "w",
        }
    )
    assert r.start < r.end


def test_chart_limits_carry_marks_per_source():
    from vestigo.agent.chart_exec import AGENT_CHART_LIMITS, ANALYST_CHART_LIMITS

    assert AGENT_CHART_LIMITS.marks_per_source == 20
    assert ANALYST_CHART_LIMITS.marks_per_source is None  # the viz_marks_max setting


# ── marks: execution ─────────────────────────────────────────────────────────


async def test_marks_resolve_into_the_envelope_and_the_tool_result_keeps_only_the_summary(
    store, monkeypatch
):
    fake = _patch_chart_service(monkeypatch)
    # In-app scope: the card fetches its own data and resolves its own marks,
    # so the model is handed the compressed summary and nothing else. The
    # external /mcp surface keeps both — see the propose_chart surface tests.
    in_app = _scope("c1", "t1", source_ids=["s1"])
    in_app.conversation_id = "conv1"
    server = build_tool_server(in_app)
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "time",
                "marks": [
                    {"kind": "events", "filters": {"q": "beacon"}, "label": "beacons"},
                    {"kind": "instant", "at": "2026-07-20T09:41:00Z", "label": "first"},
                ],
            }
        ),
    )
    assert result["ok"] is True
    assert "marks" not in result and "result" not in result
    assert result["summary"]["marks"]["shown"] == 2
    assert [s["kind"] for s in result["summary"]["marks"]["sources"]] == ["events", "instant"]
    assert result["resolved"]["marks"][0]["kind"] == "events"
    limit_args, kw = next((a, k) for n, a, k in fake.calls if n == "mark_instants")
    assert limit_args == (20,) and kw == {"q": "beacon"}  # AGENT_CHART_LIMITS.marks_per_source


async def test_marks_on_a_figure_without_them_are_refused(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {
            "chart_type": "bar",
            "field": "user",
            "marks": [{"kind": "instant", "at": "2026-07-20T09:41:00Z", "label": "x"}],
        },
    )
    assert 'chart_type="bar" takes no marks' in message and "time, line" in message


async def test_execute_chart_spec_carries_resolved_marks_for_the_export(store, monkeypatch):
    from vestigo.agent.chart_exec import ANALYST_CHART_LIMITS, execute_chart_spec
    from vestigo.agent.tools import ChartSpec

    fake = _patch_chart_service(monkeypatch)
    spec = ChartSpec.model_validate(
        {
            "chart_type": "time",
            "marks": [
                {
                    "kind": "range",
                    "start": "2026-07-20T09:00:00Z",
                    "end": "2026-07-20T10:00:00Z",
                    "label": "w",
                }
            ],
        }
    )
    envelope = await execute_chart_spec(
        _scope("c1", "t1", source_ids=["s1"]), spec, service=fake, limits=ANALYST_CHART_LIMITS
    )
    assert envelope["marks"]["marks"][0] == {
        "kind": "range",
        "start": "2026-07-20T09:00:00+00:00",
        "end": "2026-07-20T10:00:00+00:00",
        "label": "w",
        "source": 0,
        "provenance": {"kind": "analyst"},
    }
    assert envelope["marks"]["cap"] == 50  # viz_marks_max default under the analyst limits


async def test_propose_chart_returns_the_visualize_deep_link(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "time", "filters": {"q": "4624"}})
    )
    assert result["open_url"].startswith("/cases/c1/timelines/t1/visualize?")
    assert "q=4624" in result["open_url"] and "c_type=time" in result["open_url"]
    assert not any("open_url" in w for w in result["warnings"])


async def test_propose_chart_warns_when_open_url_cannot_carry_the_scope(store, monkeypatch):
    """`event_ids` / `run_id` / `collapse_routine` have no URL form: the link
    draws a wider chart than the result describes, and must say so."""
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "bar",
                "field": "attr:user",
                "filters": {"event_ids": ["e1", "e2"], "collapse_routine": True},
            }
        ),
    )
    assert result["open_url"].startswith("/cases/c1/timelines/t1/visualize?")
    (warning,) = [w for w in result["warnings"] if w.startswith("open_url")]
    assert "a fixed event set" in warning and "routine collapse" in warning


# ── cumulative and calendar ──────────────────────────────────────────────────


async def test_cumulative_resolves_the_quantity_from_field_and_scale(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    fieldless = await _call(server, "propose_chart", _chart({"chart_type": "cumulative"}))
    assert fieldless["ok"] is True and fieldless["resolved"]["options"]["quantity"] == "events"
    assert fieldless["summary"] == {
        "total": 3,
        "events": 4,
        "unparsed": 1,
        "buckets": 1,
        "interval_seconds": 3600,
    }
    measure = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "cumulative",
                "field": "bytes",
                "scale": "ratio",
                "options": {"buckets": 12},
            }
        ),
    )
    assert measure["resolved"]["options"] == {"buckets": 12, "quantity": "sum"}
    categories = await _call(
        server, "propose_chart", _chart({"chart_type": "cumulative", "field": "user"})
    )
    assert categories["resolved"]["options"]["quantity"] == "distinct"
    assert [a for n, a, _ in fake.calls if n == "cumulative"] == [
        (None, "events", 30),
        ("bytes", "sum", 12),
        ("user", "distinct", 30),
    ]


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        (
            {"chart_type": "cumulative", "options": {"quantity": "sum"}},
            'quantity="sum" needs field',
        ),
        (
            {"chart_type": "cumulative", "options": {"quantity": "distinct"}},
            'quantity="distinct" needs field',
        ),
        (
            {
                "chart_type": "cumulative",
                "field": "user",
                "scale": "nominal",
                "options": {"quantity": "sum"},
            },
            'quantity="sum" needs scale="ratio"',
        ),
        (
            {
                "chart_type": "cumulative",
                "field": "bytes",
                "scale": "ratio",
                "options": {"quantity": "distinct"},
            },
            'quantity="distinct" needs scale="nominal" or "ordinal"',
        ),
        (
            {"chart_type": "calendar", "field": "time:hour_of_day"},
            "a calendar part is always present",
        ),
        # `interval` was advertised by CHART_META and reachable by no quantity:
        # `sum` demanded ratio, `distinct` demanded nominal/ordinal, `events`
        # discarded the field, so the refusals cycled (#332). The scale is off
        # the figure — a running total needs the true zero ratio has — and the
        # scale check now precedes the per-figure rules, so the message names
        # the scale instead of sending the model back round the quantities.
        (
            {"chart_type": "cumulative", "field": "bytes", "scale": "interval"},
            'requires scale in {"nominal", "ordinal", "ratio"}',
        ),
        (
            {
                "chart_type": "cumulative",
                "field": "bytes",
                "scale": "interval",
                "options": {"quantity": "distinct"},
            },
            'requires scale in {"nominal", "ordinal", "ratio"}',
        ),
    ],
)
async def test_cumulative_and_calendar_refusals(store, monkeypatch, spec, needle):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    assert needle in await _reject(server, spec)


async def test_cumulative_events_with_a_field_warns_that_the_field_is_ignored(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "cumulative", "field": "user", "options": {"quantity": "events"}}),
    )
    assert result["ok"] is True
    assert any("field is ignored" in w for w in result["warnings"])


async def test_calendar_summarises_the_cap_and_takes_marks_nowhere(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "calendar", "field": "user"})
    )
    assert result["summary"] == {
        "total": 2,
        "max_count": 2,
        "weeks": 1,
        "weeks_total": 1,
        "truncated": False,
        "dropped": 0,
    }
    assert next(a for n, a, _ in fake.calls if n == "calendar") == ("user", 53)
    message = await _reject(
        server,
        {
            "chart_type": "calendar",
            "marks": [{"kind": "instant", "at": "2026-07-20T09:41:00Z", "label": "x"}],
        },
    )
    assert 'chart_type="calendar" takes no marks' in message


async def test_cumulative_takes_marks(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "cumulative",
                "marks": [{"kind": "instant", "at": "2026-07-20T09:41:00Z", "label": "x"}],
            }
        ),
    )
    assert result["ok"] is True and result["summary"]["marks"]["shown"] == 1


# ── ranked change ────────────────────────────────────────────────────────────


async def test_change_needs_a_comparison_layer_and_says_why(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(server, {"chart_type": "change", "field": "user"})
    assert 'chart_type="change" needs a comparison layer' in message
    assert 'compare.mode to "baseline" or "custom"' in message


async def test_change_runs_both_windows_and_summarises_the_ranking(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "change",
                "field": "user",
                "compare": {"mode": "custom", "filters": {"q": "phase-a"}},
                "options": {"top_n": 5, "layout": "slope"},
            }
        ),
    )
    assert result["ok"] is True
    assert result["resolved"]["options"] == {"top_n": 5, "layout": "slope"}
    assert result["resolved"]["compare_mode"] == "custom"
    assert result["summary"] == {
        "primary_total": 20,
        "comparison_total": 10,
        "union_size": 4,
        "rows_shown": 3,
        "truncated": True,
        "top_rows": [
            {"value": "alice", "status": "fell", "delta_share": -0.4},
            {"value": "bob", "status": "rose", "delta_share": 0.3},
            {"value": "dave", "status": "new", "delta_share": 0.15},
        ],
    }
    assert _called(fake, "field_change") == ("user", 5, 30)


async def test_change_clamps_top_n_to_the_agent_ceiling_and_defaults_the_layout(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "change",
                "field": "user",
                "compare": {"mode": "baseline"},
                "options": {"top_n": 50},
            }
        ),
    )
    assert result["resolved"]["options"] == {"top_n": 20, "layout": "dumbbell"}
    assert any("clamped to 20" in w for w in result["warnings"])
    assert _called(fake, "field_change") == ("user", 20, 30)


async def test_change_takes_no_marks(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {
            "chart_type": "change",
            "field": "user",
            "compare": {"mode": "baseline"},
            "marks": [{"kind": "instant", "at": "2026-07-20T09:41:00Z", "label": "x"}],
        },
    )
    assert 'chart_type="change" takes no marks' in message


# ── interval lanes ───────────────────────────────────────────────────────────


async def test_lanes_first_last_by_default_and_summarises_the_lanes(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "lanes", "field": "status", "options": {"limit_y": 50}}),
    )
    assert result["ok"] is True
    assert result["resolved"]["options"] == {"limit_y": 20}
    assert any("clamped to 20" in w for w in result["warnings"])
    assert result["summary"] == {
        "pairing": "first_last",
        "lanes_shown": 2,
        "lanes_total": 3,
        "lane_cap_hit": True,
        "intervals": 3,
        "unpaired_starts": 1,
        "orphan_ends": 1,
        "rows_truncated": False,
        "undated": 1,
        "top_lanes": [
            {"key": "h2", "count": 4, "intervals": 2},
            {"key": "h1", "count": 3, "intervals": 1},
        ],
    }
    assert _called(fake, "field_lanes") == ("status", "first_last", 20, 2000)
    assert fake.calls[-1][2]["layers"] == (False, False)


async def test_lanes_next_end_builds_both_layers_and_echoes_the_inputs(store, monkeypatch):
    fake = _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "lanes",
                "field": "status",
                "inputs": {
                    "pairing": "next_end",
                    "start_filter": {"filters": {"attr:kind": ["logon"]}},
                    "end_filter": {"filters": {"attr:kind": ["logoff"]}},
                },
            }
        ),
    )
    assert result["ok"] is True
    assert result["resolved"]["inputs"]["pairing"] == "next_end"
    assert result["resolved"]["inputs"]["start_filter"]["filters"] == {"attr:kind": ["logon"]}
    assert _called(fake, "field_lanes") == ("status", "next_end", 10, 2000)
    assert fake.calls[-1][2]["layers"] == (True, True)


async def test_lanes_next_end_needs_both_filters_and_says_so(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server,
        {
            "chart_type": "lanes",
            "field": "status",
            "inputs": {"pairing": "next_end", "start_filter": {"q": "logon"}},
        },
    )
    assert 'pairing="next_end" needs inputs.start_filter and inputs.end_filter' in message


async def test_lane_inputs_on_another_figure_are_refused_and_first_last_ignores_filters(
    store, monkeypatch
):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    message = await _reject(
        server, {"chart_type": "bar", "field": "status", "inputs": {"pairing": "first_last"}}
    )
    assert 'inputs.pairing / start_filter / end_filter are chart_type="lanes" only' in message
    result = await _call(
        server,
        "propose_chart",
        _chart({"chart_type": "lanes", "field": "status", "inputs": {"start_filter": {"q": "x"}}}),
    )
    assert result["ok"] is True
    assert any('ignored under pairing="first_last"' in w for w in result["warnings"])


async def test_lanes_draw_marks(store, monkeypatch):
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server,
        "propose_chart",
        _chart(
            {
                "chart_type": "lanes",
                "field": "status",
                "marks": [{"kind": "instant", "at": "2026-07-20T09:41:00Z", "label": "x"}],
            }
        ),
    )
    assert result["ok"] is True and result["summary"]["marks"]["shown"] == 1


# ── the external /mcp surface: no card, so the figure is the payload ────────


async def test_propose_chart_keeps_the_figures_data_for_an_external_client(store, monkeypatch):
    """Over /mcp there is no card to draw the chart and no analyst to read it.

    The in-app path drops `result` because the card fetches its own copy; an
    external client has neither, and a summary of `{"buckets": 48}` describes
    a figure the caller cannot see, plot, or quote a number from.
    """
    _patch_chart_service(monkeypatch)
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "bar", "field": "attr:status"})
    )
    assert result["ok"] is True
    # Columnar, like every other tabular tool result on this transport (A13).
    assert result["result"]["values"] == {
        "columns": ["value", "count"],
        "rows": [["a", 60], ["b", 40]],
    }
    # The summary the in-app model gets is still there — it is the reading, and
    # the data is the evidence for it.
    assert result["summary"]["total"] == 100


async def test_propose_chart_data_does_not_reach_the_in_app_model(store, monkeypatch):
    """The other half of the contract: nothing changes for the agent panel."""
    _patch_chart_service(monkeypatch)
    in_app = _scope("c1", "t1", source_ids=["s1"])
    in_app.conversation_id = "conv1"
    result = await _call(
        build_tool_server(in_app),
        "propose_chart",
        _chart({"chart_type": "bar", "field": "attr:status"}),
    )
    assert "result" not in result and "marks" not in result
    assert result["summary"]["total"] == 100


async def test_propose_chart_compacts_a_timeseries_the_way_field_timeseries_does(
    store, monkeypatch
):
    """The shared time axis is stated once, not repeated per series."""
    fake = _patch_chart_service(monkeypatch)
    starts = ["2026-03-13T09:00:00Z", "2026-03-13T10:00:00Z"]
    fake.field_value_timeseries = lambda query, field, buckets, series_limit: {
        "field": field,
        "series": [
            {
                "value": v,
                "buckets": [{"start": t, "count": n} for t, n in zip(starts, counts, strict=True)],
            }
            for v, counts in (("ok", [5, 7]), ("fail", [1, 9]))
        ],
        "interval_seconds": 3600,
        "min": starts[0],
        "max": starts[-1],
        "distinct": 2,
        "other_count": 0,
        "series_truncated": False,
    }
    server = build_tool_server(_scope("c1", "t1", source_ids=["s1"]))
    result = await _call(
        server, "propose_chart", _chart({"chart_type": "heatmap", "field": "attr:status"})
    )
    # The axis once, not once per series — the dominant term in a wide figure.
    assert result["result"]["bucket_starts"] == starts
    assert result["result"]["series"] == {
        "columns": ["value", "counts"],
        "rows": [["ok", [5, 7]], ["fail", [1, 9]]],
    }


async def test_propose_finding_is_in_app_only(store, monkeypatch):
    """It writes nothing and shows nothing without a card to show.

    Over /mcp the model reports its conclusion to its own caller; a tool whose
    entire product is an analyst-facing card would spend schema budget to
    return a hit count `search_events` already gives, under a docstring that
    tells the model an analyst is looking at something.
    """
    import vestigo.agent.tools as tools_module

    monkeypatch.setattr(tools_module, "embeddings_available", lambda: True)
    await store.init_schema()
    async with FastMCPClient(build_tool_server(_scope("c1", "t1"))) as client:
        external = {t.name for t in await client.list_tools()}
    async with FastMCPClient(
        build_tool_server(_scope_with_conversation("c1", "t1", "conv1"))
    ) as client:
        in_app = {t.name for t in await client.list_tools()}
    assert "propose_finding" not in external
    assert "propose_finding" in in_app
    # It leaves with the other card-shaped tools, not with the read tools.
    assert "propose_chart" in external and "search_events" in external


async def test_external_instructions_do_not_steer_at_a_tool_that_is_not_served(store):
    """The prose an external client reads has to describe the surface it got.

    ``instructions`` reach /mcp clients only, so they are the one place that can
    tell a model how to work here — and the one place that can send it looking
    for `propose_finding`, which this transport does not register.
    """
    await store.init_schema()
    server = build_tool_server(_scope("c1", "t1"))
    async with FastMCPClient(server) as client:
        served = {t.name for t in await client.list_tools()}
    text = server.instructions or ""
    # The iterate-then-what sentence, which is what used to name findings.
    assert "return refined filters as findings" not in text
    for name in ("propose_finding", "propose_annotation", "propose_story_block"):
        assert name not in served
        assert name not in text
    assert "propose_chart" in text and "propose_chart" in served


async def test_propose_chart_is_described_for_the_surface_it_is_on(store, monkeypatch):
    """The docstring promises a card, a Save button and an analyst. Over /mcp
    none of the three exist, and a tool description is what the model plans
    against — so that surface reads its own."""
    await store.init_schema()

    async def described(scope) -> str:
        async with FastMCPClient(build_tool_server(scope)) as client:
            return next(
                t for t in await client.list_tools() if t.name == "propose_chart"
            ).description

    external = await described(_scope("c1", "t1"))
    in_app = await described(_scope_with_conversation("c1", "t1", "conv1"))
    assert "card" in in_app and "Save" in in_app
    assert "card" not in external and "Save" not in external
    assert "open_url" in external and "open_url" in in_app
