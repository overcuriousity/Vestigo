"""Live-ClickHouse verification of the statistical viz aggregations.

Checks the ClickHouse-native statistics (``corr``, ``rankCorr``,
``simpleLinearRegression``, ``skewPop``) against straightforward Python
recomputations over the same fixture, plus the Freedman–Diaconis auto-bin
path end-to-end — this is the test that settles the "do the natives'
semantics (NULL handling, ties) match what we claim in captions?" question
on the real server. Requires the dev compose stack (skipped when ClickHouse
is unreachable), same pattern as ``test_viz_timeseries_fused_clickhouse.py``.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import pytest

from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.queries import EventQuery, EventQueryService
from vestigo.models.event import Event

CASE_ID = f"tc-vizstats-{uuid.uuid4().hex[:8]}"
SRC = "src-vizstats"

# Deterministic fixture: y = 3x + 7 + alternating noise, plus rows that are
# non-numeric on one or both axes (must be excluded pairwise), plus a
# right-skewed value column for the skewness/FD checks.
_PAIRS = [(float(i), 3.0 * i + 7.0 + (0.5 if i % 2 == 0 else -0.5)) for i in range(40)]
_SKEWED = [1.0] * 30 + [2.0] * 10 + [3.0] * 5 + [20.0, 40.0, 80.0]


#: Grouped-distribution fixture: three groups of different sizes plus two
#: tiny ones that must fall outside a top-2 cut.
_GROUPS = {"alpha": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "beta": [10.0, 20.0, 30.0, 40.0]}
_TINY_GROUPS = {"gamma": [7.0], "delta": [8.0], "epsilon": [9.0]}


def _event(i: int, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SRC,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="a" * 64,
        parser_name="test-vizstats",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=f"2026-03-01T{i % 24:02d}:{i % 60:02d}:00+00:00",
        timestamp_desc="Test Time",
        artifact="test:vizstats",
        attributes=attrs,
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0
    for x, y in _PAIRS:
        events.append(_event(i, {"x": str(x), "y": str(y), "val": str(_SKEWED[i % len(_SKEWED)])}))
        i += 1
    # Non-numeric on y — excluded from the pair set, included nowhere.
    events.append(_event(i, {"x": "999", "y": "not-a-number"}))
    i += 1
    # Non-numeric on both.
    events.append(_event(i, {"x": "n/a", "y": "n/a"}))
    i += 1
    for group, values in {**_GROUPS, **_TINY_GROUPS}.items():
        for v in values:
            events.append(_event(i, {"grp": group, "lat": str(v)}))
            i += 1
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
    store.delete_source_events(CASE_ID, SRC)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return num / den


def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True)) / sum(
        (a - mx) ** 2 for a in xs
    )
    return slope, my - slope * mx


def test_scatter_stats_match_python_recomputation(service: EventQueryService) -> None:
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    result = service.field_scatter(query, "attr:x", "attr:y", limit=1000)

    # Pairwise-complete n: the two non-numeric rows are excluded.
    assert result["total"] == len(_PAIRS)
    xs = [p[0] for p in _PAIRS]
    ys = [p[1] for p in _PAIRS]

    stats_block = result["stats"]
    assert stats_block["basis"] == "full"
    assert stats_block["pearson"]["r"] == pytest.approx(_pearson(xs, ys), abs=1e-9)
    assert stats_block["pearson"]["p"] is not None and stats_block["pearson"]["p"] < 1e-6

    # rankCorr semantics check: a strictly monotonic relationship must give
    # Spearman ρ = 1 — this is the live gate on the native's tie/NULL handling.
    assert stats_block["spearman"]["rho"] == pytest.approx(1.0, abs=1e-9)

    slope, intercept = _least_squares(xs, ys)
    assert stats_block["regression"]["slope"] == pytest.approx(slope, abs=1e-9)
    assert stats_block["regression"]["intercept"] == pytest.approx(intercept, abs=1e-9)
    assert stats_block["regression"]["r_squared"] == pytest.approx(_pearson(xs, ys) ** 2, abs=1e-9)

    # Sample covered every pair, so Kendall/Shapiro ran over the full set.
    assert stats_block["kendall"]["tau"] == pytest.approx(1.0, abs=1e-9)
    assert stats_block["kendall"]["p"] is not None and stats_block["kendall"]["p"] < 1e-6
    assert stats_block["shapiro"]["x"] is not None
    assert stats_block["recommendation"] in {"pearson", "spearman"}


def test_scatter_degenerate_axis_nulls_coefficients(service: EventQueryService) -> None:
    """A constant axis has no variance — corr/rankCorr NaN must become None."""
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC], field_filters={"attr:x": ["1.0"]})
    result = service.field_scatter(query, "attr:x", "attr:y", limit=10)
    if result["total"] == 0:
        pytest.skip("filter matched no rows — fixture drifted")
    assert result["stats"]["pearson"]["r"] is None
    assert result["stats"]["pearson"]["p"] is None


def test_numeric_stats_skewness_sign_and_fd_bins(service: EventQueryService) -> None:
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    result = service.field_numeric_stats(query, "attr:val")

    values = [_SKEWED[i % len(_SKEWED)] for i in range(len(_PAIRS))]
    n = len(values)
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    g1 = sum(((v - mean) / sd) ** 3 for v in values) / n

    assert result["count"] == n
    assert result["skewness"] == pytest.approx(g1, abs=1e-9)
    assert result["skewness"] > 0.5  # the fixture is right-skewed by construction

    assert result["bin_rule"] == "fd"
    assert 5 <= len(result["bins"]) <= 60
    assert result["bin_width"] == pytest.approx(
        (result["max"] - result["min"]) / len(result["bins"]), abs=1e-9
    )

    # Manual override still honored.
    manual = service.field_numeric_stats(query, "attr:val", bins=7)
    assert manual["bin_rule"] == "manual"
    assert len(manual["bins"]) == 7


def test_numeric_grouped_quantiles_omission_and_points(service: EventQueryService) -> None:
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    result = service.field_numeric_grouped(
        query, "attr:lat", "attr:grp", groups=2, bins=4, points=True, points_limit=1000
    )

    all_values = [v for vs in {**_GROUPS, **_TINY_GROUPS}.values() for v in vs]
    assert result["total"] == len(all_values)
    assert result["min"] == min(all_values)
    assert result["max"] == max(all_values)
    assert result["distinct_groups"] == len(_GROUPS) + len(_TINY_GROUPS)

    # Top-2 by count: alpha (6) then beta (4). The three singletons are
    # omitted, never rolled into an "Other" box.
    assert [g["value"] for g in result["groups"]] == ["alpha", "beta"]
    assert result["omitted_groups"] == len(_TINY_GROUPS)
    assert result["omitted_count"] == sum(len(v) for v in _TINY_GROUPS.values())

    for group in result["groups"]:
        expected = sorted(_GROUPS[group["value"]])
        assert group["count"] == len(expected)
        assert group["min"] == expected[0]
        assert group["max"] == expected[-1]
        assert group["mean"] == pytest.approx(sum(expected) / len(expected), abs=1e-9)
        # Per-group bins share the GLOBAL value range, so silhouettes compare.
        assert group["bins"][0]["x0"] == result["min"]
        assert group["bins"][-1]["x1"] == pytest.approx(result["max"], abs=1e-9)
        assert sum(b["count"] for b in group["bins"]) == len(expected)

    points = result["points"]
    assert points["total"] == sum(len(v) for v in _GROUPS.values())
    assert points["shown"] == points["total"]  # cap far above the fixture size
    assert {g for g, _ in points["values"]} == set(_GROUPS)


def test_numeric_grouped_point_sample_respects_the_cap(service: EventQueryService) -> None:
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    result = service.field_numeric_grouped(
        query, "attr:lat", "attr:grp", groups=2, points=True, points_limit=3
    )
    assert result["points"]["shown"] == 3
    assert result["points"]["total"] == sum(len(v) for v in _GROUPS.values())


def test_field_correlation_is_pairwise_complete(service: EventQueryService) -> None:
    """The whole reason this does not use corrMatrix: a row missing one field
    must not drop out of the OTHER pairs (listwise deletion)."""
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    result = service.field_correlation(query, ["attr:x", "attr:y", "attr:val"])

    assert result["kind"] == "corr"
    by_pair = {(p["x"], p["y"]): p for p in result["pairs"]}
    assert set(by_pair) == {
        ("attr:x", "attr:y"),
        ("attr:x", "attr:val"),
        ("attr:y", "attr:val"),
    }

    xy = by_pair[("attr:x", "attr:y")]
    xs = [p[0] for p in _PAIRS]
    ys = [p[1] for p in _PAIRS]
    assert xy["n"] == len(_PAIRS)
    assert xy["pearson"] == pytest.approx(_pearson(xs, ys), abs=1e-9)
    assert xy["spearman"] == pytest.approx(1.0, abs=1e-9)
    assert xy["p_pearson"] is not None and xy["p_pearson"] < 1e-6

    # The x-only row (x=999, y non-numeric) participates in no pair, but the
    # x↔val pair keeps every row where BOTH are numeric.
    assert by_pair[("attr:x", "attr:val")]["n"] == len(_PAIRS)


def test_field_correlation_reports_non_numeric_fields(service: EventQueryService) -> None:
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    result = service.field_correlation(query, ["attr:x", "attr:grp"])
    assert result["dropped_fields"] == [{"field": "attr:grp", "reason": "non_numeric"}]
    pair = result["pairs"][0]
    assert pair["n"] == 0
    assert pair["pearson"] is None and pair["p_pearson"] is None


def test_field_correlation_rejects_degenerate_field_counts(service: EventQueryService) -> None:
    query = EventQuery(case_id=CASE_ID, source_ids=[SRC])
    with pytest.raises(ValueError, match="at least two fields"):
        service.field_correlation(query, ["attr:x"])
    with pytest.raises(ValueError, match="at most 8 fields"):
        service.field_correlation(query, [f"attr:f{i}" for i in range(9)])
