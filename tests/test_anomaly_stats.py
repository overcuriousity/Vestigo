"""Tests for StatisticalAnomalyService.

All tests use fakes/mocks for ClickHouse so they run without external services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tracevector.db.anomaly_stats import (
    FreqFinding,
    NoveltyFieldInfo,
    StatisticalAnomalyService,
    ValueFinding,
    _classify_field,
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
        self._all_parameters: list[dict] = []

    def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
        self._calls.append(sql.strip().split("\n")[0].strip())
        self._all_parameters.append(parameters or {})
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
    assert _col_expr("artifact", params) == "artifact"
    assert params == {}


def test_col_expr_top_level_column_shared_with_queries_allowlist():
    """F10: anomaly_stats and queries.py share one top-level-column allowlist
    (via db._columns), so a field like `parser_version` — a real top-level
    column, not previously in anomaly_stats' narrower local list — resolves
    to the column here too instead of silently becoming an always-empty
    attribute lookup."""
    params: dict[str, Any] = {}
    assert _col_expr("parser_version", params) == "parser_version"
    assert params == {}


def test_col_expr_attr_prefix():
    params: dict[str, Any] = {}
    expr = _col_expr("attr:user_agent", params)
    assert expr == "attributes[{fk:String}]"
    assert params == {"fk": "user_agent"}


def test_col_expr_bare_attr_name():
    """Bare names not in the top-level set are treated as attribute keys."""
    params: dict[str, Any] = {}
    expr = _col_expr("ip_address", params)
    assert expr == "attributes[{fk:String}]"
    assert params["fk"] == "ip_address"


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
                # Naive datetimes — matches clickhouse-connect's real return type
                # for a DateTime column with no explicit timezone.
                ("suspicious.exe", 1, datetime(2024, 1, 2), "evt-1", "s1", "msg1"),
                ("unusual_tool", 2, datetime(2024, 1, 1), "evt-2", "s1", "msg2"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
        # timestamp_desc field: one rare value
        FakeQueryResult(
            result_rows=[
                ("Malware execution", 1, datetime(2024, 1, 2, 1), "evt-3", "s1", "msg3"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
        # display_name field: no rare values
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
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
                (f"val_{i}", 1, datetime(2024, 1, 1), f"evt-{j * 3 + i}", "s1", "m")
                for i in range(3)
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        )
        for j in range(3)
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
            *per_field,
        ]
    )
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact", "timestamp_desc", "display_name"],
        limit=4,
    )
    assert result.status == "ok"
    assert len(result.results) <= 4


def test_value_novelty_event_id_populated():
    """Each finding carries the event_id of its first occurrence."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("backdoor.exe", 1, datetime(2024, 1, 1), "evt-abc", "s1", "bad msg")],
                column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.event_id == "evt-abc"
    assert r.event is not None
    assert r.event["message"] == "bad msg"
    assert r.value == "backdoor.exe"
    # first_seen must carry an explicit UTC offset — a bare "YYYY-MM-DD
    # HH:MM:SS" string is ambiguous to JS's Date parser (browsers treat it as
    # local time), which silently shifted histogram markers and event-grid
    # anomaly matching by the browser's UTC offset.
    assert r.first_seen is not None
    assert r.first_seen.endswith("+00:00") or r.first_seen.endswith("Z")
    assert r.event["timestamp"] == r.first_seen


