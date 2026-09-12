"""A sort larger than its scan's cap spills to disk instead of dying at the cap.

Every heavy and foreground scan carries a ``max_bytes_before_external_sort``
sized under its own ``max_memory_usage`` (``db/_scan.py``). Since ClickHouse
25.1, ``max_bytes_ratio_before_external_sort`` defaults to 0.5, and a sort then
spills only once the query also holds half of the server's free memory — a
number unrelated to the per-query cap and usually far above it. The spill never
engaged and the query failed with code 241 at the cap: the production
``interval_periodicity`` failure, 819 MiB cap, on a stock 26.6 server.

These run real sorts at a deliberately small cap, each several times larger than
that cap unspilled, so they only pass if the spill actually fires. GROUP BY is
not covered here because it was never affected: its ratio only lowers the
absolute threshold.
"""

from __future__ import annotations

import pytest

from vestigo.db import _scan
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = [pytest.mark.clickhouse, pytest.mark.slow]

# The heavy cap; the foreground clause gets half of it. Far below half of any
# test server's free memory, which is what the ratio would have demanded before
# a sort could spill.
_CAP = 256 * 1024**2


@pytest.fixture
def small_cap(cap_scan):
    cap_scan(_CAP)


@pytest.fixture(scope="module")
def client():
    return ClickHouseStore().client


# Rows scale with each clause's cap: roughly 3x the cap unspilled, while keeping
# the spill-file count low. Reading spilled files back costs memory per file, so
# a tiny cap over a huge sort fails on the way back in — a limit of spilling
# itself, not the defect under test.
@pytest.mark.parametrize(
    ("clause", "rows"),
    [
        pytest.param(_scan.heavy_scan_settings, 20_000_000, id="heavy"),
        pytest.param(_scan.foreground_scan_settings, 10_000_000, id="foreground"),
    ],
)
def test_a_sort_bigger_than_the_cap_spills(client, small_cap, clause, rows):
    client.command(
        f"SELECT n, s FROM (SELECT number AS n, toString(cityHash64(number)) AS s "
        f"FROM numbers({rows})) ORDER BY s "
        f"{clause()} FORMAT Null"
    )


def test_a_window_function_sort_bigger_than_the_cap_spills(client, small_cap):
    """The interval_periodicity shape: ``lagInFrame`` over ``PARTITION BY value``.

    The docs used to say window sorts cannot spill at all. They can; what
    stopped them was the same ratio as every other sort.
    """
    rows, partitions = 20_000_000, 50_000
    result = client.query(
        f"SELECT count(), countIf(prev IS NULL) FROM ("
        f"  SELECT lagInFrame(toNullable(ts)) OVER w AS prev FROM ("
        f"    SELECT toString(number % {partitions}) AS val,"
        f"           toDateTime64(number, 3) AS ts, generateUUIDv4(number) AS event_id"
        f"    FROM numbers({rows}))"
        f"  WINDOW w AS (PARTITION BY val ORDER BY ts, event_id"
        f"               ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING)"
        f") {_scan.heavy_scan_settings()}"
    ).result_rows
    # One NULL lag per partition: its first row has no predecessor.
    assert result == [(rows, partitions)]
