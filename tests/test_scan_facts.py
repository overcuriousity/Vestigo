"""What the startup probe reads off ClickHouse, and how it is parsed.

ClickHouse 26.6.1 exposes no CPU-count metric in `system.asynchronous_metrics`
(no CGroupMaxCPU, no OSNProcessors). The server's own resolved core count is
`system.settings.max_threads`, reported as `'auto(N)'` — and that N *is*
cgroup-quota aware, unlike the per-core `OSUserTimeCPU*` series, which counts
host cores and would reproduce the bug on a CPU-limited container.
"""

from __future__ import annotations

import pytest

from vestigo.db._scan import _COUNTED_CACHES, parse_max_threads_setting, resolve_cache_bytes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("'auto(12)'", (12, True)),  # what the HTTP interface returns, quotes included
        ("auto(12)", (12, True)),  # what the native client returns
        ("auto(2)", (2, True)),  # verified under `--cpus=2`
        # An operator pinned it server-side: a thread limit, not a core count,
        # which is why `is_auto` is reported rather than the two collapsed.
        ("16", (16, False)),
        ("'16'", (16, False)),
        ("auto(0)", None),  # nonsense N is not a core count
        ("0", None),
        ("auto()", None),
        ("", None),
        (None, None),
        ("auto(abc)", None),
    ],
)
def test_parse_max_threads_setting(raw, expected):
    assert parse_max_threads_setting(raw) == expected


def test_probe_reads_caches_and_core_count_from_a_real_server():
    """The names in the queries are the names 26.6 actually has.

    A renamed setting fails silently — the probe suppresses and returns what it
    got — so the only thing that catches a typo'd column or table is asking the
    server the suite already requires to be up.
    """
    from vestigo.db.clickhouse import ClickHouseStore

    facts = ClickHouseStore().server_resource_facts()

    assert facts, "the probe answered at all"
    for name in _COUNTED_CACHES:
        assert name in facts, f"{name} is not a setting this server has"
    assert facts.get("resolved_max_threads", 0) >= 1
    assert "max_threads_is_auto" in facts


def test_the_probe_does_not_read_caches_it_does_not_count():
    """Every name in the query has to end up in the breakdown an operator acts
    on. A fact nobody counts is one the next reader has to work out is inert."""
    from vestigo.db.clickhouse import ClickHouseStore

    facts = ClickHouseStore().server_resource_facts()
    _, breakdown = resolve_cache_bytes(facts)

    assert set(breakdown) == set(_COUNTED_CACHES)
