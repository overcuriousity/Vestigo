"""What the startup probe reads off ClickHouse, and how it is parsed.

ClickHouse 26.6.1 exposes no CPU-count metric in `system.asynchronous_metrics`
(no CGroupMaxCPU, no OSNProcessors). The server's own resolved core count is
`system.settings.max_threads`, reported as `'auto(N)'` — and that N *is*
cgroup-quota aware, unlike the per-core `OSUserTimeCPU*` series, which counts
host cores and would reproduce the bug on a CPU-limited container.
"""

from __future__ import annotations

import pytest

from vestigo.db._scan import parse_resolved_max_threads


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("'auto(12)'", 12),  # what the HTTP interface returns, quotes included
        ("auto(12)", 12),  # what the native client returns
        ("auto(2)", 2),  # verified under `--cpus=2`
        ("16", 16),  # an operator pinned it server-side
        ("'16'", 16),
        ("auto(0)", None),  # nonsense N is not a core count
        ("0", None),
        ("auto()", None),
        ("", None),
        (None, None),
        ("auto(abc)", None),
    ],
)
def test_parse_resolved_max_threads(raw, expected):
    assert parse_resolved_max_threads(raw) == expected


def test_probe_reads_caches_and_core_count_from_a_real_server():
    """The names in the queries are the names 26.6 actually has.

    A renamed setting fails silently — the probe suppresses and returns what it
    got — so the only thing that catches a typo'd column or table is asking the
    server the suite already requires to be up.
    """
    from vestigo.db.clickhouse import ClickHouseStore

    facts = ClickHouseStore().server_resource_facts()

    assert facts, "the probe answered at all"
    assert "mark_cache_size" in facts
    assert "index_mark_cache_size" in facts
    assert "primary_index_cache_size" in facts
    assert facts.get("resolved_max_threads", 0) >= 1
