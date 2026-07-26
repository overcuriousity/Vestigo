"""Story/StoryBlock/StoryExport store-layer tests (SQLite)."""

import asyncio

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


async def test_concurrent_updates_lose_exactly_one(store):
    """Two writers at the same version: one wins, the other gets a 409.

    A sequential test passes against a read-then-write version check too, so
    it cannot tell a correct implementation from a lost update. This one can:
    both calls start from ``version=1``.
    """
    story = await _story(store)
    b = await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")

    results = await asyncio.gather(
        store.update_story_block(b.id, {"text": "alice"}, expected_version=1, user="alice"),
        store.update_story_block(b.id, {"text": "bob"}, expected_version=1, user="bob"),
        return_exceptions=True,
    )
    stale = [r for r in results if isinstance(r, StaleBlockError)]
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(stale) == 1, results
    assert len(winners) == 1, results
    assert winners[0].version == 2
    # The loser's edit is nowhere: the winner's text stands, unmerged.
    current = await store.get_story_block(b.id)
    assert current.content == winners[0].content
    assert current.version == 2


async def test_concurrent_appends_do_not_collide(store):
    """Concurrent inserts must not tie for a position — document order is evidence."""
    story = await _story(store)
    await asyncio.gather(
        *(
            store.create_story_block(story.id, f"b{i}", "markdown", {"text": str(i)}, user="alice")
            for i in range(8)
        )
    )
    blocks = await store.list_story_blocks(story.id)
    positions = [b.position for b in blocks]
    assert len(blocks) == 8
    assert len(set(positions)) == 8
    assert positions == sorted(positions)


async def test_artifact_seals_exactly_once_under_concurrency(store):
    """A second upload must never overwrite a sealed forensic record."""
    story = await _story(store)
    await store.create_story_export("e1", story.id, "c1", {"v": 1}, "hash", user="alice")

    results = await asyncio.gather(
        store.seal_story_export_artifact("e1", "<p>alice</p>", "a"),
        store.seal_story_export_artifact("e1", "<p>bob</p>", "b"),
    )
    sealed = [r for r in results if r is not None]
    assert len(sealed) == 1
    stored = await store.get_story_export("c1", "e1")
    assert stored.html == sealed[0].html
    assert stored.html_hash == sealed[0].html_hash


async def test_renumber_leaves_sibling_timestamps_and_versions_alone(store):
    """Renumbering is bookkeeping, not an edit.

    A bumped ``updated_at`` makes an untouched block look edited (with a stale
    ``updated_by``) to a polling client, and a bumped ``version`` would
    manufacture 409s for collaborators who are holding a valid one.
    """
    story = await _story(store)
    await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    await store.create_story_block(story.id, "b2", "markdown", {"text": "b"}, user="alice")
    before = {b.id: (b.updated_at, b.version) for b in await store.list_story_blocks(story.id)}

    for i in range(12):  # squeeze the same gap until a renumber is forced
        await store.create_story_block(
            story.id, f"mid{i}", "markdown", {"text": str(i)}, user="alice", after_block_id="b1"
        )

    after = {b.id: (b.updated_at, b.version) for b in await store.list_story_blocks(story.id)}
    for block_id, stamp in before.items():
        assert after[block_id] == stamp, f"{block_id} was touched by a renumber"


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

    summary = await store.delete_story(case.id, "s1")
    assert summary == {"block_count": 0, "exports": []}
    assert await store.get_story(case.id, "s1") is None
    assert await store.delete_story(case.id, "s1") is None


async def test_update_story_leaves_unsent_fields_alone(store):
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    await store.create_story(case.id, "s1", "T", "the original blurb", user="alice")

    # Title-only patch must not blank the description...
    only_title = await store.update_story(case.id, "s1", title="T2", user="bob")
    assert only_title.description == "the original blurb"
    # ...and an explicit None must be able to clear it.
    cleared = await store.update_story(case.id, "s1", description=None, user="bob")
    assert cleared.description is None
    assert cleared.title == "T2"


async def test_delete_story_reports_what_went_with_it(store):
    """The audit record is the only place a deleted export's hash survives."""
    story = await _story(store)
    await store.create_story_block(story.id, "b1", "markdown", {"text": "a"}, user="alice")
    await store.create_story_export("e1", story.id, "c1", {"v": 1}, "deadbeef", user="alice")
    await store.seal_story_export_artifact("e1", "<p>x</p>", "cafe")

    summary = await store.delete_story("c1", story.id)
    assert summary["block_count"] == 1
    assert summary["exports"] == [{"id": "e1", "snapshot_hash": "deadbeef", "html_hash": "cafe"}]
    assert await store.list_story_exports(story.id) == []


async def test_story_to_dict_shape(store):
    await store.init_schema()
    case = await store.create_case("c1", "Case One")
    story = await store.create_story(case.id, "s1", "T", "desc", user="alice")
    d = story.to_dict()
    assert d["id"] == "s1"
    assert d["case_id"] == case.id
    assert d["description"] == "desc"
    assert "created_at" in d and "updated_at" in d
