"""Story/StoryBlock/StoryExport store-layer tests (SQLite)."""

import pytest

from vestigo.db.postgres import STORY_POSITION_GAP, StaleBlockError


async def _story(store):
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    return await store.create_story(case.id, "s1", "T", None, user="alice")


async def test_blocks_append_with_gap_positions(store):
    story = await _story(store)
    b1 = await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    b2 = await store.create_story_block(story.id, "b2", "markdown", {"text": "b"}, user="alice")
    assert b1.position == STORY_POSITION_GAP
    assert b2.position == 2 * STORY_POSITION_GAP
    assert [b.id for b in await store.list_story_blocks(story.id)] == ["b1", "b2"]


async def test_insert_between_takes_midpoint(store):
    story = await _story(store)
    await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    await store.create_story_block(story.id, "b2", "markdown", {"text": "b"}, user="alice")
    b3 = await store.create_story_block(
        story.id, "b3", "markdown", {"text": "mid"}, user="alice", after_block_id="b1"
    )
    assert STORY_POSITION_GAP < b3.position < 2 * STORY_POSITION_GAP
    assert [b.id for b in await store.list_story_blocks(story.id)] == ["b1", "b3", "b2"]


async def test_unknown_after_block_rejected(store):
    story = await _story(store)
    with pytest.raises(ValueError):
        await store.create_story_block(
            story.id, "b1", "markdown", {"text": "a"}, user="alice", after_block_id="ghost"
        )


async def test_exhausted_gap_triggers_renumber(store):
    story = await _story(store)
    await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    await store.create_story_block(story.id, "b2", "markdown", {"text": "b"}, user="alice")
    # Squeeze repeatedly into the same gap until midpoints run out.
    for i in range(12):
        await store.create_story_block(
            story.id, f"mid{i}", "markdown", {"text": str(i)}, user="alice", after_block_id="b1"
        )
    blocks = await store.list_story_blocks(story.id)
    # Order intact: b1 first, b2 last, newest insert directly after b1.
    assert blocks[0].id == "b1"
    assert blocks[-1].id == "b2"
    assert blocks[1].id == "mid11"
    # Positions strictly increasing (renumber preserved uniqueness).
    positions = [b.position for b in blocks]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)


async def test_update_block_bumps_version_and_rejects_stale(store):
    story = await _story(store)
    b = await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    updated = await store.update_story_block(b.id, {"text": "b"}, expected_version=1, user="bob")
    assert updated.version == 2
    assert updated.updated_by == "bob"
    with pytest.raises(StaleBlockError) as exc:
        await store.update_story_block(b.id, {"text": "c"}, expected_version=1, user="carol")
    assert exc.value.current.version == 2
    assert exc.value.current.content == {"text": "b"}
    assert await store.update_story_block("ghost", {}, expected_version=1, user="x") is None


async def test_move_block(store):
    story = await _story(store)
    for bid in ("b1", "b2", "b3"):
        await store.create_story_block(story.id, bid, "markdown", {"text": bid}, user="alice")
    moved = await store.move_story_block(
        "b3", after_block_id=None, expected_version=1, user="alice"
    )
    assert moved.version == 2
    assert [b.id for b in await store.list_story_blocks(story.id)] == ["b3", "b1", "b2"]
    await store.move_story_block("b3", after_block_id="b2", expected_version=2, user="alice")
    assert [b.id for b in await store.list_story_blocks(story.id)] == ["b1", "b2", "b3"]


async def test_move_block_stale_version(store):
    story = await _story(store)
    for bid in ("b1", "b2"):
        await store.create_story_block(story.id, bid, "markdown", {"text": bid}, user="alice")
    with pytest.raises(StaleBlockError):
        await store.move_story_block("b2", after_block_id=None, expected_version=9, user="alice")


async def test_delete_block(store):
    story = await _story(store)
    await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    assert await store.delete_story_block("b1") is True
    assert await store.delete_story_block("b1") is False


async def test_story_crud_roundtrip(store):
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    story = await store.create_story(case.id, "s1", "Intrusion report", None, user="alice")
    assert story.title == "Intrusion report"
    assert story.created_by == "alice"

    listed = await store.list_stories(case.id)
    assert [s.id for s in listed] == ["s1"]

    updated = await store.update_story(case.id, "s1", title="Final report", user="bob")
    assert updated.title == "Final report"
    assert updated.updated_by == "bob"

    assert await store.delete_story(case.id, "s1") is True
    assert await store.get_story(case.id, "s1") is None
    assert await store.delete_story(case.id, "s1") is False


async def test_story_to_dict_shape(store):
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    story = await store.create_story(case.id, "s1", "T", "desc", user="alice")
    d = story.to_dict()
    assert d["id"] == "s1"
    assert d["case_id"] == case.id
    assert d["description"] == "desc"
    assert "created_at" in d and "updated_at" in d
