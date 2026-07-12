"""Live-ClickHouse tests for the baseline-compare cache (M24c).

Two properties, per compare kind:

* token vs. no-token results are identical — the no-token path is the
  unchanged pre-M24c implementation (including the union range scan the
  token path skips), so this is the equivalence oracle;
* cold vs. warm cached renders are identical.

Requires the dev compose stack (skipped when ClickHouse is unreachable).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracesignal.db import viz_cache
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.queries import EventQuery, EventQueryService
from tracesignal.models.event import Event

CASE_ID = f"tc-cmpcache-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-cmpcache"
TOKEN = (CASE_ID, ((SOURCE_ID, "2026-01-01T00:00:00+00:00", 40),))


def _event(i: int, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="c" * 64,
        parser_name="test-cmpcache",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i} {'dos' if i % 3 == 0 else 'ok'}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:cmpcache",
        attributes=attrs,
    )


@pytest.fixture(scope="module")
def service():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    events = [
        _event(
            i,
            f"2026-03-01T{i % 24:02d}:{(i * 7) % 60:02d}:00+00:00",
            {"method": "GET" if i % 2 == 0 else "POST", "bytes": str(100 + i * 13)},
        )
        for i in range(40)
    ]
    store.insert_events(events)
    yield EventQueryService(store=store)
    store.delete_source_events(CASE_ID, SOURCE_ID)


@pytest.fixture(autouse=True)
def fresh_cache():
    viz_cache.reset_baseline_cache()
    yield
    viz_cache.reset_baseline_cache()


def _layers(explicit_window: bool = False) -> tuple[EventQuery, EventQuery]:
    window = {}
    if explicit_window:
        window = {
            "start": datetime(2026, 3, 1, 2, tzinfo=UTC),
            "end": datetime(2026, 3, 1, 20, tzinfo=UTC),
        }
    primary = EventQuery(case_id=CASE_ID, source_ids=[SOURCE_ID], q="dos", **window)
    comparison = EventQuery(case_id=CASE_ID, source_ids=[SOURCE_ID], **window)
    return primary, comparison


@pytest.mark.parametrize("explicit_window", [False, True])
def test_time_histogram_token_equivalent_and_warm_identical(service, explicit_window):
    primary, comparison = _layers(explicit_window)
    oracle = service.compare_time_histogram(primary, comparison, buckets=12)
    cold = service.compare_time_histogram(
        primary, comparison, buckets=12, baseline_cache_token=TOKEN
    )
    warm = service.compare_time_histogram(
        primary, comparison, buckets=12, baseline_cache_token=TOKEN
    )
    assert cold == oracle
    assert warm == oracle
    assert oracle["primary_total"] > 0


def test_field_terms_token_equivalent_and_warm_identical(service):
    primary, comparison = _layers()
    oracle = service.compare_field_terms(primary, comparison, "attr:method")
    cold = service.compare_field_terms(
        primary, comparison, "attr:method", baseline_cache_token=TOKEN
    )
    warm = service.compare_field_terms(
        primary, comparison, "attr:method", baseline_cache_token=TOKEN
    )
    assert cold == oracle
    assert warm == oracle
    assert oracle["comparison_total"] == 40


def test_field_numeric_token_equivalent_and_warm_identical(service):
    primary, comparison = _layers()
    oracle = service.compare_field_numeric(primary, comparison, "attr:bytes", bins=10)
    cold = service.compare_field_numeric(
        primary, comparison, "attr:bytes", bins=10, baseline_cache_token=TOKEN
    )
    warm = service.compare_field_numeric(
        primary, comparison, "attr:bytes", bins=10, baseline_cache_token=TOKEN
    )
    assert cold == oracle
    assert warm == oracle
    assert oracle["comparison_total"] == 40
