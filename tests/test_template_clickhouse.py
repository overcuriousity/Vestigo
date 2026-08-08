"""Live-ClickHouse tests for the W6 template_hash materialized column.

Proves structurally identical messages (differing only in masked
digit/hex/UUID/IP substrings) collapse to the same template_hash while a
distinct shape gets a different one, that the in-place upgrade path works on
a pre-template_hash table, and that MATERIALIZED semantics make old parts
correct immediately (before MATERIALIZE COLUMN drains). Requires the dev
compose stack (skipped when ClickHouse is unreachable), same pattern as
``test_search_blob_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vestigo.db._template import template_hash_expr, template_normalize_expr
from vestigo.db.anomaly_stats import StatisticalAnomalyService
from vestigo.db.clickhouse import _EVENTS_TABLE_DDL, ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-tmpl-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-tmpl"


def _event(i: int, **overrides) -> Event:
    kwargs: dict = {
        "case_id": CASE_ID,
        "source_id": SOURCE_ID,
        "source_file": Path("evidence.log"),
        "byte_offset": i * 100,
        "content_hash": f"{i:064d}",
        "file_hash": "c" * 64,
        "parser_name": "test-template",
        "parser_version": "1.0.0",
        "raw_line": f"raw {i}",
        "message": f"event {i}",
        "timestamp": "2026-01-01T10:00:00+00:00",
        "timestamp_desc": "Test Time",
        "artifact": "test:template",
    }
    kwargs.update(overrides)
    return Event(**kwargs)


def _fixture_events() -> list[Event]:
    return [
        _event(0, message="Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443"),
        _event(1, message="Allow TCP 10.0.0.6:8811 -> 10.0.0.9:443"),
        _event(2, message="Allow TCP 10.0.0.7:9922 -> 10.0.0.9:443"),
        _event(
            3,
            message="Deny UDP 185.220.101.4:0 -> 10.0.0.9:3389 (spoofed-src flag)",
        ),
        _event(4, message="HTTP 404 returned in 12ms"),
        _event(5, message="HTTP 500 returned in 87ms"),
    ]


@pytest.fixture(scope="module")
def store():
    s = ClickHouseStore()
    s.init_schema()
    s.insert_events(_fixture_events())
    yield s
    s.delete_source_events(CASE_ID, SOURCE_ID)


def _template_hashes(store: ClickHouseStore) -> dict[int, int]:
    result = store.client.query(
        f"SELECT byte_offset, template_hash FROM {store.database}.events "
        f"WHERE case_id = {{c:String}} AND source_id = {{s:String}}",
        parameters={"c": CASE_ID, "s": SOURCE_ID},
    )
    return {row[0]: row[1] for row in result.result_rows}


def test_identical_shapes_collapse_to_same_hash(store):
    hashes = _template_hashes(store)
    routine = {hashes[0], hashes[100], hashes[200]}
    assert len(routine) == 1


def test_distinct_shape_gets_a_different_hash(store):
    hashes = _template_hashes(store)
    assert hashes[300] != hashes[0]


def test_all_digits_masked_merges_status_codes(store):
    # Confirmed design decision: mask all digit runs, so "HTTP 404" and
    # "HTTP 500" collapse to one template.
    hashes = _template_hashes(store)
    assert hashes[400] == hashes[500]


def test_hash_matches_python_side_expression(store):
    hashes = _template_hashes(store)
    result = store.client.query(
        f"SELECT {template_hash_expr('message')} FROM {store.database}.events "
        f"WHERE case_id = {{c:String}} AND source_id = {{s:String}} AND byte_offset = 0",
        parameters={"c": CASE_ID, "s": SOURCE_ID},
    )
    assert result.result_rows[0][0] == hashes[0]


def test_grouping_reconstructs_template_text(store):
    result = store.client.query(
        f"SELECT template_hash, count(), "
        f"{template_normalize_expr('any(message)')} AS template "
        f"FROM {store.database}.events "
        f"WHERE case_id = {{c:String}} AND source_id = {{s:String}} "
        f"GROUP BY template_hash ORDER BY count() DESC",
        parameters={"c": CASE_ID, "s": SOURCE_ID},
    )
    rows = {row[2]: row[1] for row in result.result_rows}
    assert rows["Allow TCP <IP>:<NUM> -> <IP>:<NUM>"] == 3
    assert rows["Deny UDP <IP>:<NUM> -> <IP>:<NUM> (spoofed-src flag)"] == 1
    assert rows["HTTP <NUM> returned in <NUM>ms"] == 2


def test_list_log_templates_collapses_and_reports_counts(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    result = svc.list_log_templates("__missing__", ["__missing__"])
    assert result.total_templates == 0  # scope isolation sanity check
    result = svc.list_log_templates(CASE_ID, [SOURCE_ID])
    by_template = {row.template: row for row in result.templates}
    assert by_template["Allow TCP <IP>:<NUM> -> <IP>:<NUM>"].count == 3
    assert by_template["Deny UDP <IP>:<NUM> -> <IP>:<NUM> (spoofed-src flag)"].count == 1
    assert by_template["HTTP <NUM> returned in <NUM>ms"].count == 2
    assert result.total_templates == 3
    routine = by_template["Allow TCP <IP>:<NUM> -> <IP>:<NUM>"]
    assert routine.template_id.isdigit()  # decimal string, not a JS-unsafe int
    assert routine.distinct_sources == 1
    assert routine.first_seen is not None
    assert routine.example.startswith("Allow TCP")


def test_list_log_templates_order_by_count_is_descending(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    result = svc.list_log_templates(CASE_ID, [SOURCE_ID], order="count", limit=1)
    assert result.templates[0].count == 3  # the routine "Allow TCP" shape wins


def test_list_log_templates_total_survives_truncation(store):
    """`total_templates` counts every distinct shape, not the page. It rides in
    on `count() OVER ()`, evaluated before LIMIT — so truncating the listing
    must not truncate the count the UI shows ("showing top N of M")."""
    svc = StatisticalAnomalyService(clickhouse=store)
    result = svc.list_log_templates(CASE_ID, [SOURCE_ID], limit=1)
    assert len(result.templates) == 1
    assert result.total_templates == 3


def test_list_log_templates_only_new_filters_by_baseline(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    # only_new keeps templates whose first_seen >= baseline_end — "never
    # seen before the baseline ended". All fixture events are stamped
    # 2026-01-01, so a far-future split means none of them are "new"
    # relative to it, and a far-past split means all of them are.
    from datetime import UTC, datetime

    far_future = datetime(2999, 1, 1, tzinfo=UTC)
    result = svc.list_log_templates(CASE_ID, [SOURCE_ID], only_new=True, baseline_end=far_future)
    assert result.total_templates == 0  # nothing is "new" relative to a future split

    far_past = datetime(2000, 1, 1, tzinfo=UTC)
    result = svc.list_log_templates(CASE_ID, [SOURCE_ID], only_new=True, baseline_end=far_past)
    assert result.total_templates == 3  # everything is "new" relative to a past split


def test_list_log_templates_non_message_field(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    result = svc.list_log_templates(CASE_ID, [SOURCE_ID], field="artifact")
    # `artifact` is constant across the fixture ("test:template") — one
    # template, proving the unindexed field-agnostic path executes and
    # groups correctly, not just the default `message` fast path.
    assert result.total_templates == 1
    assert result.templates[0].count == 6


def test_template_id_facet_filters_the_grid(store):
    """W6-3: template_id resolves through the same filter plumbing as any
    other field token, so the Explorer grid can be scoped to one template."""
    svc = StatisticalAnomalyService(clickhouse=store)
    listing = svc.list_log_templates(CASE_ID, [SOURCE_ID])
    routine = next(t for t in listing.templates if t.count == 3)

    query_service = EventQueryService(store=store)
    page = query_service.query(
        EventQuery(
            case_id=CASE_ID,
            source_ids=[SOURCE_ID],
            field_filters={"template_id": [routine.template_id]},
            limit=100,
        )
    )
    assert page.total == 3
    assert all(e["message"].startswith("Allow TCP") for e in page.events)


def test_mute_template_collapse_and_unmute_round_trip(store):
    """W6-4: template_hash-based collapse — no motif_occurrences aux table.
    Mute excludes the routine shape's events, unmute restores them, and
    count_routine_collapsed reports the union (not a naive sum) so an event
    covered by multiple mechanisms is never double-counted."""
    svc = StatisticalAnomalyService(clickhouse=store)
    listing = svc.list_log_templates(CASE_ID, [SOURCE_ID])
    routine = next(t for t in listing.templates if t.count == 3)
    routine_hash = int(routine.template_id)

    query_service = EventQueryService(store=store)
    unfiltered = query_service.query(EventQuery(case_id=CASE_ID, source_ids=[SOURCE_ID], limit=100))
    assert unfiltered.total == 6

    muted = query_service.query(
        EventQuery(
            case_id=CASE_ID,
            source_ids=[SOURCE_ID],
            exclude_template_hashes=[routine_hash],
            limit=100,
        )
    )
    assert muted.total == 3
    assert all(not e["message"].startswith("Allow TCP") for e in muted.events)

    collapsed = store.count_routine_collapsed(
        CASE_ID, [SOURCE_ID], motif_disposition_ids=None, template_hashes=[routine_hash]
    )
    assert collapsed == 3

    restored = query_service.query(EventQuery(case_id=CASE_ID, source_ids=[SOURCE_ID], limit=100))
    assert restored.total == 6  # "unmute" == simply not passing the exclusion


def test_upgrade_path_adds_column_and_index(store):
    """A pre-W6 table (no template_hash) upgrades in place."""
    scratch_db = f"vestigo_tmpltest_{uuid.uuid4().hex[:8]}"
    legacy_ddl = _EVENTS_TABLE_DDL.format(
        database=scratch_db,
        table="events",
        search_blob_column="legacy_blob_placeholder UInt8 DEFAULT 0",
        search_blob_index=(
            "legacy_blob_idx legacy_blob_placeholder TYPE bloom_filter GRANULARITY 4"
        ),
        template_hash_column="legacy_template_placeholder UInt8 DEFAULT 0",
        template_hash_index=(
            "legacy_template_idx legacy_template_placeholder TYPE bloom_filter GRANULARITY 4"
        ),
    )
    store.client.command(f"CREATE DATABASE IF NOT EXISTS {scratch_db}")
    store.client.command(legacy_ddl)
    upgraded = ClickHouseStore.__new__(ClickHouseStore)
    upgraded.client = store.client
    upgraded.database = scratch_db
    try:
        upgraded._ensure_template_hash()
        cols = store.client.query(
            "SELECT name FROM system.columns WHERE database = {d:String} AND table = 'events'",
            parameters={"d": scratch_db},
        ).result_rows
        assert ("template_hash",) in cols
        idx = store.client.query(
            "SELECT name FROM system.data_skipping_indices "
            "WHERE database = {d:String} AND table = 'events'",
            parameters={"d": scratch_db},
        ).result_rows
        assert "template_hash_idx" in {r[0] for r in idx}
        # Correct on old (unmaterialized) parts immediately — MATERIALIZED
        # columns compute on read regardless of mutation completion.
        store.client.command(
            f"INSERT INTO {scratch_db}.events "
            "(event_id, case_id, source_id, message, timestamp) VALUES "
            "(generateUUIDv4(), 'c1', 's1', 'Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443', now())"
        )
        row = store.client.query(
            f"SELECT template_hash FROM {scratch_db}.events LIMIT 1"
        ).result_rows[0]
        assert row[0] != 0
        # Idempotent: second call issues no further DDL.
        upgraded._ensure_template_hash()
    finally:
        store.client.command(f"DROP DATABASE IF EXISTS {scratch_db}")
