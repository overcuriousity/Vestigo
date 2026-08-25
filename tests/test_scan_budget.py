"""Heavy-scan memory budget resolution and admission gate (db/_scan.py).

The heavy-scan memory budget is a *total* across concurrent scans — pinned
via ``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` or auto-sized to a ratio of detected
RAM (cgroup-aware) — and each query's ``max_memory_usage`` is budget /
``VESTIGO_STAT_SCAN_CONCURRENCY``. ``HEAVY_SCAN_GATE`` enforces that no more than
that many detector scans run at once.
"""

from __future__ import annotations

import pytest

from vestigo.db import _scan
from vestigo.db._scan import (
    _FALLBACK_MAX_MEMORY_BYTES,
    _resolve_scan_memory_budget,
)


@pytest.fixture(autouse=True)
def _no_probed_ceiling():
    """Start every test from "the probe has not run".

    The ceiling lives in module state that startup recovery writes once, so any
    earlier test that boots the app leaves the *dev* ClickHouse's real ceiling
    behind — and the local-detection tests below then measure that instead of
    the memory they mocked. Ordering-dependent, which is the worst way to find
    out.
    """
    saved = (
        _scan._clickhouse_ceiling,
        _scan._clickhouse_bounded,
        _scan._clickhouse_cache_bytes,
        _scan._clickhouse_cache_breakdown,
    )
    _scan.configure_scan_budget(None, bounded=False)
    yield
    _scan.configure_scan_budget(
        saved[0], bounded=saved[1], cache_bytes=saved[2], cache_breakdown=saved[3]
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
    clause = _scan.heavy_scan_settings()
    value = int(clause.rsplit("max_memory_usage = ", 1)[1])
    assert value > 0
    assert "max_bytes_before_external_sort" in clause


def test_spill_thresholds_stay_below_the_per_query_cap():
    """Spill must engage before the cap kills the query — a threshold at or
    above max_memory_usage would never fire."""
    clause = _scan.heavy_scan_settings()
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
            if "system." in sql:

                class _Empty:
                    result_rows = []

                return _Empty()
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
    in the settings registry, and is interpolated from ``get_settings()`` when
    the apply runs. This test is what makes that claim honest.
    """
    from vestigo.core.config import set_runtime_overrides
    from vestigo.db.clickhouse import ClickHouseStore

    sql_seen: list[str] = []

    class _Client:
        def command(self, sql):
            pass

        def query(self, sql, parameters=None):
            sql_seen.append(sql)

            class _Empty:
                result_rows = []

            return _Empty()

    store = ClickHouseStore.__new__(ClickHouseStore)
    store.client = _Client()
    store.database = "testdb"

    try:
        set_runtime_overrides({"enrichment_apply_insert_block_bytes": 8_388_608})
        store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])
    finally:
        set_runtime_overrides({})

    assert "min_insert_block_size_bytes = 8388608" in sql_seen[0]


# ── Deriving the budget from ClickHouse rather than from our own host ───────
#
# The production failure this replaces (session-186): a full-docker stack on a
# 64 GiB host, no container limits and no `max_server_memory_usage`. The app
# detected 64 GiB *from inside its own container*, authorized 0.8 x 64 / 2 =
# 25.6 GiB per query, and the kernel OOM-killed clickhouse-server with nothing
# in ClickHouse's own log. Every guardrail read as satisfied throughout.


def test_explicit_server_ceiling_is_taken_as_bounded():
    """`max_server_memory_usage` set by an operator is the ceiling, full stop."""
    ceiling, bounded = _scan.resolve_clickhouse_ceiling(
        {"max_server_memory_usage": 10 << 30, "os_memory_total": 64 << 30}
    )

    assert (ceiling, bounded) == (10 << 30, True)


def test_cgroup_limited_server_derives_a_bounded_ceiling():
    """No explicit setting, but a real container limit: ratio x the cgroup."""
    ceiling, bounded = _scan.resolve_clickhouse_ceiling(
        {
            "max_server_memory_usage": 0,
            "max_server_memory_usage_to_ram_ratio": 0.9,
            "cgroup_memory_total": 32 << 30,
            "os_memory_total": 64 << 30,
        }
    )

    assert ceiling == int((32 << 30) * 0.9)
    assert bounded is True


def test_unlimited_container_is_reported_unbounded():
    """The session-186 configuration must not read as a limit.

    ClickHouse always reports *a* ceiling — absent an explicit setting it is
    0.9 x detected RAM. In a container with no memory limit that is the whole
    host, which the server neither owns nor is alone on. Sizing against it
    still beats guessing, so it is returned; calling it bounded would hide
    exactly the misconfiguration this probe exists to surface.
    """
    ceiling, bounded = _scan.resolve_clickhouse_ceiling(
        {
            "max_server_memory_usage": 0,
            "max_server_memory_usage_to_ram_ratio": 0.9,
            "cgroup_memory_total": 0,
            "os_memory_total": 64 << 30,
        }
    )

    assert ceiling == int((64 << 30) * 0.9)
    assert bounded is False


def test_ratio_is_not_truncated_to_zero():
    """Regression: reading the ratio as an int makes 0.9 become 0.

    That would resolve every unpinned server to a zero ceiling — i.e. to
    "ClickHouse may use nothing", inverting the conclusion.
    """
    ceiling, _ = _scan.resolve_clickhouse_ceiling(
        {"max_server_memory_usage_to_ram_ratio": 0.9, "os_memory_total": 64 << 30}
    )

    assert ceiling and ceiling > 0


def test_failed_probe_yields_no_ceiling():
    """An old server, or a user without rights on system.server_settings."""
    assert _scan.resolve_clickhouse_ceiling({}) == (None, False)


def _report_with(monkeypatch, ceiling, bounded, per_query=None):
    """A report against a chosen ceiling, and optionally a chosen budget.

    The budget is injected by patching :func:`detect_scan_memory_budget` rather
    than by setting ``stat_scan_max_memory_bytes``: a repository ``.env`` pins
    that variable, and an environment value always beats the runtime-override
    layer a test could set.
    """
    monkeypatch.setattr(_scan, "_clickhouse_ceiling", ceiling)
    monkeypatch.setattr(_scan, "_clickhouse_bounded", bounded)
    if per_query is not None:
        monkeypatch.setattr(_scan, "detect_scan_memory_budget", lambda: per_query)
    return _scan.scan_budget_report()


def test_report_flags_a_budget_over_the_server_ceiling(monkeypatch):
    """Scans alone authorized more than ClickHouse may use in total."""
    concurrency = _scan.get_settings().stat_scan_concurrency
    report = _report_with(monkeypatch, 8 << 30, True, per_query=(16 << 30) // concurrency)

    assert report["risk"] == "over_budget"
    assert report["total_bytes"] > report["clickhouse_ceiling_bytes"]


def test_report_flags_an_unbounded_server(monkeypatch):
    """A derived ceiling over RAM the server does not own is not a limit."""
    assert _report_with(monkeypatch, 57 << 30, False)["risk"] == "unbounded"


def test_report_is_ok_when_the_budget_fits(monkeypatch):
    concurrency = _scan.get_settings().stat_scan_concurrency
    report = _report_with(monkeypatch, 28 << 30, True, per_query=(16 << 30) // concurrency)

    assert report["risk"] == "ok"
    assert report["clickhouse_ceiling_is_explicit"] is True


def test_clause_follows_the_configured_ceiling(monkeypatch):
    """The clause is built per query, so the probe reaches every scan.

    Before this, the clause was a module constant built at import — the one
    moment at which the app cannot ask ClickHouse anything, which is why the
    budget could only ever be sized from the app's own host.
    """
    monkeypatch.setattr(_scan, "_clickhouse_ceiling", None)
    monkeypatch.setattr(_scan, "_clickhouse_bounded", False)
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 64 << 30)
    before = _scan.heavy_scan_settings()

    _scan.configure_scan_budget(8 << 30, bounded=True)
    try:
        after = _scan.heavy_scan_settings()
    finally:
        _scan.configure_scan_budget(None, bounded=False)

    settings = _scan.get_settings()
    expected = int((8 << 30) * settings.stat_scan_memory_ratio) // settings.stat_scan_concurrency
    assert before != after
    assert f"max_memory_usage = {expected}" in after


# ── The merge window the admission gate used to miss ────────────────────────


def _apply_store(merge_counts):
    """A ClickHouseStore whose system.merges answers from *merge_counts*."""
    from vestigo.db.clickhouse import ClickHouseStore

    seen: list[str] = []

    class _Client:
        def command(self, sql):
            seen.append(sql)

        def query(self, sql, parameters=None):
            seen.append(sql)
            if "system.merges" in sql:
                # The poll must name the partitions the apply just wrote, not
                # the whole table — an unscoped one waits out merges caused by
                # unrelated ingest, holding the admission slot the whole time.
                assert "partition_id IN" in sql
                assert parameters["pids"] == ["pid-1"]
                value = merge_counts.pop(0) if merge_counts else 0

                class _R:
                    result_rows = [(value,)]

                return _R()
            if "system.parts" in sql:

                class _P:
                    result_rows = [("pid-1",)]

                return _P()

            class _Empty:
                result_rows = []

            return _Empty()

    store = ClickHouseStore.__new__(ClickHouseStore)
    store.client = _Client()
    store.database = "testdb"
    return store, seen


def test_apply_holds_its_slot_until_merges_drain(monkeypatch):
    """`REPLACE PARTITION` returns before the merges it queues are done.

    Merge memory is the one consumer `max_memory_usage` cannot reach, so
    releasing the admission slot at the ALTER admitted the next detector sweep
    straight into the merge burst — which is how the gate could be held
    "across the swap" and still not cover the expensive part.
    """
    monkeypatch.setattr("vestigo.db.clickhouse.time.sleep", lambda _s: None)
    store, seen = _apply_store([2, 1, 0])

    store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])

    merge_polls = [sql for sql in seen if "system.merges" in sql]
    assert len(merge_polls) == 3, "polled until the merge count reached zero"
    replace_at = next(i for i, sql in enumerate(seen) if "REPLACE PARTITION" in sql)
    assert seen.index(merge_polls[0]) > replace_at, "waits after the swap, not before"


def test_apply_gives_up_on_the_merge_wait_rather_than_failing(monkeypatch):
    """A slow merge must never fail an apply that is already swapped in.

    The wait is a courtesy to whatever runs next, not a correctness
    requirement — the partition is durable by the time it starts.
    """
    monkeypatch.setattr("vestigo.db.clickhouse.time.sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 1.0, 2.0, 999.0])
    monkeypatch.setattr("vestigo.db.clickhouse.time.monotonic", lambda: next(clock))
    store, _seen = _apply_store([5, 5, 5, 5, 5])

    store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])


def test_merge_wait_can_be_switched_off():
    """0 skips it — right when ClickHouse bounds merges itself."""
    from vestigo.core.config import set_runtime_overrides

    store, seen = _apply_store([9, 9, 9])
    try:
        set_runtime_overrides({"enrichment_apply_merge_wait_seconds": 0})
        store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])
    finally:
        set_runtime_overrides({})

    assert not [sql for sql in seen if "system.merges" in sql]


# ── The two halves of one budget, and one ceiling that is not a decision ────


def test_explicit_server_ceiling_is_clamped_by_the_ratio():
    """`max_server_memory_usage` above (ratio x RAM) is not what the server adopts.

    ClickHouse takes the *lower* of the two and logs a "lowered to" line. The
    reference stack pins 10 GiB against a 12 GiB container limit and a 0.8
    ratio, so reporting the pinned number would size the budget — and the
    `over_budget` check — against a ceiling ~4% above the real one.
    """
    ceiling, bounded = _scan.resolve_clickhouse_ceiling(
        {
            "max_server_memory_usage": 10 << 30,
            "max_server_memory_usage_to_ram_ratio": 0.8,
            "cgroup_memory_total": 12 << 30,
            "os_memory_total": 64 << 30,
        }
    )

    assert ceiling == int((12 << 30) * 0.8)
    assert bounded is True, "an operator still set it; it is a decision, not a guess"


def test_unbounded_server_ceiling_is_capped_by_local_detection(monkeypatch):
    """A ceiling ClickHouse only derived must not out-vote the app's own limit.

    App in a 4 GiB container, ClickHouse unlimited on a 64 GiB host: the probe
    reports 0.9 x 64 GiB, which `resolve_clickhouse_ceiling` has already
    classified as "unbounded". Believing it authorizes ~23 GiB to a single
    query. Two guesses, so the lower one wins.
    """
    monkeypatch.setattr(_scan, "_clickhouse_ceiling", int((64 << 30) * 0.9))
    monkeypatch.setattr(_scan, "_clickhouse_bounded", False)
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 4 << 30)

    assert _scan.scan_memory_ceiling() == 4 << 30


def test_bounded_server_ceiling_beats_local_detection(monkeypatch):
    """A ceiling an operator set describes the machine the queries run on.

    It is used even when it is *larger* than what the app's own container sees
    — that is the whole point of asking ClickHouse instead of guessing.
    """
    monkeypatch.setattr(_scan, "_clickhouse_ceiling", 28 << 30)
    monkeypatch.setattr(_scan, "_clickhouse_bounded", True)
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 4 << 30)

    assert _scan.scan_memory_ceiling() == 28 << 30


def test_budget_divides_by_the_gate_size_not_the_live_setting():
    """A live `stat_scan_concurrency` edit must not move the divisor alone.

    `HEAVY_SCAN_GATE` is sized at import and imported by value, so an admin
    console edit cannot resize it — but it *does* reach `get_settings()`
    immediately. Dividing by the live value would let a 2 -> 4 edit halve every
    query's cap while the gate still admitted 2, and a 4 -> 2 edit double it
    while the gate still admitted 4: twice the total budget ClickHouse was
    sized for, which is the OOM this pair exists to prevent.
    """
    before = _scan.detect_scan_memory_budget()
    pending = _scan._GATE_CONCURRENCY + 2
    # Patched rather than set through `set_runtime_overrides`: a repository
    # `.env` pins this variable, and an environment value always beats the
    # runtime-override layer — which is the same reason `_report_with` injects
    # its budget directly.
    edited = _scan.get_settings().model_copy(update={"stat_scan_concurrency": pending})
    original = _scan.get_settings
    _scan.get_settings = lambda: edited
    try:
        assert _scan.get_settings().stat_scan_concurrency == pending, "the edit did land"
        assert _scan.detect_scan_memory_budget() == before
        report = _scan.scan_budget_report()
    finally:
        _scan.get_settings = original

    assert report["concurrency"] == _scan._GATE_CONCURRENCY
    assert report["pending_concurrency"] == pending, "the waiting value is disclosed"
    assert report["total_bytes"] == before * _scan._GATE_CONCURRENCY


def test_merge_wait_is_skipped_when_the_apply_wrote_no_parts():
    """No staged parts means no merges of ours to wait for.

    The scratch table's partition ids are how the wait is scoped; an empty
    answer must skip the wait rather than fall back to polling the whole table,
    which is the unscoped behaviour this replaces.
    """
    store, seen = _apply_store([9, 9, 9])

    class _NoParts:
        result_rows = []

    original = store.client.query

    def query(sql, parameters=None):
        if "system.parts" in sql:
            seen.append(sql)
            return _NoParts()
        return original(sql, parameters=parameters)

    store.client.query = query
    store.finalize_enrichment_apply("c1", "s1", "job1", ["geo_country"])

    assert not [sql for sql in seen if "system.merges" in sql]


# ── Caches share the ceiling the budget is taken from ───────────────────────

_CACHE_FACTS = {
    "mark_cache_size": 2 << 30,
    "index_mark_cache_size": 512 << 20,
    "primary_index_cache_size": 1 << 30,
    "uncompressed_cache_size": 0,
    "index_uncompressed_cache_size": 0,
}


def test_cache_bytes_sums_only_the_caches_a_mergetree_scan_fills():
    """The server ships many cache settings; five of them are ours.

    Summing text-index/vector/iceberg/parquet cache maxima would report
    `over_budget` forever on a stack that never allocates any of them.
    """
    total, breakdown = _scan.resolve_cache_bytes(
        {**_CACHE_FACTS, "text_index_postings_cache_size": 2 << 30, "os_memory_total": 64 << 30}
    )

    assert total == (2 << 30) + (512 << 20) + (1 << 30)
    assert set(breakdown) == set(_CACHE_FACTS)
    assert breakdown["mark_cache_size"] == 2 << 30


def test_cache_bytes_is_zero_when_the_probe_said_nothing():
    """No probe (the CLI, or a pre-probe scan) must behave exactly as before."""
    assert _scan.resolve_cache_bytes({}) == (0, {})


def test_budget_is_a_ratio_of_what_is_left_after_caches():
    """`stat_scan_memory_ratio`'s help text has always claimed the remainder is
    headroom for merges and caches. It was taken of the whole ceiling instead.
    """
    assert _scan._resolve_scan_memory_budget(0, 0.8, 10 << 30, concurrency=2, caches=2 << 30) == (
        int((8 << 30) * 0.8) // 2
    )
    # An explicit pin is a decision and still bypasses the derivation entirely.
    assert _scan._resolve_scan_memory_budget(
        8 << 30, 0.8, 10 << 30, concurrency=2, caches=2 << 30
    ) == (4 << 30)


def test_caches_larger_than_the_ceiling_do_not_produce_a_negative_budget():
    """26.6's own defaults are 12 GiB of cache maxima under a 9.5 GiB ceiling —
    the exact condition the shipped memory.xml fix addresses, and one an operator
    can recreate at any time. The budget floors at the conservative fallback
    rather than going negative or to zero."""
    assert (
        _scan._resolve_scan_memory_budget(0, 0.8, 9 << 30, concurrency=2, caches=12 << 30)
        == _FALLBACK_MAX_MEMORY_BYTES // 2
    )


def test_report_counts_caches_against_the_ceiling(monkeypatch):
    """Scans that fit alone but not once the caches under the same ceiling are
    counted. This is the shipped-defaults condition from issue #302."""
    monkeypatch.setattr(_scan, "_clickhouse_ceiling", 10 << 30)
    monkeypatch.setattr(_scan, "_clickhouse_bounded", True)
    monkeypatch.setattr(_scan, "_clickhouse_cache_bytes", 5 << 30)
    monkeypatch.setattr(_scan, "_clickhouse_cache_breakdown", {"mark_cache_size": 5 << 30})
    monkeypatch.setattr(_scan, "detect_scan_memory_budget", lambda: (6 << 30) // 2)

    report = _scan.scan_budget_report()

    assert report["total_bytes"] < report["clickhouse_ceiling_bytes"], "scans alone fit"
    assert report["risk"] == "over_budget", "scans plus caches do not"
    assert report["cache_bytes"] == 5 << 30
    assert report["cache_breakdown"] == {"mark_cache_size": 5 << 30}
    assert report["headroom_bytes"] == (10 << 30) - (6 << 30) - (5 << 30)


def test_report_is_ok_when_scans_and_caches_both_fit(monkeypatch):
    monkeypatch.setattr(_scan, "_clickhouse_ceiling", 16 << 30)
    monkeypatch.setattr(_scan, "_clickhouse_bounded", True)
    monkeypatch.setattr(_scan, "_clickhouse_cache_bytes", 4 << 30)
    monkeypatch.setattr(_scan, "_clickhouse_cache_breakdown", {"mark_cache_size": 4 << 30})
    monkeypatch.setattr(_scan, "detect_scan_memory_budget", lambda: (8 << 30) // 2)

    report = _scan.scan_budget_report()

    assert report["risk"] == "ok"
    assert report["headroom_bytes"] == (16 << 30) - (8 << 30) - (4 << 30)
