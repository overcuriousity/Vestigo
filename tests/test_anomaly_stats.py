"""Tests for StatisticalAnomalyService.

All tests use fakes/mocks for ClickHouse so they run without external services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from tracesignal.db._offsets import OFFSET_SRC_PARAM, OFFSET_VAL_PARAM
from tracesignal.db.anomaly_stats import (
    AnalysisWindows,
    FreqFinding,
    NoveltyFieldInfo,
    StatisticalAnomalyService,
    TimeWindow,
    ValueFinding,
    _bh_qvalues,
    _chi2_sf_df1,
    _classify_field,
    _col_expr,
    _full_bucket_starts,
    _g_statistic,
    _greenwood_p,
    _poisson_rate_g,
    _window_preds,
    effective_ts_sql,
    windows_from_split,
)


def _one_suspect(
    baseline_start: datetime,
    baseline_end: datetime,
    suspect_start: datetime,
    suspect_end: datetime,
    label: str = "suspect",
) -> AnalysisWindows:
    """Build an AnalysisWindows with one baseline and one suspect window."""
    return AnalysisWindows(
        baseline=TimeWindow("baseline", baseline_start, baseline_end),
        suspects=(TimeWindow(label, suspect_start, suspect_end),),
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
    # The pre-cap survivor count is reported so the UI can offer "load more"
    # instead of silently truncating.
    assert result.total_findings == 9


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
    """Temporal mode flags values absent in baseline but present in a suspect window."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="exfil-window",
    )
    svc = _svc(
        [
            # Total events
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            # Window totals: baseline_total, w0_total
            FakeQueryResult(result_rows=[(300, 120)], column_names=["bl_total", "w0_total"]),
            # artifact field: val, baseline_cnt, w0_cnt, w0_first, w0_evt
            FakeQueryResult(
                result_rows=[
                    ("first_time_process.exe", 0, 3, datetime(2024, 1, 16), "evt-9"),
                ],
                column_names=["val", "baseline_cnt", "w0_cnt", "w0_first", "w0_evt"],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"], windows=windows)
    assert result.status == "ok"
    assert result.method == "temporal"
    assert result.baseline_size == 300
    assert len(result.results) == 1
    r = result.results[0]
    assert r.value == "first_time_process.exe"
    assert r.count == 3
    assert r.details["method"] == "temporal"
    # Surprise denominator is the suspect window's own event count, not the corpus.
    assert r.details["window_total_events"] == 120
    assert r.details["total_events"] == 120
    assert r.details["window_label"] == "exfil-window"
    assert result.windows["suspect_windows"][0]["label"] == "exfil-window"


def test_value_novelty_temporal_window_bounds_converted_to_utc_for_sql():
    """Window bounds with a non-UTC offset (e.g. FastAPI-parsed `+02:00`) must
    be converted to the equivalent UTC instant before being spliced into the
    ClickHouse SQL string literals — otherwise the window lands 2h off (F8)."""
    from datetime import timedelta, timezone

    plus_two = timezone(timedelta(hours=2))
    windows = _one_suspect(
        datetime(2024, 1, 1, 2, 0, 0, tzinfo=plus_two),
        datetime(2024, 1, 15, 14, 0, 0, tzinfo=plus_two),
        datetime(2024, 1, 16, 2, 0, 0, tzinfo=plus_two),
        datetime(2024, 1, 20, 2, 0, 0, tzinfo=plus_two),
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(300, 0)], column_names=["bl_total", "w0_total"]),
            FakeQueryResult(
                result_rows=[],
                column_names=["val", "baseline_cnt", "w0_cnt", "w0_first", "w0_evt"],
            ),
        ]
    )
    svc.find_value_novelty("c1", ["s1"], fields=["artifact"], windows=windows)
    b1_values = [p["b1"] for p in svc.ch.client._all_parameters if "b1" in p]
    assert b1_values
    # 14:00 +02:00 == 12:00 UTC.
    assert all(v == "2024-01-15 12:00:00" for v in b1_values)


def test_value_novelty_two_suspect_windows_attributed_separately():
    """A value present in two suspect windows yields one finding per window,
    each scored against its own window's event count."""
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 10, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow("w-a", datetime(2024, 1, 11, tzinfo=UTC), datetime(2024, 1, 12, tzinfo=UTC)),
            TimeWindow("w-b", datetime(2024, 1, 20, tzinfo=UTC), datetime(2024, 1, 21, tzinfo=UTC)),
        ),
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            # baseline_total, w0_total, w1_total
            FakeQueryResult(result_rows=[(600, 100, 200)], column_names=["bl", "w0", "w1"]),
            # val, baseline_cnt, w0_cnt, w0_first, w0_evt, w1_cnt, w1_first, w1_evt
            FakeQueryResult(
                result_rows=[
                    (
                        "svc_x",
                        0,
                        4,
                        datetime(2024, 1, 11),
                        "evt-a",
                        6,
                        datetime(2024, 1, 20),
                        "evt-b",
                    )
                ],
                column_names=[
                    "val",
                    "baseline_cnt",
                    "w0_cnt",
                    "w0_first",
                    "w0_evt",
                    "w1_cnt",
                    "w1_first",
                    "w1_evt",
                ],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    by_label = {r.details["window_label"]: r for r in result.results}
    assert set(by_label) == {"w-a", "w-b"}
    assert by_label["w-a"].count == 4
    assert by_label["w-a"].details["window_total_events"] == 100
    assert by_label["w-b"].count == 6
    assert by_label["w-b"].details["window_total_events"] == 200


def test_value_novelty_allowlist_suppresses_value_everywhere():
    """An allowlisted (field, value) is dropped regardless of its event."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[
                    ("keep_me", 1, datetime(2024, 1, 1), "evt-1"),
                    ("known_good", 1, datetime(2024, 1, 1), "evt-2"),
                ],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact"],
        allowlist={("artifact", "known_good")},
    )
    assert [r.value for r in result.results] == ["keep_me"]


def test_value_novelty_small_window_warns():
    """A suspect window below _MIN_WINDOW_EVENTS gets a warning, not suppression."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 10, tzinfo=UTC),
        datetime(2024, 1, 11, tzinfo=UTC),
        datetime(2024, 1, 12, tzinfo=UTC),
        label="tiny",
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(600, 5)], column_names=["bl", "w0"]),
            FakeQueryResult(
                result_rows=[("svc_x", 0, 3, datetime(2024, 1, 11), "evt-a")],
                column_names=["val", "baseline_cnt", "w0_cnt", "w0_first", "w0_evt"],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    assert len(result.results) == 1  # still surfaced
    assert any("tiny" in w and "unstable" in w for w in result.warnings)


def test_windows_from_split_reproduces_adjacent_split():
    """The legacy shim yields baseline=[min, split), one suspect=[split, max+1ms)."""
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    split = datetime(2024, 1, 15, tzinfo=UTC)
    max_ts = datetime(2024, 1, 31, tzinfo=UTC)
    w = windows_from_split(split, min_ts, max_ts)
    assert w.baseline.start == min_ts
    assert w.baseline.end == split
    assert len(w.suspects) == 1
    assert w.suspects[0].start == split
    assert w.suspects[0].end > max_ts


def test_full_bucket_starts_excludes_partial_edges():
    """_full_bucket_starts only yields buckets fully inside the window."""
    # Window [00:10, 02:00) with 1h buckets: only the 01:00 bucket is fully in.
    w = TimeWindow(
        "x", datetime(2024, 1, 1, 0, 10, tzinfo=UTC), datetime(2024, 1, 1, 2, 0, tzinfo=UTC)
    )
    starts = _full_bucket_starts(w, 3600)
    assert starts == [datetime(2024, 1, 1, 1, 0, tzinfo=UTC)]


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


def test_frequency_windowed_bounds_hydration_input():
    """Temporal frequency can emit a finding per suspect-window bucket (every
    silent bucket is a drop vs the baseline). Hydration must run on a bounded
    slice, not the full list, or its ClickHouse query params overflow the
    field-length limit ("Field value too long") on a real cluster."""
    # Baseline 6h → 6 full 1h buckets; a long suspect window (72h → 72 buckets)
    # for a series that is silent throughout → ~72 drop findings.
    windows = _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 4, 6, 0, tzinfo=UTC),
        label="wide",
    )
    # Baseline buckets ~10/bucket so a silent (0) suspect bucket scores as a drop.
    bucket_rows = [(_h(i), "LOG", 10) for i in range(6)]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])

    hydration_sizes: list[int] = []

    def _spy(findings, *a, **kw):
        hydration_sizes.append(len(findings))
        return findings

    svc._hydrate_freq_findings = _spy  # type: ignore[method-assign]
    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=windows, bucket_count=6, z_threshold=2.0, limit=50
    )
    assert result.status == "ok"
    assert len(result.results) <= 50
    # Hydration saw at most the buffered cap (max(limit*3, 100)), not all ~72.
    assert hydration_sizes and all(n <= max(50 * 3, 100) for n in hydration_sizes)


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


