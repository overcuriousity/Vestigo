"""Export resolver: per-block freezing, honesty flags, and the export store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vestigo.db.postgres import User, View
from vestigo.db.queries import EventPage
from vestigo.stories.export import _view_filter_to_spec, resolve_story_snapshot
from vestigo.stories.schemas import canonical_hash

FROZEN_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _user() -> User:
    return User(id="u1", username="alice", is_admin=True, is_active=True)


async def _case_with_story(store, blocks, setup=None):
    """Build a case + story and add *blocks*.

    ``setup`` runs against the case before the blocks are added: a ``view_ref``
    block is written under its referent's row lock, so any view it points at
    has to exist by then.
    """
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    story = await store.create_story(case.id, "s1", "Report", None, user="alice")
    if setup is not None:
        await setup(case)
    created = []
    for i, (kind, content) in enumerate(blocks):
        created.append(
            await store.create_story_block(story.id, f"b{i}", kind, content, user="alice")
        )
    return case, story, created


async def _hard_delete_view(store, case_id, view_id):
    """Remove a view row outright, past the hide-if-referenced rule.

    Reproduces the state a pre-``deleted_at`` database or an imported case can
    still be in — a ``view_ref`` block whose referent is simply gone. The write
    path no longer produces it (the insert takes the referent's row lock), so
    the resolver's dangling branch has to be set up rather than created.
    """
    from sqlalchemy import delete as sa_delete

    async with store.session_factory() as session:
        await session.execute(sa_delete(View).where(View.case_id == case_id, View.id == view_id))
        await session.commit()


async def test_markdown_and_dangling_view_resolve(store):
    async def _setup(case):
        await store.create_view(case.id, "ghost", "Ghost", "", {})

    case, story, blocks = await _case_with_story(
        store,
        [
            ("markdown", {"text": "# findings"}),
            (
                "view_ref",
                {"view_id": "ghost", "timeline_id": "t1", "display": {"limit": 200}},
            ),
        ],
        setup=_setup,
    )
    await _hard_delete_view(store, case.id, "ghost")
    snapshot = await resolve_story_snapshot(
        story, blocks, user=_user(), store=store, now=lambda: FROZEN_NOW
    )
    assert snapshot["v"] == 1
    assert snapshot["story"]["exported_by"] == "alice"
    md, view = snapshot["blocks"]
    assert md["data"] == {"text": "# findings"}
    assert md["resolution"]["error"] is None
    assert view["data"] is None
    assert "not found" in view["resolution"]["error"]


async def test_view_block_truncation_flagged(store):
    async def _setup(case):
        await store.create_view(case.id, "v1", "SSH hits", query="ssh", view_filter={"q": "ssh"})

    case, story, blocks = await _case_with_story(
        store,
        [("view_ref", {"view_id": "v1", "timeline_id": "t1", "display": {"limit": 200}})],
        setup=_setup,
    )

    def fake_event_query(query):
        rows = [{"event_id": f"e{i}", "message": "ssh"} for i in range(query.limit)]
        return EventPage(total=14203, offset=0, limit=query.limit, events=rows)

    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_event_query=fake_event_query,
        resolve_scope=lambda case_id, timeline_id: (["src1"], None, None),
        now=lambda: FROZEN_NOW,
    )
    blk = snapshot["blocks"][0]
    assert blk["resolution"]["error"] is None
    assert blk["data"]["rows_included"] == 200
    assert blk["data"]["row_count_total"] == 14203
    assert blk["data"]["truncated"] is True
    assert blk["ref"]["name"] == "SSH hits"


async def test_view_block_query_carries_the_timeline_field_mappings(store):
    """A frozen view resolves through the same canonical fields the analyst saw
    on screen — both for filtering and for the values the rows carry
    (db/field_mappings.py::project_mapped_fields runs off this)."""

    async def _setup(case):
        await store.create_view(case.id, "v1", "IPs", query=None, view_filter={})

    case, story, blocks = await _case_with_story(
        store,
        [("view_ref", {"view_id": "v1", "timeline_id": "t1", "display": {"limit": 10}})],
        setup=_setup,
    )
    mappings = {"ip_address": ["src_ip", "ip_addr"]}
    seen = {}

    def fake_event_query(query):
        seen["field_mappings"] = query.field_mappings
        return EventPage(total=0, offset=0, limit=query.limit, events=[])

    await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_event_query=fake_event_query,
        resolve_scope=lambda case_id, timeline_id: (["src1"], mappings, None),
        now=lambda: FROZEN_NOW,
    )
    assert seen["field_mappings"] == mappings


async def test_chart_block_freezes_execution_result(store):
    case, story, blocks = await _case_with_story(
        store, [("chart_ref", {"chart_id": "ch1", "timeline_id": "t1"})]
    )
    # The real stored shape: the frontend's camelCase ChartConfig, verbatim.
    await store.create_saved_chart(
        case.id,
        "t1",
        "ch1",
        "Top ports",
        {"v": 1, "chartType": "bar", "scale": "nominal", "field": "port", "options": {}},
    )

    async def fake_chart(scope, spec):
        assert spec.chart_type == "bar"
        return {
            "ok": True,
            "resolved": {"chart_type": "bar"},
            "warnings": ["clamped"],
            "summary": {"total": 10},
            "result": {"values": [{"value": "22", "count": 10}], "total": 10},
        }

    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_chart=fake_chart,
        resolve_scope=lambda case_id, timeline_id: (["src1"], None, None),
        now=lambda: FROZEN_NOW,
    )
    blk = snapshot["blocks"][0]
    assert blk["resolution"]["error"] is None
    assert blk["data"]["name"] == "Top ports"
    assert blk["data"]["chart"]["total"] == 10
    assert blk["data"]["resolved"] == {"chart_type": "bar"}
    assert blk["data"]["warnings"] == ["clamped"]


async def test_chart_block_queues_for_the_lane_rather_than_failing(store):
    """An export has no spinner, no request to answer 503 to and no retry.

    The bounded foreground wait exists for a chart an analyst is watching;
    applied to a job it turns "the lane was busy for 30s" into a failed block
    in an attested report (#305).
    """
    from vestigo.db._scan import foreground_wait_seconds

    case, story, blocks = await _case_with_story(
        store, [("chart_ref", {"chart_id": "ch1", "timeline_id": "t1"})]
    )
    await store.create_saved_chart(
        case.id,
        "t1",
        "ch1",
        "Top ports",
        {"v": 1, "chartType": "bar", "scale": "nominal", "field": "port", "options": {}},
    )
    seen: dict[str, object] = {}

    async def fake_chart(scope, spec):
        seen["wait"] = foreground_wait_seconds(30.0)
        return {"ok": True, "resolved": {}, "warnings": [], "summary": {}, "result": {}}

    await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_chart=fake_chart,
        resolve_scope=lambda case_id, timeline_id: (["src1"], None, None),
        now=lambda: FROZEN_NOW,
    )
    assert seen["wait"] is None, "the export queued instead of giving up"


async def test_event_block_and_failing_block_isolation(store):
    case, story, blocks = await _case_with_story(
        store,
        [
            ("event_ref", {"event_id": "e1", "source_id": "s1", "caption": "the pivot"}),
            ("event_ref", {"event_id": "gone", "source_id": "s1", "caption": None}),
        ],
    )

    def fake_event_query(query):
        if query.event_ids == ["e1"]:
            return EventPage(
                total=1, offset=0, limit=1, events=[{"event_id": "e1", "message": "boom"}]
            )
        return EventPage(total=0, offset=0, limit=1, events=[])

    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_event_query=fake_event_query,
        now=lambda: FROZEN_NOW,
    )
    ok, missing = snapshot["blocks"]
    assert ok["data"]["event"]["message"] == "boom"
    assert missing["data"] is None
    assert "not found" in missing["resolution"]["error"]


def test_view_filter_to_spec_maps_payload():
    view = View(
        id="v",
        case_id="c",
        name="n",
        query="ssh",
        view_filter={
            "q": "ssh",
            "qRegex": True,
            "artifact": "syslog",
            "artifacts": [],
            "sourceId": "s1",
            "tagsInclude": ["lateral"],
            "tagsExclude": [],
            "filters": {"src_ip": ["1.2.3.4"], "empty": []},
            "filterModes": {"src_ip": "exact"},
            "start": "2026-01-01T00:00:00Z",
            "annotated": [],
            "annotationTagValue": None,
        },
    )
    spec = _view_filter_to_spec(view)
    assert spec.q == "ssh" and spec.q_regex is True
    assert spec.artifacts == ["syslog"]
    assert spec.source_id == "s1"
    assert spec.tags_include == ["lateral"]
    assert spec.tags_exclude is None
    assert spec.filters == {"src_ip": ["1.2.3.4"]}
    assert spec.filter_modes == {"src_ip": "exact"}
    assert spec.start is not None and spec.start.year == 2026
    assert spec.annotated is None


async def test_snapshot_hash_stable(store):
    case, story, blocks = await _case_with_story(store, [("markdown", {"text": "same"})])
    kwargs = {"user": _user(), "store": store, "now": lambda: FROZEN_NOW}
    a = await resolve_story_snapshot(story, blocks, **kwargs)
    b = await resolve_story_snapshot(story, blocks, **kwargs)
    assert canonical_hash(a) == canonical_hash(b)


async def test_export_store_seal_once(store):
    await store.init_schema()
    exp = await store.create_story_export("e1", "s1", "c1", {"v": 1}, "ab" * 32, user="alice")
    assert exp.html is None
    listed = await store.list_story_exports("s1")
    assert [e.id for e in listed] == ["e1"]
    assert listed[0].to_dict()["has_artifact"] is False

    sealed = await store.seal_story_export_artifact("e1", "<html>", "cd" * 32)
    assert sealed.html_hash == "cd" * 32
    assert await store.seal_story_export_artifact("e1", "<html>2", "ee" * 32) is None
    assert await store.seal_story_export_artifact("ghost", "x", "ff" * 32) is None

    got = await store.get_story_export("c1", "e1")
    assert got.html == "<html>"
    assert await store.delete_story_export("e1") is True
    assert await store.delete_story_export("e1") is False


async def test_snapshot_coerces_non_json_query_values(store):
    """ClickHouse hands back FixedString columns as raw bytes (content_hash,
    file_hash, embedding_config_hash) and can carry datetimes — a snapshot is
    stored *and hashed* as JSON, so those have to round-trip."""
    import json

    case, story, blocks = await _case_with_story(
        store, [("event_ref", {"event_id": "e1", "source_id": "s1", "caption": None})]
    )

    def fake_event_query(query):
        return EventPage(
            total=1,
            offset=0,
            limit=1,
            events=[
                {
                    "event_id": "e1",
                    "message": "boom",
                    "content_hash": b"70e7de49",
                    "file_hash": b"\xff\xfe not utf-8",
                    "ingest_time": datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
                }
            ],
        )

    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_event_query=fake_event_query,
        now=lambda: FROZEN_NOW,
    )
    event = snapshot["blocks"][0]["data"]["event"]
    assert event["content_hash"] == "70e7de49"
    assert event["file_hash"] == b"\xff\xfe not utf-8".hex()
    assert event["ingest_time"] == "2026-07-26T12:00:00+00:00"
    # The whole bundle must survive the exact serialization the hash uses.
    assert canonical_hash(snapshot)
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_stored_chart_config_translates_to_spec():
    """`SavedChart.config` is the frontend's camelCase ChartConfig round-tripped
    verbatim; the agent's ChartSpec is snake_case. Executing a saved chart
    server-side crosses that boundary exactly once, here."""
    from vestigo.stories.export import _stored_chart_to_spec

    spec = _stored_chart_to_spec(
        {
            "v": 1,
            "chartType": "histogram",
            "scale": "ratio",
            "field": "duration",
            "fieldY": None,
            "fields": None,
            "metric": "count",
            "compare": {"mode": "custom", "filters": {"q": "ssh", "filters": {}}},
            "options": {"bins": 20, "logScale": True, "showPoints": False},
        }
    )
    assert spec.chart_type == "histogram"
    assert spec.scale == "ratio"
    assert spec.field == "duration"
    assert spec.options.bins == 20
    assert spec.options.log_scale is True
    assert spec.compare.mode == "custom"
    assert spec.compare.filters.q == "ssh"


def test_stored_chart_config_minimal_and_compare_off():
    from vestigo.stories.export import _stored_chart_to_spec

    spec = _stored_chart_to_spec(
        {
            "v": 1,
            "chartType": "time",
            "scale": "nominal",
            "metric": "count",
            "field": None,
            "fieldY": None,
            "fields": None,
            "compare": {"mode": "off"},
            "options": {},
        }
    )
    assert spec.chart_type == "time"
    assert spec.field is None
    assert spec.compare.mode == "off"


def test_chart_spec_and_stored_config_round_trip():
    """The two translators must be exact inverses.

    They live on opposite sides of the same boundary — the agent writes a
    SavedChart from a ChartSpec, the export resolver reads one back into a
    ChartSpec. A mismatch produces a chart that is silently undrawable in the
    export, the story editor and the Visualize rail, with no error at write
    time. This is the assertion that would have caught that.
    """
    from vestigo.agent.tools import ChartSpec
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    for payload in (
        {"chart_type": "bar", "field": "port", "metric": "count"},
        {
            "chart_type": "histogram",
            "scale": "ratio",
            "field": "duration",
            "metric": "count",
            "options": {"bins": 20, "log_scale": True},
        },
        {
            "chart_type": "time",
            "metric": "count",
            "options": {"buckets": 40},
            "compare": {"mode": "baseline"},
        },
        {
            "chart_type": "bar",
            "field": "user",
            "metric": "count",
            "compare": {"mode": "custom", "filters": {"q": "ssh", "tags_include": ["susp"]}},
        },
        {
            "chart_type": "scatter",
            "scale": "ratio",
            "field": "bytes_in",
            "field_y": "bytes_out",
            "metric": "count",
            "options": {"sample_limit": 900},
        },
        # The primary filter layer: a chart is the slice it was built over.
        {
            "chart_type": "bar",
            "field": "user",
            "metric": "count",
            "filters": {
                "q": "logon",
                "exclusions": {"user": ["svc_backup", "svc_scan"]},
                "tags_exclude": ["known-good"],
                "collapse_routine": True,
            },
        },
    ):
        spec = ChartSpec.model_validate(payload)
        config = spec_to_stored_chart_config(spec)
        assert config["v"] == 2, config
        assert _stored_chart_to_spec(config) == spec, config


def test_saved_chart_carries_its_filters_into_the_spec():
    """A saved chart is the *slice* it was built over, not just its shape.

    The filters an analyst had active when they saved the chart are stored
    beside the ChartConfig keys; resolving one back has to reapply them, or a
    story block and its export redraw the chart over the whole timeline and
    show exactly the data the analyst filtered out.
    """
    from vestigo.stories.export import _stored_chart_to_spec

    spec = _stored_chart_to_spec(
        {
            "v": 1,
            "chartType": "bar",
            "scale": "nominal",
            "field": "user",
            "metric": "count",
            "compare": {"mode": "off"},
            "options": {},
            "filters": {
                "q": "logon",
                "exclusions": {"user": ["svc_backup"]},
                "tagsExclude": ["known-good"],
                "start": "2026-01-01T00:00:00+00:00",
            },
        }
    )
    assert spec.filters is not None
    assert spec.filters.q == "logon"
    assert spec.filters.exclusions == {"user": ["svc_backup"]}
    assert spec.filters.tags_exclude == ["known-good"]
    assert spec.filters.start is not None


def test_saved_chart_without_filters_key_resolves_unfiltered():
    """Charts saved before filters were captured must keep drawing.

    Their config has no ``filters`` key, and the absence has to mean "whole
    timeline" — the behavior they have always had — not an error.
    """
    from vestigo.stories.export import _stored_chart_to_spec

    spec = _stored_chart_to_spec(
        {
            "v": 1,
            "chartType": "bar",
            "scale": "nominal",
            "field": "user",
            "metric": "count",
            "compare": {"mode": "off"},
            "options": {},
        }
    )
    assert spec.filters is None


def test_json_safe_coerces_non_finite_floats_and_sets():
    """NaN/Infinity are not JSON, and set order is not stable across processes."""
    import json
    import math

    from vestigo.stories.export import _json_safe

    coerced = _json_safe(
        {"nan": float("nan"), "inf": float("inf"), "ninf": -math.inf, "tags": {"b", "a"}}
    )
    assert coerced["nan"] is None
    assert coerced["inf"] is None
    assert coerced["ninf"] is None
    assert coerced["tags"] == ["a", "b"]
    # ``allow_nan=False`` is what the hash uses; bare NaN would slip past a
    # default json.dumps and produce bytes no conforming parser accepts.
    assert json.dumps(coerced, allow_nan=False)
    assert canonical_hash(coerced)


async def test_export_runs_charts_under_analyst_limits(store, monkeypatch):
    """A frozen chart must show what the analyst's card showed.

    The agent's context-budget caps would clamp a top-50 bar chart to top-30
    and stamp agent-facing wording ("agent context budget") into the report.
    ``execute_chart_spec`` itself is patched rather than passing ``run_chart``,
    because the default wiring is exactly what's under test.
    """
    from vestigo.agent import chart_exec

    case, story, blocks = await _case_with_story(
        store, [("chart_ref", {"chart_id": "ch1", "timeline_id": "t1"})]
    )
    await store.create_saved_chart(
        case.id,
        "t1",
        "ch1",
        "Top ports",
        {"v": 1, "chartType": "bar", "field": "port", "metric": "count", "options": {"topN": 50}},
    )

    seen: dict[str, object] = {}

    async def fake_execute(scope, spec, **kwargs):
        seen["limits"] = kwargs.get("limits")
        return {"resolved": {}, "warnings": [], "result": {"values": []}}

    monkeypatch.setattr(chart_exec, "execute_chart_spec", fake_execute)

    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        resolve_scope=lambda case_id, timeline_id: ([], {}, {}),
        now=lambda: FROZEN_NOW,
    )
    assert snapshot["blocks"][0]["resolution"]["error"] is None
    assert seen["limits"] is chart_exec.ANALYST_CHART_LIMITS
    # 50 is inside the analyst ceiling and outside the agent's.
    assert chart_exec.ANALYST_CHART_LIMITS.terms_top_n[1] >= 50
    assert chart_exec.AGENT_CHART_LIMITS.terms_top_n[1] < 50


async def test_analyst_limits_do_not_clamp_a_normal_saved_chart():
    """Through the real executor: a top-50 saved chart stays top-50.

    Under the agent's caps the same spec resolves to 30 and picks up a clamp
    warning worded for the model — both of which would end up in the report.
    """
    from vestigo.agent.chart_exec import (
        AGENT_CHART_LIMITS,
        ANALYST_CHART_LIMITS,
        execute_chart_spec,
    )
    from vestigo.db.postgres import User as _User
    from vestigo.stories.export import _stored_chart_to_spec

    from vestigo.agent.tools import AgentScope  # isort: skip

    spec = _stored_chart_to_spec(
        {"v": 1, "chartType": "bar", "field": "port", "metric": "count", "options": {"topN": 50}}
    )
    scope = AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=_User(id="u1", username="alice", is_admin=True, is_active=True),
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )

    captured: dict[str, int] = {}

    class _Service:
        def field_terms(self, query, field, top_n):
            captured["top_n"] = top_n
            return {"total": 0, "distinct": 0, "values": []}

    async def _no_check(token, label):
        return None

    async def _run(limits):
        return await execute_chart_spec(
            scope,
            spec,
            service=_Service(),
            validated=lambda f: f,
            check_field=_no_check,
            limits=limits,
        )

    analyst = await _run(ANALYST_CHART_LIMITS)
    assert captured["top_n"] == 50
    assert analyst["warnings"] == []

    agent = await _run(AGENT_CHART_LIMITS)
    assert captured["top_n"] == 30
    assert any("agent context budget" in w for w in agent["warnings"])


async def test_terms_top_n_narrows_for_the_marks_bounded_by_legibility():
    """A pie is capped where the *mark* stops working, not where the endpoint does.

    ``field_terms`` backs bar, pie and waffle alike, so ``terms_top_n``'s 500
    would let an agent-proposed or exported pie carry ten times what the
    Visualize page clamps the same config to — a snapshot the analyst cannot
    reproduce or edit back to what it shows.
    """
    from vestigo.agent.chart_exec import ANALYST_CHART_LIMITS, execute_chart_spec
    from vestigo.db.postgres import User as _User
    from vestigo.stories.export import _stored_chart_to_spec

    from vestigo.agent.tools import AgentScope  # isort: skip

    scope = AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=_User(id="u1", username="alice", is_admin=True, is_active=True),
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )
    captured: dict[str, int] = {}

    class _Service:
        def field_terms(self, query, field, top_n):
            captured["top_n"] = top_n
            return {"total": 0, "distinct": 0, "values": [], "other_count": 0}

    async def _no_check(token, label):
        return None

    async def _run(chart_type: str):
        spec = _stored_chart_to_spec(
            {
                "v": 1,
                "chartType": chart_type,
                "field": "port",
                "metric": "count",
                "options": {"topN": 400},
            }
        )
        return await execute_chart_spec(
            scope,
            spec,
            service=_Service(),
            validated=lambda f: f,
            check_field=_no_check,
            limits=ANALYST_CHART_LIMITS,
        )

    bar = await _run("bar")
    assert captured["top_n"] == 400
    assert bar["warnings"] == []

    for mark in ("pie", "waffle"):
        result = await _run(mark)
        assert captured["top_n"] == 50, mark
        assert any("clamped to 50" in w for w in result["warnings"]), mark


def test_spec_to_stored_chart_config_writes_the_current_version() -> None:
    from vestigo.agent.tools import ChartSpec
    from vestigo.stories.export import CHART_CONFIG_VERSION, spec_to_stored_chart_config

    stored = spec_to_stored_chart_config(ChartSpec(chart_type="bar", field="artifact"))
    assert stored["v"] == CHART_CONFIG_VERSION == 2


# ── derivations ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "spec_kind"),
    [
        ({"kind": "bins", "mode": "log", "count": 8}, "bins"),
        ({"kind": "bins", "mode": "custom", "edges": [0, 1024]}, "bins"),
        ({"kind": "timePart", "part": "weekday"}, "time_part"),
    ],
)
def test_derive_round_trips_between_stored_config_and_spec(stored, spec_kind) -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    config = {
        "v": 2,
        "chartType": "bar",
        "scale": "ordinal",
        "field": "attr:bytes",
        "options": {},
        "derive": stored,
    }
    spec = _stored_chart_to_spec(config)
    assert spec.derive is not None and spec.derive.kind == spec_kind
    back = spec_to_stored_chart_config(spec)
    assert back["derive"] == stored


def test_stored_config_without_derive_yields_none() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    spec = _stored_chart_to_spec(
        {"v": 2, "chartType": "bar", "scale": "nominal", "field": "x", "options": {}}
    )
    assert spec.derive is None
    assert "derive" not in spec_to_stored_chart_config(spec)


def test_table_inputs_and_options_round_trip_between_stored_config_and_spec() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    config = {
        "v": 2,
        "chartType": "table",
        "scale": "nominal",
        "field": "attr:user",
        "fieldY": "attr:host",
        "options": {
            "topN": 20,
            "tableSortBy": "last_seen",
            "tableSortDir": "asc",
            "highlight": ["alice"],
        },
        "inputs": {"columns": ["count", "share", "distinct_second"]},
    }
    spec = _stored_chart_to_spec(config)
    assert spec.inputs is not None and spec.inputs.columns == ["count", "share", "distinct_second"]
    assert spec.options.table_sort_by == "last_seen" and spec.options.highlight == ["alice"]
    back = spec_to_stored_chart_config(spec)
    assert back["inputs"] == {"columns": ["count", "share", "distinct_second"]}
    assert back["options"] == config["options"]


def test_stored_config_without_inputs_yields_none() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    spec = _stored_chart_to_spec(
        {"v": 2, "chartType": "bar", "scale": "nominal", "field": "x", "options": {}}
    )
    assert spec.inputs is None
    assert "inputs" not in spec_to_stored_chart_config(spec)


def test_marks_round_trip_between_stored_config_and_spec() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    config = {
        "v": 2,
        "chartType": "time",
        "scale": "interval",
        "field": None,
        "options": {},
        "marks": [
            {
                "kind": "events",
                "filters": {"tagsInclude": ["exfil"], "eventIds": ["e1"]},
                "label": "tagged exfil",
            },
            {"kind": "baseline", "definitionId": "bd1"},
            {"kind": "view", "viewId": "v1"},
            {"kind": "instant", "at": "2026-03-13T09:41:00+00:00", "label": "first beacon"},
            {
                "kind": "range",
                "start": "2026-03-13T09:00:00+00:00",
                "end": "2026-03-13T10:00:00+00:00",
                "label": "w",
            },
        ],
    }
    spec = _stored_chart_to_spec(config)
    assert [m.kind for m in spec.marks] == ["events", "baseline", "view", "instant", "range"]
    assert spec.marks[0].filters.tags_include == ["exfil"] and spec.marks[0].filters.event_ids == [
        "e1"
    ]
    assert spec.marks[1].definition_id == "bd1" and spec.marks[2].view_id == "v1"
    assert spec.marks[3].at.isoformat() == "2026-03-13T09:41:00+00:00"
    back = spec_to_stored_chart_config(spec)
    assert back["marks"] == config["marks"]


def test_stored_config_without_marks_yields_none() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    spec = _stored_chart_to_spec(
        {"v": 2, "chartType": "time", "scale": "interval", "field": None, "options": {}}
    )
    assert spec.marks is None
    assert "marks" not in spec_to_stored_chart_config(spec)


async def test_chart_block_freezes_resolved_marks_beside_the_aggregation(store):
    case, story, blocks = await _case_with_story(
        store, [("chart_ref", {"chart_id": "ch1", "timeline_id": "t1"})]
    )
    await store.create_saved_chart(
        case.id,
        "t1",
        "ch1",
        "Beacons over time",
        {
            "v": 2,
            "chartType": "time",
            "scale": "interval",
            "field": None,
            "options": {},
            "marks": [{"kind": "instant", "at": "2026-07-20T09:41:00+00:00", "label": "first"}],
        },
    )

    async def fake_chart(scope, spec):
        assert spec.marks and spec.marks[0].kind == "instant"
        return {
            "ok": True,
            "resolved": {"chart_type": "time"},
            "warnings": [],
            "summary": {},
            "result": {"buckets": []},
            "marks": {
                "marks": [
                    {
                        "kind": "instant",
                        "at": "2026-07-20T09:41:00+00:00",
                        "label": "first",
                        "source": 0,
                        "provenance": {"kind": "analyst"},
                    }
                ],
                "sources": [],
                "cap": 50,
            },
        }

    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=_user(),
        store=store,
        run_chart=fake_chart,
        resolve_scope=lambda case_id, timeline_id: (["src1"], None, None),
        now=lambda: FROZEN_NOW,
    )
    blk = snapshot["blocks"][0]
    assert blk["data"]["marks"]["marks"][0]["label"] == "first"


def test_lanes_inputs_round_trip_between_stored_config_and_spec() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    config = {
        "v": 2,
        "chartType": "lanes",
        "scale": "nominal",
        "field": "attr:host",
        "options": {"limitY": 12},
        "inputs": {
            "pairing": "nextEnd",
            "startFilter": {"filters": {"attr:kind": ["logon"]}},
            "endFilter": {"q": "logoff", "filters": {"attr:kind": ["logoff"]}},
        },
    }
    spec = _stored_chart_to_spec(config)
    assert spec.inputs is not None and spec.inputs.pairing == "next_end"
    assert spec.inputs.start_filter is not None
    assert spec.inputs.start_filter.filters == {"attr:kind": ["logon"]}
    assert spec.inputs.end_filter is not None and spec.inputs.end_filter.q == "logoff"
    assert spec.options.limit_y == 12
    back = spec_to_stored_chart_config(spec)
    assert back["inputs"] == config["inputs"]
    assert back["options"] == {"limitY": 12}


def test_first_last_lanes_config_carries_only_the_pairing() -> None:
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    spec = _stored_chart_to_spec(
        {
            "v": 2,
            "chartType": "lanes",
            "scale": "nominal",
            "field": "attr:host",
            "inputs": {"pairing": "firstLast"},
        }
    )
    assert spec.inputs is not None and spec.inputs.pairing == "first_last"
    assert spec.inputs.start_filter is None and spec.inputs.end_filter is None
    assert spec_to_stored_chart_config(spec)["inputs"] == {"pairing": "firstLast"}


def test_an_empty_table_column_set_round_trips_through_the_spec() -> None:
    """`columns: []` is the value-only table the page can produce — dropping
    it re-instated every default column on export."""
    from vestigo.stories.export import _stored_chart_to_spec, spec_to_stored_chart_config

    config = {
        "v": 2,
        "chartType": "table",
        "scale": "nominal",
        "field": "artifact",
        "options": {},
        "inputs": {"columns": []},
    }
    spec = _stored_chart_to_spec(config)
    assert spec.inputs is not None and spec.inputs.columns == []
    assert spec_to_stored_chart_config(spec)["inputs"] == {"columns": []}
