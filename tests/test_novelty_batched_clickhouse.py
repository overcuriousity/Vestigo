"""Live-ClickHouse equivalence test for the batched value-novelty scan (M23b).

Proves the single ARRAY JOIN pass over ``attributes`` returns findings
identical to the retired one-query-per-field loop. The old per-field SQL is
kept HERE, test-only, as the oracle. Requires the dev compose stack (skipped
when ClickHouse is unreachable), same pattern as
``test_arrow_insert_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracesignal.db.anomaly_stats import (
    AnalysisWindows,
    StatisticalAnomalyService,
    TimeWindow,
    _window_preds,
)
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.models.event import Event

CASE_ID = f"tc-novbatch-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-novbatch"

# Fixture design: two attr keys with values around rarity_floor=2, count ties,
# per-field limit overflow, a key present in only some events, and temporal
# windows with baseline-present and baseline-absent values.
BASELINE_START = datetime(2026, 1, 1, tzinfo=UTC)
SUSPECT_START = datetime(2026, 1, 10, tzinfo=UTC)
SUSPECT_END = datetime(2026, 1, 11, tzinfo=UTC)


def _event(i: int, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="e" * 64,
        parser_name="test-novbatch",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:novbatch",
        attributes=attrs,
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0

    def add(ts: str, attrs: dict[str, str]) -> None:
        nonlocal i
        events.append(_event(i, ts, attrs))
        i += 1

    # Baseline window: common values (above floor), plus baseline-present
    # value "bl_only" for temporal mode.
    for n in range(5):
        add(f"2026-01-02T10:0{n}:00+00:00", {"user": "alice", "host": "web-1"})
        add(f"2026-01-03T10:0{n}:00+00:00", {"user": "bob", "host": "web-2"})
    add("2026-01-04T09:00:00+00:00", {"user": "bl_only"})
    # Rare values in the corpus (self-baseline floor = 2): counts 1 and 2,
    # with a count tie between "carol" and "dave".
    add("2026-01-05T10:00:00+00:00", {"user": "carol", "host": "db-9"})
    add("2026-01-05T11:00:00+00:00", {"user": "dave"})
    add("2026-01-06T10:00:00+00:00", {"user": "erin"})
    add("2026-01-06T11:00:00+00:00", {"user": "erin", "host": "db-9"})
    # Suspect window: values absent from baseline (temporal findings) and one
    # baseline-present value (must NOT be flagged).
    add("2026-01-10T10:00:00+00:00", {"user": "intruder", "host": "evil-host"})
    add("2026-01-10T11:00:00+00:00", {"user": "intruder"})
    add("2026-01-10T12:00:00+00:00", {"user": "alice"})
    return events


@pytest.fixture(scope="module")
def svc():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    store.insert_events(_fixture_events())
    service = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    service.ch = store
    yield service
    store.delete_source_events(CASE_ID, SOURCE_ID)


def _windows() -> AnalysisWindows:
    return AnalysisWindows(
        baseline=TimeWindow("baseline", BASELINE_START, SUSPECT_START),
        suspects=(TimeWindow("suspect", SUSPECT_START, SUSPECT_END),),
    )


def _oracle_rows(svc, field_key: str, *, floor: int, lim: int, windows=None) -> list[tuple]:
    """The retired per-field SQL, verbatim — test-only oracle."""
    db = svc.ch.database
    params = {"cid": CASE_ID, "src": [SOURCE_ID], "fk": field_key, "lim": lim}
    col = "attributes[{fk:String}]"
    if windows is None:
        params["floor"] = floor
        sql = f"""
            SELECT {col} AS val, count() AS cnt, min(timestamp) AS first_seen,
                   toString(argMin(event_id, timestamp)) AS evt_id
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {col} != ''
            GROUP BY val
            HAVING cnt <= {{floor:UInt32}}
            ORDER BY cnt ASC, first_seen ASC
            LIMIT {{lim:UInt32}}
        """
    else:
        bp, sps = _window_preds(windows, params, None)
        w_blocks = ", ".join(
            f"countIf({sp}) AS w{i}_cnt, minIf(timestamp, {sp}) AS w{i}_first,"
            f" toString(argMinIf(event_id, timestamp, {sp})) AS w{i}_evt"
            for i, sp in enumerate(sps)
        )
        w_sum = " + ".join(f"w{i}_cnt" for i in range(len(sps)))
        union_pred = " OR ".join([bp, *sps])
        sql = f"""
            SELECT {col} AS val, countIf({bp}) AS baseline_cnt, {w_blocks}
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {col} != ''
              AND ({union_pred})
            GROUP BY val
            HAVING baseline_cnt = 0 AND ({w_sum}) > 0
            ORDER BY ({w_sum}) ASC
            LIMIT {{lim:UInt32}}
        """
    return svc.ch.client.query(sql, parameters=params).result_rows


def _finding_keys(result):
    return [(f.field, f.value, f.count) for f in result.results]


def test_self_baseline_matches_handcrafted_expectations(svc):
    result = svc.find_value_novelty(
        CASE_ID, [SOURCE_ID], fields=["attr:user", "attr:host"], rarity_floor=2
    )
    assert result.status == "ok"
    found = {(f.field, f.value): f.count for f in result.results}
    # user: bl_only/carol/dave (1), erin (2), intruder (2); alice=11, bob=10 above floor.
    assert found[("attr:user", "carol")] == 1
    assert found[("attr:user", "dave")] == 1
    assert found[("attr:user", "bl_only")] == 1
    assert found[("attr:user", "erin")] == 2
    assert found[("attr:user", "intruder")] == 2
    assert ("attr:user", "alice") not in found
    # host: db-9 (2), evil-host (1); web-1/web-2 (5) above floor.
    assert found[("attr:host", "db-9")] == 2
    assert found[("attr:host", "evil-host")] == 1
    assert ("attr:host", "web-1") not in found


def test_self_baseline_equivalent_to_per_field_oracle(svc):
    result = svc.find_value_novelty(
        CASE_ID, [SOURCE_ID], fields=["attr:user", "attr:host"], rarity_floor=2, limit=100
    )
    batched = sorted((f.field, f.value, f.count) for f in result.results)
    oracle = sorted(
        (f"attr:{key}", str(val), int(cnt))
        for key in ("user", "host")
        for val, cnt, _first, _evt in _oracle_rows(svc, key, floor=2, lim=25)
    )
    assert batched == oracle


def test_per_field_limit_matches_oracle(svc):
    """per_field_limit=2 must keep the same survivors the per-field loop kept."""
    result = svc.find_value_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user", "attr:host"],
        rarity_floor=2,
        per_field_limit=2,
        limit=100,
    )
    batched = sorted((f.field, f.value, f.count) for f in result.results)
    oracle = sorted(
        (f"attr:{key}", str(val), int(cnt))
        for key in ("user", "host")
        for val, cnt, _first, _evt in _oracle_rows(svc, key, floor=2, lim=2)
    )
    assert batched == oracle
    # 2 survivors per field, not a global cap of 2.
    assert len(batched) == 4


def test_temporal_matches_handcrafted_expectations(svc):
    result = svc.find_value_novelty(
        CASE_ID, [SOURCE_ID], fields=["attr:user", "attr:host"], windows=_windows()
    )
    assert result.status == "ok"
    found = {(f.field, f.value): f for f in result.results}
    # Absent from baseline, present in suspect window:
    assert found[("attr:user", "intruder")].count == 2
    assert found[("attr:host", "evil-host")].count == 1
    # Baseline-present values are never temporal findings.
    assert ("attr:user", "alice") not in found
    assert ("attr:user", "bl_only") not in found
    assert all(f.details["window_label"] == "suspect" for f in result.results)


def test_temporal_equivalent_to_per_field_oracle(svc):
    result = svc.find_value_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user", "attr:host"],
        windows=_windows(),
        limit=100,
    )
    batched = sorted((f.field, f.value, f.count, f.event_id) for f in result.results)
    oracle = sorted(
        (f"attr:{key}", str(row[0]), int(row[2]), str(row[4]))
        for key in ("user", "host")
        for row in _oracle_rows(svc, key, floor=2, lim=25, windows=_windows())
    )
    assert batched == oracle