def _freq_windows() -> AnalysisWindows:
    """6h baseline + 3h suspect window; with bucket_count=6 the interval is 1h,
    giving 6 full baseline buckets and 3 full suspect buckets."""
    return _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        label="incident",
    )


def _h(hour: int) -> datetime:
    return datetime(2024, 1, 1, hour, 0, tzinfo=UTC)


def test_frequency_temporal_baseline():
    """Temporal mode learns mean/std from the baseline window's (zero-filled,
    full-only) buckets and scores each suspect-window bucket against them.
    The windowed path issues a single bucket scan — no min/max query."""
    bucket_rows = [
        (_h(0), "LOG", 10),
        (_h(1), "LOG", 12),
        (_h(2), "LOG", 11),
        (_h(3), "LOG", 10),
        (_h(4), "LOG", 9),
        (_h(5), "LOG", 10),
        # Suspect window buckets: a spike at 07:00, quiet otherwise.
        (_h(6), "LOG", 10),
        (_h(7), "LOG", 200),
        (_h(8), "LOG", 10),
    ]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=_freq_windows(), bucket_count=6, z_threshold=2.0
    )
    assert result.status == "ok"
    assert result.method == "temporal-z-score"
    assert len(result.results) == 1
    spike = result.results[0]
    assert spike.observed == 200
    assert spike.z_score > 0
    assert spike.details["suspect_window_label"] == "incident"
    assert result.windows["suspect_windows"][0]["label"] == "incident"


def test_frequency_temporal_zero_baseline_flagged():
    """A series absent from the baseline but active in a suspect window is flagged.

    Zero-fill makes its baseline mean 0; the std floor lets the suspect-window
    activity score instead of dividing by ~0 — exactly the "brand-new activity
    after the incident start" case temporal mode exists to surface.
    """
    bucket_rows = [
        (_h(6), "NEWPROC", 50),
        (_h(7), "NEWPROC", 60),
    ]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=_freq_windows(), bucket_count=6, z_threshold=2.0
    )
    assert result.status == "ok"
    assert {r.series_value for r in result.results} == {"NEWPROC"}


def test_frequency_temporal_hand_computed_z():
    """The suspect-window z-score matches a hand-computed mean/std over the
    zero-filled baseline buckets."""
    import numpy as np

    baseline_counts = [10, 12, 11, 10, 9, 10]
    bucket_rows = [(_h(i), "LOG", c) for i, c in enumerate(baseline_counts)]
    bucket_rows.append((_h(7), "LOG", 200))
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=_freq_windows(), bucket_count=6, z_threshold=2.0
    )
    arr = np.array(baseline_counts, dtype=float)
    mean = arr.mean()
    std = max(arr.std(ddof=1), 0.5)
    expected_z = (200 - mean) / std
    spike = next(r for r in result.results if r.observed == 200)
    assert abs(spike.z_score - round(expected_z, 4)) < 0.01


def test_frequency_temporal_partial_buckets_excluded():
    """Buckets cut by a window edge are excluded from scoring — a window
    shorter than one interval yields a warning, never a bogus 1-bucket z."""
    # Baseline 6h (6 full 1h buckets); suspect window only 30 min → no full bucket.
    windows = _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 15, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 45, tzinfo=UTC),
        label="tiny",
    )
    bucket_rows = [(_h(i), "LOG", 10) for i in range(6)]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=windows, bucket_count=6, z_threshold=2.0
    )
    assert result.results == []
    assert any("tiny" in w and "shorter than" in w for w in result.warnings)


def test_frequency_temporal_baseline_too_short_warns():
    """A baseline spanning fewer than _MIN_FREQUENCY_BUCKETS full buckets can't
    build a distribution — insufficient_data with an explanatory warning."""
    # Baseline 90 min, bucket_count=6 → interval 900s (15 min) → 6 buckets, ok.
    # Force too-few by making the baseline only 20 min with a 15-min interval.
    windows = _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 20, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    )
    svc = _svc([FakeQueryResult(result_rows=[], column_names=["bucket", "sv", "cnt"])])
    # bucket_count=2 over a 20-min baseline → interval 600s (10 min) → only 2
    # full buckets, below the _MIN_FREQUENCY_BUCKETS=3 floor.
    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=windows, bucket_count=2, z_threshold=2.0
    )
    assert result.status == "insufficient_data"
    assert any("Baseline window" in w for w in result.warnings)


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


# Query order (per-source scans): count, then one summary per source, then
# one detail per violating source; messages hydrated via get_events_by_ids.
_ORD_SUMMARY_COLS = ["n_viol", "max_skew"]
_ORD_DETAIL_COLS = ["event_id", "timestamp", "prev_ts", "skew", "byte_offset", "line_number"]


