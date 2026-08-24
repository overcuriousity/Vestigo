"""Heavy-scan memory budget resolution and admission gate (db/_scan.py).

The heavy-scan memory budget is a *total* across concurrent scans — pinned
via ``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` or auto-sized to a ratio of detected
RAM (cgroup-aware) — and each query's ``max_memory_usage`` is budget /
``VESTIGO_STAT_SCAN_CONCURRENCY``. ``HEAVY_SCAN_GATE`` enforces that no more than
that many detector scans run at once.
"""

from __future__ import annotations

from vestigo.db import _scan
from vestigo.db._scan import (
    _FALLBACK_MAX_MEMORY_BYTES,
    _resolve_scan_memory_budget,
)


def test_explicit_value_pins_the_budget():
    """A nonzero VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES wins over any detection."""
    assert _resolve_scan_memory_budget(12_000_000_000, 0.8, 128 << 30) == 12_000_000_000


def test_budget_is_divided_across_concurrency_slots():
    """Explicit or auto, the budget is a total: per-query cap = budget / N.

    Regression for the session-52 OOM: a pinned 8 GiB cap was per *query*,
    so two concurrent detector scans stacked 16 GiB onto a 12 GiB host.
    """
    assert _resolve_scan_memory_budget(8 << 30, 0.8, None, concurrency=2) == 4 << 30
    assert _resolve_scan_memory_budget(0, 0.5, 16 << 30, concurrency=2) == 4 << 30
    # Degenerate concurrency never divides by zero or inflates the budget.
    assert _resolve_scan_memory_budget(8 << 30, 0.8, None, concurrency=0) == 8 << 30


def test_auto_uses_ratio_of_detected_memory():
    assert _resolve_scan_memory_budget(0, 0.8, 128 << 30) == int((128 << 30) * 0.8)
    assert _resolve_scan_memory_budget(0, 0.5, 16 << 30) == 8 << 30


def test_detection_failure_falls_back_to_conservative_default():
    assert _resolve_scan_memory_budget(0, 0.8, None) == _FALLBACK_MAX_MEMORY_BYTES
    assert _resolve_scan_memory_budget(0, 0.8, 0) == _FALLBACK_MAX_MEMORY_BYTES


def test_cgroup_limit_bounds_detection(monkeypatch):
    """Inside a memory-limited container the cgroup limit wins over host RAM."""
    monkeypatch.setattr(_scan, "_cgroup_memory_limit", lambda: 8 << 30)
    monkeypatch.setattr(_scan, "_meminfo_total", lambda: 128 << 30)
    monkeypatch.setattr(_scan, "_physical_memory_total", lambda: 128 << 30)
    settings = _scan.get_settings()
    expected = int((8 << 30) * 0.8) // settings.stat_scan_concurrency
    assert _scan.detect_scan_memory_budget() == expected


def test_unlimited_cgroup_uses_physical_memory(monkeypatch):
    monkeypatch.setattr(_scan, "_cgroup_memory_limit", lambda: None)
    monkeypatch.setattr(_scan, "_meminfo_total", lambda: None)
    monkeypatch.setattr(_scan, "_physical_memory_total", lambda: 64 << 30)
    settings = _scan.get_settings()
    expected = int((64 << 30) * 0.8) // settings.stat_scan_concurrency
    assert _scan.detect_scan_memory_budget() == expected


def test_meminfo_beats_ballooned_sysinfo(monkeypatch):
    """On VMs with memory ballooning sysinfo() overreports (503 GiB on a
    128 GiB box); MemTotal is the usable truth and must win via min()."""
    monkeypatch.setattr(_scan, "_cgroup_memory_limit", lambda: None)
    monkeypatch.setattr(_scan, "_meminfo_total", lambda: 128 << 30)
    monkeypatch.setattr(_scan, "_physical_memory_total", lambda: 503 << 30)
    settings = _scan.get_settings()
    expected = int((128 << 30) * 0.8) // settings.stat_scan_concurrency
    assert _scan.detect_scan_memory_budget() == expected


def test_heavy_scan_settings_carries_a_positive_budget():
    """The clause always renders a concrete positive max_memory_usage."""
    clause = _scan.HEAVY_SCAN_SETTINGS
    value = int(clause.rsplit("max_memory_usage = ", 1)[1])
    assert value > 0
    assert "max_bytes_before_external_sort" in clause