def test_value_novelty_skips_empty_values():
    """Rows with empty string values must be filtered out."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[
                    ("", 1, datetime(2024, 1, 1), "evt-1", "s1", "msg"),
                    ("real_value", 2, datetime(2024, 1, 1), "evt-2", "s1", "msg2"),
                ],
                column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
            ),
        ]
    )
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
    svc = _svc(
        [
            # Total events
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            # Baseline size
            FakeQueryResult(result_rows=[(300,)], column_names=["count()"]),
            # artifact field: one first-seen value
            FakeQueryResult(
                result_rows=[
                    (
                        "first_time_process.exe",
                        3,
                        0,
                        datetime(2024, 1, 16),
                        "evt-9",
                        "s1",
                        "new exe",
                    ),
                ],
                column_names=[
                    "val",
                    "detect_cnt",
                    "baseline_cnt",
                    "first_seen",
                    "evt_id",
                    "src_id",
                    "msg",
                ],
            ),
        ]
    )
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
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


def test_value_novelty_temporal_baseline_converts_non_utc_offset_for_sql():
    """A `baseline_end` with a non-UTC offset (e.g. FastAPI-parsed `+02:00`)
    must be converted to the equivalent UTC instant before being spliced
    into the ClickHouse SQL string literal — otherwise the temporal split
    lands 2 hours later than intended (F8)."""
    from datetime import timedelta, timezone

    plus_two = timezone(timedelta(hours=2))
    baseline_end = datetime(2024, 1, 15, 14, 0, 0, tzinfo=plus_two)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(300,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[],
                column_names=[
                    "val",
                    "detect_cnt",
                    "baseline_cnt",
                    "first_seen",
                    "evt_id",
                    "src_id",
                    "msg",
                ],
            ),
        ]
    )
    svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact"],
        baseline_end=baseline_end,
    )
    bl_values = [p["bl"] for p in svc.ch.client._all_parameters if "bl" in p]
    assert bl_values
    assert all(v == "2024-01-15 12:00:00" for v in bl_values)


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
        # Default: flat series of 5 buckets + one spike (4h buckets for a
        # 24h window with 6 buckets).
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
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(None, None)], column_names=["min", "max"]),
        ]
    )
    result = svc.find_frequency_anomalies("c1", ["s1"])
    assert result.status == "no_data"


def test_frequency_no_data_when_no_bucket_rows():
    """Returns no_data when the bucket query returns no rows."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
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
        "c1",
        ["s1"],
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
    bucket_rows = [(min_dt.replace(hour=h * 4), "LOG", 10) for h in range(6)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=2.0)
    assert result.status == "ok"
    assert result.results == []


