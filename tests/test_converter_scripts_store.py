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


async def _script(store, case_id, *, raw="d" * 64, status="generating", name="x2vestigo"):
    return await store.create_converter_script(
        case_id=case_id,
        name=name,
        version=1,
        raw_file_hash=raw,
        raw_filename="x.log",
        model="m",
        provider_endpoint="e",
        prompt_hash="p",
        sample_hash="s",
        sample_excerpt="verbatim slice of the evidence",
        hint=None,
        created_by=None,
        status=status,
    )


@pytest.mark.asyncio
async def test_delete_case_takes_converter_scripts_with_it(store: PostgresStore) -> None:
    case = await store.create_case("c", "d")
    other = await store.create_case("o", "d")
    s = await _script(store, case.id)
    keep = await _script(store, other.id)
    assert await store.delete_case(case.id) is True
    assert await store.get_converter_script(case.id, s.id) is None
    assert await store.list_converter_scripts(case.id) == []
    assert (await store.get_converter_script(other.id, keep.id)) is not None


@pytest.mark.asyncio
async def test_converter_raw_hash_counts_as_retention_in_use(store: PostgresStore) -> None:
    # The raw log a converter was written from lives in the same content-addressed
    # store as sources; a rollback/reconciliation unlinking "unused" hashes must
    # see the converter row as an owner.
    case = await store.create_case("c", "d")
    assert await store.source_hash_in_use("e" * 64, exclude_source_id="none") is False
    await _script(store, case.id, raw="e" * 64)
    assert await store.source_hash_in_use("e" * 64, exclude_source_id="none") is True


@pytest.mark.asyncio
async def test_fail_stale_generations(store: PostgresStore) -> None:
    case = await store.create_case("c", "d")
    stuck = await _script(store, case.id, status="generating")
    fine = await _script(store, case.id, status="working", name="y2vestigo")
    failed = await store.fail_stale_converter_generations()
    assert [r.id for r in failed] == [stuck.id]
    stuck = await store.get_converter_script(case.id, stuck.id)
    assert stuck.status == "failed"
    assert stuck.attempts[-1]["error"].startswith("generation interrupted")
    assert (await store.get_converter_script(case.id, fine.id)).status == "working"
    assert await store.fail_stale_converter_generations() == []


@pytest.mark.asyncio
async def test_get_source_by_converter_input(store: PostgresStore) -> None:
    case = await store.create_case("c", "d")
    s = await _script(store, case.id, status="working")
    src = await store.create_source(
        case_id=case.id,
        source_id="src1",
        name="x",
        file_hash="c" * 64,
        size_bytes=1,
        converter_script_id=s.id,
        converter_input_hash="d" * 64,
    )
    assert src.to_dict()["converter_input_hash"] == "d" * 64
    found = await store.get_source_by_converter_input(case.id, s.id, "d" * 64)
    assert found is not None and found.id == "src1"
    assert await store.get_source_by_converter_input(case.id, s.id, "f" * 64) is None
    assert await store.get_source_by_converter_input(case.id, "other", "d" * 64) is None
