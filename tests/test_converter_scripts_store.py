"""ConverterScript rows: creation, versioning, update, listing."""

from __future__ import annotations

import pytest

from vestigo.db.postgres import PostgresStore


@pytest.mark.asyncio
async def test_create_and_version(store: PostgresStore) -> None:
    case = await store.create_case("c", "d")
    s1 = await store.create_converter_script(
        case_id=case.id,
        name="myapp2vestigo",
        version=1,
        raw_file_hash="a" * 64,
        raw_filename="app.log",
        model="m",
        provider_endpoint="http://x/v1",
        prompt_hash="p",
        sample_hash="s",
        sample_excerpt="line",
        hint=None,
        created_by="u1",
    )
    assert s1.status == "generating"
    assert await store.next_converter_version(case.id, "myapp2vestigo") == 2
    assert await store.next_converter_version(case.id, "other2vestigo") == 1

    s1 = await store.update_converter_script(
        s1.id, status="working", source_code="print(1)", attempts=[{"n": 1, "ok": True}]
    )
    assert s1.status == "working"
    assert s1.attempts == [{"n": 1, "ok": True}]

    listed = await store.list_converter_scripts(case.id)
    assert [s.id for s in listed] == [s1.id]
    assert (await store.get_converter_script(case.id, s1.id)).source_code == "print(1)"
    assert await store.get_converter_script("nope", s1.id) is None
    d = s1.to_dict()
    assert "source_code" not in d and s1.to_dict(include_code=True)["source_code"] == "print(1)"


@pytest.mark.asyncio
async def test_source_links_to_script(store: PostgresStore) -> None:
    case = await store.create_case("c", "d")
    s = await store.create_converter_script(
        case_id=case.id,
        name="x2vestigo",
        version=1,
        raw_file_hash="b" * 64,
        raw_filename="x.log",
        model="m",
        provider_endpoint="e",
        prompt_hash="p",
        sample_hash="s",
        sample_excerpt="",
        hint=None,
        created_by=None,
    )
    src = await store.create_source(
        case_id=case.id,
        source_id="src1",
        name="x",
        file_hash="c" * 64,
        size_bytes=1,
        converter_script_id=s.id,
    )
    assert src.converter_script_id == s.id
    assert src.to_dict()["converter_script_id"] == s.id
    assert await store.count_sources_by_converter(case.id) == {s.id: 1}