def test_order_no_violations():
    """Total events > 0 but zero backwards jumps → ok with empty results."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            # per-source summary: zero violations
            FakeQueryResult(result_rows=[(0, None)], column_names=_ORD_SUMMARY_COLS),
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
            FakeQueryResult(result_rows=[(2, 60.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[
                    ("evt-a", ts_a, prev_a, 60.0, 100, 3),
                    ("evt-b", ts_b, prev_b, 5.0, 250, 8),
                ],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    svc.ch.events_by_id = {"evt-a": {"message": "record a"}}
    result = svc.find_order_violations("c1", ["s1"], min_skew_seconds=1.0)
    assert result.status == "ok"
    assert [f.event_id for f in result.results] == ["evt-a", "evt-b"]
    assert result.total_findings == 2
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
    # message comes from the post-scan hydration, one batched fetch.
    assert worst.event["message"] == "record a"
    assert svc.ch.hydration_calls == [["evt-a", "evt-b"]]


def test_order_min_skew_bound_as_param():
    """min_skew_seconds is bound into both summary and detail queries."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1, 3.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[
                    (
                        "evt-a",
                        datetime(2024, 1, 1, tzinfo=UTC),
                        datetime(2024, 1, 1, 0, 0, 3, tzinfo=UTC),
                        3.0,
                        10,
                        1,
                    )
                ],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_order_violations("c1", ["s1"], min_skew_seconds=2.5)
    params = svc.ch.client._all_parameters
    # params[0] = count(); [1] = summary(s1); [2] = detail(s1)
    assert params[1]["skew"] == 2.5
    assert params[2]["skew"] == 2.5
    summary_sql, detail_sql = svc.ch.client.full_queries[1], svc.ch.client.full_queries[2]
    # ClickHouse can't spill a window-function sort to disk, so the scans are
    # per source (bounded sort) and never drag `message` through it — that
    # combination is what blew the memory cap on a 300M-row case.
    for sql in (summary_sql, detail_sql):
        assert "message" not in sql
        assert "source_id = {sid:String}" in sql
        assert "PARTITION BY" not in sql
        # The shared guardrails spill sorts too, not just GROUP BYs.
        assert "max_bytes_before_external_sort" in sql
    assert "toString(event_id)" not in summary_sql
    assert "toString(event_id) AS event_id" in detail_sql


def test_order_excludes_normal_marked_events():
    """Events marked normal are suppressed before the limit is applied."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(2, 60.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[
                    (
                        "evt-a",
                        datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC),
                        datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC),
                        60.0,
                        100,
                        3,
                    ),
                    (
                        "evt-b",
                        datetime(2024, 1, 1, 12, 0, 30, tzinfo=UTC),
                        datetime(2024, 1, 1, 12, 0, 35, tzinfo=UTC),
                        5.0,
                        250,
                        8,
                    ),
                ],
                column_names=_ORD_DETAIL_COLS,
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
    """Temporal mode flags combos absent from baseline, present in a suspect window."""
    total = 500
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
    )
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # window totals: baseline_total, w0_total
        FakeQueryResult(result_rows=[(300, 80)], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(
            result_rows=[
                # v0, v1, baseline_cnt, w0_cnt, w0_first, w0_evt
                ("admin", "10.0.0.9", 0, 2, fs, "evt-x"),
            ],
            column_names=["v0", "v1", "baseline_cnt", "w0_cnt", "w0_first", "w0_evt"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos("c1", ["s1"], fields=["attr:user", "attr:ip"], windows=windows)
    assert result.method == "temporal"
    assert result.baseline_size == 300
    assert result.results[0].values == ["admin", "10.0.0.9"]
    assert result.results[0].count == 2
    assert result.results[0].details["baseline_size"] == 300
    assert result.results[0].details["window_total_events"] == 80
    # The combo's allowlist key flattens the tuple reversibly.
    assert result.results[0].details["allowlist_value"] == "admin\x1f10.0.0.9"


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
    """Temporal mode learns exact min/max from the baseline window and
    attributes the violation to the suspect window it fell in."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        label="spike",
    )
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # baseline min=10, max=500, n=300
        FakeQueryResult(result_rows=[(10.0, 500.0, 300)], column_names=["lo", "hi", "n"]),
        FakeQueryResult(
            # val, cnt, first_seen, evt_id, win_idx
            result_rows=[(9999.0, 1, fs, "evt-x", 0)],
            column_names=["val", "cnt", "first_seen", "evt_id", "win_idx"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"], windows=windows)
    assert result.method == "temporal-range"
    f = result.results[0]
    assert f.lower == 10.0
    assert f.upper == 500.0
    assert f.direction == "above"
    assert f.details["baseline_min"] == 10.0
    assert f.details["baseline_max"] == 500.0
    assert f.details["window_label"] == "spike"


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
    """Temporal mode: reference set = baseline-window charset; suspect-window
    values with never-seen chars flag, with the year-2299 sentinel excluded."""
    import math

    from tracesignal.db._dt import TS_NOT_SENTINEL_SQL

    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        label="win",
    )
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # baseline charset over 50 distinct baseline values
        FakeQueryResult(result_rows=[(list("abcdefghij"), 50)], column_names=["charset", "n"]),
        FakeQueryResult(
            # val, novel, cnt, first_seen, evt_id, win_idx
            result_rows=[("ab☃cd", ["☃"], 1, fs, "evt-snow", 0)],
            column_names=["val", "novel", "cnt", "first_seen", "evt_id", "win_idx"],
        ),
    ]
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    client = RecordingClient(responses)
    svc.ch = FakeClickHouseStore(FakeClient([]))
    svc.ch.client = client
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    assert result.status == "ok"
    assert result.method == "temporal-charset"
    f = result.results[0]
    assert f.novel_chars == ["☃"]
    assert f.details["window_label"] == "win"
    # Never seen in baseline → +1-smoothed surprise over 50 distinct values.
    assert f.score == round(math.log(51), 4)
    baseline_sql = client.full_queries[1]
    detect_sql = client.full_queries[2]
    # Baseline learns from the baseline window; detect scans the suspect union.
    assert "{b1:String}" in baseline_sql
    assert "{w0s:String}" in detect_sql
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
    """Temporal mode: fence from the baseline window only; suspect-window
    values scored with the sentinel excluded; min-length clause applies to
    baseline and detect alike."""

    from tracesignal.db._dt import TS_NOT_SENTINEL_SQL

    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        label="win",
    )
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 2.5, 100)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            # val, ent, cnt, first_seen, evt_id, win_idx
            result_rows=[("x9k2q8vz", 4.9, 1, fs, "evt-hi", 0)],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id", "win_idx"],
        ),
    ]
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    client = RecordingClient(responses)
    svc.ch = FakeClickHouseStore(FakeClient([]))
    svc.ch.client = client
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"], windows=windows)
    assert result.status == "ok"
    assert result.method == "temporal-iqr"
    f = result.results[0]
    assert f.direction == "above"
    assert f.details["window_label"] == "win"
    baseline_sql = client.full_queries[1]
    detect_sql = client.full_queries[2]
    assert "{b1:String}" in baseline_sql
    assert "{w0s:String}" in detect_sql
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


