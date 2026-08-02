"""Building the demo case end to end against live ClickHouse.

Skipped (visibly, via the clickhouse marker) when the dev compose stack is
absent — the same pattern as ``tests/test_transfer_roundtrip_clickhouse.py``.
Uses the SQLite ``store`` fixture for Postgres.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.demo import metadata, scenario
from vestigo.demo.build import build_demo_case

pytestmark = pytest.mark.clickhouse


@pytest.fixture(scope="module")
def ch_store():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse unavailable")
    return store


@pytest_asyncio.fixture()
async def built(store, ch_store):
    """Seed one demo case and clean its partitions up afterwards."""
    await store.init_schema()
    owner = await store.create_user(user_id="u_demo_build", username="demo-build")
    result = await build_demo_case(store, ch_store, owner_id=owner.id)
    yield result
    for source in await store.list_sources(result.case_id):
        ch_store.delete_source_events(result.case_id, source.id)


@pytest.mark.asyncio
async def test_demo_case_has_every_artifact(built, store):
    assert 240_000 < built.events < 260_000
    assert built.sources == 4
    assert built.annotations == len(metadata.ANNOTATIONS)

    case_id = built.case_id
    assert len(await store.list_sources(case_id)) == 4
    # Three named timelines plus the default "all sources" one every case gets.
    assert len(await store.list_timelines(case_id)) == 4
    assert len(await store.list_views(case_id)) == len(metadata.VIEWS)
    assert len(await store.list_sigma_rules(case_id)) == len(metadata.SIGMA_RULES)
    assert len(await store.list_stories(case_id)) == 1


@pytest.mark.asyncio
async def test_baseline_declares_the_four_suspect_windows(built, store):
    timelines = await store.list_timelines(built.case_id)
    full = next(t for t in timelines if t.name == "Full incident")
    definitions = await store.list_baseline_definitions(built.case_id, full.id)
    assert len(definitions) == 1
    definition = definitions[0]
    # SQLite drops tzinfo on the way back out; compare the instants, not the
    # awareness (Postgres keeps it — this is a test-store artifact).
    assert definition.baseline_start.replace(tzinfo=None) == scenario.SCENARIO_START.replace(
        tzinfo=None
    )
    assert definition.baseline_end.replace(tzinfo=None) == scenario.BASELINE_END.replace(
        tzinfo=None
    )
    assert [w["label"] for w in definition.suspect_windows] == [
        phase.label for phase in scenario.PHASES
    ]


@pytest.mark.asyncio
async def test_notes_and_tags_land_on_real_events(built, store, ch_store):
    source_ids = [source.id for source in await store.list_sources(built.case_id)]
    annotations = await store.list_source_annotations(built.case_id, source_ids)
    comments = [a for a in annotations if a.annotation_type == "comment"]
    tags = [a for a in annotations if a.annotation_type == "tag"]
    assert len(comments) == len(metadata.ANNOTATIONS)
    assert tags
    assert {t.content for t in tags} == set(metadata.TAGS)

    # Every annotated event id must actually exist in ClickHouse: a note
    # hanging off nothing is the failure mode this whole resolution step exists
    # to prevent.
    event_ids = [a.event_id for a in comments]
    rows = ch_store.client.query(
        f"SELECT count() FROM {ch_store.database}.events"
        " WHERE case_id = %(case_id)s AND toString(event_id) IN %(ids)s",
        parameters={"case_id": built.case_id, "ids": event_ids},
    ).result_rows
    assert rows[0][0] == len(set(event_ids))


@pytest.mark.asyncio
async def test_story_embeds_resolve_into_a_snapshot(built, store, ch_store):
    """The demo story must export cleanly, every block kind included.

    A ``chart_ref`` or ``view_ref`` whose referent is wrong does not fail the
    seed loudly — it freezes as a ``resolution.error`` in the report. This is
    the check that keeps the shipped example from teaching that failure.
    """
    from vestigo.db.queries import EventQueryService
    from vestigo.stories.export import resolve_story_snapshot

    case_id = built.case_id
    story = (await store.list_stories(case_id))[0]
    blocks = await store.list_story_blocks(story.id)
    kinds = {block.kind for block in blocks}
    assert kinds == {"markdown", "view_ref", "chart_ref", "event_ref"}

    charts_by_timeline = {}
    for timeline in await store.list_timelines(case_id):
        charts_by_timeline[timeline.id] = await store.list_saved_charts(case_id, timeline.id)
    assert sum(len(v) for v in charts_by_timeline.values()) == len(metadata.CHARTS)

    sources_by_timeline = {
        timeline.id: [s.id for s in await store.list_timeline_sources(case_id, timeline.id)]
        for timeline in await store.list_timelines(case_id)
    }
    service = EventQueryService(store=ch_store)
    user = await store.get_user("u_demo_build")
    snapshot = await resolve_story_snapshot(
        story,
        blocks,
        user=user,
        store=store,
        run_event_query=service.query,
        resolve_scope=lambda cid, tid: (sources_by_timeline[tid], None, None),
    )
    errors = {
        (b["kind"], b["resolution"]["error"])
        for b in snapshot["blocks"]
        if b["resolution"]["error"]
    }
    assert not errors
    for block in snapshot["blocks"]:
        if block["kind"] == "event_ref":
            assert block["data"]["event"]
            assert block["data"]["caption"]
        if block["kind"] == "view_ref":
            assert block["data"]["rows"]
        if block["kind"] == "chart_ref":
            assert block["data"]["chart"]


@pytest.mark.asyncio
async def test_two_seeds_do_not_collide(store, ch_store, built):
    """Every user gets their own copy, so ids must not be shared."""
    other = await store.create_user(user_id="u_demo_build2", username="demo-build-2")
    second = await build_demo_case(store, ch_store, owner_id=other.id)
    try:
        assert second.case_id != built.case_id
        first_sources = [s.id for s in await store.list_sources(built.case_id)]
        second_sources = [s.id for s in await store.list_sources(second.case_id)]
        first_ids = {
            a.id for a in await store.list_source_annotations(built.case_id, first_sources)
        }
        second_ids = {
            a.id for a in await store.list_source_annotations(second.case_id, second_sources)
        }
        assert not (first_ids & second_ids)
    finally:
        for source in await store.list_sources(second.case_id):
            ch_store.delete_source_events(second.case_id, source.id)
