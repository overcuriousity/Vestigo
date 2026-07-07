"""Tests for StatisticalAnomalyService.

All tests use fakes/mocks for ClickHouse so they run without external services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tracesignal.db.anomaly_stats import (
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


class RecordingClient(FakeClient):
    """FakeClient that also captures the full SQL text of every query.

    Used by the temporal-mode tests to assert on the baseline/detect clauses.
    """

    def __init__(self, responses: list[FakeQueryResult]) -> None:
        super().__init__(responses)
        self.full_queries: list[str] = []

    def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
        self.full_queries.append(sql)
        return super().query(sql, parameters)


class FakeClickHouseStore:
    """Minimal ClickHouseStore wrapper using FakeClient."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.database = "tracesignal"
        # Seedable hydration source for get_events_by_ids; calls are recorded
        # so tests can assert hydration is one batched fetch.
        self.events_by_id: dict[str, dict] = {}
        self.hydration_calls: list[list[str]] = []

    def init_schema(self) -> None:
        pass

    def get_events_by_ids(
        self, case_id: str, source_ids: list[str], event_ids: list[str]
    ) -> dict[str, dict]:
        self.hydration_calls.append(list(event_ids))
        return {i: self.events_by_id[i] for i in event_ids if i in self.events_by_id}


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


def test_value_novelty_supplied_inventory_skips_live_field_inventory():
    """M22(d): when the router passes a cache-built inventory, the fields=None
    auto-selection path must feed it to the recommender instead of running the
    live field_inventory map scan (the expensive ARRAY JOIN query family)."""
    responses = [
        # _count_events
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # per-field novelty scans return nothing — irrelevant here
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=None,
        inventory=[("attr:user", 5, 90)],
        inventory_total=100,
    )
    assert result.status in ("ok", "no_data")
    # The scanned field set came from the supplied inventory — if the live
    # field_inventory scan had run instead, it would have consumed the canned
    # responses, yielded an empty recommendation, and fallen back to
    # _DEFAULT_NOVELTY_FIELDS (which don't include attr:user).
    assert any("user" in str(p.values()) for p in svc.ch.client._all_parameters)
    # Exactly one aggregate ran before the per-field scans: _count_events.
    # A live inventory path would add its batched top-level + ARRAY JOIN scans.
    count_queries = [q for q in svc.ch.client._calls if q.startswith("SELECT count()")]
    assert len(count_queries) == 1


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
                ("suspicious.exe", 1, datetime(2024, 1, 2), "evt-1"),
                ("unusual_tool", 2, datetime(2024, 1, 1), "evt-2"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
        # timestamp_desc field: one rare value
        FakeQueryResult(
            result_rows=[
                ("Malware execution", 1, datetime(2024, 1, 2, 1), "evt-3"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
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
                (f"val_{i}", 1, datetime(2024, 1, 1), f"evt-{j * 3 + i}") for i in range(3)
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
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
                result_rows=[("backdoor.exe", 1, datetime(2024, 1, 1), "evt-abc")],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    # Seed the hydration source: the scan only aggregates event_id, the full
    # event is fetched in one get_events_by_ids batch on the final slice.
    svc.ch.events_by_id["evt-abc"] = {
        "event_id": "evt-abc",
        "message": "bad msg",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.event_id == "evt-abc"
    assert r.event is not None
    assert r.event["message"] == "bad msg"
    assert r.value == "backdoor.exe"
    # Hydration ran as exactly one batched fetch.
    assert svc.ch.hydration_calls == [["evt-abc"]]
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
                    ("", 1, datetime(2024, 1, 1), "evt-1"),
                    ("real_value", 2, datetime(2024, 1, 1), "evt-2"),
                ],
                column_names=["val", "cnt", "first_seen", "evt_id"],
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
                    ),
                ],
                column_names=[
                    "val",
                    "detect_cnt",
                    "baseline_cnt",
                    "first_seen",
                    "evt_id",
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

    # artifact: categorical cardinality, but synthetic (pipeline-added) →
    # never auto-recommended
    assert by_token["artifact"].kind == "categorical"
    assert by_token["artifact"].recommended is False

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
            result_rows=[("404", 2, datetime(2024, 1, 1), "evt-1")],
            column_names=["val", "cnt", "first_seen", "evt_id"],
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
    from tracesignal.db.anomaly_stats import _MAX_AUTO_SCAN_FIELDS, NoveltyFieldInfo

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
                ("malware.exe", 1, datetime(2024, 1, 1), "evt-bad"),
                ("tool.exe", 2, datetime(2024, 1, 1), "evt-ok"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
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

    from tracesignal.db.anomaly_stats import FreqFinding

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

    svc = _svc(
        [
            FakeQueryResult(
                result_rows=[
                    (bucket_a.replace(tzinfo=None), "A", "evt-a"),
                    (bucket_b.replace(tzinfo=None), "B", "evt-b"),
                ],
                column_names=["bucket", "series_val", "evt_id"],
            ),
        ]
    )
    svc.ch.events_by_id = {
        "evt-a": {"event_id": "evt-a", "message": "spike A"},
        "evt-b": {"event_id": "evt-b", "message": "spike B"},
    }

    hydrated = svc._hydrate_freq_findings(
        findings,
        "c1",
        ["s1"],
        "artifact",
        "tracesignal",
        {},
        3600,
    )

    # One grouped argMin(event_id) scan + one batched get_events_by_ids.
    assert len(svc.ch.client._calls) == 1
    assert svc.ch.hydration_calls == [["evt-a", "evt-b"]]
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


# ---------------------------------------------------------------------------
# find_order_violations — timestamp-order detector (D2)
# ---------------------------------------------------------------------------


def test_order_no_data():
    """Returns no_data when the source has no events."""
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_order_violations("c1", ["s1"])
    assert result.status == "no_data"
    assert result.detector == "timestamp_order"
    assert result.method == "sequential"
    assert result.results == []


def test_order_no_violations():
    """Total events > 0 but zero backwards jumps → ok with empty results."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            # summary: one source, zero violations
            FakeQueryResult(
                result_rows=[("s1", 0, None)],
                column_names=["source_id", "n_viol", "max_skew"],
            ),
        ]
    )
    result = svc.find_order_violations("c1", ["s1"])
    assert result.status == "ok"
    assert result.results == []
    assert result.baseline_size == 100


def test_order_flags_backwards_jump_ranked_by_skew():
    """Violations returned worst-skew first, with prev/skew details."""
    ts_a = datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC)
    prev_a = datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC)  # 60s ahead
    ts_b = datetime(2024, 1, 1, 12, 0, 30, tzinfo=UTC)
    prev_b = datetime(2024, 1, 1, 12, 0, 35, tzinfo=UTC)  # 5s ahead
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("s1", 2, 60.0)],
                column_names=["source_id", "n_viol", "max_skew"],
            ),
            FakeQueryResult(
                result_rows=[
                    ("s1", "evt-a", ts_a, prev_a, 60.0, 100, 3, "record a"),
                    ("s1", "evt-b", ts_b, prev_b, 5.0, 250, 8, "record b"),
                ],
                column_names=[
                    "source_id",
                    "event_id",
                    "timestamp",
                    "prev_ts",
                    "skew",
                    "byte_offset",
                    "line_number",
                    "message",
                ],
            ),
        ]
    )
    result = svc.find_order_violations("c1", ["s1"], min_skew_seconds=1.0)
    assert result.status == "ok"
    assert [f.event_id for f in result.results] == ["evt-a", "evt-b"]
    worst = result.results[0]
    assert worst.skew_seconds == 60.0
    assert worst.score == 60.0
    assert worst.byte_offset == 100
    assert worst.prev_timestamp == prev_a.isoformat()
    assert worst.timestamp == ts_a.isoformat()
    assert worst.details["source_total_violations"] == 2
    assert worst.details["source_max_skew"] == 60.0
    assert worst.details["min_skew_seconds"] == 1.0
    assert worst.event["byte_offset"] == 100


def test_order_min_skew_bound_as_param():
    """min_skew_seconds is bound into both summary and detail queries."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("s1", 1, 3.0)],
                column_names=["source_id", "n_viol", "max_skew"],
            ),
            FakeQueryResult(
                result_rows=[
                    (
                        "s1",
                        "evt-a",
                        datetime(2024, 1, 1, tzinfo=UTC),
                        datetime(2024, 1, 1, 0, 0, 3, tzinfo=UTC),
                        3.0,
                        10,
                        1,
                        "m",
                    )
                ],
                column_names=[
                    "source_id",
                    "event_id",
                    "timestamp",
                    "prev_ts",
                    "skew",
                    "byte_offset",
                    "line_number",
                    "message",
                ],
            ),
        ]
    )
    svc.find_order_violations("c1", ["s1"], min_skew_seconds=2.5)
    params = svc.ch.client._all_parameters
    # params[0] = count(); [1] = summary; [2] = detail
    assert params[1]["skew"] == 2.5
    assert params[2]["skew"] == 2.5


def test_order_excludes_normal_marked_events():
    """Events marked normal are suppressed before the limit is applied."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("s1", 2, 60.0)],
                column_names=["source_id", "n_viol", "max_skew"],
            ),
            FakeQueryResult(
                result_rows=[
                    (
                        "s1",
                        "evt-a",
                        datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC),
                        datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC),
                        60.0,
                        100,
                        3,
                        "a",
                    ),
                    (
                        "s1",
                        "evt-b",
                        datetime(2024, 1, 1, 12, 0, 30, tzinfo=UTC),
                        datetime(2024, 1, 1, 12, 0, 35, tzinfo=UTC),
                        5.0,
                        250,
                        8,
                        "b",
                    ),
                ],
                column_names=[
                    "source_id",
                    "event_id",
                    "timestamp",
                    "prev_ts",
                    "skew",
                    "byte_offset",
                    "line_number",
                    "message",
                ],
            ),
        ]
    )
    result = svc.find_order_violations("c1", ["s1"], exclude_event_ids={"evt-a"})
    assert [f.event_id for f in result.results] == ["evt-b"]


# ---------------------------------------------------------------------------
# find_value_combos — value-combo detector (D1)
# ---------------------------------------------------------------------------


def test_value_combo_requires_two_explicit_fields():
    """Explicit single-field selection is rejected."""
    import pytest

    svc = _svc([FakeQueryResult(result_rows=[(100,)], column_names=["count()"])])
    with pytest.raises(ValueError):
        svc.find_value_combos("c1", ["s1"], fields=["artifact"])


def test_value_combo_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_value_combos("c1", ["s1"], fields=["artifact", "display_name"])
    assert result.status == "no_data"
    assert result.detector == "value_combo"


def test_value_combo_self_baseline_returns_rare_combos():
    """Rare (field-a, field-b) combinations, rarest first, with surprise score."""
    import math

    total = 1000
    fs = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("login_ok", "03:00", 1, fs, "evt-a"),
                ("login_ok", "09:00", 3, fs, "evt-b"),
            ],
            column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos(
        "c1", ["s1"], fields=["attr:action", "attr:hour"], rarity_floor=3
    )
    assert result.status == "ok"
    assert result.detector == "value_combo"
    # Rarest (count 1) first.
    assert result.results[0].values == ["login_ok", "03:00"]
    assert result.results[0].count == 1
    assert result.results[0].fields == ["attr:action", "attr:hour"]
    assert result.results[0].score == round(-math.log(1 / total), 4)
    assert result.results[0].event_id == "evt-a"
    assert result.results[0].details["detector"] == "value_combo"


def test_value_combo_binds_distinct_prefixes_for_attr_fields():
    """Two attribute-key fields bind to fk0 / fk1 without colliding."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("a", "b", 1, datetime(2024, 1, 1, tzinfo=UTC), "e")],
                column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    svc.find_value_combos("c1", ["s1"], fields=["attr:action", "attr:hour"])
    combo_params = svc.ch.client._all_parameters[1]
    assert combo_params["fk0"] == "action"
    assert combo_params["fk1"] == "hour"


def test_value_combo_temporal_flags_new_combos():
    """Temporal mode flags combos absent from baseline, present after split."""
    total = 500
    bl = datetime(2024, 1, 2, tzinfo=UTC)
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(300,)], column_names=["count()"]),  # baseline size
        FakeQueryResult(
            result_rows=[
                # v0, v1, detect_cnt, baseline_cnt, first_seen, evt_id
                ("admin", "10.0.0.9", 2, 0, fs, "evt-x"),
            ],
            column_names=[
                "v0",
                "v1",
                "detect_cnt",
                "baseline_cnt",
                "first_seen",
                "evt_id",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos("c1", ["s1"], fields=["attr:user", "attr:ip"], baseline_end=bl)
    assert result.method == "temporal"
    assert result.baseline_size == 300
    assert result.results[0].values == ["admin", "10.0.0.9"]
    assert result.results[0].count == 2
    assert result.results[0].details["baseline_size"] == 300


def test_value_combo_auto_insufficient_when_fewer_than_two_recommended():
    """Auto mode with <2 recommended fields returns insufficient_data, not an error."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # field_inventory: top-level agg (4 candidate cols → all constant/identifier)
        FakeQueryResult(
            result_rows=[(1, 100, 1, 100, 1, 100, 1, 100)],
            column_names=[
                "artifact_dist",
                "artifact_cov",
                "timestamp_desc_dist",
                "timestamp_desc_cov",
                "display_name_dist",
                "display_name_cov",
                "parser_name_dist",
                "parser_name_cov",
            ],
        ),
        # attr keys: none
        FakeQueryResult(result_rows=[], column_names=["key", "dist", "cov_count"]),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos("c1", ["s1"], fields=None)
    assert result.status == "insufficient_data"


def test_value_combo_excludes_normal_marked_events():
    total = 1000
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("a", "1", 1, fs, "evt-keep"),
                ("b", "2", 1, fs, "evt-drop"),
            ],
            column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos(
        "c1", ["s1"], fields=["attr:x", "attr:y"], exclude_event_ids={"evt-drop"}
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]


# ---------------------------------------------------------------------------
# find_range_violations — numeric-range detector (D4)
# ---------------------------------------------------------------------------


def test_range_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"])
    assert result.status == "no_data"
    assert result.detector == "numeric_range"


def test_range_insufficient_when_baseline_too_small():
    """A field with < _MIN_RANGE_BASELINE numeric samples is skipped."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # stats: only 5 numeric samples → below the floor
        FakeQueryResult(result_rows=[(10.0, 20.0, 5)], column_names=["q1", "q3", "n"]),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"])
    assert result.status == "insufficient_data"


def test_range_self_baseline_iqr_flags_outliers():
    """Self-baseline uses a Tukey fence; values outside [q1-1.5IQR, q3+1.5IQR] flag."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    # q1=100, q3=200 → IQR=100 → band [-50, 350]. Value 9000 is far above.
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[(9000.0, 2, fs, "evt-hi")],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"])
    assert result.status == "ok"
    assert result.method == "iqr"
    f = result.results[0]
    assert f.value == 9000.0
    assert f.direction == "above"
    assert f.lower == -50.0
    assert f.upper == 350.0
    # excess = 9000 - 350 = 8650; width = 400 → score = 21.625
    assert f.score == round(8650.0 / 400.0, 4)
    assert f.details["q1"] == 100.0
    assert f.details["baseline_n"] == 500


def test_range_temporal_uses_baseline_minmax():
    """Temporal mode learns exact min/max from the baseline window."""
    bl = datetime(2024, 1, 2, tzinfo=UTC)
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # baseline min=10, max=500, n=300
        FakeQueryResult(result_rows=[(10.0, 500.0, 300)], column_names=["lo", "hi", "n"]),
        FakeQueryResult(
            result_rows=[(9999.0, 1, fs, "evt-x")],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"], baseline_end=bl)
    assert result.method == "temporal-range"
    f = result.results[0]
    assert f.lower == 10.0
    assert f.upper == 500.0
    assert f.direction == "above"
    assert f.details["baseline_min"] == 10.0
    assert f.details["baseline_max"] == 500.0


def test_range_excludes_normal_marked_events():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[
                (9000.0, 1, fs, "evt-drop"),
                (8000.0, 1, fs, "evt-keep"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations(
        "c1", ["s1"], fields=["attr:bytes"], exclude_event_ids={"evt-drop"}
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]


def test_recommend_numeric_fields_filters_by_ratio():
    """Only fields whose values mostly parse as numbers are recommended."""
    # inventory: (token, distinct, non_empty_count); total passed explicitly.
    inventory = [("attr:bytes", 50, 100), ("attr:user", 40, 100)]
    responses = [
        # probe: bytes 98/100 numeric, user 3/100 numeric
        FakeQueryResult(
            result_rows=[(98, 100, 3, 100)],
            column_names=["num0", "ne0", "num1", "ne1"],
        ),
    ]
    svc = _svc(responses)
    fields = svc.recommend_numeric_fields("c1", ["s1"], total=100, inventory=inventory)
    by_token = {f.token: f for f in fields}
    assert by_token["attr:bytes"].recommended is True
    assert by_token["attr:bytes"].numeric_ratio == 0.98
    assert by_token["attr:user"].recommended is False


def test_heavy_detector_scans_carry_memory_settings():
    """Every whole-corpus detector scan must carry the shared SETTINGS clause
    (external GROUP BY spill + per-query memory cap + thread cap) — a scan
    without it trusts the server-wide limit and can take the box down on a
    300M-row case."""
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    class _RecordingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self.full_queries: list[str] = []

        def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
            self.full_queries.append(sql)
            if sql.strip().startswith("SELECT count()"):
                return FakeQueryResult(result_rows=[(100,)], column_names=["count()"])
            return FakeQueryResult(result_rows=[], column_names=[])

    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    store = FakeClickHouseStore(FakeClient([]))
    client = _RecordingClient()
    store.client = client
    svc.ch = store

    svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    svc.find_value_combos("c1", ["s1"], fields=["artifact", "timestamp_desc"])
    svc.find_charset_novelty("c1", ["s1"], fields=["artifact"])
    svc.find_entropy_outliers("c1", ["s1"], fields=["artifact"])
    svc.field_inventory("c1", ["s1"], total=100)

    scans = [
        q
        for q in client.full_queries
        if not q.strip().startswith("SELECT count()") and "min(timestamp), max(timestamp)" not in q
    ]
    assert scans
    for sql in scans:
        assert HEAVY_SCAN_SETTINGS in sql, sql[:120]


# ---------------------------------------------------------------------------
# find_charset_novelty — charset detector (D3)
# ---------------------------------------------------------------------------


def test_charset_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    assert result.status == "no_data"
    assert result.detector == "charset"


def test_charset_insufficient_when_baseline_too_small():
    """A field with < _MIN_CHARSET_BASELINE distinct values is skipped."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # per-char distinct-value counts + the total distinct-value count folded
        # into the same scan (3rd column). Only 5 distinct values → below floor.
        FakeQueryResult(result_rows=[("a", 5, 5), ("b", 5, 5)], column_names=["c", "n", "n_vals"]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    assert result.status == "insufficient_data"


def test_charset_skips_huge_alphabet():
    """A reference charset larger than _MAX_CHARSET_SIZE (free text in large
    scripts) is skipped — "novel character" is meaningless there."""
    big = [(chr(0x4E00 + i), 50, 80) for i in range(5001)]
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        FakeQueryResult(result_rows=big, column_names=["c", "n", "n_vals"]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:msg"])
    assert result.status == "insufficient_data"


def test_charset_self_baseline_flags_rare_char():
    """Self mode: chars in ≤ rarity_floor distinct values are rare; values
    containing them flag with -log(n_vals_with_char / n_vals) surprise."""
    import math

    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # 'a'/'b' common (90/85 distinct values), NUL byte rare (1 value); the
        # total distinct-value count (100) is folded in as the 3rd column.
        FakeQueryResult(
            result_rows=[("a", 90, 100), ("b", 85, 100), ("\x00", 1, 100)],
            column_names=["c", "n", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("ab\x00ab", ["\x00"], 2, fs, "evt-nul")],
            column_names=["val", "novel", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    assert result.status == "ok"
    assert result.method == "rare-chars"
    f = result.results[0]
    assert f.field == "attr:user"
    assert f.novel_chars == ["\x00"]
    assert f.count == 2
    assert f.score == round(-math.log(1 / 100), 4)
    assert f.details["codepoints"] == ["U+0000"]
    assert f.details["rarity_floor"] == 3
    assert f.details["char_value_counts"] == {"\x00": 1}
    # The reference-set parameter must exclude the rare char.
    base_params = [p["base"] for p in svc.ch.client._all_parameters if "base" in p]
    assert base_params == [["a", "b"]]


def test_charset_temporal_flags_never_seen_chars_and_guards_sentinel():
    """Temporal mode: reference set = baseline-window charset; detect window
    is timestamp >= baseline_end with the year-2299 sentinel excluded."""
    import math

    from tracesignal.db._dt import TS_NOT_SENTINEL_SQL

    bl = datetime(2024, 1, 2, tzinfo=UTC)
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # baseline charset over 50 distinct baseline values
        FakeQueryResult(result_rows=[(list("abcdefghij"), 50)], column_names=["charset", "n"]),
        FakeQueryResult(
            result_rows=[("ab☃cd", ["☃"], 1, fs, "evt-snow")],
            column_names=["val", "novel", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    client = RecordingClient(responses)
    svc.ch = FakeClickHouseStore(FakeClient([]))
    svc.ch.client = client
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], baseline_end=bl)
    assert result.status == "ok"
    assert result.method == "temporal-charset"
    f = result.results[0]
    assert f.novel_chars == ["☃"]
    # Never seen in baseline → +1-smoothed surprise over 50 distinct values.
    assert f.score == round(math.log(51), 4)
    baseline_sql = client.full_queries[1]
    detect_sql = client.full_queries[2]
    assert "timestamp < {bl:String}" in baseline_sql
    assert "timestamp >= {bl:String}" in detect_sql
    assert TS_NOT_SENTINEL_SQL in detect_sql


def test_charset_excludes_normal_marked_events_and_limits():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[("a", 90, 100), ("$", 1, 100), ("%", 1, 100)],
            column_names=["c", "n", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[
                ("x$", ["$"], 1, fs, "evt-drop"),
                ("y%", ["%"], 1, fs, "evt-keep"),
            ],
            column_names=["val", "novel", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty(
        "c1", ["s1"], fields=["attr:user"], exclude_event_ids={"evt-drop"}, limit=1
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]
    # Hydration happens once, on the surviving slice only.
    assert svc.ch.hydration_calls == [["evt-keep"]]


# ---------------------------------------------------------------------------
# find_entropy_outliers — entropy detector (D5)
# ---------------------------------------------------------------------------


def test_entropy_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.status == "no_data"
    assert result.detector == "entropy"


def test_entropy_insufficient_when_baseline_too_small():
    """A field with < _MIN_ENTROPY_BASELINE qualifying distinct values is skipped."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 3.0, 5)], column_names=["q1", "q3", "n"]),
    ]
    svc = _svc(responses)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.status == "insufficient_data"


def test_entropy_self_baseline_iqr_flags_both_directions():
    """Self mode: Tukey fence over corpus entropies; score = excess / width."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    # q1=2.0, q3=3.0 → IQR=1.0 → band [0.5, 4.5], width 4.0.
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 3.0, 200)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[
                ("kq3v9xz2m8w1", 5.5, 3, fs, "evt-dga"),
                ("aaaaaaaaaaaa", 0.1, 7, fs, "evt-pad"),
            ],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.status == "ok"
    assert result.method == "iqr"
    hi, lo = result.results[0], result.results[1]
    assert hi.direction == "above"
    assert hi.entropy == 5.5
    assert hi.lower == 0.5
    assert hi.upper == 4.5
    # excess = 5.5 - 4.5 = 1.0; width = 4.0 → 0.25
    assert hi.score == 0.25
    assert lo.direction == "below"
    # excess = 0.5 - 0.1 = 0.4; width = 4.0 → 0.1
    assert lo.score == 0.1
    assert hi.details["q1"] == 2.0
    assert hi.details["baseline_n"] == 200


def test_entropy_temporal_learns_band_from_baseline_and_guards_sentinel():
    """Temporal mode: fence from the baseline window only; detect window is
    timestamp >= baseline_end with the sentinel excluded; min-length clause
    applies to baseline and detect alike."""

    from tracesignal.db._dt import TS_NOT_SENTINEL_SQL

    bl = datetime(2024, 1, 2, tzinfo=UTC)
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 2.5, 100)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[("x9k2q8vz", 4.9, 1, fs, "evt-hi")],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    client = RecordingClient(responses)
    svc.ch = FakeClickHouseStore(FakeClient([]))
    svc.ch.client = client
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"], baseline_end=bl)
    assert result.status == "ok"
    assert result.method == "temporal-iqr"
    f = result.results[0]
    assert f.direction == "above"
    baseline_sql = client.full_queries[1]
    detect_sql = client.full_queries[2]
    assert "timestamp < {bl:String}" in baseline_sql
    assert "timestamp >= {bl:String}" in detect_sql
    assert TS_NOT_SENTINEL_SQL in detect_sql
    assert "lengthUTF8" in baseline_sql
    assert "lengthUTF8" in detect_sql


def test_entropy_excludes_normal_marked_events():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 3.0, 200)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[
                ("zzzz11119999", 5.9, 1, fs, "evt-drop"),
                ("q8m2x7c4v1n6", 5.5, 1, fs, "evt-keep"),
            ],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_entropy_outliers(
        "c1", ["s1"], fields=["attr:host"], exclude_event_ids={"evt-drop"}
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]
    assert svc.ch.hydration_calls == [["evt-keep"]]