# ---------------------------------------------------------------------------
# proportion_shift — statistics helpers
# ---------------------------------------------------------------------------


def test_g_statistic_hand_computed():
    """G for a hand-computable 2×2 table, and 0 for identical proportions."""
    # rows (baseline 10/1000, window 40/1000): expected cells 25/975 each row.
    # G = 2·(10·ln(10/25) + 990·ln(990/975) + 40·ln(40/25) + 960·ln(960/975))
    assert abs(_g_statistic(10, 990, 40, 960) - 19.7360) < 1e-3
    # Identical proportions carry zero evidence.
    assert _g_statistic(10, 990, 10, 990) == 0.0
    # Zero cells contribute nothing rather than NaN.
    assert _g_statistic(0, 0, 0, 0) == 0.0
    assert _g_statistic(200, 800, 0, 500) > 0


def test_chi2_sf_df1_known_values():
    """The erfc closed form matches the classic df=1 chi² critical values."""
    assert abs(_chi2_sf_df1(3.841459) - 0.05) < 1e-4
    assert abs(_chi2_sf_df1(6.634897) - 0.01) < 1e-4
    assert _chi2_sf_df1(0.0) == 1.0


def test_bh_qvalues():
    """BH step-up with monotone enforcement, returned in input order."""
    q = _bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert abs(q[0] - 0.04) < 1e-9
    assert abs(q[1] - 0.04 * 4 / 3) < 1e-9
    # p=0.03 (rank 2) would be 0.06 raw; monotonicity pulls it down to rank 3's q.
    assert abs(q[2] - 0.04 * 4 / 3) < 1e-9
    assert abs(q[3] - 0.5) < 1e-9
    assert _bh_qvalues([]) == []


# ---------------------------------------------------------------------------
# proportion_shift — detector
# ---------------------------------------------------------------------------


def _shift_windows() -> AnalysisWindows:
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="incident",
    )


def test_proportion_shift_insufficient_data_without_windows():
    """Temporal-only: no windows → insufficient_data without touching ClickHouse."""
    svc = _svc([])
    result = svc.find_proportion_shifts("c1", ["s1"], fields=["attr:user"])
    assert result.status == "insufficient_data"
    assert result.detector == "proportion_shift"
    assert result.method == "g-test"
    assert any("temporal-only" in w for w in result.warnings)
    assert svc.ch.client._calls == []


def test_proportion_shift_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert result.status == "no_data"