def test_frequency_insufficient_buckets_series_skipped():
    """Series with < _MIN_FREQUENCY_BUCKETS data points are skipped, and the
    result says so via status="insufficient_data" rather than a silent "ok"
    with an empty result list (indistinguishable from "nothing anomalous")."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    # Only 2 buckets — below the minimum of 3.
    bucket_rows = [
        (min_dt, "LOG", 10),
        (min_dt.replace(hour=12), "LOG", 100),
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=1.0)
    assert result.status == "insufficient_data"
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
            bucket_rows.append(
                (
                    min_dt.replace(hour=0) + __import__("datetime").timedelta(hours=h * 12 + i * 2),
                    sv,
                    cnt,
                )
            )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
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
    # 3 baseline buckets (slightly varied so std > 0) + 1 detect bucket (spike).
    # Variation is small relative to the spike so the detect window is anomalous.
    bucket_rows = [
        (min_dt, "LOG", 10),
        (min_dt + timedelta(hours=4), "LOG", 12),
        (min_dt + timedelta(hours=8), "LOG", 11),
        # Detect window (after baseline_end): massive spike.
        (min_dt + timedelta(hours=14), "LOG", 200),
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        baseline_end=baseline_end,
        z_threshold=2.0,
    )
    assert result.status == "ok"
    assert result.method == "temporal-z-score"
    assert len(result.results) >= 1
    spike = result.results[0]
    assert spike.observed == 200
    assert spike.z_score > 0


def test_frequency_temporal_baseline_zero_baseline_flagged():
    """A series absent from the baseline but active in the detect window is flagged.

    Regression test: previously any series with zero baseline buckets was
    skipped outright (no std computable), silently missing exactly the
    "brand-new activity after the incident start" case temporal mode exists
    to surface.
    """
    from datetime import datetime, timedelta

    baseline_end = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    bucket_rows = [
        # "NEWPROC" never appears before baseline_end, only after.
        (min_dt + timedelta(hours=13), "NEWPROC", 50),
        (min_dt + timedelta(hours=14), "NEWPROC", 60),
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        baseline_end=baseline_end,
        z_threshold=2.0,
    )
    assert result.status == "ok"
    values = {r.series_value for r in result.results}
    assert "NEWPROC" in values


def test_frequency_exclude_event_ids_backfills_from_next_ranked():
    """Excluded findings must be dropped before the limit slice, not after.

    Regression test: filtering exclude_event_ids after `[:limit]` silently
    shrinks the page below `limit` when the top-ranked finding is excluded,
    instead of promoting the next-ranked finding to fill the slot.
    """
    from dataclasses import replace as _replace
    from datetime import datetime as _dt

    min_dt = _dt(2024, 1, 1, tzinfo=UTC)
    bucket_rows = [
        # Series "A": baseline of 10s + a huge spike (highest-ranked finding).
        (min_dt, "A", 10),
        (min_dt.replace(hour=4), "A", 10),
        (min_dt.replace(hour=8), "A", 10),
        (min_dt.replace(hour=12), "A", 100),
        # Series "B": baseline of 10s + a smaller spike (lower-ranked finding).
        (min_dt.replace(hour=16), "B", 10),
        (min_dt.replace(hour=18), "B", 10),
        (min_dt.replace(hour=20), "B", 10),
        (min_dt.replace(hour=22), "B", 40),
    ]
    svc = _svc(_make_freq_responses(bucket_rows=bucket_rows))

    def _fake_hydrate(findings, *a, **kw):
        # Assign event ids in insertion order: series "A" is scanned first,
        # so its spike (the higher-ranked finding) becomes evt-0.
        return [_replace(f, event_id=f"evt-{i}") for i, f in enumerate(findings)]

    svc._hydrate_freq_findings = _fake_hydrate  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        z_threshold=1.0,
        limit=1,
        exclude_event_ids={"evt-0"},
    )
    assert result.status == "ok"
    # With the top-ranked finding (series "A") excluded, series "B"'s
    # finding must backfill the slot rather than leaving the page empty.
    assert len(result.results) == 1
    assert result.results[0].series_value == "B"


# ---------------------------------------------------------------------------
# _classify_field unit tests
# ---------------------------------------------------------------------------


def test_classify_field_constant():
    kind, recommended = _classify_field(distinct=1, non_empty_count=1000)
    assert kind == "constant"
    assert recommended is False


def test_classify_field_zero_distinct():
    kind, recommended = _classify_field(distinct=0, non_empty_count=0)
    assert kind == "constant"
    assert recommended is False


def test_classify_field_identifier():
    # 900 unique values out of 1000 non-empty → ratio = 0.9 = exact boundary
    kind, recommended = _classify_field(distinct=900, non_empty_count=1000)
    assert kind == "identifier"
    assert recommended is False


def test_classify_field_identifier_near_unique():
    # Near-unique: hash-like field (9800/9800 = 1.0)
    kind, recommended = _classify_field(distinct=9800, non_empty_count=9800)
    assert kind == "identifier"
    assert recommended is False


def test_classify_field_sparse():
    # Only 1% coverage — sparse (50 non-empty out of 5000 total)
    kind, recommended = _classify_field(distinct=50, non_empty_count=50, total=5000)
    assert kind == "sparse"
    assert recommended is False


def test_classify_field_categorical():
    # Good cardinality and coverage: 10 distinct out of 950 non-empty (0.01 ratio)
    kind, recommended = _classify_field(distinct=10, non_empty_count=950, total=1000)
    assert kind == "categorical"
    assert recommended is True


def test_classify_field_categorical_moderate():
    # status_code-like: 5 distinct, 1000 non-empty
    kind, recommended = _classify_field(distinct=5, non_empty_count=1000, total=1000)
    assert kind == "categorical"
    assert recommended is True


# ---------------------------------------------------------------------------
# recommend_novelty_fields tests
# ---------------------------------------------------------------------------


def _svc_with_recommend_responses(
    top_row: tuple,
    attr_rows: list[tuple],
    total: int = 1000,
) -> StatisticalAnomalyService:
    """Build a service whose FakeClient provides recommend_novelty_fields responses."""
    responses = [
        # Total count
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # Top-level batch (4 columns × 2 agg each = 8 values in one row)
        FakeQueryResult(result_rows=[top_row], column_names=["c"] * 8),
        # Attribute keys + cardinality
        FakeQueryResult(result_rows=attr_rows, column_names=["key", "dist", "cov_count"]),
    ]
    return _svc(responses)


def test_recommend_novelty_fields_empty_on_no_data():
    """Returns empty list when no events exist."""
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.recommend_novelty_fields("c1", ["s1"])
    assert result == []


def test_recommend_novelty_fields_categorical_recommended():
    """Categorical fields (moderate cardinality) should be recommended."""
    # artifact: 5 distinct, 1000 non-empty  → categorical
    # timestamp_desc: 20 distinct, 950 non-empty → categorical
    # display_name: 1 distinct (constant) → not recommended
    # parser_name: 1000 distinct, 1000 non-empty (identifier, ratio=1.0) → not recommended
    # (total=1000 for all coverage computations)
    top_row = (
        5,
        1000,  # artifact: 5 distinct, 1000 non-empty
        20,
        950,  # timestamp_desc: 20 distinct, 950 non-empty
        1,
        900,  # display_name: 1 distinct → constant
        1000,
        1000,  # parser_name: 1000/1000 = 1.0 → identifier
    )
    attr_rows = [
        # (key, distinct, non_empty_count)
        ("status_code", 6, 1000),  # 6/1000=0.006 → categorical
        ("url_path", 840, 1000),  # 840/1000=0.84 < 0.9 → categorical
        ("session_id", 980, 980),  # 980/980=1.0 → identifier
    ]
    svc = _svc_with_recommend_responses(top_row, attr_rows)
    result = svc.recommend_novelty_fields("c1", ["s1"])

    assert isinstance(result, list)
    assert all(isinstance(f, NoveltyFieldInfo) for f in result)

    by_token = {f.token: f for f in result}

    # artifact: categorical
    assert by_token["artifact"].kind == "categorical"
    assert by_token["artifact"].recommended is True

    # timestamp_desc: categorical
    assert by_token["timestamp_desc"].kind == "categorical"
    assert by_token["timestamp_desc"].recommended is True

    # display_name: constant
    assert by_token["display_name"].kind == "constant"
    assert by_token["display_name"].recommended is False

    # parser_name: identifier
    assert by_token["parser_name"].kind == "identifier"
    assert by_token["parser_name"].recommended is False

    # status_code: categorical
    assert by_token["attr:status_code"].kind == "categorical"
    assert by_token["attr:status_code"].recommended is True

    # session_id: identifier
    assert by_token["attr:session_id"].kind == "identifier"
    assert by_token["attr:session_id"].recommended is False


def test_recommend_novelty_fields_recommended_first():
    """Recommended fields should appear before non-recommended ones."""
    top_row = (
        5,
        1000,  # artifact — categorical
        20,
        950,  # timestamp_desc — categorical
        1,
        900,  # display_name — constant → not recommended
        1000,
        1000,  # parser_name — identifier → not recommended
    )
    attr_rows: list[tuple] = []
    svc = _svc_with_recommend_responses(top_row, attr_rows)
    result = svc.recommend_novelty_fields("c1", ["s1"])

    recommended = [f for f in result if f.recommended]
    not_recommended = [f for f in result if not f.recommended]
    # All recommended fields should appear before non-recommended.
    if recommended and not_recommended:
        last_rec_idx = max(i for i, f in enumerate(result) if f.recommended)
        first_not_rec_idx = min(i for i, f in enumerate(result) if not f.recommended)
        assert last_rec_idx < first_not_rec_idx


def test_field_inventory_empty_on_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    inventory, total = svc.field_inventory("c1", ["s1"])
    assert inventory == []
    assert total == 0


def test_field_inventory_returns_unclassified_counts():
    """Every candidate field appears with raw distinct/non-empty counts —
    no novelty classification, no constant/identifier filtering."""
    top_row = (
        5,
        1000,  # artifact
        20,
        950,  # timestamp_desc
        1,
        900,  # display_name — constant, but still listed
        1000,
        1000,  # parser_name — identifier, but still listed
    )
    attr_rows = [
        ("status_code", 6, 1000),
        ("session_id", 980, 980),
    ]
    svc = _svc_with_recommend_responses(top_row, attr_rows)
    inventory, total = svc.field_inventory("c1", ["s1"])

    assert total == 1000
    assert inventory == [
        ("artifact", 5, 1000),
        ("timestamp_desc", 20, 950),
        ("display_name", 1, 900),
        ("parser_name", 1000, 1000),
        ("attr:status_code", 6, 1000),
        ("attr:session_id", 980, 980),
    ]


def test_field_inventory_skips_count_query_when_total_supplied():
    top_row = (5, 100, 2, 90, 1, 80, 100, 100)
    responses = [
        FakeQueryResult(result_rows=[top_row], column_names=["c"] * 8),
        FakeQueryResult(result_rows=[], column_names=["key", "dist", "cov_count"]),
    ]
    svc = _svc(responses)
    inventory, total = svc.field_inventory("c1", ["s1"], total=100)
    assert total == 100
    assert len(inventory) == 4
    # No count() round-trip: only the two enumeration queries ran.
    assert len(svc.ch.client._calls) == 2


# ---------------------------------------------------------------------------
# find_value_novelty — smart default via recommender
# ---------------------------------------------------------------------------


def test_value_novelty_smart_default_calls_recommender():
    """When fields=None, the recommender is invoked and its tokens are used.

    find_value_novelty computes `total` once and passes it into
    recommend_novelty_fields, which then skips its own identical count()
    query (C12) — so the total is queried only once, not twice.
    """
    total = 500
    responses = [
        # find_value_novelty's own total (reused by recommend_novelty_fields):
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # recommend_novelty_fields (total supplied, no count() query of its own):
        FakeQueryResult(
            result_rows=[(5, 500, 1, 500, 1, 500, 1, 500)],  # 1 categorical, 3 constant/id
            column_names=["c"] * 8,
        ),  # top-level batch
        FakeQueryResult(
            result_rows=[("status_code", 6, 500)],  # attr categorical
            column_names=["key", "dist", "cov_count"],
        ),  # attribute keys
        # find_value_novelty field scans (recommended fields = artifact + attr:status_code):
        FakeQueryResult(
            result_rows=[("404", 2, datetime(2024, 1, 1), "evt-1", "s1", "404 not found")],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),  # artifact field scan
        FakeQueryResult(
            result_rows=[],
            column_names=[],
        ),  # attr:status_code field scan
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty("c1", ["s1"], rarity_floor=3)
    assert result.status == "ok"
    # Findings from the attribute field should be present.
    assert len(result.results) >= 1
    # Only one total-count round trip, not one per caller (C12).
    total_count_calls = [c for c in svc.ch.client._calls if c.startswith("SELECT count()")]
    assert len(total_count_calls) == 1


def test_value_novelty_auto_mode_caps_scanned_fields():
    """C11: auto-selected fields are capped at _MAX_AUTO_SCAN_FIELDS — each
    field is a separate sequential ClickHouse round-trip, so an uncapped
    recommended set could turn one panel-open into dozens of them."""
    from tracevector.db.anomaly_stats import _MAX_AUTO_SCAN_FIELDS, NoveltyFieldInfo

    total = 1000
    many_fields = [
        NoveltyFieldInfo(
            token=f"attr:field_{i}",
            distinct=10,
            coverage=0.9,
            kind="categorical",
            recommended=True,
        )
        for i in range(_MAX_AUTO_SCAN_FIELDS + 10)
    ]
    svc = _svc([FakeQueryResult(result_rows=[(total,)], column_names=["count()"])])
    svc.recommend_novelty_fields = lambda *a, **k: many_fields  # noqa: ARG005

    svc.find_value_novelty("c1", ["s1"], rarity_floor=3)

    # First call is the total() count; every call after it is one field scan.
    field_scan_calls = svc.ch.client._calls[1:]
    assert len(field_scan_calls) == _MAX_AUTO_SCAN_FIELDS


# ---------------------------------------------------------------------------
# exclude_event_ids suppression
# ---------------------------------------------------------------------------


def test_value_novelty_exclude_event_ids():
    """Findings whose event_id is in exclude_event_ids should be suppressed."""
    total = 100
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("malware.exe", 1, datetime(2024, 1, 1), "evt-bad", "s1", "msg"),
                ("tool.exe", 2, datetime(2024, 1, 1), "evt-ok", "s1", "msg2"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id", "src_id", "msg"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact"],
        exclude_event_ids={"evt-bad"},
    )
    assert result.status == "ok"
    event_ids = [r.event_id for r in result.results]
    assert "evt-bad" not in event_ids
    assert "evt-ok" in event_ids


def test_frequency_exclude_event_ids():
    """Findings whose event_id is in exclude_event_ids are suppressed after hydration."""
    svc = _svc(_make_freq_responses())

    # Hydrate with a pre-set event_id so we can test suppression.
    def _fake_hydrate(findings, *a, **kw):
        out = []
        for i, f in enumerate(findings):
            from dataclasses import replace as _replace

            out.append(_replace(f, event_id=f"evt-{i}"))
        return out

    svc._hydrate_freq_findings = _fake_hydrate  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        z_threshold=2.0,
        exclude_event_ids={"evt-0"},
    )
    assert result.status == "ok"
    assert all(f.event_id != "evt-0" for f in result.results)


def test_hydrate_freq_findings_batches_into_a_single_query():
    """_hydrate_freq_findings issues one query for all findings, not one each.

    Regression test: hydration previously ran one ClickHouse round-trip per
    finding (up to `limit`, capped at 500); it must now batch every
    (series_value, window) pair into a single grouped query.
    """
    from datetime import timedelta

    from tracevector.db.anomaly_stats import _EVENT_COLUMNS, FreqFinding

    bucket_a = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    bucket_b = datetime(2024, 1, 1, 4, 0, 0, tzinfo=UTC)
    findings = [
        FreqFinding(
            series_field="artifact",
            series_value="A",
            window_start=bucket_a.isoformat(),
            window_end=(bucket_a + timedelta(hours=1)).isoformat(),
            observed=100,
            expected=10.0,
            z_score=90.0,
            score=90.0,
            event_id=None,
            event=None,
            details={},
        ),
        FreqFinding(
            series_field="artifact",
            series_value="B",
            window_start=bucket_b.isoformat(),
            window_end=(bucket_b + timedelta(hours=1)).isoformat(),
            observed=40,
            expected=10.0,
            z_score=30.0,
            score=30.0,
            event_id=None,
            event=None,
            details={},
        ),
    ]

    def _build_row(event_id: str, message: str) -> tuple:
        values = {"event_id": event_id, "case_id": "c1", "source_id": "s1", "message": message}
        return tuple(values.get(col) for col in _EVENT_COLUMNS)

    row_a = _build_row("evt-a", "spike A")
    row_b = _build_row("evt-b", "spike B")
    svc = _svc(
        [
            FakeQueryResult(
                result_rows=[
                    (bucket_a.replace(tzinfo=None), "A", *row_a),
                    (bucket_b.replace(tzinfo=None), "B", *row_b),
                ],
                column_names=["bucket", "series_val", *_EVENT_COLUMNS],
            ),
        ]
    )

    hydrated = svc._hydrate_freq_findings(
        findings,
        "c1",
        ["s1"],
        "artifact",
        "tracevector",
        {},
        3600,
    )

    assert len(svc.ch.client._calls) == 1
    assert {f.event_id for f in hydrated} == {"evt-a", "evt-b"}
    by_value = {f.series_value: f for f in hydrated}
    assert by_value["A"].event["message"] == "spike A"
    assert by_value["B"].event["message"] == "spike B"


# ---------------------------------------------------------------------------
# get_timeline_midpoint
# ---------------------------------------------------------------------------


def test_get_timeline_midpoint_returns_midpoint():
    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 3, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        ]
    )
    mid = svc.get_timeline_midpoint("c1", ["s1"])
    assert mid == datetime(2024, 1, 2, tzinfo=UTC)


def test_get_timeline_midpoint_returns_none_when_no_events():
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(None, None)], column_names=["min", "max"]),
        ]
    )
    assert svc.get_timeline_midpoint("c1", ["s1"]) is None
