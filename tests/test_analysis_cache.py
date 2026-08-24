"""Fingerprint cache: a hit must be proof, not a hope.

Sources are immutable after ingestion except for enrichment applies, and both
ingestion and enrichment refresh ``source_field_stats``. Every input that can
change an answer is therefore in the key, and these tests pin that one by one —
a missing input is a silently wrong answer served from cache, which is the
failure mode a forensic tool can least afford.
"""

from __future__ import annotations

import pytest

from vestigo.db.analysis_cache import (
    cache_get,
    cache_put,
    enrichment_generation,
    fingerprint,
)


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
    "baseline_config_hash": "cfg-1",
    "field_mappings": {"user": ["usr", "username"]},
    "source_offsets": {"src-1": 0},
    "field_overrides": {"attr:status_code": False},
    "detector_settings": {"stat_z_threshold": 2.5},
    "method": "frequency",
    "params": {"z_threshold": 3.0, "bucket_count": 12},
    "limit": 50,
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
        # A definition is edited in place and keeps its id, so its *contents*
        # have to be in the key or an edited window serves the pre-edit answer.
        ("baseline_config_hash", "cfg-2"),
        # Both are resolved per request and handed to every detector: a remapped
        # field changes what was scanned, an offset shifts every timestamp.
        ("field_mappings", {"user": ["usr"]}),
        ("source_offsets", {"src-1": 3600}),
        # The method's field declaration decides what a detector scans when it
        # picks its own fields, so a pre-declaration answer is a different one.
        ("field_overrides", {"attr:status_code": True}),
        # The runtime-editable thresholds a runner falls back to when a knob is
        # omitted. An admin lowering one in the console changes every answer.
        ("detector_settings", {"stat_z_threshold": 2.0}),
        ("method", "charset"),
        ("params", {"z_threshold": 2.0, "bucket_count": 12}),
        # The runners truncate to `limit`, so a payload computed at 50 rows is
        # not the answer to a request for 500.
        ("limit", 500),
        ("dispositions_hash", "dh-2"),
    ],
)
def test_every_input_independently_changes_the_key(field, value):
    assert fingerprint(**{**BASE, field: value}) != fingerprint(**BASE)


def test_mapping_and_offset_ordering_does_not_change_the_key():
    """Both arrive as dicts assembled per request; only their content is the key."""
    reordered = {
        **BASE,
        "field_mappings": {"user": ["username", "usr"]},
        "source_offsets": {"src-1": 0},
    }
    assert fingerprint(**reordered) == fingerprint(**BASE)


def test_detector_settings_material_covers_stat_knobs_but_not_scan_budget():
    """The prefix sweep is what keeps a newly added threshold from being missed.

    ``stat_scan_*`` is excluded deliberately: those tune ClickHouse's resource
    budget for the scan and cannot change what the scan concludes, so including
    them would evict every cached answer on a memory tweak.
    """
    from vestigo.core.config import Settings
    from vestigo.db.analysis_cache import detector_settings

    material = detector_settings(Settings())
    assert "stat_z_threshold" in material
    assert "stat_interval_fdr_q" in material
    assert not any(name.startswith("stat_scan_") for name in material)
    assert not any(name.startswith("analysis_gate_") for name in material)


@pytest.mark.asyncio
async def test_losing_the_insert_race_is_not_an_error(migrated, monkeypatch):
    """Two clients missing the same key both computed the same answer.

    The loser's unique-index violation must not fail a request whose ClickHouse
    scan already succeeded — that would report a method as unrunnable because a
    *cache write* collided.
    """
    from sqlalchemy.exc import IntegrityError

    from vestigo.db import analysis_cache

    async def boom(*_args, **_kwargs):
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(analysis_cache, "_cache_put", boom)
    await cache_put(migrated, "case-1", "racy", {"n": 1}, max_rows=10)


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