def test_proportion_shift_flags_up_direction():
    """A value whose share jumps 0.5% → 8% is flagged 'up' with G as score."""
    responses = [
        FakeQueryResult(result_rows=[(11000,)], column_names=["count()"]),
        # window totals: baseline 10000, suspect 1000
        FakeQueryResult(result_rows=[(10000, 1000)], column_names=["bl", "w0"]),
        # val, baseline_cnt, bl_last, bl_evt, w0_cnt, w0_first, w0_evt
        FakeQueryResult(
            result_rows=[
                ("4625", 50, datetime(2024, 1, 14), "evt-bl", 80, datetime(2024, 1, 16), "evt-w"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:eventid"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert result.method == "g-test"
    assert result.baseline_size == 10000
    assert len(result.results) == 1
    r = result.results[0]
    assert r.value == "4625"
    assert r.direction == "up"
    assert r.count == 80
    assert r.baseline_count == 50
    # G computed on raw counts: (50, 9950, 80, 920).
    assert abs(r.g_statistic - 225.2477) < 1e-3
    assert r.score == r.g_statistic
    assert r.q_value <= 0.05
    assert abs(r.rate_ratio - 16.0) < 0.01
    assert r.event_id == "evt-w"
    assert r.first_seen is not None
    assert r.details["window_label"] == "incident"
    assert r.details["allowlist_field"] == "attr:eventid"
    assert r.details["allowlist_value"] == "4625"
    assert r.details["m_tests"] == 1


def test_proportion_shift_vanished_value_is_down():
    """A baseline value absent from the suspect window is a maximal 'down'."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                ("heartbeat", 200, datetime(2024, 1, 14, 23), "evt-last", 0, None, ""),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:service"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "down"
    assert r.count == 0
    assert r.first_seen is None
    # Representative event = last baseline occurrence.
    assert r.event_id == "evt-last"
    assert r.details["last_seen_baseline"] is not None
    # Ratio uses Haldane–Anscombe smoothing (0.5/500 over 200/1000).
    assert abs(r.rate_ratio - (0.5 / 500) / 0.2) < 1e-6
    # The test itself used raw counts: G(200, 800, 0, 500).
    assert abs(r.g_statistic - 177.2186) < 1e-3


def test_proportion_shift_sql_excludes_first_seen():
    """The candidate scan prunes only first-seen values and keeps baseline rows."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_proportion_shifts("c1", ["s1"], fields=["attr:user"], windows=_shift_windows())
    candidate_sql = client.full_queries[2]
    assert "HAVING baseline_cnt >= 1" in candidate_sql
    # Baseline predicate must be in the WHERE union so vanished values survive.
    assert "{b0:String}" in candidate_sql and "{b1:String}" in candidate_sql
    assert "ORDER BY (baseline_cnt + w0_cnt) DESC" in candidate_sql


def test_proportion_shift_effect_floor():
    """A statistically significant but small (<min_ratio) shift is suppressed."""
    responses = [
        FakeQueryResult(result_rows=[(200000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(100000, 100000)], column_names=["bl", "w0"]),
        # 10% → 12%: hugely significant at this volume, ratio only 1.2.
        FakeQueryResult(
            result_rows=[
                (
                    "200",
                    10000,
                    datetime(2024, 1, 14),
                    "evt-bl",
                    12000,
                    datetime(2024, 1, 16),
                    "evt-w",
                ),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:status"], windows=_shift_windows(), min_ratio=2.0
    )
    assert result.status == "ok"
    assert result.results == []


def test_proportion_shift_fdr_gates_insignificant_shift():
    """A large ratio without statistical evidence (tiny counts) fails the q gate."""
    responses = [
        FakeQueryResult(result_rows=[(1100,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 100)], column_names=["bl", "w0"]),
        # 1/1000 → 1/100: ratio 10× but one event in the window — no evidence.
        FakeQueryResult(
            result_rows=[
                ("rareval", 1, datetime(2024, 1, 14), "evt-bl", 1, datetime(2024, 1, 16), "evt-w"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_proportion_shift_allowlist_suppression():
    responses = [
        FakeQueryResult(result_rows=[(11000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(10000, 1000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                ("4625", 50, datetime(2024, 1, 14), "evt-bl", 80, datetime(2024, 1, 16), "evt-w"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1",
        ["s1"],
        fields=["attr:eventid"],
        windows=_shift_windows(),
        allowlist={("attr:eventid", "4625")},
    )
    assert result.status == "ok"
    assert result.results == []


def test_proportion_shift_tiny_window_warning():
    """A suspect window under the event floor is warned about, never dropped."""
    responses = [
        FakeQueryResult(result_rows=[(1030,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 30)], column_names=["bl", "w0"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert any("only 30 events" in w for w in result.warnings)


def test_proportion_shift_candidate_cap_warning():
    """Hitting the per-field candidate cap surfaces an FDR-coverage warning."""
    responses = [
        FakeQueryResult(result_rows=[(11000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(10000, 1000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                ("a", 500, datetime(2024, 1, 14), "e1", 50, datetime(2024, 1, 16), "e2"),
                ("b", 400, datetime(2024, 1, 14), "e3", 40, datetime(2024, 1, 16), "e4"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_shift_windows(),
        max_candidates_per_field=2,
    )
    assert any("candidate cap" in w and "attr:user" in w for w in result.warnings)


def test_proportion_shift_insufficient_data_empty_baseline():
    """A baseline window with zero events cannot anchor a proportion."""
    responses = [
        FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(0, 500)], column_names=["bl", "w0"]),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert result.status == "insufficient_data"
    assert any("baseline window contains no events" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# interval_periodicity — statistics helpers
# ---------------------------------------------------------------------------


def test_poisson_rate_g_equal_rates_is_zero():
    assert _poisson_rate_g(100, 10.0, 50, 5.0) == 0.0


def test_poisson_rate_g_total_silence():
    # All 100 events on one side of an even exposure split: G = 2·100·ln 2.
    g = _poisson_rate_g(100, 1.0, 0, 1.0)
    assert abs(g - 200.0 * np.log(2.0)) < 1e-9


def test_poisson_rate_g_degenerate_inputs():
    assert _poisson_rate_g(0, 1.0, 0, 1.0) == 0.0
    assert _poisson_rate_g(10, 0.0, 5, 1.0) == 0.0


def test_greenwood_p_too_few_spacings():
    assert _greenwood_p(0.5, 1) == (0.0, 1.0)


def test_greenwood_moments_match_simulation():
    """The Greenwood E[G]/Var[G] constants match a uniform-spacings simulation."""
    rng = np.random.default_rng(42)
    n = 10
    gs = []
    for _ in range(4000):
        pts = np.sort(rng.random(n - 1))
        spacings = np.diff(np.concatenate(([0.0], pts, [1.0])))
        gs.append(float(np.sum(spacings**2)))
    mean_expected = 2.0 / (n + 1)
    var_expected = 4.0 * (n - 1) / ((n + 1) ** 2 * (n + 2) * (n + 3))
    assert abs(np.mean(gs) - mean_expected) < 0.02 * mean_expected
    assert abs(np.var(gs) - var_expected) < 0.10 * var_expected
    # Perfectly even spacings sit in the left tail — but at the N = 10
    # minimum the tail is shallow (z ≈ -1.9): a minimal beacon train is
    # borderline by design, not a slam dunk.
    _, p_even_10 = _greenwood_p(1.0 / n, n)
    assert 0.01 < p_even_10 < 0.05
    # With a longer train the same perfect regularity is decisive.
    _, p_even_100 = _greenwood_p(1.0 / 100, 100)
    assert p_even_100 < 1e-6
    # A G at the null expectation is unremarkable.
    _, p_null = _greenwood_p(mean_expected, n)
    assert 0.4 < p_null < 0.6


# ---------------------------------------------------------------------------
# interval_periodicity — detector
# ---------------------------------------------------------------------------

# Row layout produced by the candidate scan: val, then 10 columns per window
# (n, k, mean, std, med, sum2, first, last, first_evt, last_evt), baseline
# first. `_iv_row` keeps the tests readable.

_IV_COLS = ["val"] + [
    f"w{w}_{c}"
    for w in (0, 1)
    for c in ("n", "k", "mean", "std", "med", "sum2", "first", "last", "first_evt", "last_evt")
]


def _iv_row(val: str, bl: tuple, wb: tuple) -> tuple:
    return (val, *bl, *wb)


def _iv_empty_block() -> tuple:
    # ClickHouse shape for a window with no occurrences: NaN aggregates and
    # type-default min/max/argMin values.
    nan = float("nan")
    epoch = datetime(1970, 1, 1)
    return (0, 0, nan, nan, nan, 0.0, epoch, epoch, "", "")


def _iv_windows() -> AnalysisWindows:
    # Baseline 14 days, suspect 4 days (d_b = 1,209,600 s, d_w = 345,600 s).
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="incident",
    )


def _beacon_windows() -> AnalysisWindows:
    # Short 2-hour suspect window (d_w = 7,200 s) so a 100-minute beacon train
    # covers well over the span floor.
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 16, 2, 0, tzinfo=UTC),
        label="incident",
    )


# A 60-second heartbeat over the 14-day baseline: regular (CV ≈ 0.017).
_HEARTBEAT_BL = (
    20160,
    20159,
    60.0,
    1.0,
    60.0,
    20159 * 3601.0,
    datetime(2024, 1, 1, 0, 0),
    datetime(2024, 1, 14, 23, 59),
    "evt-bl-first",
    "evt-bl-last",
)

# A bursty baseline (CV = 1.5) — beaconing-gate eligible, cadence-ineligible.
_BURSTY_BL = (
    51,
    50,
    100.0,
    150.0,
    40.0,
    50 * 32500.0,
    datetime(2024, 1, 2),
    datetime(2024, 1, 14),
    "evt-bl-first",
    "evt-bl-last",
)


def test_interval_insufficient_data_without_windows():
    """Temporal-only: no windows → insufficient_data without touching ClickHouse."""
    svc = _svc([])
    result = svc.find_interval_periodicity("c1", ["s1"], fields=["attr:service"])
    assert result.status == "insufficient_data"
    assert result.detector == "interval_periodicity"
    assert result.method == "cadence"
    assert any("temporal-only" in w for w in result.warnings)
    assert svc.ch.client._calls == []


def test_interval_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "no_data"


def test_interval_missed_cadence_full_silence():
    """A baseline heartbeat with zero suspect-window events is a 'missed' finding."""
    responses = [
        FakeQueryResult(result_rows=[(21000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(20500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, _iv_empty_block())],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.method == "cadence"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "missed"
    assert r.count == 0
    assert r.baseline_count == 20160
    assert r.first_seen is None
    # Representative event = last baseline occurrence (the D6 silence case).
    assert r.event_id == "evt-bl-last"
    assert r.details["last_seen_baseline"] is not None
    # ~5,760 arrivals expected from the 60 s baseline cadence over 4 days.
    assert abs(r.details["expected_count"] - 5760) < 1
    assert r.q_value <= 0.05
    assert r.score > 10
    assert r.details["allowlist_field"] == "attr:service"
    assert r.details["allowlist_value"] == "heartbeat"
    assert r.details["m_tests"] == 1


def test_interval_accelerated_cadence():
    """A regular value whose rate jumps 6× is flagged 'accelerated'."""
    wb = (
        34560,
        34559,
        10.0,
        0.5,
        10.0,
        34559 * 100.25,
        datetime(2024, 1, 16, 0, 0),
        datetime(2024, 1, 19, 23, 59),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(60000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(21000, 35000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "accelerated"
    assert abs(r.details["rate_ratio"] - 6.0) < 0.01
    assert r.event_id == "evt-w-first"
    assert r.first_seen is not None
    assert r.window_median_interval == 10.0


def test_interval_beaconing_new_regularity():
    """A bursty baseline value arriving every 60 s in the window is beaconing."""
    wb = (
        101,
        100,
        60.0,
        0.0,
        60.0,
        100 * 3600.0,  # Σδ² with every delta exactly 60 s
        datetime(2024, 1, 16, 0, 5),
        datetime(2024, 1, 16, 1, 45),  # span = 6,000 s of the 7,200 s window
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("10.0.0.66", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:dest_ip"], windows=_beacon_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "new_regularity"
    assert r.window_cv == 0.0
    assert r.baseline_cv == 1.5
    # G = 360,000 / 6,000² = 0.01, well below E[G] = 2/101.
    assert abs(r.statistic - 0.01) < 1e-9
    assert r.details["greenwood_z"] < -3
    assert abs(r.details["span_fraction"] - 6000 / 7200) < 1e-3
    assert r.q_value <= 0.05
    assert r.event_id == "evt-w-first"


def test_interval_beacon_span_floor_suppresses_burst():
    """An evenly spaced but short burst (tiny span fraction) is not beaconing."""
    wb = (
        101,
        100,
        60.0,
        0.0,
        60.0,
        100 * 3600.0,
        datetime(2024, 1, 16, 0, 5),
        datetime(2024, 1, 16, 1, 45),  # span 6,000 s of a 345,600 s window
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("10.0.0.66", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    # Same data, but the long 4-day suspect window: span fraction ≈ 0.017.
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:dest_ip"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_beacon_cv_floor():
    """A significant Greenwood z with a loose window CV (> ceiling) is suppressed."""
    wb = (
        101,
        100,
        60.0,
        30.0,  # CV 0.5 > beacon_cv_max 0.3
        60.0,
        100 * 4500.0,  # Σδ² = k·(mean² + var) = 100·(3600 + 900)
        datetime(2024, 1, 16, 0, 5),
        datetime(2024, 1, 16, 1, 45),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("10.0.0.66", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:dest_ip"], windows=_beacon_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_rate_ratio_effect_floor():
    """A significant but small (< min_rate_ratio) rate change is suppressed."""
    wb = (
        8640,  # 1.5× the baseline rate over the 4-day window
        8639,
        40.0,
        2.0,
        40.0,
        8639 * 1604.0,
        datetime(2024, 1, 16, 0, 0),
        datetime(2024, 1, 19, 23, 59),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(30000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(21000, 9000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_regularity_gate_excludes_bursty_baseline():
    """A bursty baseline value with a count drop gets no cadence test at all."""
    wb = (
        3,
        2,
        1000.0,
        800.0,
        900.0,
        2 * 1640000.0,
        datetime(2024, 1, 16, 1, 0),
        datetime(2024, 1, 16, 2, 0),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("job", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    # Bursty baseline → no cadence-break test; k_w = 2 < beacon minimum → no
    # beacon test either. status stays ok (the field was evaluated).
    assert result.status == "ok"
    assert result.results == []


def test_interval_dead_band_gets_no_test():
    """A baseline CV between the regular ceiling and irregular floor is untested."""
    dead_band_bl = (
        201,
        200,
        100.0,
        65.0,  # CV 0.65 — inside the deliberate dead band [0.5, 0.8]
        90.0,
        200 * 14225.0,
        datetime(2024, 1, 1),
        datetime(2024, 1, 14),
        "evt-bl-first",
        "evt-bl-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("svc", dead_band_bl, _iv_empty_block())],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_fdr_gates_weak_evidence():
    """A regular but sparse baseline (7 events) can't establish a missed arrival."""
    sparse_regular_bl = (
        7,
        6,
        172800.0,  # every ~2 days
        1000.0,
        172800.0,
        6 * 172800.0**2,
        datetime(2024, 1, 1),
        datetime(2024, 1, 13),
        "evt-bl-first",
        "evt-bl-last",
    )
    wb = (
        1,
        0,
        float("nan"),
        float("nan"),
        float("nan"),
        0.0,
        datetime(2024, 1, 17),
        datetime(2024, 1, 17),
        "evt-w",
        "evt-w",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("backup", sparse_regular_bl, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    # Expected ~2 arrivals, observed 1 — p ≈ 0.5, nowhere near the q gate.
    assert result.status == "ok"
    assert result.results == []


def test_interval_allowlist_suppression():
    responses = [
        FakeQueryResult(result_rows=[(21000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(20500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, _iv_empty_block())],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1",
        ["s1"],
        fields=["attr:service"],
        windows=_iv_windows(),
        allowlist={("attr:service", "heartbeat")},
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_candidate_cap_warning():
    responses = [
        FakeQueryResult(result_rows=[(21000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(20500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                _iv_row("a", _HEARTBEAT_BL, _iv_empty_block()),
                _iv_row("b", _HEARTBEAT_BL, _iv_empty_block()),
            ],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1",
        ["s1"],
        fields=["attr:service"],
        windows=_iv_windows(),
        max_candidates_per_field=2,
    )
    assert any("candidate cap" in w and "attr:service" in w for w in result.warnings)


def test_interval_tiny_window_warning():
    responses = [
        FakeQueryResult(result_rows=[(1030,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 30)], column_names=["bl", "w0"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert any("only 30 events" in w for w in result.warnings)


def test_interval_insufficient_data_empty_baseline():
    responses = [
        FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(0, 500)], column_names=["bl", "w0"]),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "insufficient_data"
    assert any("baseline window contains no events" in w for w in result.warnings)


def test_interval_sql_partitions_deltas_within_windows():
    """The candidate scan partitions the lag by (value, window) and keeps baseline rows."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_interval_periodicity("c1", ["s1"], fields=["attr:service"], windows=_iv_windows())
    candidate_sql = client.full_queries[2]
    # Deltas must be computed strictly within one (value, window) partition —
    # a boundary-straddling delta would corrupt both windows' statistics.
    assert "PARTITION BY val, win" in candidate_sql
    assert "lagInFrame(toNullable(ts))" in candidate_sql
    assert "arrayJoin(arrayFilter" in candidate_sql
    # Baseline-only (silent) values must survive; first-seen must not.
    assert "HAVING w0_n >= 1" in candidate_sql
    assert "{b0:String}" in candidate_sql and "{b1:String}" in candidate_sql


# ---------------------------------------------------------------------------
# W2 — per-source clock-skew correction (offset-corrected effective timestamp)
# ---------------------------------------------------------------------------


def _w2_windows() -> AnalysisWindows:
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )


def test_window_preds_fast_path_is_bare_timestamp():
    """No active offset → predicates are byte-identical to the pre-W2 form and
    no offset params are bound (keeps ClickHouse's primary-index path intact)."""
    params: dict[str, Any] = {}
    bp, sps = _window_preds(_w2_windows(), params, None)
    assert bp == "(timestamp >= {b0:String} AND timestamp < {b1:String})"
    assert sps == ["(timestamp >= {w0s:String} AND timestamp < {w0e:String})"]
    assert OFFSET_SRC_PARAM not in params and OFFSET_VAL_PARAM not in params


def test_window_preds_offset_path_uses_effective_ts_and_binds_arrays():
    """An active offset rewrites the predicates over the effective-ts expression
    and binds the parallel source/offset arrays consumed by transform()."""
    params: dict[str, Any] = {}
    offsets = {"s1": 3600}
    bp, sps = _window_preds(_w2_windows(), params, offsets)
    eff = effective_ts_sql(offsets)
    assert eff != "timestamp"
    assert bp == f"({eff} >= {{b0:String}} AND {eff} < {{b1:String}})"
    assert sps == [f"({eff} >= {{w0s:String}} AND {eff} < {{w0e:String}})"]
    assert params[OFFSET_SRC_PARAM] == ["s1"]
    assert params[OFFSET_VAL_PARAM] == [3600]


def test_value_novelty_temporal_fast_path_binds_no_offset_params():
    """A zero/absent offset map leaves every query's params free of the offset
    arrays — the byte-identical fast path detectors share with the query layer."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 10)], column_names=["bl", "w0"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    svc.find_value_novelty("c1", ["s1"], fields=["artifact"], windows=_w2_windows())
    assert all(OFFSET_SRC_PARAM not in p for p in svc.ch.client._all_parameters)


def test_value_novelty_temporal_offset_uses_effective_ts():
    """With an offset, the temporal scan's representative aggregates and window
    predicates are built over the effective timestamp and the arrays are bound."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 10)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty(
        "c1", ["s1"], fields=["artifact"], windows=_w2_windows(), source_offsets={"s1": 3600}
    )
    scan_sql = client.full_queries[2]
    assert "addSeconds(timestamp, transform(source_id" in scan_sql
    assert "minIf(if(" in scan_sql  # minIf over the effective-ts expression
    assert any(p.get(OFFSET_VAL_PARAM) == [3600] for p in client._all_parameters)


def test_range_violations_offset_projects_source_id_in_subquery():
    """The effective-ts expression references source_id, so the numeric
    subqueries must project it — otherwise the outer window predicate / min()
    would reference an out-of-scope column."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            # baseline stat (min/max/n)
            FakeQueryResult(result_rows=[(0.0, 100.0, 500)], column_names=["lo", "hi", "n"]),
            # violations
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_range_violations(
        "c1", ["s1"], fields=["attr:bytes"], windows=_w2_windows(), source_offsets={"s1": -120}
    )
    stat_sql = client.full_queries[1]
    viol_sql = client.full_queries[2]
    # source_id projected into the inner num subqueries (fast path omits it).
    assert "AS num, timestamp, source_id" in stat_sql
    assert "source_id" in viol_sql
    assert "addSeconds(timestamp, transform(source_id" in stat_sql
    assert any(p.get(OFFSET_VAL_PARAM) == [-120] for p in client._all_parameters)


def test_frequency_self_baseline_offset_buckets_effective_ts():
    """Self-baseline bucketing runs over the effective timestamp so a skewed
    source's events land in their corrected bucket."""
    client = RecordingClient(
        [
            # timeline range (min, max)
            FakeQueryResult(
                result_rows=[(datetime(2024, 1, 1), datetime(2024, 1, 3))],
                column_names=["min", "max"],
            ),
            # bucket scan
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_frequency_anomalies("c1", ["s1"], series_field="artifact", source_offsets={"s1": 3600})
    range_sql = client.full_queries[0]
    bucket_sql = client.full_queries[1]
    assert "min(if(" in range_sql  # range over effective-ts
    assert "toStartOfInterval(if(" in bucket_sql
    assert any(p.get(OFFSET_SRC_PARAM) == ["s1"] for p in client._all_parameters)


def test_get_timeline_range_offset_uses_effective_ts():
    client = RecordingClient(
        [
            FakeQueryResult(
                result_rows=[(datetime(2024, 1, 1), datetime(2024, 1, 3))], column_names=[]
            )
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.get_timeline_range("c1", ["s1"], source_offsets={"s1": 3600})
    sql = client.full_queries[0]
    assert "min(if(" in sql and "max(if(" in sql
    assert client._all_parameters[0].get(OFFSET_VAL_PARAM) == [3600]


def test_order_violations_offset_shifts_reported_timestamps_only():
    """The skew math stays on the raw column (offset cancels within a source);
    only the reported timestamp/prev_timestamp are shifted for presentation."""
    ts = datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC)
    prev = datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1, 60.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[("evt-a", ts, prev, 60.0, 100, 3)],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    result = svc.find_order_violations(
        "c1", ["s1"], min_skew_seconds=1.0, source_offsets={"s1": 3600}
    )
    f = result.results[0]
    assert f.timestamp == (ts + timedelta(hours=1)).isoformat()
    assert f.prev_timestamp == (prev + timedelta(hours=1)).isoformat()
    # Skew delta is invariant to a uniform per-source shift.
    assert f.skew_seconds == 60.0


def test_interval_periodicity_offset_uses_effective_ts_for_gaps():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_w2_windows(), source_offsets={"s1": 3600}
    )
    candidate_sql = client.full_queries[2]
    # ts (the lag/gap column) is the effective timestamp, so gaps are computed
    # on the corrected timeline.
    assert "addSeconds(timestamp, transform(source_id" in candidate_sql
    assert "AS ts" in candidate_sql


# ---------------------------------------------------------------------------
# sequence_novelty — detector
# ---------------------------------------------------------------------------

# Query order: count, window totals, per-window n-gram totals, novel n-grams.
# Novel-gram row layout: gram (list of values), baseline_cnt, then 3 columns
# per suspect window (cnt, first_ts, first_evt).

_SEQ_TOTALS_COLS = ["w_idx", "n"]
_SEQ_NOVEL_COLS = ["gram", "baseline_cnt", "w0_cnt", "w0_first", "w0_evt"]


def _seq_windows() -> AnalysisWindows:
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="incident",
    )


def _seq_responses(
    total: int,
    window_totals: tuple[int, int],
    ngram_totals: list[tuple[int, int]],
    novel_rows: list[tuple],
) -> list[FakeQueryResult]:
    return [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[window_totals], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(result_rows=ngram_totals, column_names=_SEQ_TOTALS_COLS),
        FakeQueryResult(result_rows=novel_rows, column_names=_SEQ_NOVEL_COLS),
    ]


def test_sequence_insufficient_data_without_windows():
    """Temporal-only: no windows → insufficient_data without touching ClickHouse."""
    svc = _svc([])
    result = svc.find_sequence_novelty("c1", ["s1"])
    assert result.status == "insufficient_data"
    assert result.detector == "sequence_novelty"
    assert result.method == "ngram"
    assert any("temporal-only" in w for w in result.warnings)
    assert svc.ch.client._calls == []


def test_sequence_ngram_validation():
    svc = _svc([])
    for bad in (1, 6):
        try:
            svc.find_sequence_novelty("c1", ["s1"], ngram=bad, windows=_seq_windows())
        except ValueError as exc:
            assert "between 2 and 5" in str(exc)
        else:
            raise AssertionError(f"ngram={bad} did not raise")
    assert svc.ch.client._calls == []


def test_sequence_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "no_data"
    assert result.windows is not None


def test_sequence_baseline_without_ngrams_insufficient():
    """A baseline window with no complete n-grams cannot vouch for anything."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2, 500)], column_names=["bl_total", "w0_total"]),
        # Only the suspect window has complete n-grams.
        FakeQueryResult(result_rows=[(0, 498)], column_names=_SEQ_TOTALS_COLS),
    ]
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "insufficient_data"
    assert any("no complete sequences of length 3" in w for w in result.warnings)
    # The novel-gram query must not have run.
    assert len(svc.ch.client._calls) == 3


def test_sequence_novel_ngram_flagged():
    first_ts = datetime(2024, 1, 17, 12, 0, tzinfo=UTC)
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=[(["login", "priv_esc", "wipe"], 0, 2, first_ts, "evt-1")],
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "ok"
    assert result.detector == "sequence_novelty"
    assert result.method == "ngram"
    assert result.baseline_size == 8000
    assert len(result.results) == 1
    r = result.results[0]
    assert r.field == "artifact"
    assert r.values == ["login", "priv_esc", "wipe"]
    assert r.value == "login → priv_esc → wipe"
    assert r.count == 2
    assert abs(r.score - (-np.log(2 / 1998))) < 1e-3
    assert r.event_id == "evt-1"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-17T12:00")
    assert r.details["n"] == 3
    assert r.details["window_ngram_total"] == 1998
    assert r.details["baseline_ngram_total"] == 7998
    assert r.details["window_label"] == "incident"
    assert r.details["allowlist_field"] == "artifact"
    assert r.details["allowlist_value"] == "login → priv_esc → wipe"
    assert result.windows is not None


def test_sequence_multiple_suspect_windows():
    """One finding per (gram, suspect window with cnt > 0)."""
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 15, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow("w-a", datetime(2024, 1, 16, tzinfo=UTC), datetime(2024, 1, 18, tzinfo=UTC)),
            TimeWindow("w-b", datetime(2024, 1, 19, tzinfo=UTC), datetime(2024, 1, 21, tzinfo=UTC)),
        ),
    )
    ts_a = datetime(2024, 1, 16, 1, 0, tzinfo=UTC)
    ts_b = datetime(2024, 1, 19, 1, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[(8000, 1000, 1000)], column_names=["bl_total", "w0_total", "w1_total"]
        ),
        FakeQueryResult(
            result_rows=[(-1, 7998), (0, 998), (1, 998)], column_names=_SEQ_TOTALS_COLS
        ),
        FakeQueryResult(
            result_rows=[(["a", "b", "c"], 0, 3, ts_a, "evt-a", 1, ts_b, "evt-b")],
            column_names=[
                "gram",
                "baseline_cnt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
                "w1_cnt",
                "w1_first",
                "w1_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=windows)
    assert result.status == "ok"
    assert len(result.results) == 2
    by_label = {r.details["window_label"]: r for r in result.results}
    assert by_label["w-a"].count == 3 and by_label["w-a"].event_id == "evt-a"
    assert by_label["w-b"].count == 1 and by_label["w-b"].event_id == "evt-b"
    # Rarer (count 1) window scores higher.
    assert by_label["w-b"].score > by_label["w-a"].score


def test_sequence_allowlist_suppression():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=[
            (["a", "b", "c"], 0, 2, first_ts, "evt-1"),
            (["x", "y", "z"], 0, 5, first_ts, "evt-2"),
        ],
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty(
        "c1",
        ["s1"],
        windows=_seq_windows(),
        allowlist={("artifact", "a → b → c")},
    )
    assert result.status == "ok"
    assert [r.value for r in result.results] == ["x → y → z"]


def test_sequence_exclude_event_ids():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-normal")],
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty(
        "c1", ["s1"], windows=_seq_windows(), exclude_event_ids={"evt-normal"}
    )
    assert result.status == "ok"
    assert result.results == []


def test_sequence_tiny_window_and_cap_warnings():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    novel_rows = [([f"v{i}", "b", "c"], 0, 1, first_ts, f"evt-{i}") for i in range(3)]
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 30),
        ngram_totals=[(-1, 7998), (0, 28)],
        novel_rows=novel_rows,
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows(), max_candidates=3)
    assert result.status == "ok"
    assert any("only 28 complete sequences" in w for w in result.warnings)
    assert any("candidate cap" in w for w in result.warnings)


def test_sequence_limit_applied():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    novel_rows = [([f"v{i}", "b", "c"], 0, i + 1, first_ts, f"evt-{i}") for i in range(5)]
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=novel_rows,
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows(), limit=2)
    assert len(result.results) == 2
    # Sorted by surprise descending — lowest counts first.
    assert [r.count for r in result.results] == [1, 2]


def test_sequence_sql_shape():
    """The generated SQL is the explainable per-(source, window) lag chain."""
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-1")],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_sequence_novelty(
        "c1", ["s1"], series_field="attr:proc", ngram=3, windows=_seq_windows()
    )
    assert result.status == "ok"
    totals_sql = client.full_queries[2]
    novel_sql = client.full_queries[3]
    for sql in (totals_sql, novel_sql):
        assert "PARTITION BY source_id, w_idx" in sql
        assert "ORDER BY ets, byte_offset, line_number, event_id" in sql
        assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in sql
        assert "lagInFrame(val, 2) OVER w" in sql
        assert "lagInFrame(val, 1) OVER w" in sql
        assert "lagInFrame(toNullable(val), 2) OVER w AS guard" in sql
        assert "guard IS NOT NULL" in sql
        assert "attributes[{fk:String}]" in sql
        assert "multiIf(" in sql
    assert "HAVING baseline_cnt = 0 AND (w0_cnt) > 0" in novel_sql
    # Window bounds bound as parameters (forensic reproducibility).
    params = client._all_parameters[3]
    assert params["b0"] == "2024-01-01 00:00:00"
    assert params["w0s"] == "2024-01-16 00:00:00"


def test_sequence_offset_uses_effective_ts():
    """W2: active source offsets switch ordering and windows to effective ts."""
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-1")],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows(), source_offsets={"s1": 3600})
    novel_sql = client.full_queries[3]
    assert "addSeconds(timestamp, transform(source_id" in novel_sql
    assert "AS ets" in novel_sql
