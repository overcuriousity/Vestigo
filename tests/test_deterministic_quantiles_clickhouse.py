"""Every detector quantile is a pure function of the data (ROADMAP D19).

Plain ClickHouse ``quantile`` reservoir-samples with a random generator above
8192 values, and the numeric-range and entropy fences it fed *gate findings*:
two runs over identical data could disagree about what is out of band. The
detectors now use ``quantileDeterministic`` keyed on a per-row hash, so a rerun
reproduces the fence, the medians and the drift quantiles exactly. The
populations here are all well past the 8192-value reservoir, which is where a
random reservoir would have shown.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.conftest import insert_generated_events
from vestigo.db.anomaly_stats import AnalysisWindows, StatisticalAnomalyService, TimeWindow
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = pytest.mark.clickhouse

_CASE = f"determinism-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-determinism"
#: Well above the 8192-value reservoir, and the interval/drift windows below
#: each hold more than that too.
_N = 40_000

_WINDOWS = AnalysisWindows(
    baseline=TimeWindow(
        "baseline", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC)
    ),
    suspects=(
        TimeWindow(
            "suspect", datetime(2026, 1, 1, 6, tzinfo=UTC), datetime(2026, 1, 1, 12, tzinfo=UTC)
        ),
    ),
)


@pytest.fixture(scope="module")
def store():
    """40k events a second apart; a pseudo-random numeric field and a hashed
    string field, both with tens of thousands of distinct values, and a
    two-valued ``svc`` series whose gaps are pseudo-random too."""
    s = ClickHouseStore()
    s.init_schema()
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_SOURCE,
        n=_N,
        attributes=(
            "map('n', toString(cityHash64(number) % 100000),"
            " 'q', concat('q', toString(cityHash64(number * 7)), 'zz'),"
            " 'svc', if(cityHash64(number) % 3 = 0, 'alpha', 'beta'))"
        ),
    )
    yield s
    s.delete_source_events(_CASE, _SOURCE)


def _twice(run):
    first = run()
    second = run()
    assert first.status == second.status == "ok", (first.warnings, second.warnings)
    return first, second


def test_numeric_range_fence_is_reproducible(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    a, b = _twice(lambda: svc.find_range_violations(_CASE, [_SOURCE], fields=["attr:n"]))
    assert [f.details for f in a.results] == [f.details for f in b.results]
    assert a.total_findings == b.total_findings


def test_entropy_fence_is_reproducible(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    a, b = _twice(lambda: svc.find_entropy_outliers(_CASE, [_SOURCE], fields=["attr:q"]))
    assert [f.details for f in a.results] == [f.details for f in b.results]
    assert a.total_findings == b.total_findings


def test_drift_quantiles_are_reproducible(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    a, b = _twice(
        lambda: svc.find_distribution_drift(
            _CASE, [_SOURCE], fields=["attr:n"], windows=_WINDOWS, min_ks_d=0.0, fdr_q=1.0
        )
    )
    assert [f.details for f in a.results] == [f.details for f in b.results]


def test_interval_medians_are_reproducible(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    # fdr_q=1 and a ratio floor of 1+ε keep every tested value in the result
    # so the medians are compared rather than an empty list.
    a, b = _twice(
        lambda: svc.find_interval_periodicity(
            _CASE,
            [_SOURCE],
            fields=["attr:svc"],
            windows=_WINDOWS,
            fdr_q=1.0,
            min_rate_ratio=1.0000001,
            cv_regular_max=10.0,
        )
    )
    assert [f.details for f in a.results] == [f.details for f in b.results]
