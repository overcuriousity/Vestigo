"""Shared ClickHouse scan guardrails.

Single home for the SETTINGS clause every whole-corpus scan (GROUP BY over up
to hundreds of millions of rows) must carry: spill large aggregation states to
disk instead of ballooning RAM, cap the query's memory hard (fail one query,
not the server), and bound thread fan-out so several concurrent scans don't
oversubscribe the box (``VESTIGO_STAT_SCAN_MAX_THREADS`` at its 0 default derives
that width from the cores ClickHouse reports for itself, divided by the gate's
size — unless an operator pinned ``max_threads`` in a ClickHouse profile, which
is that width already; see :func:`detect_scan_max_threads`). The limits are ``VESTIGO_*`` tunables (see
``core/config.py``).

The memory budget is a **total across concurrent scans**, resolved from
``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` when nonzero, else auto-sized to
``VESTIGO_STAT_SCAN_MEMORY_RATIO`` (0.8) of the detected memory — the cgroup limit
when the process runs in a memory-limited container, the machine's physical
RAM otherwise. Each heavy query's ``max_memory_usage`` is budget /
(``VESTIGO_STAT_SCAN_CONCURRENCY`` + 2) — the two extra slots are the foreground
class's share, see :data:`FOREGROUND_SCAN_GATE` — and :data:`HEAVY_SCAN_GATE` (acquired by every
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

The clause is produced by :func:`heavy_scan_settings`, called at query build
time rather than frozen into a module constant at import. That is what lets the
budget be derived from **ClickHouse's own ceiling** instead of from whatever
memory the *app* process happens to see: the app cannot query ClickHouse at
import, so a constant could only ever be sized from local detection. See
:func:`configure_scan_budget`, which the API lifespan calls once the store is
reachable. It also means an admin console edit to a ``stat_scan_*`` value takes
effect on the next query, with no restart.

Local detection remains as the fallback for before the probe has run (and for
the CLI, which never probes) — and as a *cap* on a ceiling ClickHouse could
only derive rather than be given (:func:`scan_memory_ceiling`). On its own it is
exactly the assumption that failed in production: a full-docker stack on a 64 GiB host detected 64 GiB *from the app
container*, authorized 0.8 x 64 / 2 = 25.6 GiB per query (the divisor was the bare
concurrency then; today's N + 2 makes it 12.8, which is no better an idea), and ClickHouse — with
no container limit and no ``max_server_memory_usage`` — was OOM-killed by the
kernel with nothing in its own log (session-186). Asking ClickHouse what it is
allowed to use removes the guess.

:data:`HEAVY_SCAN_GATE` is imported *by value* into every scan module, so
rebinding it here would not reach those bindings — its size is therefore fixed
at import (:data:`_GATE_CONCURRENCY`) and ``VESTIGO_STAT_SCAN_CONCURRENCY`` stays
``restart_required`` in ``core/settings_registry.py``. The per-query divisor
reads that frozen value too, not the live setting: the two halves are one
budget seen from two sides, and a live edit that moved only the divisor would
authorize N full-budget queries against a gate still admitting M of them.

The gate is not detector-only: ``ClickHouseStore.finalize_enrichment_apply``
takes a slot for the enrichment partition rewrite too. It is the same class of
whole-partition query and, before it was gated, it stacked on top of a full set
of admitted detector scans and OOM-killed a 32 GiB host mid-apply.
"""

import contextlib
import logging
import os
import re
import threading
import time
import uuid
import weakref
from collections.abc import Iterator, Mapping, MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vestigo.core.config import get_settings

logger = logging.getLogger(__name__)

# Used when detection is explicitly disabled nowhere but fails (exotic
# platforms) — the pre-auto-detection default from the session-27 incident.
_FALLBACK_MAX_MEMORY_BYTES = 12_000_000_000

