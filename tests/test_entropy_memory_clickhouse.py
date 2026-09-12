"""Entropy outliers learn a high-cardinality field's band under the per-query cap.

The band is learned over *distinct* values, and that pass read them through
``SELECT DISTINCT`` — a set held in memory that never spills, the shape the
charset learner was moved off (``tests/test_charset_memory_clickhouse.py``)
over the same auto-selected free-text fields. Three million distinct values at
a 512 MiB cap (four threads, pinned) fail that shape with code 241 and fit
once the distinct pass is a ``GROUP BY``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.conftest import insert_generated_events
from vestigo.db.anomaly_stats import AnalysisWindows, StatisticalAnomalyService, TimeWindow
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = [pytest.mark.clickhouse, pytest.mark.slow]

_CASE = f"memtest-entropy-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-entropy-mem"
_PLAIN = 3_000_000
_CAP = 256 * 1024**2


@pytest.fixture
def bounded_scan(cap_scan):
    # Twice the learn's cap: the violation scan that follows runs as a
    # page/count pair at half the cap each (the same floor the grouped charset
    # scan has), and at four threads over these rows it needs ~130 MiB of its
    # own. The learn still fails at this cap on the old shape.
    cap_scan(2 * _CAP, threads=4)


@pytest.fixture(scope="module")
def store():
    """Three million distinct query strings over 30 days, plus one padding value.

    Plain values ``q=<cityHash64>&x=abcdefgh`` every 0.864 s from
    2026-03-01, so the first 1 900 000 fall before 2026-03-20; the padding
    value ``aaaaaaaaaaaaaaaa`` sits at 2026-03-28 with entropy 0, below any
    band the plain values learn.
    """
    s = ClickHouseStore()
    s.init_schema()
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_SOURCE,
        n=_PLAIN + 1,
        timestamp=(
            f"if(number < {_PLAIN}, "
            "addSeconds(toDateTime64('2026-03-01 00:00:00.500', 3), intDiv(number * 864, 1000)), "
            "toDateTime64('2026-03-28 12:00:00', 3))"
        ),
        attributes=(
            f"map('q', if(number < {_PLAIN}, "
            "concat('q=', toString(cityHash64(number)), '&x=abcdefgh'), "
            "'aaaaaaaaaaaaaaaa'))"
        ),
    )
    yield s
    s.delete_source_events(_CASE, _SOURCE)


def test_self_baseline_band(bounded_scan, store):
    svc = StatisticalAnomalyService(clickhouse=store)

    result = svc.find_entropy_outliers(_CASE, [_SOURCE], fields=["attr:q"])

    assert result.status == "ok", result.warnings
    assert [f.value for f in result.results][:1] == ["aaaaaaaaaaaaaaaa"]


def test_baseline_window_band(bounded_scan, store):
    svc = StatisticalAnomalyService(clickhouse=store)
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 20, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow(
                "suspect", datetime(2026, 3, 25, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)
            ),
        ),
    )

    result = svc.find_entropy_outliers(_CASE, [_SOURCE], fields=["attr:q"], windows=windows)

    assert result.status == "ok", result.warnings
    assert [f.value for f in result.results][:1] == ["aaaaaaaaaaaaaaaa"]
