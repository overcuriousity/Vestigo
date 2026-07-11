"""Live-ClickHouse tests for the search_blob text-search fast path (M22).

Proves the fast path returns result sets identical to the plain ILIKE
OR-chain (superset pre-filter, same source of truth), that the in-place
upgrade path works on a pre-blob table, that the ngrambf index actually
appears in the query plan, and that the enrichment partition rewrite
refreshes the blob. Requires the dev compose stack (skipped when ClickHouse
is unreachable), same pattern as ``test_arrow_insert_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tracesignal.db.clickhouse import (
    _EVENTS_TABLE_DDL,
    ClickHouseStore,
)
from tracesignal.db.queries import EventQuery, EventQueryService
from tracesignal.models.event import Event

CASE_ID = f"tc-blob-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-blob"


def _event(i: int, **overrides) -> Event:
    kwargs: dict = {
        "case_id": CASE_ID,
        "source_id": SOURCE_ID,
        "source_file": Path("evidence.log"),
        "byte_offset": i * 100,
        "content_hash": f"{i:064d}",
        "file_hash": "b" * 64,
        "parser_name": "test-blob",
        "parser_version": "1.0.0",
        "raw_line": f"raw {i}",
        "message": f"event {i}",
        "timestamp": f"2026-01-0{1 + i % 5}T10:00:00+00:00",
        "timestamp_desc": "Test Time",
        "artifact": "test:blob",
    }
    kwargs.update(overrides)
    return Event(**kwargs)


def _fixture_events() -> list[Event]:
    """One match candidate per searched field, plus metachar/case-fold bait."""
    return [
        _event(0, message="needle-in-message here"),
        _event(1, display_name="needle-in-display"),
        _event(2, artifact="needle:artifact"),
        _event(3, artifact_long="Needle Long Artifact"),
        _event(4, timestamp_desc="Needle Written"),
        _event(5, source_file=Path("needle-file.log")),
        _event(6, tags=["needle-tag", "other"]),
        _event(7, attributes={"k": "needle-attr-value"}),
        # LIKE metacharacters searched literally.
        _event(8, message="progress 100%_done today"),
        _event(9, message="plain percent-free line"),
        # Non-ASCII case folds (ILIKE vs lowerUTF8 agreement).
        _event(10, message="Straße GROSS"),
        _event(11, message="МОСКВА event"),
        _event(12, message="İstanbul login"),
        # Noise that must never match.
        _event(13, message="unrelated haystack line"),
    ]


@pytest.fixture(scope="module")
def store():
    try:
        s = ClickHouseStore()
        s.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    s.insert_events(_fixture_events())
    yield s
    s.delete_source_events(CASE_ID, SOURCE_ID)


def _search_ids(store: ClickHouseStore, q: str, *, fast: bool) -> set[str]:
    service = EventQueryService(store=store)
    original = store.search_blob_ready
    store.search_blob_ready = (lambda: True) if fast else (lambda: False)  # type: ignore[method-assign]
    try:
        page = service.query(EventQuery(case_id=CASE_ID, q=q, limit=100))
    finally:
        store.search_blob_ready = original  # type: ignore[method-assign]
    return {e["event_id"] for e in page.events}


@pytest.mark.parametrize(
    "q",
    [
        "needle",  # hits every per-field candidate
        "NEEDLE",  # case-insensitivity
        "eedle-in-mess",  # substring inside a token
        "100%_done",  # LIKE metachars, literal
        "straße",  # sharp s
        "STRASSE",  # ILIKE does NOT fold ß→ss; must stay a miss in both modes
        "москва",  # Cyrillic fold
        "istanbul",  # dotted capital İ
        "no-such-string-anywhere",
    ],
)
def test_fast_path_results_identical(store, q):
    slow = _search_ids(store, q, fast=False)
    fast = _search_ids(store, q, fast=True)
    assert fast == slow


def test_fast_path_finds_every_field(store):
    ids = _search_ids(store, "needle", fast=True)
    assert len(ids) == 8  # one per searched field: 6 columns + tags + attributes


def test_blob_index_appears_in_plan(store):
    result = store.client.query(
        f"EXPLAIN indexes = 1 SELECT count() FROM {store.database}.events "
        f"WHERE case_id = {{c:String}} AND search_blob LIKE lowerUTF8({{p:String}})",
        parameters={"c": CASE_ID, "p": "%needle%"},
    )
    plan = "\n".join(str(row[0]) for row in result.result_rows)
    assert "search_blob_idx" in plan


def test_upgrade_path_adds_column_index_and_drops_message_idx(store):
    """A pre-M22 table (no blob, tokenbf message_idx) upgrades in place."""
    scratch_db = f"tsig_blobtest_{uuid.uuid4().hex[:8]}"
    legacy_ddl = _EVENTS_TABLE_DDL.format(
        database=scratch_db,
        table="events",
        search_blob_column="legacy_placeholder UInt8 DEFAULT 0",
        search_blob_index="message_idx message TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 1",
    )
    store.client.command(f"CREATE DATABASE IF NOT EXISTS {scratch_db}")
    store.client.command(legacy_ddl)
    upgraded = ClickHouseStore.__new__(ClickHouseStore)
    upgraded.client = store.client
    upgraded.database = scratch_db
    try:
        upgraded._ensure_search_blob()
        cols = store.client.query(
            "SELECT name FROM system.columns WHERE database = {d:String} AND table = 'events'",
            parameters={"d": scratch_db},
        ).result_rows
        assert ("search_blob",) in cols
        idx = store.client.query(
            "SELECT name FROM system.data_skipping_indices "
            "WHERE database = {d:String} AND table = 'events'",
            parameters={"d": scratch_db},
        ).result_rows
        names = {r[0] for r in idx}
        assert "search_blob_idx" in names
        assert "message_idx" not in names
        # Idempotent: second call issues no further DDL (column now present).
        upgraded._ensure_search_blob()
        # Empty table → no mutations pending → ready.
        upgraded._search_blob_checked_at = None
        assert upgraded.search_blob_ready() is True
    finally:
        store.client.command(f"DROP DATABASE IF EXISTS {scratch_db}")


def test_enrichment_apply_refreshes_blob(store):
    """REPLACE PARTITION rewrite recomputes the blob from enriched attributes."""
    target = next(e for e in _fixture_events() if e.byte_offset == 1300)  # event 13
    row = store.client.query(
        f"SELECT event_id FROM {store.database}.events "
        f"WHERE case_id = {{c:String}} AND source_id = {{s:String}} AND byte_offset = 1300",
        parameters={"c": CASE_ID, "s": SOURCE_ID},
    ).result_rows
    assert row, "fixture event 13 missing"
    event_id = str(row[0][0])
    del target

    suffix = uuid.uuid4().hex[:12]
    store.create_enrichment_scratch(suffix)
    store.stage_enrichment_rows(suffix, [(event_id, "enriched_key", "EnrichedNeedleXYZ")])
    store.finalize_enrichment_apply(CASE_ID, SOURCE_ID, suffix)

    blob = store.client.query(
        f"SELECT search_blob FROM {store.database}.events "
        f"WHERE case_id = {{c:String}} AND source_id = {{s:String}} AND byte_offset = 1300",
        parameters={"c": CASE_ID, "s": SOURCE_ID},
    ).result_rows[0][0]
    assert "enrichedneedlexyz" in blob  # blob is lowercased
    # And the broad search finds it in both modes.
    assert _search_ids(store, "EnrichedNeedle", fast=True) == {event_id}
    assert _search_ids(store, "EnrichedNeedle", fast=False) == {event_id}