@pytest.mark.asyncio
async def test_refreshing_a_row_makes_it_the_newest(migrated):
    """Eviction is by ``computed_at``, so a refresh must move the row.

    Otherwise a key written once and recomputed daily keeps its first
    timestamp, ages past rows nobody has asked for since, and is evicted first —
    the inverse of the policy.
    """
    await cache_put(migrated, "case-1", "old", {"n": 0}, max_rows=2)
    await cache_put(migrated, "case-1", "mid", {"n": 1}, max_rows=2)
    # Refresh "old", then add a third row: "mid" is now the oldest and goes.
    await cache_put(migrated, "case-1", "old", {"n": 2}, max_rows=2)
    await cache_put(migrated, "case-1", "new", {"n": 3}, max_rows=2)
    assert await cache_get(migrated, "case-1", "old") == {"n": 2}
    assert await cache_get(migrated, "case-1", "mid") is None


@pytest.mark.asyncio
async def test_a_replace_at_the_cap_evicts_nothing(migrated):
    """Replacing a row cannot push the case over the cap, so it must not evict.

    The eviction pass used to run on every write, including replaces that left
    the row count untouched — materializing up to `max_rows` ids and issuing a
    `NOT IN` over them to discover there was nothing to do.
    """
    for i in range(3):
        await cache_put(migrated, "case-1", f"key-{i}", {"n": i}, max_rows=3)
    await cache_put(migrated, "case-1", "key-0", {"n": 99}, max_rows=3)
    assert await cache_get(migrated, "case-1", "key-0") == {"n": 99}
    for i in (1, 2):
        assert await cache_get(migrated, "case-1", f"key-{i}") == {"n": i}


@pytest.mark.asyncio
async def test_an_insert_exactly_at_the_cap_evicts_nothing(migrated):
    """Boundary: the cap is a ceiling to reach, not one to fall below."""
    for i in range(3):
        await cache_put(migrated, "case-1", f"key-{i}", {"n": i}, max_rows=3)
    for i in range(3):
        assert await cache_get(migrated, "case-1", f"key-{i}") == {"n": i}


@pytest.mark.asyncio
async def test_successive_enrichment_applies_move_the_generation_without_stats(migrated):
    """The stats row can be absent, and "absent" must not be one single state.

    ``enrichers/jobs.py`` drops the field-stats row when the post-apply refresh
    fails, so two enrichers that both fail their refresh leave identical
    stats-derived material over different attributes — a hit computed before
    the second enricher's fields existed, served under a contract that calls a
    hit proof. The provenance row separates them.
    """
    await migrated.create_case("case-e", "Enriched")
    await migrated.create_source("case-e", "src-e", "s.csv", "hash-e", 10)

    empty = await enrichment_generation(migrated, ["src-e"])
    await migrated.record_source_enrichment(
        case_id="case-e",
        source_id="src-e",
        timeline_id="tl-e",
        enricher_key="geoip",
        enricher_config_hash="cfg-a",
        job_id="job-a",
        rows_applied=10,
    )
    after_first = await enrichment_generation(migrated, ["src-e"])
    await migrated.record_source_enrichment(
        case_id="case-e",
        source_id="src-e",
        timeline_id="tl-e",
        enricher_key="asn",
        enricher_config_hash="cfg-b",
        job_id="job-b",
        rows_applied=10,
    )
    after_second = await enrichment_generation(migrated, ["src-e"])

    assert empty != after_first
    assert after_first != after_second


@pytest.mark.asyncio
async def test_deleting_a_source_purges_the_case_cache(migrated):
    """Cached payloads hold event ids, field values and message templates.

    They are keyed by a fingerprint rather than by source id, so there is no
    way to select the rows computed over the deleted source's events —
    unreachable is not gone, and eviction only ever runs on a later write for
    the same case.
    """
    await migrated.create_case("case-d", "Doomed")
    await migrated.create_source("case-d", "src-d", "s.csv", "hash-d", 10)
    await cache_put(migrated, "case-d", "findings", {"results": ["evidence"]}, max_rows=10)

    assert await migrated.delete_source("case-d", "src-d") is True
    assert await cache_get(migrated, "case-d", "findings") is None
