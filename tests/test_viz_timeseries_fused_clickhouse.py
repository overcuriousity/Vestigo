"""Live-ClickHouse equivalence test for the fused field_value_timeseries scan (M24b).

Proves the single fused query (top-N ranking + per-bucket counts in one pass)
returns a response identical to the retired two-query flow (``_field_terms_impl``
for top values, then a separate bucketed scan). The old bucket SQL is kept HERE,
test-only, as the oracle. Requires the dev compose stack (skipped when
ClickHouse is unreachable), same pattern as ``test_novelty_batched_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tracesignal.db._buckets import (
    aligned_bucket_starts,
    bucket_interval_seconds,
    query_timestamp_range,
)
from tracesignal.db._dt import (
    NULL_TS_SENTINEL_ISO,
    TS_NOT_SENTINEL_SQL,
    ensure_utc,
    ensure_utc_iso,
)
from tracesignal.db._offsets import effective_ts_sql
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.queries import EventQuery, EventQueryService, _field_column_expr
from tracesignal.models.event import Event

CASE_ID = f"tc-fusedts-{uuid.uuid4().hex[:8]}"
SRC_A = "src-fused-a"
SRC_B = "src-fused-b"


def _event(i: int, source_id: str, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=source_id,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="f" * 64,
        parser_name="test-fusedts",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:fusedts",
        attributes=attrs,
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0

    def add(source_id: str, ts: str, attrs: dict[str, str]) -> None:
        nonlocal i
        events.append(_event(i, source_id, ts, attrs))
        i += 1

    # Dominant value: tcp, spread over several hours.
    for n in range(6):
        add(SRC_A, f"2026-02-01T1{n}:00:00+00:00", {"proto": "tcp", "uid": f"u{n:03d}"})
    # Exact count tie at the top-N boundary: udp and icmp both 3 plotted rows
    # — tie must break by value ASC (icmp before udp), matching the old
    # _field_terms_impl ordering.
    for n in range(3):
        add(SRC_A, f"2026-02-01T1{n}:30:00+00:00", {"proto": "udp", "uid": f"u1{n:02d}"})
        add(SRC_A, f"2026-02-01T1{n}:45:00+00:00", {"proto": "icmp", "uid": f"u2{n:02d}"})
    # Singleton values below the boundary.
    add(SRC_A, "2026-02-01T18:00:00+00:00", {"proto": "gre"})
    add(SRC_A, "2026-02-01T19:00:00+00:00", {"proto": "esp"})
    # Sentinel-timestamp row: counts toward ranking (old terms scan had no
    # sentinel filter) but must never appear as a plotted bucket. The extra
    # sentinel udp row pushes udp (4) above icmp (3) in the ranking while
    # both still plot 3 real rows.
    add(SRC_A, NULL_TS_SENTINEL_ISO, {"proto": "udp"})
    # Second source with a clock-skew offset (W2 path).
    for n in range(4):
        add(SRC_B, f"2026-02-01T2{n % 4}:10:00+00:00", {"proto": "tcp", "protocol": "tcp6"})
    # Mapped-field material: SRC_B rows carry "protocol", SRC_A rows "proto".
    add(SRC_B, "2026-02-01T22:20:00+00:00", {"protocol": "sctp"})
    # High-cardinality field: 120 distinct uids on top of those above.
    for n in range(120):
        add(SRC_A, f"2026-02-02T{n % 24:02d}:{n % 60:02d}:00+00:00", {"uid": f"hc{n:04d}"})
    return events


@pytest.fixture(scope="module")
def service():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    store.insert_events(_fixture_events())
    svc = EventQueryService(store=store)
    yield svc
    store.delete_source_events(CASE_ID, SRC_A)
    store.delete_source_events(CASE_ID, SRC_B)


def _oracle(
    svc: EventQueryService,
    query: EventQuery,
    field_token: str,
    buckets: int = 60,
    series_limit: int = 12,
) -> dict[str, Any]:
    """The retired two-query implementation, verbatim — test-only oracle."""
    where, parameters = svc._build_where(query)
    database = svc.store.database
    eff = effective_ts_sql(query.source_offsets)

    if query.start is not None and query.end is not None:
        min_ts = ensure_utc(query.start)
        max_ts = ensure_utc(query.end)
    else:
        min_ts, max_ts = query_timestamp_range(
            svc.store.client, database, where, parameters, ts_expr=eff
        )

    empty: dict[str, Any] = {
        "field": field_token,
        "interval_seconds": 0,
        "min": None,
        "max": None,
        "series": [],
    }
    if min_ts is None or max_ts is None:
        return empty

    terms = svc._field_terms_impl(query, field_token, limit=series_limit)
    top_values = [v["value"] for v in terms["values"]]
    if not top_values:
        return {
            **empty,
            "interval_seconds": 0,
            "min": min_ts.isoformat(),
            "max": max_ts.isoformat(),
        }

    interval = bucket_interval_seconds(min_ts, max_ts, buckets)
    col_expr = _field_column_expr(
        field_token, parameters, "field_key", field_mappings=query.field_mappings
    )
    parameters["series_values"] = top_values

    bucket_result = svc.store.client.query(
        f"""
        SELECT toStartOfInterval({eff}, INTERVAL {interval} second) AS bucket,
               {col_expr} AS val,
               count() AS c
        FROM {database}.events
        WHERE {where} AND {TS_NOT_SENTINEL_SQL}
            AND has({{series_values:Array(String)}}, {col_expr})
        GROUP BY bucket, val
        ORDER BY bucket
        """,
        parameters=parameters,
    )

    by_value: dict[str, dict[str, int]] = {v: {} for v in top_values}
    for bucket_ts, val, count in bucket_result.result_rows:
        by_value.setdefault(val, {})[ensure_utc_iso(bucket_ts)] = count

    all_starts = aligned_bucket_starts(min_ts, max_ts, interval)
    series = [
        {
            "value": value,
            "buckets": [
                {"start": start, "count": by_value.get(value, {}).get(start, 0)}
                for start in all_starts
            ],
        }
        for value in top_values
    ]
    return {
        "field": field_token,
        "interval_seconds": interval,
        "min": min_ts.isoformat(),
        "max": max_ts.isoformat(),
        "series": series,
    }


def _assert_equivalent(svc: EventQueryService, query: EventQuery, field: str, **kw: Any) -> None:
    assert svc.field_value_timeseries(query, field, **kw) == _oracle(svc, query, field, **kw)


def test_basic_implicit_window(service):
    _assert_equivalent(service, EventQuery(case_id=CASE_ID, source_ids=[SRC_A]), "attr:proto")


def test_top_n_boundary_tie_and_sentinel_ranking(service):
    """series_limit=2: udp (4 incl. one sentinel row) must outrank icmp (3);
    the tie-broken ordering and the sentinel row's exclusion from plotted
    buckets must match the old flow exactly."""
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC_A])
    fused = service.field_value_timeseries(query, "attr:proto", series_limit=2)
    assert [s["value"] for s in fused["series"]] == ["tcp", "udp"]
    assert sum(b["count"] for b in fused["series"][1]["buckets"]) == 3  # sentinel not plotted
    _assert_equivalent(service, query, "attr:proto", series_limit=2)
    _assert_equivalent(service, query, "attr:proto", series_limit=3)


def test_explicit_window(service):
    query = EventQuery(
        case_id=CASE_ID,
        source_ids=[SRC_A],
        start=datetime.fromisoformat("2026-02-01T10:00:00+00:00"),
        end=datetime.fromisoformat("2026-02-01T14:00:00+00:00"),
    )
    _assert_equivalent(service, query, "attr:proto")


def test_clock_offset(service):
    query = EventQuery(
        case_id=CASE_ID, source_ids=[SRC_A, SRC_B], source_offsets={SRC_B: -3600}
    )
    _assert_equivalent(service, query, "attr:proto")


def test_mapped_field(service):
    query = EventQuery(
        case_id=CASE_ID,
        source_ids=[SRC_A, SRC_B],
        field_mappings={"proto_c": ["proto", "protocol"]},
    )
    _assert_equivalent(service, query, "proto_c")


def test_high_cardinality_field(service):
    _assert_equivalent(
        service,
        EventQuery(case_id=CASE_ID, source_ids=[SRC_A]),
        "attr:uid",
        series_limit=12,
        buckets=24,
    )


def test_empty_field(service):
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC_A])
    _assert_equivalent(service, query, "attr:nonexistent")


def test_filtered_query(service):
    query = EventQuery(
        case_id=CASE_ID, source_ids=[SRC_A], field_filters={"proto": ["tcp", "udp"]}
    )
    _assert_equivalent(service, query, "attr:proto")
