"""Fingerprint cache: a hit must be proof, not a hope.

Sources are immutable after ingestion except for enrichment applies, and both
ingestion and enrichment refresh ``source_field_stats``. Every input that can
change an answer is therefore in the key, and these tests pin that one by one —
a missing input is a silently wrong answer served from cache, which is the
failure mode a forensic tool can least afford.
"""

from __future__ import annotations

import pytest

from vestigo.db.analysis_cache import cache_get, cache_put, fingerprint


@pytest.fixture()
async def migrated(store):
    """The bare ``store`` fixture never migrates — the app lifespan normally does.

    Running it here also exercises revision 0026 against SQLite, which is the
    dialect-portability check that matters for this migration.
    """
    await store.init_schema()
    return store


BASE = {
    "timeline_id": "tl-1",
    "source_hashes": ["aaa", "bbb"],
    "enrichment_generation": "gen-1",
    "frame": "baseline",
    "baseline_id": "bl-1",
    "method": "frequency",
    "params": {"z_threshold": 3.0, "bucket_count": 12},
    "dispositions_hash": "dh-1",
}


def test_same_inputs_produce_the_same_key():
    assert fingerprint(**BASE) == fingerprint(**BASE)


def test_source_order_does_not_change_the_key():
    assert fingerprint(**{**BASE, "source_hashes": ["bbb", "aaa"]}) == fingerprint(**BASE)


def test_params_key_order_does_not_change_the_key():
    reordered = {**BASE, "params": {"bucket_count": 12, "z_threshold": 3.0}}
    assert fingerprint(**reordered) == fingerprint(**BASE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeline_id", "tl-2"),
        ("source_hashes", ["aaa", "bbb", "ccc"]),
        ("enrichment_generation", "gen-2"),
        ("frame", "self"),
        ("baseline_id", "bl-2"),
        ("method", "charset"),
        ("params", {"z_threshold": 2.0, "bucket_count": 12}),
        ("dispositions_hash", "dh-2"),
    ],
)
def test_every_input_independently_changes_the_key(field, value):
    assert fingerprint(**{**BASE, field: value}) != fingerprint(**BASE)


@pytest.mark.asyncio
async def test_put_then_get_round_trips(migrated):
    key = fingerprint(**BASE)
    await cache_put(migrated, "case-1", key, {"results": [1, 2, 3]}, max_rows=10)
    assert await cache_get(migrated, "case-1", key) == {"results": [1, 2, 3]}


@pytest.mark.asyncio
async def test_get_misses_on_an_unknown_key(migrated):
    assert await cache_get(migrated, "case-1", "nope") is None


@pytest.mark.asyncio
async def test_get_is_scoped_to_its_case(migrated):
    key = fingerprint(**BASE)
    await cache_put(migrated, "case-1", key, {"results": []}, max_rows=10)
    assert await cache_get(migrated, "case-2", key) is None


@pytest.mark.asyncio
async def test_eviction_keeps_the_newest_rows_within_the_cap(migrated):
    for i in range(5):
        await cache_put(migrated, "case-1", f"key-{i}", {"n": i}, max_rows=3)
    assert await cache_get(migrated, "case-1", "key-0") is None
    assert await cache_get(migrated, "case-1", "key-4") == {"n": 4}


@pytest.mark.asyncio
async def test_eviction_does_not_reach_into_another_case(migrated):
    """One busy case must not evict a quiet one's cached answers."""
    await cache_put(migrated, "case-quiet", "keep-me", {"n": 0}, max_rows=3)
    for i in range(5):
        await cache_put(migrated, "case-busy", f"key-{i}", {"n": i}, max_rows=3)
    assert await cache_get(migrated, "case-quiet", "keep-me") == {"n": 0}


@pytest.mark.asyncio
async def test_put_on_an_existing_key_replaces_rather_than_duplicates(migrated):
    key = fingerprint(**BASE)
    await cache_put(migrated, "case-1", key, {"n": 1}, max_rows=10)
    await cache_put(migrated, "case-1", key, {"n": 2}, max_rows=10)
    assert await cache_get(migrated, "case-1", key) == {"n": 2}
