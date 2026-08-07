"""Shared ClickHouse scan guardrails.

Single home for the SETTINGS clause every whole-corpus scan (GROUP BY over up
to hundreds of millions of rows) must carry: spill large aggregation states to
disk instead of ballooning RAM, cap the query's memory hard (fail one query,
not the server), and bound thread fan-out so several concurrent scans don't
oversubscribe the box. The limits are ``VESTIGO_*`` tunables (see
``core/config.py``).

The memory budget is a **total across concurrent scans**, resolved from
``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` when nonzero, else auto-sized to
``VESTIGO_STAT_SCAN_MEMORY_RATIO`` (0.8) of the detected memory — the cgroup limit
when the process runs in a memory-limited container, the machine's physical
RAM otherwise. Each query's ``max_memory_usage`` is budget /
``VESTIGO_STAT_SCAN_CONCURRENCY``, and :data:`HEAVY_SCAN_GATE` (acquired by every
detector entry point in ``db/anomaly_stats.py``) holds surplus scans so no
more than that many run at once — ``max_memory_usage`` alone is per *query*,
and N parallel detector requests stacking N full-budget queries is exactly
how a correctly-pinned budget still OOM-killed a 12 GiB ClickHouse host
(session-52 incident). The spill thresholds are clamped to half the per-query
cap so external aggregation/sort actually engages before the cap kills the
query.

Auto-detection is **local to the app process**; that matches the supported
deployments (compose stack or native app with local backing services, where
app and ClickHouse share the box). When ClickHouse runs on a different host,
pin the budget to that host's RAM (minus server-cache/merge headroom — ~70%
is a good start) with ``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` — a nonzero value
always wins over auto-detection. ClickHouse's own 90%-of-RAM server limit
cannot be relied on here: inside containers/VMs it may misdetect total memory
(observed 503 GiB on a 128 GiB VM), so these caps are the only real bound.

The clause is a string constant, built once at import from the process
settings, because it is interpolated into f-string SQL literals throughout the
detectors — a live function call there would embed the function repr, not the
clause. :data:`HEAVY_SCAN_GATE` is likewise imported *by value* into every scan
module, so rebinding it here would not reach those bindings. Both facts mean an
admin console edit to a ``stat_scan_*`` setting cannot take effect in the
running process, which is why every one of them is declared
``restart_required`` in ``core/settings_registry.py`` — making the values live
is a real refactor, and silently accepting an edit that does nothing is worse
than telling the operator to restart.

The gate is not detector-only: ``ClickHouseStore.finalize_enrichment_apply``
takes a slot for the enrichment partition rewrite too. It is the same class of
whole-partition query and, before it was gated, it stacked on top of a full set
of admitted detector scans and OOM-killed a 32 GiB host mid-apply.
"""

import os
import threading
from pathlib import Path

from vestigo.core.config import get_settings

# Used when detection is explicitly disabled nowhere but fails (exotic
# platforms) — the pre-auto-detection default from the session-27 incident.
_FALLBACK_MAX_MEMORY_BYTES = 12_000_000_000


def _cgroup_memory_limit() -> int | None:
    """The container's memory limit, if one is set (cgroup v2, then v1)."""
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == "max":  # v2: no limit configured
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # v1 reports "no limit" as PAGE_COUNTER_MAX (a huge sentinel).
        if 0 < value < 1 << 60:
            return value
    return None


def _meminfo_total() -> int | None:
    """MemTotal from /proc/meminfo — the kernel-managed usable RAM.

    Preferred over ``sysconf``: on VMs with memory ballooning/hotplug the
    ``sysinfo()`` syscall behind ``SC_PHYS_PAGES`` can report the *possible*
    memory ceiling (observed 503 GiB on a 128 GiB VM — the same misdetection
    that makes ClickHouse's own server limit unreliable there), while
    MemTotal matches what ``free`` reports.
    """
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _physical_memory_total() -> int | None:
    """Total physical RAM of the (virtual) machine (sysinfo-backed fallback)."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size


def _resolve_scan_memory_budget(
    explicit: int, ratio: float, detected: int | None, concurrency: int = 1
) -> int:
    """Pure resolution: explicit nonzero pins the *total* budget, else ratio of
    detected, else fallback — then divided across the concurrency slots."""
    if explicit > 0:
        total = explicit
    elif detected is None or detected <= 0:
        total = _FALLBACK_MAX_MEMORY_BYTES
    else:
        total = int(detected * ratio)
    return total // max(concurrency, 1)


def detect_scan_memory_budget() -> int:
    """Resolve the per-query ``max_memory_usage`` for heavy scans (see module docstring)."""
    s = get_settings()
    detected = min(
        (v for v in (_cgroup_memory_limit(), _meminfo_total(), _physical_memory_total()) if v),
        default=None,
    )
    return _resolve_scan_memory_budget(
        s.stat_scan_max_memory_bytes,
        s.stat_scan_memory_ratio,
        detected,
        s.stat_scan_concurrency,
    )


def _build_heavy_scan_settings() -> str:
    s = get_settings()
    budget = detect_scan_memory_budget()
    # Spill must engage well before the cap kills the query — a configured
    # threshold at or above the per-query cap would never fire.
    group_by_spill = min(s.stat_scan_external_group_by_bytes, budget // 2)
    sort_spill = min(s.stat_scan_external_sort_bytes, budget // 2)
    return (
        f"SETTINGS max_threads = {s.stat_scan_max_threads}, "
        f"max_bytes_before_external_group_by = {group_by_spill}, "
        # Plain ORDER BY sorts spill at this threshold. Window-function sorts
        # cannot spill at all (see docs/ANOMALY_DETECTION.md) — bound those
        # scans structurally (per source / slim columns) instead.
        f"max_bytes_before_external_sort = {sort_spill}, "
        f"max_memory_usage = {budget}"
    )


HEAVY_SCAN_SETTINGS = _build_heavy_scan_settings()

# Admission gate for heavy detector scans: at most VESTIGO_STAT_SCAN_CONCURRENCY
# run against ClickHouse at once; surplus callers block (threadpool threads,
# so blocking is fine). Every public find_* detector entry point in
# db/anomaly_stats.py acquires this — nested helpers (recommend_*/inventory)
# deliberately do not, so a gated scan can call them without deadlocking.
HEAVY_SCAN_GATE = threading.BoundedSemaphore(get_settings().stat_scan_concurrency)
