"""Export resolver: per-block freezing, honesty flags, and the export store."""

from __future__ import annotations

from datetime import UTC, datetime

from vestigo.db.postgres import User, View
from vestigo.db.queries import EventPage
from vestigo.stories.export import _view_filter_to_spec, resolve_story_snapshot
from vestigo.stories.schemas import canonical_hash

FROZEN_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _user() -> User:
    return User(id="u1", username="alice", is_admin=True, is_active=True)


async def _case_with_story(store, blocks):
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    story = await store.create_story(case.id, "s1", "Report", None, user="alice")
    created = []
    for i, (kind, content) in enumerate(blocks):
        created.append(
            await store.create_story_block(story.id, f"b{i}", kind, content, user="alice")
        )
    return case, story, created


async def test_markdown_and_dangling_view_resolve(store):
    case, story, blocks = await _case_with_story(
        store,
        [
            ("markdown", {"text": "# findings"}),
            (
                "view_ref",
                {"view_id": "ghost", "timeline_id": "t1", "display": {"limit": 200}},
            ),
        ],
    )
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
    case, story, blocks = await _case_with_story(
        store,
        [("view_ref", {"view_id": "v1", "timeline_id": "t1", "display": {"limit": 200}})],
    )
    await store.create_view(case.id, "v1", "SSH hits", query="ssh", view_filter={"q": "ssh"})

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