def test_spill_thresholds_stay_below_the_per_query_cap():
    """Spill must engage before the cap kills the query — a threshold at or
    above max_memory_usage would never fire."""
    clause = _scan.HEAVY_SCAN_SETTINGS
    cap = int(clause.rsplit("max_memory_usage = ", 1)[1])
    group_by = int(clause.split("max_bytes_before_external_group_by = ")[1].split(",")[0])
    sort = int(clause.split("max_bytes_before_external_sort = ")[1].split(",")[0])
    assert group_by <= cap // 2
    assert sort <= cap // 2


def test_gate_admits_at_most_the_configured_concurrency():
    """HEAVY_SCAN_GATE holds surplus scans; find_* entry points acquire it."""
    settings = _scan.get_settings()
    n = settings.stat_scan_concurrency
    acquired = []
    for _ in range(n):
        assert _scan.HEAVY_SCAN_GATE.acquire(blocking=False)
        acquired.append(True)
    try:
        assert not _scan.HEAVY_SCAN_GATE.acquire(blocking=False)
    finally:
        for _ in acquired:
            _scan.HEAVY_SCAN_GATE.release()


def test_every_detector_entry_point_is_gated():
    from vestigo.db.anomaly_stats import StatisticalAnomalyService

    detectors = [name for name in dir(StatisticalAnomalyService) if name.startswith("find_")]
    assert detectors, "no find_* detectors discovered"
    for name in detectors:
        fn = getattr(StatisticalAnomalyService, name)
        assert getattr(fn, "__wrapped__", None) is not None, f"{name} is not gated"


def test_enrichment_partition_rewrite_takes_a_gate_slot():
    """The enrichment apply is gated like a detector scan.

    ``max_memory_usage`` is per *query*: an ungated whole-partition rewrite
    stacked on top of a full set of admitted detector scans is how a 32 GiB
    full-docker host OOM-killed clickhouse-server mid-apply (session-56).
    """
    from vestigo.db.clickhouse import ClickHouseStore

    seen: list[str] = []

    class _Gate:
        def __enter__(self):
            seen.append("acquired")
            return self

        def __exit__(self, *exc):
            seen.append("released")
            return False

    class _Client:
        def command(self, sql):
            # The swap must happen while the slot is still held: it queues
            # merges on freshly written parts, which the per-query cap misses.
            assert seen == ["acquired"], "rewrite ran outside the gate"

        def query(self, sql, parameters=None):
            assert seen == ["acquired"], "rewrite ran outside the gate"
            assert "min_insert_block_size_bytes" in sql
            # Left at ClickHouse's default (0 = single-threaded INSERT SELECT):
            # every insert thread carries its own squashing buffer, so raising
            # it adds write-side memory to the query being bounded here.
            assert "max_insert_threads" not in sql

    store = ClickHouseStore.__new__(ClickHouseStore)
    store.client = _Client()
    store.database = "testdb"

    import vestigo.db.clickhouse as ch_mod

    original = ch_mod.HEAVY_SCAN_GATE
    ch_mod.HEAVY_SCAN_GATE = _Gate()
    try:
        store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])
    finally:
        ch_mod.HEAVY_SCAN_GATE = original

    assert seen == ["acquired", "released"]


def test_apply_insert_block_size_is_read_per_call():
    """An admin-console edit reaches the next apply without a restart.

    ``enrichment_apply_insert_block_bytes`` is declared ``restart_required=False``
    in the settings registry, unlike every ``stat_scan_*`` setting — those are
    frozen into the module-level ``HEAVY_SCAN_SETTINGS`` string at import, while
    this one is interpolated from ``get_settings()`` when the apply runs. This
    test is what makes that claim honest.
    """
    from vestigo.core.config import set_runtime_overrides
    from vestigo.db.clickhouse import ClickHouseStore

    sql_seen: list[str] = []

    class _Client:
        def command(self, sql):
            pass

        def query(self, sql, parameters=None):
            sql_seen.append(sql)

    store = ClickHouseStore.__new__(ClickHouseStore)
    store.client = _Client()
    store.database = "testdb"

    try:
        set_runtime_overrides({"enrichment_apply_insert_block_bytes": 8_388_608})
        store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])
    finally:
        set_runtime_overrides({})

    assert "min_insert_block_size_bytes = 8388608" in sql_seen[0]
