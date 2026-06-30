"""Tests for StatisticalAnomalyService.

All tests use fakes/mocks for ClickHouse so they run without external services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tracevector.db.anomaly_stats import (
    FreqFinding,
    StatisticalAnomalyService,
    ValueFinding,
    _col_expr,
)


# ---------------------------------------------------------------------------
# Fake ClickHouse infrastructure
# ---------------------------------------------------------------------------


@dataclass
class FakeQueryResult:
    result_rows: list[tuple]
    column_names: list[str]


class FakeClient:
    """Minimal ClickHouse client fake driven by a pre-seeded query→result map."""

    def __init__(self, responses: list[FakeQueryResult]) -> None:
        # Responses are consumed in order (FIFO) for each query call.
        self._responses: list[FakeQueryResult] = list(responses)
        self._calls: list[str] = []

    def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
        self._calls.append(sql.strip().split("\n")[0].strip())
        if self._responses:
            return self._responses.pop(0)
        return FakeQueryResult(result_rows=[], column_names=[])


class FakeClickHouseStore:
    """Minimal ClickHouseStore wrapper using FakeClient."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.database = "tracevector"

    def init_schema(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _svc(responses: list[FakeQueryResult]) -> StatisticalAnomalyService:
    """Build a service backed by a FakeClient with canned responses."""
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(FakeClient(responses))
    return svc


# ---------------------------------------------------------------------------
# _col_expr unit tests
# ---------------------------------------------------------------------------


def test_col_expr_top_level_column():
    params: dict[str, Any] = {}
    ctr = [0]
    assert _col_expr("artifact", params, ctr) == "artifact"
    assert params == {}
    assert ctr == [0]


def test_col_expr_attr_prefix():
    params: dict[str, Any] = {}
    ctr = [0]
    expr = _col_expr("attr:user_agent", params, ctr)
    assert expr == "attributes[{fk0:String}]"
    assert params == {"fk0": "user_agent"}
    assert ctr == [1]


def test_col_expr_bare_attr_name():
    """Bare names not in the top-level set are treated as attribute keys."""
    params: dict[str, Any] = {}
    ctr = [0]
    expr = _col_expr("ip_address", params, ctr)
    assert expr == "attributes[{fk0:String}]"
    assert params["fk0"] == "ip_address"


def test_col_expr_counter_increments():
    params: dict[str, Any] = {}
    ctr = [0]
    _col_expr("attr:a", params, ctr)
    _col_expr("attr:b", params, ctr)
    assert "fk0" in params
    assert "fk1" in params
    assert ctr == [2]


# ---------------------------------------------------------------------------
# find_value_novelty — self-baseline
# ---------------------------------------------------------------------------


def test_value_novelty_no_data():
    """Returns no_data when total_events = 0."""
    responses = [
        FakeQueryResult(result_rows=[(0,)], column_names=["count()"]),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty("c1", ["s1"])
    assert result.status == "no_data"
    assert result.results == []


def test_value_novelty_self_baseline_returns_rare_values():
    """Rare values (count ≤ rarity_floor) should be returned, rarest first."""
    import math

    total = 1000
    responses = [
        # Total events count
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # artifact field: two rare values
        FakeQueryResult(
            result_rows=[
                ("suspicious.exe", 1, "2024-01-02 00:00:00", "evt-1", "s1", "msg1"),
                ("unusual_tool", 2, "2024-01-01 00:00:00", "evt-2", "s1", "msg2"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
        # timestamp_desc field: one rare value
        FakeQueryResult(
            result_rows=[
                ("Malware execution", 1, "2024-01-02 01:00:00", "evt-3", "s1", "msg3"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
        # display_name field: no rare values
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1", ["s1"],
        fields=["artifact", "timestamp_desc", "display_name"],
        rarity_floor=3,
        limit=50,
    )

    assert result.status == "ok"
    assert result.detector == "value_novelty"
    assert result.method == "self-baseline"
    assert len(result.results) == 3  # 2 + 1

    # All findings are ValueFinding instances.
    assert all(isinstance(r, ValueFinding) for r in result.results)

    # Sorted by score descending (count=1 has higher score than count=2).
    counts = [r.count for r in result.results]
    assert counts[0] <= counts[-1], "Rarest values should rank first"

    # Score = -log(count/total); count=1 → -log(1/1000) ≈ 6.9
    top = result.results[0]
    expected_score = -math.log(1 / total)
    assert abs(top.score - expected_score) < 0.01

    # Details shape.
    for r in result.results:
        d = r.details
        assert d["detector"] == "value_novelty"
        assert d["method"] == "self-baseline"
        assert "field" in d
        assert "value" in d
        assert "count" in d
        assert "surprise" in d


def test_value_novelty_self_baseline_limit_applied():
    """Limit caps the number of returned findings across all fields."""
    total = 500
    # Three fields each returning 3 rare values → 9 total, but limit=4.
    per_field = [
        FakeQueryResult(
            result_rows=[
                (f"val_{i}", 1, "2024-01-01", f"evt-{j*3+i}", "s1", "m")
                for i in range(3)
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        )
        for j in range(3)
    ]
    svc = _svc([
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        *per_field,
    ])
    result = svc.find_value_novelty(
        "c1", ["s1"],
        fields=["artifact", "timestamp_desc", "display_name"],
        limit=4,
    )
    assert result.status == "ok"
    assert len(result.results) <= 4


def test_value_novelty_event_id_populated():
    """Each finding carries the event_id of its first occurrence."""
    svc = _svc([
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[("backdoor.exe", 1, "2024-01-01", "evt-abc", "s1", "bad msg")],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
    ])
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.event_id == "evt-abc"
    assert r.event is not None
    assert r.event["message"] == "bad msg"
    assert r.value == "backdoor.exe"


def test_value_novelty_skips_empty_values():
    """Rows with empty string values must be filtered out."""
    svc = _svc([
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("", 1, "2024-01-01", "evt-1", "s1", "msg"),
                ("real_value", 2, "2024-01-01", "evt-2", "s1", "msg2"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
    ])
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    assert result.status == "ok"
    values = [r.value for r in result.results]
    assert "" not in values
    assert "real_value" in values


# ---------------------------------------------------------------------------
# find_value_novelty — temporal baseline
# ---------------------------------------------------------------------------


def test_value_novelty_temporal_baseline_first_seen():
    """Temporal mode flags values absent in baseline but present in detect window."""
    baseline_end = datetime(2024, 1, 15, tzinfo=UTC)
    svc = _svc([
        # Total events
        FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
        # Baseline size
        FakeQueryResult(result_rows=[(300,)], column_names=["count()"]),
        # artifact field: one first-seen value
        FakeQueryResult(
            result_rows=[
                ("first_time_process.exe", 3, 0, "2024-01-16 00:00:00", "evt-9", "s1", "new exe"),
            ],
            column_names=["val", "detect_cnt", "baseline_cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
    ])
    result = svc.find_value_novelty(
        "c1", ["s1"],
        fields=["artifact"],
        baseline_end=baseline_end,
    )
    assert result.status == "ok"
    assert result.method == "temporal"
    assert result.baseline_size == 300
    assert len(result.results) == 1
    r = result.results[0]
    assert r.value == "first_time_process.exe"
    assert r.count == 3
    assert r.details["method"] == "temporal"
    assert "baseline_size" in r.details


# ---------------------------------------------------------------------------
# find_frequency_anomalies
# ---------------------------------------------------------------------------


def _make_freq_responses(
    min_ts: str = "2024-01-01 00:00:00",
    max_ts: str = "2024-01-02 00:00:00",
    bucket_rows: list[tuple] | None = None,
) -> list[FakeQueryResult]:
    """Build the canned responses for find_frequency_anomalies."""
    from datetime import datetime

    min_dt = datetime.strptime(min_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    max_dt = datetime.strptime(max_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    if bucket_rows is None:
        # Default: flat series of 5 buckets + one spike.
        interval = 14400  # 4h buckets for a 24h window with 6 buckets
        bucket_rows = [
            (min_dt, "LOG", 10),
            (min_dt.replace(hour=4), "LOG", 10),
            (min_dt.replace(hour=8), "LOG", 10),
            (min_dt.replace(hour=12), "LOG", 10),
            (min_dt.replace(hour=16), "LOG", 10),
            # Spike: 5× the mean
            (min_dt.replace(hour=20), "LOG", 50),
        ]

    return [
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
    ]


def test_frequency_no_data_when_no_events():
    """Returns no_data when the events table has no timestamps."""
    svc = _svc([
        FakeQueryResult(result_rows=[(None, None)], column_names=["min", "max"]),
    ])
    result = svc.find_frequency_anomalies("c1", ["s1"])
    assert result.status == "no_data"


def test_frequency_no_data_when_no_bucket_rows():
    """Returns no_data when the bucket query returns no rows."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _svc([
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ])
    result = svc.find_frequency_anomalies("c1", ["s1"])
    assert result.status == "no_data"


def test_frequency_spike_detected():
    """A window 5× the series mean should be flagged as anomalous."""
    svc = _svc(_make_freq_responses())
    # Suppress the hydration query for this test (no DB available).
    # find_frequency_anomalies calls _hydrate_freq_findings which hits CH.
    # We monkey-patch it to be a no-op.
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"],
        series_field="artifact",
        z_threshold=2.0,
        limit=10,
    )
    assert result.status == "ok"
    assert result.detector == "frequency"
    assert result.method == "z-score"
    assert len(result.results) >= 1

    # The spike bucket should rank first.
    top = result.results[0]
    assert isinstance(top, FreqFinding)
    assert top.observed > top.expected
    assert top.z_score > 0
    assert top.score == abs(top.z_score)
    assert top.series_field == "artifact"
    assert top.series_value == "LOG"


def test_frequency_details_shape():
    """FreqFinding.details carries the expected keys."""
    svc = _svc(_make_freq_responses())
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=2.0)
    assert result.status == "ok"
    for r in result.results:
        d = r.details
        assert d["detector"] == "frequency"
        assert "series_field" in d
        assert "series_value" in d
        assert "window_start" in d
        assert "window_end" in d
        assert "observed" in d
        assert "expected" in d
        assert "z_score" in d
        assert "interval_seconds" in d


def test_frequency_constant_series_ignored():
    """A perfectly flat series (std=0) must not produce any findings."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    # 6 perfectly uniform buckets.
    bucket_rows = [
        (min_dt.replace(hour=h * 4), "LOG", 10)
        for h in range(6)
    ]
    svc = _svc([
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
    ])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=2.0)
    assert result.status == "ok"
    assert result.results == []


def test_frequency_insufficient_buckets_series_skipped():
    """Series with < _MIN_FREQUENCY_BUCKETS data points are skipped."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    # Only 2 buckets — below the minimum of 3.
    bucket_rows = [
        (min_dt, "LOG", 10),
        (min_dt.replace(hour=12), "LOG", 100),
    ]
    svc = _svc([
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
    ])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=1.0)
    assert result.status == "ok"
    assert result.results == []


def test_frequency_limit_applied():
    """Limit caps the number of findings returned."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 4, tzinfo=UTC)
    # 3 series each with one spike bucket.
    bucket_rows = []
    for i, sv in enumerate(["A", "B", "C"]):
        for h in range(5):
            cnt = 10 if h < 4 else 100  # spike on last bucket
            bucket_rows.append((min_dt.replace(hour=0) + __import__("datetime").timedelta(hours=h * 12 + i * 2), sv, cnt))
    svc = _svc([
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
    ])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], limit=2, z_threshold=2.0)
    assert len(result.results) <= 2


def test_frequency_sorted_by_z_score():
    """Findings must be sorted by |z_score| descending."""
    svc = _svc(_make_freq_responses())
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=1.0)
    scores = [r.score for r in result.results]
    assert scores == sorted(scores, reverse=True)


def test_frequency_temporal_baseline():
    """Temporal mode uses only baseline-window buckets for mean/std."""
    from datetime import datetime, timedelta

    baseline_end = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    # 3 baseline buckets (flat) + 1 detect bucket (spike).
    bucket_rows = [
        (min_dt, "LOG", 10),
        (min_dt + timedelta(hours=4), "LOG", 10),
        (min_dt + timedelta(hours=8), "LOG", 10),
        # Detect window (after baseline_end):
        (min_dt + timedelta(hours=14), "LOG", 200),
    ]
    svc = _svc([
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
    ])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"],
        baseline_end=baseline_end,
        z_threshold=2.0,
    )
    assert result.status == "ok"
    assert result.method == "temporal-z-score"
    assert len(result.results) >= 1
    spike = result.results[0]
    assert spike.observed == 200
    assert spike.z_score > 0
