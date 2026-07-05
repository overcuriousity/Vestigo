"""Live-ClickHouse integration tests for timeline field mappings (issue #10).

Two sources carry the same IP data under different attribute keys
(``src_ip`` vs ``ip_addr``); a canonical ``ip_address`` mapping must make
filters, terms aggregation, coverage, and value-novelty see them as one field.
Requires the dev compose stack (skipped when ClickHouse is unreachable), same
as the upload/pipeline integration tests.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tracesignal.db.anomaly_stats import StatisticalAnomalyService
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.queries import EventQuery, EventQueryService
from tracesignal.models.event import Event

CASE_ID = f"tc-fieldmap-{uuid.uuid4().hex[:8]}"
SRC_A, SRC_B = "src-a", "src-b"
MAPPINGS = {"ip_address": ["src_ip", "ip_addr"]}


def _event(source_id: str, i: int, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=source_id,
        source_file=Path(f"{source_id}.csv"),
        byte_offset=i * 100,
        content_hash=f"ch-{source_id}-{i}",
        file_hash=f"fh-{source_id}",
        parser_name="test",
        parser_version="1",
        raw_line=f"line {i}",
        message=f"event {i} from {source_id}",
        timestamp=f"2026-01-01T10:{i:02d}:00Z",
        timestamp_desc="Test Time",
        artifact="test:artifact",
        attributes=attrs,
    )


@pytest.fixture(scope="module")
def ch_store():
    # Constructing the store already opens a connection — keep it inside the
    # guard so an unreachable ClickHouse skips the module instead of erroring
    # every test at fixture setup.
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    events = [
        # Source A uses src_ip; one shared value (10.0.0.1) and one unique.
        _event(SRC_A, 1, {"src_ip": "10.0.0.1", "status": "200"}),
        _event(SRC_A, 2, {"src_ip": "10.0.0.2", "status": "200"}),
        # Source B uses ip_addr for the same kind of data.
        _event(SRC_B, 3, {"ip_addr": "10.0.0.1", "proto": "tcp"}),
        _event(SRC_B, 4, {"ip_addr": "10.0.0.3", "proto": "udp"}),
    ]
    store.insert_events(events)
    yield store
    for sid in (SRC_A, SRC_B):
        store.delete_source_events(CASE_ID, sid)


def _query(**kw) -> EventQuery:
    return EventQuery(
        case_id=CASE_ID,
        source_ids=[SRC_A, SRC_B],
        field_mappings=MAPPINGS,
        **kw,
    )


def test_filter_on_canonical_field_matches_both_sources(ch_store):
    service = EventQueryService(store=ch_store)
    page = service.query(_query(field_filters={"ip_address": "10.0.0.1"}))
    assert page.total == 2
    assert {e["source_id"] for e in page.events} == {SRC_A, SRC_B}


def test_exclusion_on_canonical_field(ch_store):
    service = EventQueryService(store=ch_store)
    page = service.query(_query(field_exclusions={"ip_address": ["10.0.0.1"]}))
    assert page.total == 2
    values = {e["attributes"].get("src_ip") or e["attributes"].get("ip_addr") for e in page.events}
    assert values == {"10.0.0.2", "10.0.0.3"}


def test_field_terms_aggregates_across_sources(ch_store):
    service = EventQueryService(store=ch_store)
    result = service.field_terms(_query(), "ip_address")
    counts = {t["value"]: t["count"] for t in result["values"]}
    assert counts == {"10.0.0.1": 2, "10.0.0.2": 1, "10.0.0.3": 1}


def test_list_fields_hides_raws_and_surfaces_canonical(ch_store):
    service = EventQueryService(store=ch_store)
    result = service.list_fields(CASE_ID, [SRC_A, SRC_B], MAPPINGS)
    assert "ip_address" in result["attributes"]
    assert "src_ip" not in result["attributes"]
    assert "ip_addr" not in result["attributes"]
    assert result["mapped"] == [{"name": "ip_address", "raw_fields": ["src_ip", "ip_addr"]}]


def test_field_coverage_lists_raw_keys_with_samples(ch_store):
    service = EventQueryService(store=ch_store)
    result = service.field_coverage(CASE_ID, [SRC_A, SRC_B])
    by_key = {f["key"]: f["sources"] for f in result["fields"]}
    assert set(by_key) == {"src_ip", "ip_addr", "status", "proto"}
    src_ip_sources = {s["source_id"]: s for s in by_key["src_ip"]}
    assert list(src_ip_sources) == [SRC_A]
    assert src_ip_sources[SRC_A]["count"] == 2
    assert set(src_ip_sources[SRC_A]["samples"]) <= {"10.0.0.1", "10.0.0.2"}


def test_value_novelty_on_canonical_field(ch_store):
    svc = StatisticalAnomalyService(clickhouse=ch_store)
    result = svc.find_value_novelty(
        CASE_ID,
        [SRC_A, SRC_B],
        fields=["ip_address"],
        rarity_floor=1,
        field_mappings=MAPPINGS,
    )
    assert result.status == "ok"
    flagged = {f.value for f in result.results}
    # 10.0.0.1 occurs twice (above floor); the two singletons are flagged.
    assert flagged == {"10.0.0.2", "10.0.0.3"}


def test_anomaly_field_inventory_replaces_raws_with_canonical(ch_store):
    svc = StatisticalAnomalyService(clickhouse=ch_store)
    inventory, _total = svc.field_inventory(CASE_ID, [SRC_A, SRC_B], field_mappings=MAPPINGS)
    tokens = [t for t, _, _ in inventory]
    assert "ip_address" in tokens
    assert "attr:src_ip" not in tokens
    assert "attr:ip_addr" not in tokens
    entry = next((t, d, c) for t, d, c in inventory if t == "ip_address")
    assert entry[1] == 3  # distinct: .1, .2, .3
    assert entry[2] == 4  # coverage: all four events carry a value
