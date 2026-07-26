"""Story/StoryBlock/StoryExport store-layer tests (SQLite)."""


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
