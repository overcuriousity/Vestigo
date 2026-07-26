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
    await store.create_saved_chart(
        case.id, "t1", "ch1", "Top ports", {"v": 1, "chart_type": "bar", "field": "port"}
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
