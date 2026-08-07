"""Views outlive their deletion when a story block still embeds them.

Deleting a view a ``view_ref`` block points at would make that story's export
fail (``stories/export.py`` resolves the View live). So a referenced view is
hidden rather than removed, and swept once the last reference goes.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from vestigo.db.postgres import PostgresStore, ReferentGoneError


@pytest_asyncio.fixture()
async def store(tmp_path):
    db_path = tmp_path / "test_view_lifecycle.db"
    s = PostgresStore(url=f"sqlite+aiosqlite:///{db_path}")
    await s.init_schema()
    yield s
    await s.engine.dispose()


async def _case_with_view(store: PostgresStore) -> None:
    await store.create_case("c1", "Case One")
    await store.create_view("c1", "v1", "My View", "", {"filters": {"a": ["b"]}})


async def _story_referencing(store: PostgresStore, view_id: str) -> tuple[str, str]:
    story = await store.create_story("c1", "s1", "Story One", None, "alice")
    block = await store.create_story_block(
        story.id, "b1", "view_ref", {"view_id": view_id}, "alice"
    )
    assert block is not None
    return story.id, block.id


@pytest.mark.asyncio
async def test_unreferenced_view_is_deleted_outright(store):
    await _case_with_view(store)
    assert await store.delete_view("c1", "v1") == "deleted"
    assert await store.get_view("c1", "v1") is None


@pytest.mark.asyncio
async def test_referenced_view_is_hidden_not_removed(store):
    await _case_with_view(store)
    await _story_referencing(store, "v1")
    assert await store.delete_view("c1", "v1") == "hidden"
    # Gone from the analyst's list...
    assert [v.id for v in await store.list_views("c1")] == []
    # ...but still resolvable, which is what keeps the story's export working.
    hidden = await store.get_view("c1", "v1")
    assert hidden is not None
    assert hidden.deleted_at is not None


@pytest.mark.asyncio
async def test_deleting_unknown_view_reports_not_found(store):
    await store.create_case("c1", "Case One")
    assert await store.delete_view("c1", "nope") is None


@pytest.mark.asyncio
async def test_deleting_the_block_purges_the_hidden_view(store):
    await _case_with_view(store)
    _story_id, block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.delete_story_block(block_id)
    assert await store.get_view("c1", "v1") is None


@pytest.mark.asyncio
async def test_deleting_the_story_purges_the_hidden_view(store):
    await _case_with_view(store)
    story_id, _block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.delete_story("c1", story_id)
    assert await store.get_view("c1", "v1") is None


@pytest.mark.asyncio
async def test_repointing_the_block_purges_the_hidden_view(store):
    await _case_with_view(store)
    await store.create_view("c1", "v2", "Other View", "", {})
    _story_id, block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.update_story_block(block_id, {"view_id": "v2"}, 1, "alice")
    assert await store.get_view("c1", "v1") is None
    assert await store.get_view("c1", "v2") is not None


@pytest.mark.asyncio
async def test_purge_leaves_live_and_still_referenced_views_alone(store):
    await _case_with_view(store)
    await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.create_view("c1", "v2", "Live View", "", {})
    assert await store.purge_orphaned_hidden_views("c1") == 0
    assert await store.get_view("c1", "v1") is not None
    assert await store.get_view("c1", "v2") is not None


@pytest.mark.asyncio
async def test_purge_is_idempotent(store):
    await _case_with_view(store)
    _story_id, block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.delete_story_block(block_id)
    assert await store.purge_orphaned_hidden_views("c1") == 0


@pytest.mark.asyncio
async def test_hidden_view_is_scoped_to_its_own_case(store):
    """A block in another case must not keep this case's view alive.

    The referent lock the insert takes is case-scoped like the reference count
    is, so a foreign view is not a view this block can point at — it is refused
    at the write rather than left to keep another case's view alive.
    """
    await _case_with_view(store)
    await store.create_case("c2", "Case Two")
    story = await store.create_story("c2", "s2", "Other Story", None, "alice")
    with pytest.raises(ReferentGoneError):
        await store.create_story_block(story.id, "b2", "view_ref", {"view_id": "v1"}, "alice")
    assert await store.delete_view("c1", "v1") == "deleted"


@pytest.mark.asyncio
async def test_block_cannot_be_created_against_an_already_hidden_view(store):
    """The insert re-checks the referent under its row lock.

    `validate_block_scope` runs in an earlier transaction, so this is the only
    check a concurrent `delete_view` cannot slip past — without it the delete
    could count zero references and hard-delete the view a block committed
    against a moment later, leaving the frozen `resolution.error` in an export
    that hiding exists to prevent.
    """
    await _case_with_view(store)
    _story_id, _block_id = await _story_referencing(store, "v1")
    assert await store.delete_view("c1", "v1") == "hidden"
    story = await store.create_story("c1", "s2", "Second Story", None, "alice")
    with pytest.raises(ReferentGoneError):
        await store.create_story_block(story.id, "b2", "view_ref", {"view_id": "v1"}, "alice")


@pytest.mark.asyncio
async def test_repointing_a_block_onto_a_hidden_view_is_refused(store):
    await _case_with_view(store)
    await store.create_view("c1", "v2", "Other View", "", {})
    _story_id, block_id = await _story_referencing(store, "v1")
    story = await store.create_story("c1", "s2", "Second Story", None, "alice")
    await store.create_story_block(story.id, "b2", "view_ref", {"view_id": "v2"}, "alice")
    assert await store.delete_view("c1", "v2") == "hidden"
    with pytest.raises(ReferentGoneError):
        await store.update_story_block(block_id, {"view_id": "v2"}, 1, "alice")
    # The refused update left the block pointing where it was.
    assert await store.get_view("c1", "v1") is not None