# What `stat_scan_max_threads` was before it became auto-sized. Kept as the
# detection fallback for the same reason the memory fallback exists: an exotic
# platform, or a server that will not answer, must still start.
_FALLBACK_MAX_THREADS = 8


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
    explicit: int, ratio: float, detected: int | None, concurrency: int = 1, caches: int = 0
) -> int:
    """Pure resolution: explicit nonzero pins the *total* budget, else ratio of
    what is left of *detected* after the caches that share it, else fallback —
    then divided across the concurrency slots.

    Subtracting the caches first is what makes ``stat_scan_memory_ratio``'s own
    description true: it has always said the remainder is headroom for merges
    and caches, while being taken of the whole ceiling with the caches counted
    nowhere. Lowering the default ratio instead would silently shrink every
    existing deployment's budget without saying why.
    """
    available = (detected or 0) - max(caches, 0)
    if explicit > 0:
        total = explicit
    elif available <= 0:
        # Nothing detected, or caches alone at or over the ceiling. Nothing here
        # can fix that configuration, so fall back to the conservative constant
        # rather than to zero (which would fail every scan) or to a negative cap
        # (which ClickHouse reads as "unlimited").
        total = _FALLBACK_MAX_MEMORY_BYTES
        if detected and detected > 0:
            # ...but never *above* what the ceiling alone would have allowed.
            # The fallback constant is unrelated to the ceiling we have just
            # found too small, so on a small one it is the larger number: an
            # 8 GiB app container would go from 6.4 GB to 12 GB precisely
            # because its caches were found to be over-committed. Subtracting
            # the caches may only ever lower the budget.
            total = min(total, int(detected * ratio))
    else:
        total = int(available * ratio)
    # Never 0: ClickHouse reads `max_memory_usage = 0` as *unlimited*, so a
    # degenerate ceiling must fail queries loudly rather than remove the cap.
    return max(total // max(concurrency, 1), 1)


#: What ClickHouse reported it is allowed to use, in bytes — set once by
#: :func:`configure_scan_budget` when the store first becomes reachable, and
#: ``None`` until then (and forever in the CLI, which never probes).
_clickhouse_ceiling: int | None = None


def detect_local_memory_total() -> int | None:
    """Smallest credible memory total visible to *this process*.

    Deliberately uncached, though it is consulted per query: two small
    ``/proc`` reads cost nothing against a scan that is about to read the whole
    corpus, and a cache here would hold a stale answer across a settings edit
    for no measurable gain. It is only ever the fallback —
    :data:`_clickhouse_ceiling` is the number that describes the machine the
    queries actually run on.
    """
    return min(
        (v for v in (_cgroup_memory_limit(), _meminfo_total(), _physical_memory_total()) if v),
        default=None,
    )


#: Whether that ceiling is one an operator actually set, as opposed to one
#: ClickHouse derived from RAM it does not own. See
#: :func:`resolve_clickhouse_ceiling`.
_clickhouse_bounded: bool = False

#: The MergeTree caches ClickHouse reported, which live *under* the ceiling
#: above and are therefore not available to scans. Zero until the probe runs
#: (and forever in the CLI), which makes the pre-probe budget exactly what
#: every release before this computed.
_clickhouse_cache_bytes: int = 0
_clickhouse_cache_breakdown: dict[str, int] = {}

#: The cache settings a MergeTree scan workload actually fills. ClickHouse 26.6
#: ships a dozen more (``text_index_*``, ``vector_similarity_*``,
#: ``parquet_metadata_*``, ``iceberg_*``, ``unique_key_*``); those belong to
#: engines and index types Vestigo does not use, and counting their maxima would
#: report ``over_budget`` on a stack that never allocates a byte of them.
#:
#: ``uncompressed_cache_size`` and ``index_uncompressed_cache_size`` are left
#: out for exactly the same reason, even though a scan workload *would* fill
#: them if they were live: they are gated on the ``use_uncompressed_cache``
#: query setting, which is off by default. A stock server therefore reports an
#: 8 GiB ``uncompressed_cache_size`` it will never allocate a byte of, and
#: counting it took 8 GiB off the scan budget of every deployment that does not
#: mount our ``memory.xml`` — i.e. every externally managed ClickHouse — and
#: could push it into a false ``over_budget``. The shipped ``memory.xml`` pins
#: both to 0 so this premise cannot be turned over by an upgrade.
_COUNTED_CACHES = (
    "mark_cache_size",
    "index_mark_cache_size",
    "primary_index_cache_size",
)


#: Cores ClickHouse resolved for itself (``system.settings.max_threads``
#: reported as ``auto(N)``, cgroup-quota aware), or ``None`` until the probe
#: runs. Only ever a *core count* — a server-side pin lands in
#: :data:`_clickhouse_pinned_max_threads` instead, because it is not one.
_clickhouse_cores: int | None = None

#: A ``max_threads`` an operator pinned in a ClickHouse profile: a thread limit,
#: already the width they asked scans to run at. ``None`` unless the server
#: reported a plain integer.
_clickhouse_pinned_max_threads: int | None = None


def configure_scan_threads(resolved: int | None, is_auto: bool = True) -> None:
    """Record what the server said about ``max_threads``.

    *is_auto* is what keeps the two apart: ``auto(N)`` is the core count
    ClickHouse resolved for itself, from which the auto width is derived; a
    plain integer is an operator's own thread limit, which is that width
    already. Defaults to True so a caller that has only a core count (and no
    reason to think otherwise) reads as before.
    """
    global _clickhouse_cores, _clickhouse_pinned_max_threads  # noqa: PLW0603
    value = int(resolved) if resolved and resolved > 0 else None
    _clickhouse_cores = value if is_auto else None
    _clickhouse_pinned_max_threads = None if is_auto else value


def detect_scan_max_threads() -> int:
    """``max_threads`` for heavy scans: explicit, else an even share of the cores.

    ``VESTIGO_STAT_SCAN_MAX_THREADS`` at 0 means auto, spelled the same way
    ``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` already spells it. Auto is
    ``cores // _GATE_CONCURRENCY``: the gate admits that many scans at once, so
    an even share is the width at which a *full* gate exactly saturates the box.
    The old constant 8 was wrong in both directions — 40% of a 20-core host, and
    4x oversubscription on a 4-core one (issue #301).

    A ``max_threads`` pinned server-side is honoured as written and *not* divided
    again: it is a thread limit, not a core count, so an operator who pinned 8 on
    a 32-core host would otherwise get scans four threads wide — the opposite of
    what they configured — while the report called 8 a core count.

    Floored at 2, because a whole-corpus GROUP BY running single-threaded is not
    a fallback anybody wants to land on silently, and capped at the core count
    for the degenerate case of a gate wider than the machine.
    """
    explicit = get_settings().stat_scan_max_threads
    if explicit > 0:
        return explicit
    if _clickhouse_pinned_max_threads:
        return _clickhouse_pinned_max_threads
    if not _clickhouse_cores:
        return _FALLBACK_MAX_THREADS
    return max(2, min(_clickhouse_cores, _clickhouse_cores // max(_GATE_CONCURRENCY, 1)))


def resolve_cache_bytes(facts: Mapping[str, float]) -> tuple[int, dict[str, int]]:
    """Cache maxima that share ClickHouse's ceiling with our scans.

    Pure, so the interesting cases are testable without a server. Returns the
    total and the per-setting breakdown, because a number an operator is asked
    to act on should show its arithmetic — the same reason the report already
    separates ``budget_ceiling_bytes`` from ``clickhouse_ceiling_bytes``.

    These are configured *maxima*, not current residency: a cold server has
    allocated none of it. That is the right figure for a guardrail, which has to
    hold once the caches are warm.
    """
    breakdown = {name: int(facts.get(name, 0) or 0) for name in _COUNTED_CACHES if name in facts}
    return sum(breakdown.values()), breakdown


# A cgroup that reports a limit near or above the machine's RAM is not a limit;
# both cgroup v1 (PAGE_COUNTER_MAX) and an unconfigured v2 surface that way.
_UNLIMITED_CGROUP = 1 << 60

_AUTO_THREADS = re.compile(r"^'?(?:auto\((\d+)\)|(\d+))'?$")


def parse_max_threads_setting(value: str | None) -> tuple[int, bool] | None:
    """``system.settings.max_threads`` as ``(value, is_auto)``.

    ClickHouse 26.6 reports it as ``'auto(N)'`` where N is the core count it
    will actually use — cgroup-quota aware, which is the whole reason to ask the
    server rather than count cores locally or count the per-core
    ``OSUserTimeCPU*`` series (host cores, quota-blind). A server-side pin
    reports a plain integer instead, and that is a *thread limit*, not a core
    count: the two are returned distinguishable rather than collapsed, because
    the auto width divides one of them by the gate size and must not divide the
    other (see :func:`detect_scan_max_threads`).

    Returns ``None`` for anything that is not a positive integer, so a future
    server that words this differently degrades to the fallback rather than to
    a nonsense width.
    """
    if not value:
        return None
    match = _AUTO_THREADS.match(str(value).strip())
    if not match:
        return None
    auto = match.group(1)
    resolved = int(auto or match.group(2))
    return (resolved, auto is not None) if resolved > 0 else None


def resolve_clickhouse_ceiling(facts: Mapping[str, float]) -> tuple[int | None, bool]:
    """Turn :py:meth:`ClickHouseStore.server_resource_facts` into ``(ceiling, bounded)``.

    Pure, so the interesting cases are testable without a server. *ceiling* is
    the bytes ClickHouse will actually allow itself; *bounded* says whether
    anyone decided that number on purpose.

    The distinction is the whole point. ClickHouse always has *a* ceiling —
    absent ``max_server_memory_usage`` it is
    ``max_server_memory_usage_to_ram_ratio`` (0.9) times detected RAM. In a
    container with no memory limit, "detected RAM" is the entire host, which
    the server does not own and is not alone on. That derived number is
    therefore a ceiling in name only: it is what let a 64 GiB production host
    run ClickHouse up to ~57.6 GiB alongside three other services until the
    kernel intervened. So it is returned as the ceiling — sizing our budget
    against it still beats guessing — with ``bounded=False``, and
    :func:`scan_budget_report` reports that as ``"unbounded"``.
    """
    cgroup = int(facts.get("cgroup_memory_total", 0) or 0)
    os_total = int(facts.get("os_memory_total", 0) or 0)
    cgroup_is_a_limit = 0 < cgroup < _UNLIMITED_CGROUP and (not os_total or cgroup < os_total)
    detected = cgroup if cgroup_is_a_limit else os_total
    ratio = float(facts.get("max_server_memory_usage_to_ram_ratio", 0.9) or 0.9)
    ratio_cap = int(detected * ratio) if detected > 0 else 0

    explicit = int(facts.get("max_server_memory_usage", 0) or 0)
    if explicit > 0:
        # ClickHouse clamps the absolute setting down to the ratio cap and logs
        # a "lowered to" line when it does, so the pinned number is not always
        # the effective one — the reference stack's own 10 GiB sits ~4% above
        # the 0.8 x 12 GiB its container limit implies. Reporting the pinned
        # value there would make the budget, and `over_budget`, optimistic
        # against a ceiling the server will not actually honour.
        return (min(explicit, ratio_cap) if ratio_cap else explicit), True

    if ratio_cap <= 0:
        return None, False
    return ratio_cap, cgroup_is_a_limit


def configure_scan_budget(
    clickhouse_ceiling_bytes: int | None,
    bounded: bool = False,
    cache_bytes: int = 0,
    cache_breakdown: Mapping[str, int] | None = None,
) -> None:
    """Record what ClickHouse reported about its own ceiling and caches.

    Called once from startup recovery, after the store answers. Passing
    ``None`` (the probe failed) leaves local detection in charge — and
    :func:`scan_budget_report` will say so, because "nobody has a limit" is the
    configuration that OOM-kills the server. ``cache_bytes`` defaults to zero so
    a caller that never probed (the CLI) resolves exactly the budget every
    release before this one did.
    """
    global _clickhouse_ceiling, _clickhouse_bounded  # noqa: PLW0603
    global _clickhouse_cache_bytes, _clickhouse_cache_breakdown  # noqa: PLW0603
    _clickhouse_ceiling = clickhouse_ceiling_bytes if clickhouse_ceiling_bytes else None
    _clickhouse_bounded = bool(bounded) and _clickhouse_ceiling is not None
    _clickhouse_cache_bytes = max(int(cache_bytes or 0), 0)
    _clickhouse_cache_breakdown = dict(cache_breakdown or {})


def scan_memory_ceiling() -> int | None:
    """The ceiling the automatic budget takes its ratio of.

    ClickHouse's own, when it reported one — that is the number describing the
    machine the queries actually run on, and the whole point of the probe. With
    one exception: an *unbounded* ceiling (``bounded=False``: 0.9 x whatever RAM
    a limit-less container detected) is, in this module's own words, a ceiling
    in name only, so it is capped by local detection rather than believed
    outright. Otherwise an app in a 4 GiB container talking to an unlimited
    ClickHouse on a 64 GiB box would authorize ~23 GiB per query on the strength
    of a number :func:`resolve_clickhouse_ceiling` has just classified as
    ``"unbounded"``. Two guesses, so take the lower one; an explicit
    ``max_server_memory_usage`` is a decision, not a guess, and stands as-is.
    """
    local = detect_local_memory_total()
    if not _clickhouse_ceiling:
        return local
    if _clickhouse_bounded or not local:
        return _clickhouse_ceiling
    return min(_clickhouse_ceiling, local)


def _cache_bytes_under(ceiling: int | None) -> int:
    """The cache maxima that share *ceiling*, which is only ever ClickHouse's own.

    :func:`scan_memory_ceiling` can return local detection instead — the *app*
    process's cgroup limit or the machine's RAM — either because the probe never
    ran or because an unbounded ClickHouse ceiling was capped by it. Subtracting
    ClickHouse's cache maxima from the app container's memory total mixes two
    unrelated quantities: an app in a 4 GiB container beside a default-configured
    ClickHouse would go straight past zero into the fallback and end up with
    *more* budget than before the caches were counted at all.
    """
    if _clickhouse_ceiling and ceiling == _clickhouse_ceiling:
        return _clickhouse_cache_bytes
    return 0


def detect_scan_memory_budget() -> int:
    """Resolve the per-query ``max_memory_usage`` for heavy scans (see module docstring)."""
    s = get_settings()
    ceiling = scan_memory_ceiling()
    return _resolve_scan_memory_budget(
        s.stat_scan_max_memory_bytes,
        s.stat_scan_memory_ratio,
        ceiling,
        # Deliberately the gate's own size *plus two*, not the live setting.
        # The two extra slots are the foreground class's share (see
        # :data:`FOREGROUND_SCAN_GATE`): N heavy scans plus four foreground
        # charts at half a slot each is exactly the total, so both gates
        # fully admitted still fit. Two slots rather than one because a chart
        # over a high-cardinality field is the ordinary case, not the corner
        # one, and a quarter-slot cap made the ordinary case spill on every
        # render. The divisor and the semaphore describe one
        # budget from two sides, and this is the only value both can agree on:
        # :data:`HEAVY_SCAN_GATE` is sized at import and imported by value,
        # while an admin-console edit to `stat_scan_concurrency` lands on the
        # next `get_settings()`. Reading the live value here would let a
        # 4 -> 2 edit double every query's cap while the gate still admitted
        # four — 2x the total budget, which is precisely the OOM the pair
        # exists to prevent. `restart_required=True` on the spec is what makes
        # the frozen value honest, not enforcement.
        #
        # `_FOREGROUND_SLOTS` rather than a literal 2: this divisor and the
        # foreground gate's size are one reservation described from two
        # sides, and a literal lets an edit to one silently break the
        # identity `scan_budget_report` goes on to report as fact.
        _GATE_CONCURRENCY + _FOREGROUND_SLOTS,
        _cache_bytes_under(ceiling),
    )


def detect_foreground_memory_budget() -> int:
    """Per-query ``max_memory_usage`` for a foreground (chart) scan.

    Two heavy slots' worth, divided across the foreground gate — so charts
    have their own lane without adding to the total the heavy class already
    accounts for (the heavy divisor reserves exactly those two slots). Half a
    heavy cap per chart rather than a quarter: charts over high-cardinality
    fields are the ordinary workload here, and every one of them then spills
    at half the size a detector does rather than a sixth. Never 0, for the
    same reason as the heavy cap.
    """
    return max(detect_scan_memory_budget() * _FOREGROUND_SLOTS // _FOREGROUND_CONCURRENCY, 1)


def detect_foreground_max_threads() -> int:
    """``max_threads`` for a foreground (chart) scan.

    Two heavy slots' worth of threads, divided across the foreground gate —
    the same arithmetic :func:`detect_foreground_memory_budget` uses, but
    **not** the same guarantee, and the difference is deliberate. The memory
    path is a true reservation: :func:`detect_scan_memory_budget` divides the
    total by ``_GATE_CONCURRENCY + _FOREGROUND_SLOTS``, so the chart lane's
    share comes out of the detectors' own and the two lanes together never
    exceed the budget. :func:`detect_scan_max_threads` divides the cores by
    ``_GATE_CONCURRENCY`` alone, so the heavy width is unchanged by this lane
    existing and these threads are *added* to a box a full heavy gate already
    saturates. Every slot busy at once is therefore up to 2x the core count:
    on a 20-core host at the default concurrency, ``2 x 10 + 4 x 5 = 40``.

    That bound is accepted rather than closed. It is a quarter of the 8x this
    replaced — a full gate of four charts, each fanning out two queries at the
    *heavy* width, was 100 threads on 20 cores (issue #301) — it needs all six
    slots occupied to appear at all, and closing it would mean dividing the
    heavy width by ``N + 2`` as well, halving every detector sweep on a box
    where nobody has opened a chart. An operator who does run charts
    continuously alongside sweeps can pin ``VESTIGO_STAT_SCAN_MAX_THREADS`` to
    ``cores // (N + 2)`` and get the strict version.

    Floored at 2 for the same reason :func:`detect_scan_max_threads` is: a
    single-threaded GROUP BY is not a fallback to land on silently. A gate
    wider than twice the heavy width therefore *can* exceed even that bound on
    a very small box, where the floor matters more than the arithmetic.

    Not divided by the fan-out width the way the memory cap is
    (:func:`scan_fanout`): a fan-out of two at half the heavy width is one
    heavy slot's threads, which is inside the two-slot allowance already, and
    dividing again would put the ordinary two-wave chart at the floor.
    """
    return max(2, detect_scan_max_threads() * _FOREGROUND_SLOTS // _FOREGROUND_CONCURRENCY)


def scan_budget_report() -> dict[str, Any]:
    """What the budget resolved to and where each number came from.

    Exists so the resolution is *inspectable* rather than inferred from query
    failures. The `risk` field is the one an operator acts on:

    - ``"unbounded"`` — ClickHouse reported no ceiling of its own. Nothing
      bounds its merges, caches or allocator slack, and the kernel is the only
      backstop. This is the session-186 production configuration.
    - ``"over_budget"`` — our scans *plus the caches under the same ceiling* are
      authorized more than ClickHouse is allowed to use in total, so admitting a
      full set of scans can only end in its own memory error or a kill. The
      caches are counted because they are configured maxima under that ceiling:
      ``memory.xml``'s own comment says so, and the check did not.
    - ``"ok"`` — the total scan budget fits under ClickHouse's ceiling with
      headroom left for what the per-query cap cannot cover.
    """
    s = get_settings()
    per_query = detect_scan_memory_budget()
    foreground = detect_foreground_memory_budget()
    # Both classes fully admitted: N heavy slots plus the two slots the
    # foreground gate shares.
    total = per_query * (_GATE_CONCURRENCY + _FOREGROUND_SLOTS)
    ceiling = scan_memory_ceiling()
    committed = total + _clickhouse_cache_bytes
    if _clickhouse_ceiling is None or not _clickhouse_bounded:
        risk = "unbounded"
    elif committed > _clickhouse_ceiling:
        risk = "over_budget"
    else:
        risk = "ok"
    return {
        "risk": risk,
        "per_query_bytes": per_query,
        "total_bytes": total,
        # Cache maxima that sit under the same ceiling and are therefore not
        # available to scans. Reported with their breakdown so the comparison
        # is inspectable rather than implied: an operator told "over_budget"
        # needs to see which number to move.
        "cache_bytes": _clickhouse_cache_bytes,
        "cache_breakdown": dict(_clickhouse_cache_breakdown),
        # What is left under the ceiling once scans and caches are committed.
        # This is the merge and allocator-slack headroom, which nothing else
        # bounds — deliberately reported rather than reserved as a fraction,
        # which would be a second guess stacked on the first.
        "headroom_bytes": (_clickhouse_ceiling - committed) if _clickhouse_ceiling else None,
        "clickhouse_ceiling_bytes": _clickhouse_ceiling,
        "clickhouse_ceiling_is_explicit": _clickhouse_bounded,
        "local_detected_bytes": detect_local_memory_total(),
        # What the ratio was actually applied to — which is not
        # `clickhouse_ceiling_bytes` when an unbounded ceiling was capped by
        # local detection (see `scan_memory_ceiling`).
        "budget_ceiling_bytes": ceiling,
        "source": (
            "pinned"
            if s.stat_scan_max_memory_bytes > 0
            else (
                "clickhouse" if _clickhouse_ceiling and ceiling == _clickhouse_ceiling else "local"
            )
        ),
        # The gate's size, i.e. the number the budget was actually divided by.
        # A pending `stat_scan_concurrency` edit takes effect on restart, so
        # reporting the live setting here would describe a budget nothing is
        # using yet.
        "concurrency": _GATE_CONCURRENCY,
        "pending_concurrency": (
            s.stat_scan_concurrency if s.stat_scan_concurrency != _GATE_CONCURRENCY else None
        ),
        # The foreground class (charts): its own gate, fed by the two slots
        # the heavy divisor reserves. Disclosed so "why is my chart capped at
        # X" has an answer on the same page as the heavy cap.
        # `max_threads` is disclosed here too, and not only for the heavy
        # class: an operator asking "why is my chart capped at X" is as
        # likely to mean CPU — and this is the one number that shows the
        # chart lane's threads are a bound on top of the heavy width rather
        # than a slice out of it (see `detect_foreground_max_threads`).
        "foreground": {
            "concurrency": _FOREGROUND_CONCURRENCY,
            "per_query_bytes": foreground,
            "max_threads": detect_foreground_max_threads(),
        },
        # Thread width and its provenance. A wrong width has no symptom except
        # "everything is slow", which is the same reason the memory resolution
        # is reported here rather than left to be inferred from a failure.
        "max_threads": detect_scan_max_threads(),
        "max_threads_source": (
            "pinned"
            if s.stat_scan_max_threads > 0
            else (
                "clickhouse_pinned"
                if _clickhouse_pinned_max_threads
                else ("clickhouse" if _clickhouse_cores else "fallback")
            )
        ),
        # Cores, and only ever cores. A `max_threads` pinned in a ClickHouse
        # profile is reported as `clickhouse_pinned` above rather than here,
        # because calling a thread limit a core count is how the width came to
        # be divided by the gate twice.
        "detected_cores": _clickhouse_cores,
    }


# ── Scan context: tagging and cancellation ───────────────────────────────────


@dataclass
class ScanContext:
    """One request-driven scan: the tag its queries carry and its cancel flag.

    Bound by :func:`bind_scan_context` (the API's ``run_scan`` does this per
    request) and read by the settings-clause builders and
    :func:`acquire_scan_slot`. Background jobs and the CLI never bind one, so
    their scans are untagged and uncancellable — exactly as before.
    """

    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancelled: threading.Event = field(default_factory=threading.Event)


_scan_context_var: ContextVar[ScanContext | None] = ContextVar("vestigo_scan_context", default=None)


def scan_context() -> ScanContext | None:
    return _scan_context_var.get()


@contextlib.contextmanager
def bind_scan_context() -> Iterator[ScanContext]:
    """Bind a fresh :class:`ScanContext` for the duration of the block.

    contextvars propagate into ``starlette.concurrency.run_in_threadpool``,
    so a context bound in an endpoint is visible to the scan running in the
    threadpool — which is what lets the clause builders tag the query.
    """
    ctx = ScanContext()
    reset = _scan_context_var.set(ctx)
    try:
        yield ctx
    finally:
        _scan_context_var.reset(reset)


def scan_log_comment(token: str) -> str:
    """The ``log_comment`` value a tagged scan carries; the key ``KILL QUERY`` uses."""
    return f"vestigo-scan/{token}"


class ScanBusy(RuntimeError):
    """A bounded wait for a gate slot expired. ``ahead`` is the queue depth when it did."""

    def __init__(self, *, ahead: int, wait: float) -> None:
        self.ahead = ahead
        self.wait = wait
        super().__init__(f"scan lane busy: {ahead} waiting ahead after {wait:g}s — retry shortly")


class ScanCancelled(RuntimeError):
    """The request that started this scan went away while it waited or ran."""


#: How often a parked acquire re-checks its cancel flag. Module-level so tests
#: can shorten it; one second is invisible to a human and cheap for a thread.
_ACQUIRE_POLL_SECONDS = 1.0

_waiting_lock = threading.Lock()
# Keyed by the gate *object*, weakly: keying by ``id()`` leaks one entry per
# gate ever passed in and — because CPython reuses an id once its object is
# collected — hands a later gate the dead one's count, which is the number an
# analyst is shown as "waiting behind N scans". A test that swaps
# FOREGROUND_SCAN_GATE for a throwaway does exactly that.
_waiting: MutableMapping[Any, int] = weakref.WeakKeyDictionary()


def _waiting_count(gate: threading.BoundedSemaphore) -> int:
    with _waiting_lock:
        return _waiting.get(gate, 0)


def _adjust_waiting(gate: threading.BoundedSemaphore, delta: int) -> None:
    with _waiting_lock:
        _waiting[gate] = max(_waiting.get(gate, 0) + delta, 0)


@contextlib.contextmanager
def acquire_scan_slot(gate: threading.BoundedSemaphore, *, wait: float | None) -> Iterator[None]:
    """Hold one slot of *gate* for the block, cancellably and optionally bounded.

    Polls ``gate.acquire(timeout=_ACQUIRE_POLL_SECONDS)`` so a parked caller
    notices the bound context's ``cancelled`` flag within a poll interval —
    a plain ``acquire()`` would block until a slot came free, which on a busy
    host is minutes after the client left. ``wait=None`` waits indefinitely
    (heavy class); a float raises :class:`ScanBusy` once that many seconds
    pass without a slot (foreground class). Without a bound context there is
    nothing to cancel and the loop is just a slow-motion ``acquire()``.
    """
    ctx = scan_context()
    _adjust_waiting(gate, +1)
    deadline = None if wait is None else time.monotonic() + wait
    try:
        while True:
            if ctx is not None and ctx.cancelled.is_set():
                raise ScanCancelled("cancelled while waiting for a scan slot")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                # Counted *here*, not at entry: the number goes on to the
                # analyst's screen as "waiting behind N scans", and an entry
                # sample is up to `wait` seconds stale by the time it is
                # rendered — long enough for the queue it describes to have
                # drained entirely. Minus this caller's own +1.
                raise ScanBusy(ahead=max(_waiting_count(gate) - 1, 0), wait=wait or 0.0)
            timeout = (
                _ACQUIRE_POLL_SECONDS
                if remaining is None
                else min(_ACQUIRE_POLL_SECONDS, remaining)
            )
            if gate.acquire(timeout=timeout):
                break
    finally:
        _adjust_waiting(gate, -1)
    try:
        yield
    finally:
        gate.release()


def kill_scan_queries(client: Any, token: str) -> None:
    """Best-effort ``KILL QUERY`` for every ClickHouse query tagged with *token*.

    ``ASYNC`` so the call returns at once; the running thread then fails with
    ``QUERY_WAS_CANCELLED`` (code 394) and releases its slot. A failure here
    is logged and swallowed: the scan simply finishes on its own, as it did
    before cancellation existed.
    """
    try:
        client.command(
            "KILL QUERY WHERE Settings['log_comment'] = {tag:String} ASYNC",
            parameters={"tag": scan_log_comment(token)},
        )
    except Exception:  # noqa: BLE001 — best effort by design
        logger.warning(
            "KILL QUERY for scan %s failed; it will finish on its own", token, exc_info=True
        )


#: Number of queries a single gate slot currently has in flight. One by
#: default; a fan-out declares its width with :func:`scan_fanout`.
_scan_fanout_var: ContextVar[int] = ContextVar("vestigo_scan_fanout", default=1)


@contextlib.contextmanager
def scan_fanout(width: int) -> Iterator[None]:
    """Declare that *width* queries run concurrently under the caller's one slot.

    The per-query cap a gate slot authorizes is sized **per slot**, not per
    query: the heavy divisor reserves ``_FOREGROUND_SLOTS`` slots for the
    whole chart lane, and `detect_foreground_memory_budget` splits those
    across the foreground gate. A caller that fans out therefore has to split
    its own share rather than issue each query at the full cap — otherwise a
    fully admitted lane commits ``fanout x`` the budget that was reserved for
    it, which is exactly the over-commit the pair of gates exists to prevent.
    The factor is the fan-out width and nothing else, so no gate sizing can
    absorb it.

    Multiplies rather than replaces, so a nested fan-out (the violin chart's
    second wave under its first) divides by the product. contextvars are
    copied into the worker threads by ``copy_context().run``, so the width
    set here reaches the clause builder inside each one.
    """
    token = _scan_fanout_var.set(max(_scan_fanout_var.get() * max(width, 1), 1))
    try:
        yield
    finally:
        _scan_fanout_var.reset(token)


#: Set while a scan runs for something with nobody watching a spinner.
_foreground_unbounded_var: ContextVar[bool] = ContextVar(
    "vestigo_foreground_unbounded", default=False
)


@contextlib.contextmanager
def unbounded_foreground_wait() -> Iterator[None]:
    """Queue for a foreground slot indefinitely instead of raising :class:`ScanBusy`.

    The bounded wait exists so a chart an analyst is *looking at* says "busy"
    rather than spins. A background job — a story export rendering its chart
    blocks — has no spinner, no request to answer 503 to and no retry: for it
    the bound turns a slow answer into a failed one. Such a caller wraps its
    work in this and gets the pre-#300 behavior, queueing behind the lane
    until a slot comes free.
    """
    token = _foreground_unbounded_var.set(True)
    try:
        yield
    finally:
        _foreground_unbounded_var.reset(token)


def foreground_wait_seconds(bounded: float) -> float | None:
    """*bounded*, or ``None`` under :func:`unbounded_foreground_wait`."""
    return None if _foreground_unbounded_var.get() else bounded


def _scan_settings_clause(budget: int, threads: int) -> str:
    s = get_settings()
    # A gate slot's cap is per *slot*: a caller that fans out splits its own
    # share across the queries it has in flight, rather than issuing each at
    # the full cap. See `scan_fanout`. Never 0, for the same reason the caps
    # themselves never are.
    budget = max(budget // max(_scan_fanout_var.get(), 1), 1)
    # Spill must engage well before the cap kills the query — a configured
    # threshold at or above the per-query cap would never fire.
    group_by_spill = min(s.stat_scan_external_group_by_bytes, budget // 2)
    sort_spill = min(s.stat_scan_external_sort_bytes, budget // 2)
    # A request-driven scan is tagged so a disconnect can KILL it by this
    # value (see kill_scan_queries). The token is a uuid hex — nothing to quote.
    ctx = scan_context()
    tag = f", log_comment = '{scan_log_comment(ctx.token)}'" if ctx is not None else ""
    return (
        f"SETTINGS max_threads = {threads}, "
        f"max_bytes_before_external_group_by = {group_by_spill}, "
        # Plain ORDER BY sorts spill at this threshold. Window-function sorts
        # cannot spill at all (see docs/ANOMALY_DETECTION.md) — bound those
        # scans structurally (per source / slim columns) instead.
        f"max_bytes_before_external_sort = {sort_spill}, "
        f"max_memory_usage = {budget}{tag}"
    )


def heavy_scan_settings() -> str:
    """The SETTINGS clause every whole-corpus scan must carry.

    Built per call rather than frozen at import — see the module docstring.
    Cheap: a few f-string formats over cached inputs, against a query that is
    about to read the whole corpus.
    """
    return _scan_settings_clause(detect_scan_memory_budget(), detect_scan_max_threads())


def foreground_scan_settings() -> str:
    """The SETTINGS clause for an interactive chart aggregation.

    Same shape as :func:`heavy_scan_settings` with the foreground cap *and*
    the foreground thread width — both two heavy slots split across the gate,
    applied to the two resources a slot commits. The memory half is a true
    reservation, taken out of the heavy class's share; the thread half is a
    bound on top of it. See :func:`detect_foreground_max_threads` for why the
    width cannot simply be the heavy one, and why the asymmetry stands.
    """
    return _scan_settings_clause(detect_foreground_memory_budget(), detect_foreground_max_threads())


# Admission gate for heavy detector scans: at most VESTIGO_STAT_SCAN_CONCURRENCY
# run against ClickHouse at once; surplus callers block (threadpool threads,
# so blocking is fine). Every public find_* detector entry point in
# db/anomaly_stats.py acquires this — nested helpers (recommend_*/inventory)
# deliberately do not, so a gated scan can call them without deadlocking.
# Frozen at import and shared with `detect_scan_memory_budget`, which divides
# the total budget by exactly this number — see the comment at that call.
_GATE_CONCURRENCY = max(get_settings().stat_scan_concurrency, 1)
HEAVY_SCAN_GATE = threading.BoundedSemaphore(_GATE_CONCURRENCY)

# Admission gate for *foreground* scans: the chart aggregations an analyst is
# looking at while they wait (histogram, top terms, numeric stats, …). Its own
# lane so a 60-bucket GROUP BY never queues behind a whole-corpus detector
# sweep (issue #300). Sized as two heavy slots split four ways — in memory
# (detect_foreground_memory_budget) and in threads
# (detect_foreground_max_threads) alike — so it adds nothing to the total the
# heavy divisor already reserves, on either resource. Constants rather than settings: how finely the
# reserved slots are sliced is not a number an operator needs to reason about.
_FOREGROUND_SLOTS = 2
_FOREGROUND_CONCURRENCY = 4
FOREGROUND_SCAN_GATE = threading.BoundedSemaphore(_FOREGROUND_CONCURRENCY)

# Admission gate for *streamed* heavy scans (the value-inventory export).
# Deliberately separate from HEAVY_SCAN_GATE and deliberately one slot.
#
# A streamed scan is two phases with opposite cost profiles. The aggregation
# is the whole-corpus GROUP BY this module exists to admit, and it holds
# HEAVY_SCAN_GATE like any detector. The drain that follows is paced by the
# analyst's *browser*, not by ClickHouse — and a backgrounded or bandwidth-
# starved download can hold its slot for minutes. Every other holder of
# HEAVY_SCAN_GATE is bounded by query time; letting a client-paced one keep a
# detector slot is how one slow download starves every sweep on the box. So
# the export releases HEAVY_SCAN_GATE the moment rows start flowing (see
# `EventQueryService.iter_field_inventory`) and holds this instead.
#
# One slot, so two exports queue rather than stacking two live result streams
# against the same ClickHouse memory budget. There is no setting for it: the
# supported answer to "I want more concurrent exports" is that you don't.
EXPORT_SCAN_GATE = threading.BoundedSemaphore(1)
