"""Charset novelty learns a high-cardinality field's alphabet under its per-query cap.

Every learning scan read the field through ``SELECT DISTINCT`` — a set held
in memory that never spills — and the self-baseline ones took the distinct
count from ``count() OVER ()``, a frameless window that buffers every distinct
value's character array before emitting a row. Both grow with the field's
distinct values: 9.3 million for ``http_query`` on one production IIS source,
so the detector failed at its cap on exactly the free-text-ish fields charset
anomalies hide in.

Three million distinct values at a 256 MiB cap (512 MiB for the grouped learn;
four threads, pinned) fail every one of those shapes with code 241 and fit comfortably once the distinct
pass is a ``GROUP BY`` (measured on 26.6.1.1193: 150–230 MiB).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.conftest import insert_generated_events
from vestigo.db.anomaly_stats import AnalysisWindows, StatisticalAnomalyService, TimeWindow
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = [pytest.mark.clickhouse, pytest.mark.slow]

_CASE = f"memtest-charset-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-charset-mem"
_PLAIN = 3_000_000
_CAP = 256 * 1024**2


@pytest.fixture
def bounded_scan(cap_scan):
    cap_scan(_CAP, threads=4)


@pytest.fixture(scope="module")
def store():
    """Three million distinct query strings over 30 days, plus two carrying ``§``.

    Plain values ``q=<cityHash64>&x=abcdefgh`` share one small alphabet and are
    split across two hosts; every 0.864 s from 2026-03-01 00:00:00.500, so
    the first 1 900 000 fall before 2026-03-20. The two ``§`` values sit on host
    ``h0`` at 2026-03-28 — rare in every reference, never seen in the baseline.
    """
    s = ClickHouseStore()
    s.init_schema()
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_SOURCE,
        n=_PLAIN + 2,
        timestamp=(
            f"if(number < {_PLAIN}, "
            "addSeconds(toDateTime64('2026-03-01 00:00:00.500', 3), intDiv(number * 864, 1000)), "
            "toDateTime64('2026-03-28 12:00:00', 3))"
        ),
        attributes=(
            f"map('q', if(number < {_PLAIN}, "
            "concat('q=', toString(cityHash64(number)), '&x=abcdefgh'), "
            f"concat('q=§', toString(number - {_PLAIN - 1}), '&x=abcdefgh')), "
            f"'host', if(number < {_PLAIN}, concat('h', toString(number % 2)), 'h0'))"
        ),
    )
    yield s
    s.delete_source_events(_CASE, _SOURCE)


def _findings(result):
    assert result.status == "ok"
    return sorted(
        (f.details["novel_chars"], f.details["baseline_distinct_values"]) for f in result.results
    )


def test_self_baseline_learn(bounded_scan, store):
    svc = StatisticalAnomalyService(clickhouse=store)

    result = svc.find_charset_novelty(_CASE, [_SOURCE], fields=["attr:q"])

    assert _findings(result) == [(["§"], 3_000_002), (["§"], 3_000_002)]


def test_grouped_self_baseline_learn(cap_scan, store):
    # Twice the cap: the grouped violation scan runs as a page/count pair at half
    # the cap each, and over these rows at four threads it needs ~450 MiB of its
    # own — the same with 20 groups as with 2000, so a floor, not the scan under
    # test. The grouped learn still fails at this cap without the fix.
    cap_scan(2 * _CAP, threads=4)
    svc = StatisticalAnomalyService(clickhouse=store)

    result = svc.find_charset_novelty(_CASE, [_SOURCE], fields=["attr:q"], group_field="attr:host")

    assert {f.details["group_value"] for f in result.results} == {"h0"}
    # h0 is every other plain value plus both `§` values.
    assert _findings(result) == [(["§"], 1_500_002), (["§"], 1_500_002)]


def test_baseline_window_learn(bounded_scan, store):
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

    result = svc.find_charset_novelty(_CASE, [_SOURCE], fields=["attr:q"], windows=windows)

    assert _findings(result) == [(["§"], 1_900_000), (["§"], 1_900_000)]
