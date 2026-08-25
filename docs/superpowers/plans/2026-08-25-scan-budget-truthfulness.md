# Scan-Budget Truthfulness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every number Vestigo reports about its ClickHouse scan budget true — count the
caches that share the ceiling, derive thread width from the cores ClickHouse actually has, and
stop the airgap installer from shipping a ClickHouse with no ceiling at all.

**Architecture:** One startup probe already asks ClickHouse what it may use
(`ClickHouseStore.server_memory_facts` → `db/_scan.py::configure_scan_budget`). This round widens
that probe to also read the MergeTree cache sizes and the server's own resolved core count,
subtracts the caches before applying `stat_scan_memory_ratio`, derives `max_threads` from cores
divided by the admission gate's size, and surfaces the resulting `risk` verdict where an operator
stands when they tune it. Separately, the airgap installer gains the `cp` it never had for
`memory.xml`, plus a loud pre-flight assertion.

**Tech Stack:** Python 3.13 / FastAPI / clickhouse-connect; React 19 + Vite + Vitest; POSIX sh
(`deploy/airgap/install.sh`); pytest against real ClickHouse and PostgreSQL.

**Spec:** `docs/superpowers/specs/2026-08-25-scan-budget-truthfulness.md`

Implements GitHub issues **#301**, **#302**, **#303**. Issue **#300** is explicitly out of scope
for this branch.

## Global Constraints

- ClickHouse target version: **26.6.1** (what the reference stack pins). Every probe query in this
  plan was executed against it; do not "simplify" a query to one the spec says does not exist.
- `system.asynchronous_metrics` carries **no** CPU-count metric on 26.6.1. Core count comes from
  `SELECT value FROM system.settings WHERE name = 'max_threads'`, which reports `'auto(N)'` and is
  cgroup-quota aware (verified: `--cpus=2` → `'auto(2)'`).
- Every probe failure **warns and falls back**; none of them may refuse to start. That is the
  posture `server_memory_facts` already has and the reason it returns `{}` rather than raising.
- `_GATE_CONCURRENCY` is frozen at import and imported by value. Anything derived from it must
  read `_scan._GATE_CONCURRENCY`, never `get_settings().stat_scan_concurrency`.
- Ruff: `line-length = 100`, `E501` ignored. Google-style docstrings. Run `uv run ruff format .`
  **and** `uv run ruff check .` — the first is what CI enforces and the second does not imply it.
- `docs/ANOMALY_DETECTION.md` is the contract for the scan-cost machinery and must be updated in
  the **same commit** as any change to it (CLAUDE.md rule).
- Tests need the backing services: `podman compose up -d` before `uv run pytest`.
- Frontend: `oxlint` via `npm run lint`, types via `npm run typecheck`, tests via `npm run test`.

---

### Task 1: Widen the startup probe to cache sizes and resolved core count

**Files:**
- Modify: `src/vestigo/db/clickhouse.py:691-733` (rename and extend `server_memory_facts`)
- Modify: `src/vestigo/api/main.py:435` (the one call site)
- Modify: `src/vestigo/db/_scan.py:178` (docstring reference to the renamed method)
- Create: `tests/test_scan_facts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ClickHouseStore.server_resource_facts() -> dict[str, float]` — replaces
    `server_memory_facts`. Same four memory keys as before, plus `mark_cache_size`,
    `uncompressed_cache_size`, `index_mark_cache_size`, `index_uncompressed_cache_size`,
    `primary_index_cache_size`, and `resolved_max_threads`.
  - `vestigo.db._scan.parse_resolved_max_threads(value: str | None) -> int | None` — pure.

- [ ] **Step 1: Write the failing test for the `auto(N)` parser**

Create `tests/test_scan_facts.py`:

```python
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
        ("'auto(12)'", 12),   # what the HTTP interface returns, quotes included
        ("auto(12)", 12),     # what the native client returns
        ("auto(2)", 2),       # verified under `--cpus=2`
        ("16", 16),           # an operator pinned it server-side
        ("'16'", 16),
        ("auto(0)", None),    # nonsense N is not a core count
        ("0", None),
        ("auto()", None),
        ("", None),
        (None, None),
        ("auto(abc)", None),
    ],
)
def test_parse_resolved_max_threads(raw, expected):
    assert parse_resolved_max_threads(raw) == expected
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_scan_facts.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_resolved_max_threads'`

- [ ] **Step 3: Implement the parser in `db/_scan.py`**

Add near `resolve_clickhouse_ceiling` (after the `_UNLIMITED_CGROUP` constant):

```python
_AUTO_THREADS = re.compile(r"^'?(?:auto\((\d+)\)|(\d+))'?$")


def parse_resolved_max_threads(value: str | None) -> int | None:
    """The core count ClickHouse resolved for itself, from `system.settings`.

    ClickHouse 26.6 reports `max_threads` as ``'auto(N)'`` where N is the core
    count it will actually use — cgroup-quota aware, which is the whole reason
    to ask the server rather than count cores locally or count the per-core
    ``OSUserTimeCPU*`` series (host cores, quota-blind). A server-side pin
    reports a plain integer instead, which is equally the resolved value.

    Returns ``None`` for anything that is not a positive integer, so a future
    server that words this differently degrades to the fallback rather than to
    a nonsense width.
    """
    if not value:
        return None
    match = _AUTO_THREADS.match(str(value).strip())
    if not match:
        return None
    resolved = int(match.group(1) or match.group(2))
    return resolved if resolved > 0 else None
```

Add `import re` to the module's imports (it currently imports `os`, `threading`).

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_scan_facts.py -v`
Expected: PASS (11 parametrized cases)

- [ ] **Step 5: Extend and rename the probe**

In `src/vestigo/db/clickhouse.py`, replace the `server_memory_facts` definition
(`:691-733`) with:

```python
    def server_resource_facts(self) -> dict[str, float]:
        """What ClickHouse reports about its own ceiling, caches and core count.

        The app cannot infer any of it: its own container's view of RAM says
        nothing about what the ClickHouse container may use, and that gap is
        what OOM-killed a production server (see ``db/_scan.py``).

        Three groups, all optional — interpretation is
        :func:`vestigo.db._scan.resolve_clickhouse_ceiling`,
        :func:`vestigo.db._scan.resolve_cache_bytes` and
        :func:`vestigo.db._scan.parse_resolved_max_threads`' job:

        - the ceiling: ``max_server_memory_usage``,
          ``max_server_memory_usage_to_ram_ratio``, ``cgroup_memory_total``,
          ``os_memory_total``;
        - the caches that live *under* that ceiling and are filled by a
          MergeTree scan workload: ``mark_cache_size``,
          ``uncompressed_cache_size``, ``index_mark_cache_size``,
          ``index_uncompressed_cache_size``, ``primary_index_cache_size``.
          The server ships many more (``text_index_*``, ``vector_similarity_*``,
          ``parquet_metadata_*``, ``iceberg_*``, ``unique_key_*``) for engines
          and index types Vestigo does not use; counting those would make the
          budget permanently and wrongly read as `over_budget`;
        - ``resolved_max_threads``: the core count the server resolved for
          itself. ``system.asynchronous_metrics`` has no CPU-count metric on
          26.6, so this comes from ``system.settings`` — see
          :func:`vestigo.db._scan.parse_resolved_max_threads`.

        An empty dict means the probe failed — an old server, or a user without
        rights on the ``system`` tables — which is a reason to warn, never to
        refuse to start.
        """
        facts: dict[str, float] = {}
        try:
            rows = self.client.query(
                "SELECT name, value FROM system.server_settings WHERE name IN "
                "('max_server_memory_usage', 'max_server_memory_usage_to_ram_ratio', "
                "'mark_cache_size', 'uncompressed_cache_size', 'index_mark_cache_size', "
                "'index_uncompressed_cache_size', 'primary_index_cache_size')"
            ).result_rows
            for name, value in rows:
                # Float throughout: the ratio is 0.9, and reading it as an int
                # truncates it to 0 — which would read as "no limit at all" and
                # invert the very conclusion this probe exists to draw.
                with contextlib.suppress(TypeError, ValueError):
                    facts[name] = float(value)
            metrics = self.client.query(
                "SELECT metric, value FROM system.asynchronous_metrics WHERE metric IN "
                "('CGroupMemoryTotal', 'OSMemoryTotal')"
            ).result_rows
            for metric, value in metrics:
                with contextlib.suppress(TypeError, ValueError):
                    facts[
                        "cgroup_memory_total"
                        if metric == "CGroupMemoryTotal"
                        else "os_memory_total"
                    ] = float(value)
            # Separate query, and separate failure: a server that renames this
            # setting must still yield the memory facts above, which are the
            # ones an OOM depends on.
            with contextlib.suppress(Exception):
                threads = self.client.query(
                    "SELECT value FROM system.settings WHERE name = 'max_threads'"
                ).result_rows
                if threads:
                    from vestigo.db._scan import parse_resolved_max_threads

                    resolved = parse_resolved_max_threads(threads[0][0])
                    if resolved:
                        facts["resolved_max_threads"] = float(resolved)
        except Exception:  # noqa: BLE001 — a failed probe must not block startup
            logger.warning("could not read ClickHouse resource limits", exc_info=True)
            return {}
        return facts
```

- [ ] **Step 6: Update the call site and the stale docstring reference**

In `src/vestigo/api/main.py:435`:

```python
    facts = await asyncio.to_thread(ClickHouseStore().server_resource_facts)
```

In `src/vestigo/db/_scan.py:178`, change the first docstring line of
`resolve_clickhouse_ceiling` to reference the new name:

```python
    """Turn :py:meth:`ClickHouseStore.server_resource_facts` into ``(ceiling, bounded)``.
```

- [ ] **Step 7: Add an integration test that the probe answers on the real server**

Append to `tests/test_scan_facts.py`:

```python
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
```

- [ ] **Step 8: Run the suite for this area**

Run: `uv run pytest tests/test_scan_facts.py tests/test_scan_budget.py -v`
Expected: PASS

- [ ] **Step 9: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check .
git add src/vestigo/db/clickhouse.py src/vestigo/db/_scan.py src/vestigo/api/main.py tests/test_scan_facts.py
git commit -m "feat(scan): probe ClickHouse for its cache sizes and resolved core count

server_memory_facts becomes server_resource_facts: the same ceiling facts plus
the five MergeTree caches that live under that ceiling and the core count the
server resolved for itself. ClickHouse 26.6 exposes no CPU-count metric in
system.asynchronous_metrics, so the core count comes from system.settings'
max_threads ('auto(N)'), which is cgroup-quota aware.

Refs #301, #302"
```

---

### Task 2: Count the caches in the budget and in the risk verdict

**Files:**
- Modify: `src/vestigo/db/_scan.py` (module docstring, `configure_scan_budget`,
  `detect_scan_memory_budget`, `scan_budget_report`; add `resolve_cache_bytes`)
- Modify: `src/vestigo/api/main.py:437-470` (pass cache bytes; log them)
- Modify: `src/vestigo/core/settings_registry.py:612-618` (`stat_scan_memory_ratio` help)
- Modify: `src/vestigo/core/config.py:219` (comment on `stat_scan_memory_ratio`)
- Test: `tests/test_scan_budget.py`

**Interfaces:**
- Consumes: `ClickHouseStore.server_resource_facts()` from Task 1.
- Produces:
  - `vestigo.db._scan.resolve_cache_bytes(facts: Mapping[str, float]) -> tuple[int, dict[str, int]]`
    — pure; returns `(total_bytes, breakdown)`.
  - `configure_scan_budget(clickhouse_ceiling_bytes, bounded=False, cache_bytes=0, cache_breakdown=None)`.
  - `scan_budget_report()` gains `cache_bytes`, `cache_breakdown`, `headroom_bytes`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scan_budget.py`:

```python
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
    the exact condition Task 6 fixes in memory.xml, and one an operator can
    recreate at any time. The budget floors at the conservative fallback rather
    than going negative or to zero."""
    assert _scan._resolve_scan_memory_budget(
        0, 0.8, 9 << 30, concurrency=2, caches=12 << 30
    ) == _FALLBACK_MAX_MEMORY_BYTES // 2


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
```

Also extend the autouse `_no_probed_ceiling` fixture (`tests/test_scan_budget.py:20-34`) so the
new module state is saved and restored too:

```python
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
    _scan.configure_scan_budget(saved[0], bounded=saved[1], cache_bytes=saved[2],
                                cache_breakdown=saved[3])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scan_budget.py -v -k "cache or after_caches or negative"`
Expected: FAIL — `AttributeError: module 'vestigo.db._scan' has no attribute 'resolve_cache_bytes'`

- [ ] **Step 3: Implement in `db/_scan.py`**

Add the module state beside `_clickhouse_bounded`:

```python
#: The MergeTree caches ClickHouse reported, which live *under* the ceiling
#: above and are therefore not available to scans. Zero until the probe runs
#: (and forever in the CLI), which makes the pre-probe budget exactly what
#: every release before this computed.
_clickhouse_cache_bytes: int = 0
_clickhouse_cache_breakdown: dict[str, int] = {}

#: The cache settings a MergeTree scan workload actually fills. ClickHouse 26.6
#: ships a dozen more (`text_index_*`, `vector_similarity_*`, `parquet_metadata_*`,
#: `iceberg_*`, `unique_key_*`); those belong to engines and index types Vestigo
#: does not use, and counting their maxima would report `over_budget` on a stack
#: that never allocates a byte of them.
_COUNTED_CACHES = (
    "mark_cache_size",
    "uncompressed_cache_size",
    "index_mark_cache_size",
    "index_uncompressed_cache_size",
    "primary_index_cache_size",
)


def resolve_cache_bytes(facts: Mapping[str, float]) -> tuple[int, dict[str, int]]:
    """Cache maxima that share ClickHouse's ceiling with our scans.

    Pure, so the interesting cases are testable without a server. Returns the
    total and the per-setting breakdown, because a number an operator is asked
    to act on should show its arithmetic — the same reason the report already
    separates `budget_ceiling_bytes` from `clickhouse_ceiling_bytes`.

    These are configured *maxima*, not current residency: a cold server has
    allocated none of it. That is the right figure for a guardrail, which has to
    hold once the caches are warm.
    """
    breakdown = {
        name: int(facts.get(name, 0) or 0)
        for name in _COUNTED_CACHES
        if name in facts
    }
    return sum(breakdown.values()), breakdown
```

Extend `_resolve_scan_memory_budget` (`:131`):

```python
def _resolve_scan_memory_budget(
    explicit: int, ratio: float, detected: int | None, concurrency: int = 1, caches: int = 0
) -> int:
    """Pure resolution: explicit nonzero pins the *total* budget, else ratio of
    what is left of *detected* after the caches that share it, else fallback —
    then divided across the concurrency slots.

    Subtracting the caches first is what makes `stat_scan_memory_ratio`'s own
    description true: it has always said the remainder is headroom for merges
    and caches, while being taken of the whole ceiling with the caches counted
    nowhere. Lowering the default ratio instead would silently shrink every
    existing deployment's budget without saying why.
    """
    if explicit > 0:
        total = explicit
    else:
        available = (detected or 0) - max(caches, 0)
        if available <= 0:
            # Caches alone at or over the ceiling. Nothing here can fix that
            # configuration, so fall back to the conservative constant rather
            # than to zero (which would fail every scan) or to a negative cap
            # (which ClickHouse reads as "unlimited").
            total = _FALLBACK_MAX_MEMORY_BYTES
        else:
            total = int(available * ratio)
    return total // max(concurrency, 1)
```

Update `configure_scan_budget`:

```python
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
```

Pass the caches through `detect_scan_memory_budget`:

```python
        _GATE_CONCURRENCY,
        _clickhouse_cache_bytes,
    )
```

Rewrite the verdict and the returned mapping in `scan_budget_report`:

```python
    total = per_query * _GATE_CONCURRENCY
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
```

Extend the `scan_budget_report` docstring's `over_budget` bullet:

```python
    - ``"over_budget"`` — our scans *plus the caches under the same ceiling* are
      authorized more than ClickHouse is allowed to use in total, so admitting a
      full set of scans can only end in its own memory error or a kill. The
      caches are counted because they are configured maxima under that ceiling:
      ``memory.xml``'s own comment says so, and the check did not.
```

- [ ] **Step 4: Wire the probe's cache facts through startup**

In `src/vestigo/api/main.py::_probe_scan_budget`, after `resolve_clickhouse_ceiling`:

```python
    from vestigo.db._scan import (
        configure_scan_budget,
        resolve_cache_bytes,
        resolve_clickhouse_ceiling,
        scan_budget_report,
    )
    from vestigo.db.clickhouse import ClickHouseStore

    facts = await asyncio.to_thread(ClickHouseStore().server_resource_facts)
    ceiling, bounded = resolve_clickhouse_ceiling(facts)
    cache_bytes, cache_breakdown = resolve_cache_bytes(facts)
    configure_scan_budget(ceiling, bounded, cache_bytes, cache_breakdown)
```

and extend the `over_budget` branch's message so the log names the new term:

```python
    elif report["risk"] == "over_budget":
        logger.error(
            "Heavy-scan budget (%.1f GiB across %d slot(s)) plus ClickHouse's own caches "
            "(%.1f GiB) exceeds what ClickHouse is allowed to use in total (%.1f GiB). "
            "Admitting a full set of scans can only end in a memory error or an OOM kill. "
            "Lower VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES, shrink the caches in "
            "deploy/clickhouse/memory.xml, or raise max_server_memory_usage.",
            report["total_bytes"] / (1 << 30),
            report["concurrency"],
            report["cache_bytes"] / (1 << 30),
            report["clickhouse_ceiling_bytes"] / (1 << 30),
        )
```

- [ ] **Step 5: Make the setting's description true**

`src/vestigo/core/settings_registry.py:612-618`:

```python
    SettingSpec(
        "stat_scan_memory_ratio",
        "scans",
        "Auto-budget memory ratio",
        "Fraction of what is left of ClickHouse's memory ceiling *after* its own caches "
        "(mark, index-mark, primary-index, uncompressed) that the automatic budget uses. "
        "The remainder is headroom for the one thing no per-query cap can bound: background "
        "merges, plus allocator slack. /api/health reports the cache figure that went into "
        "the subtraction.",
    ),
```

`src/vestigo/core/config.py:219`, replace the one-line comment:

```python
    # Fraction of the ClickHouse ceiling *minus its own caches* that the auto
    # budget uses; the remainder is merge and allocator-slack headroom.
    stat_scan_memory_ratio: float = Field(default=0.8, gt=0, le=1)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_scan_budget.py tests/test_scan_facts.py -v`
Expected: PASS, including the pre-existing `test_report_flags_a_budget_over_the_server_ceiling`
and `test_report_is_ok_when_the_budget_fits` (both use `_report_with`, which leaves
`_clickhouse_cache_bytes` at the fixture's zero, so their arithmetic is unchanged).

- [ ] **Step 7: Update `docs/ANOMALY_DETECTION.md` in this commit**

In §"The budget comes from ClickHouse, not from the app's host" (around `docs/ANOMALY_DETECTION.md:94`),
replace "and takes `VESTIGO_STAT_SCAN_MEMORY_RATIO` of it, reserving the rest for merges and
caches." with:

```markdown
  reads the ceiling ClickHouse runs under (`system.server_settings`, falling back to
  `system.asynchronous_metrics`) **and the cache maxima that live under that ceiling**
  (`mark_cache_size`, `index_mark_cache_size`, `primary_index_cache_size`,
  `uncompressed_cache_size`, `index_uncompressed_cache_size`), subtracts the caches, and
  takes `VESTIGO_STAT_SCAN_MEMORY_RATIO` of what remains — leaving the rest as headroom
  for background merges, which no per-query cap reaches. Counting the caches is not
  optional bookkeeping: at 26.6 defaults `index_mark_cache_size` and
  `primary_index_cache_size` are 5 GiB *each*, so the reference stack's own caches
  exceeded its whole 9.5 GiB ceiling while `/api/health` reported `risk: "ok"`.
```

- [ ] **Step 8: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check .
uv run pytest tests/test_scan_budget.py tests/test_scan_facts.py -q
git add src/vestigo/db/_scan.py src/vestigo/api/main.py src/vestigo/core/settings_registry.py src/vestigo/core/config.py docs/ANOMALY_DETECTION.md tests/test_scan_budget.py
git commit -m "fix(scan): count ClickHouse's own caches against its ceiling

scan_budget_report()'s risk compared scans against the ceiling and ignored the
caches living under it, so it reported 'ok' for a configuration that does not
fit — including the one we ship. The ratio now applies to (ceiling - caches),
which is what stat_scan_memory_ratio's help text always claimed it did, and the
report carries cache_bytes, cache_breakdown and headroom_bytes so the
arithmetic is visible rather than implied.

Fixes #302"
```

---

### Task 3: Derive scan thread width from the cores ClickHouse has

**Files:**
- Modify: `src/vestigo/db/_scan.py` (module docstring, thread state,
  `detect_scan_max_threads`, `heavy_scan_settings`, `scan_budget_report`)
- Modify: `src/vestigo/core/config.py:204` (default 8 → 0)
- Modify: `src/vestigo/core/settings_registry.py:582-587` (help text)
- Modify: `src/vestigo/api/main.py::_probe_scan_budget` (pass the resolved count)
- Test: `tests/test_scan_budget.py`

**Interfaces:**
- Consumes: `facts["resolved_max_threads"]` (Task 1), `configure_scan_budget` (Task 2).
- Produces:
  - `configure_scan_threads(resolved_cores: int | None) -> None`
  - `detect_scan_max_threads() -> int`
  - `scan_budget_report()` gains `max_threads`, `max_threads_source`, `detected_cores`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scan_budget.py`:

```python
# ── Thread width follows the cores ClickHouse actually has ──────────────────


def test_auto_thread_width_is_an_even_share_of_the_cores(monkeypatch):
    """The gate admits `_GATE_CONCURRENCY` scans at once, so an even share is
    the width at which a full gate saturates the box instead of oversubscribing
    it. 8 was a constant: 40% of a 20-core host, and 4x oversubscription on 4.
    """
    monkeypatch.setattr(_scan, "_clickhouse_cores", 20)
    monkeypatch.setattr(_scan, "_GATE_CONCURRENCY", 2)
    monkeypatch.setattr(
        _scan, "get_settings", lambda: _scan.get_settings().model_copy(
            update={"stat_scan_max_threads": 0}
        )
    )

    assert _scan.detect_scan_max_threads() == 10


def test_auto_thread_width_never_drops_below_two(monkeypatch):
    """A single-threaded whole-corpus scan is not a useful floor to land on."""
    monkeypatch.setattr(_scan, "_clickhouse_cores", 2)
    monkeypatch.setattr(_scan, "_GATE_CONCURRENCY", 4)
    monkeypatch.setattr(
        _scan, "get_settings", lambda: _scan.get_settings().model_copy(
            update={"stat_scan_max_threads": 0}
        )
    )

    assert _scan.detect_scan_max_threads() == 2


def test_explicit_thread_width_still_wins(monkeypatch):
    """VESTIGO_STAT_SCAN_MAX_THREADS is a decision, exactly like a pinned budget."""
    monkeypatch.setattr(_scan, "_clickhouse_cores", 20)
    monkeypatch.setattr(
        _scan, "get_settings", lambda: _scan.get_settings().model_copy(
            update={"stat_scan_max_threads": 6}
        )
    )

    assert _scan.detect_scan_max_threads() == 6
    assert "max_threads = 6" in _scan.heavy_scan_settings()


def test_thread_detection_failure_falls_back_to_the_old_constant(monkeypatch):
    """Same posture as the memory probe: warn and carry on, never refuse."""
    monkeypatch.setattr(_scan, "_clickhouse_cores", None)
    monkeypatch.setattr(
        _scan, "get_settings", lambda: _scan.get_settings().model_copy(
            update={"stat_scan_max_threads": 0}
        )
    )

    assert _scan.detect_scan_max_threads() == _scan._FALLBACK_MAX_THREADS == 8


def test_report_discloses_the_thread_width_and_where_it_came_from(monkeypatch):
    """A wrong thread width has no symptom except 'everything is slow', which is
    the same reason the memory resolution is reported."""
    monkeypatch.setattr(_scan, "_clickhouse_cores", 20)
    monkeypatch.setattr(
        _scan, "get_settings", lambda: _scan.get_settings().model_copy(
            update={"stat_scan_max_threads": 0}
        )
    )

    report = _scan.scan_budget_report()

    assert report["detected_cores"] == 20
    assert report["max_threads"] == _scan.detect_scan_max_threads()
    assert report["max_threads_source"] == "clickhouse"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scan_budget.py -v -k thread`
Expected: FAIL — `AttributeError: module 'vestigo.db._scan' has no attribute '_clickhouse_cores'`

- [ ] **Step 3: Implement in `db/_scan.py`**

Beside `_FALLBACK_MAX_MEMORY_BYTES`:

```python
# What `stat_scan_max_threads` was before it became auto-sized. Kept as the
# detection fallback for the same reason the memory fallback exists: an exotic
# platform, or a server that will not answer, must still start.
_FALLBACK_MAX_THREADS = 8
```

Beside the cache state:

```python
#: Cores ClickHouse resolved for itself (`system.settings.max_threads`,
#: cgroup-quota aware), or ``None`` until the probe runs.
_clickhouse_cores: int | None = None


def configure_scan_threads(resolved_cores: int | None) -> None:
    """Record the core count ClickHouse reported, for the auto thread width."""
    global _clickhouse_cores  # noqa: PLW0603
    _clickhouse_cores = int(resolved_cores) if resolved_cores and resolved_cores > 0 else None


def detect_scan_max_threads() -> int:
    """`max_threads` for heavy scans: explicit, else an even share of the cores.

    ``VESTIGO_STAT_SCAN_MAX_THREADS`` at 0 means auto, spelled the same way
    ``VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`` already spells it. Auto is
    ``cores // _GATE_CONCURRENCY``: the gate admits that many scans at once, so
    an even share is the width at which a *full* gate exactly saturates the box.
    The old constant 8 was wrong in both directions — 40% of a 20-core host, and
    4x oversubscription on a 4-core one (issue #301).

    Floored at 2, because a whole-corpus GROUP BY running single-threaded is not
    a fallback anybody wants to land on silently, and capped at the core count
    for the degenerate case of a gate wider than the machine.
    """
    explicit = get_settings().stat_scan_max_threads
    if explicit > 0:
        return explicit
    if not _clickhouse_cores:
        return _FALLBACK_MAX_THREADS
    return max(2, min(_clickhouse_cores, _clickhouse_cores // max(_GATE_CONCURRENCY, 1)))
```

In `heavy_scan_settings`, replace `max_threads = {s.stat_scan_max_threads}` with
`max_threads = {detect_scan_max_threads()}`.

In `scan_budget_report`'s returned mapping, after `"pending_concurrency"`:

```python
        # Thread width and its provenance. A wrong width has no symptom except
        # "everything is slow", which is the same reason the memory resolution
        # is reported here rather than left to be inferred from a failure.
        "max_threads": detect_scan_max_threads(),
        "max_threads_source": (
            "pinned"
            if s.stat_scan_max_threads > 0
            else ("clickhouse" if _clickhouse_cores else "fallback")
        ),
        "detected_cores": _clickhouse_cores,
```

- [ ] **Step 4: Default the setting to auto**

`src/vestigo/core/config.py:204`:

```python
    # 0 = auto: an even share of the cores ClickHouse reports for itself
    # (cores / concurrency, floor 2), read from the server at startup. A
    # nonzero value pins it, exactly as stat_scan_max_memory_bytes pins the
    # budget. It was the constant 8, which is 40% of a 20-core host and 4x
    # oversubscription of a 4-core one.
    stat_scan_max_threads: int = 0
```

`src/vestigo/core/settings_registry.py:582-587`:

```python
    SettingSpec(
        "stat_scan_max_threads",
        "scans",
        "Max threads per scan",
        "ClickHouse max_threads for heavy detector/inventory scans. 0 = auto: an even share "
        "of the cores ClickHouse reports for itself (cores / concurrent-scans, floor 2), so "
        "a full admission gate saturates the box rather than oversubscribing it. Pin a value "
        "only to override that. /api/health reports what resolved and whether it was "
        "detected or pinned.",
    ),
```

- [ ] **Step 5: Wire it through startup**

In `_probe_scan_budget`, import `configure_scan_threads` alongside the others and call it right
after `configure_scan_budget`:

```python
    configure_scan_budget(ceiling, bounded, cache_bytes, cache_breakdown)
    configure_scan_threads(int(facts.get("resolved_max_threads", 0) or 0) or None)
```

Extend the `else` (healthy) log branch so the width is stated once at startup:

```python
    else:
        logger.info(
            "Heavy-scan budget: %.1f GiB total (%.1f GiB per query x %d) under ClickHouse's "
            "%.1f GiB ceiling, with %.1f GiB of server caches counted; %d threads per scan (%s).",
            report["total_bytes"] / (1 << 30),
            report["per_query_bytes"] / (1 << 30),
            report["concurrency"],
            report["clickhouse_ceiling_bytes"] / (1 << 30),
            report["cache_bytes"] / (1 << 30),
            report["max_threads"],
            report["max_threads_source"],
        )
```

- [ ] **Step 6: Update the module docstring's thread sentence**

In `src/vestigo/db/_scan.py`'s module docstring, replace "and bound thread fan-out so several
concurrent scans don't oversubscribe the box" with:

```
bound thread fan-out so several concurrent scans don't oversubscribe the box
(``VESTIGO_STAT_SCAN_MAX_THREADS`` at its 0 default derives that width from the
cores ClickHouse reports for itself, divided by the gate's size — see
:func:`detect_scan_max_threads`),
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_scan_budget.py tests/test_scan_facts.py tests/test_settings_registry.py -v`
Expected: PASS. `tests/test_settings_registry.py` is the coverage test that fails if a field has
no `SettingSpec`; no field was added, so it should stay green — if it asserts on default values,
update the expectation there rather than reverting the default.

- [ ] **Step 8: Update `docs/ANOMALY_DETECTION.md` in this commit**

In the `HEAVY_SCAN_SETTINGS` bullet (`docs/ANOMALY_DETECTION.md:82`), replace `max_threads = 8`
with:

```markdown
- **`HEAVY_SCAN_SETTINGS` on every whole-corpus scan** (`max_threads` auto-derived as
  cores ÷ `VESTIGO_STAT_SCAN_CONCURRENCY` from the core count ClickHouse reports for
  itself — `system.settings.max_threads`, which resolves `auto(N)` cgroup-quota-aware;
  pin it with `VESTIGO_STAT_SCAN_MAX_THREADS`, and detection failure falls back to the
  former constant 8,
```

- [ ] **Step 9: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check .
git add src/vestigo/db/_scan.py src/vestigo/core/config.py src/vestigo/core/settings_registry.py src/vestigo/api/main.py docs/ANOMALY_DETECTION.md tests/test_scan_budget.py
git commit -m "feat(scan): derive scan thread width from ClickHouse's own core count

stat_scan_max_threads was the constant 8 — 40% of a 20-core host, and 4x
oversubscription of a 4-core one. It now defaults to 0 = auto: cores divided by
the admission gate's size, floor 2, read from the core count ClickHouse resolved
for itself. An explicit value still pins it, and detection failure falls back to
8 rather than refusing to start. /api/health reports the width and its source.

Fixes #301"
```

---

### Task 4: Ship `memory.xml` in the airgap install, and refuse to start without it

**Files:**
- Modify: `deploy/airgap/install.sh` (copy step ~`:207`; add a pre-flight check; extend `--check`)
- Modify: `scripts/airgap-bundle.sh` (comment only, pointing at the parity test)
- Create: `tests/test_airgap_bundle_parity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing other tasks depend on.

**Root cause, stated plainly:** `scripts/airgap-bundle.sh:140` stages
`deploy/clickhouse/memory.xml` into the bundle. `deploy/airgap/install.sh` copies
`clickhouse/allow-default-network.xml` into the install directory and **never copies
`clickhouse/memory.xml`**. The compose mount source at
`deploy/airgap/docker-compose.airgap.yml:64` therefore does not exist, Docker creates an empty
directory at the target, and ClickHouse merges no ceiling. Every airgap install since the file
shipped has run unbounded.

- [ ] **Step 1: Write the failing parity test**

Create `tests/test_airgap_bundle_parity.py`:

```python
"""Every ClickHouse drop-in the bundle carries must reach the install directory.

`install.sh` copied `allow-default-network.xml` and silently not `memory.xml`,
so the airgap compose's `./clickhouse/memory.xml` mount source never existed.
Docker materialises a missing bind-mount source as an empty *directory*, so
ClickHouse started, merged no ceiling, and derived 0.9 x whatever RAM a
limit-less container saw — the exact unbounded condition 1.15 shipped to
prevent, on the deployment that actually reaches production.

Grepping shell is a blunt instrument, but the alternative is running an airgap
install in CI, and the failure this guards is one nobody notices for a release.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE_SCRIPT = REPO / "scripts" / "airgap-bundle.sh"
INSTALL_SCRIPT = REPO / "deploy" / "airgap" / "install.sh"
AIRGAP_COMPOSE = REPO / "deploy" / "airgap" / "docker-compose.airgap.yml"


def _staged_clickhouse_files() -> set[str]:
    """Basenames the bundle script copies into the bundle's `clickhouse/`."""
    text = BUNDLE_SCRIPT.read_text()
    return set(re.findall(r'cp deploy/clickhouse/(\S+) "\$BUNDLE/clickhouse/"', text))


def _installed_clickhouse_files() -> set[str]:
    """Basenames install.sh copies into the install directory's `clickhouse/`."""
    text = INSTALL_SCRIPT.read_text()
    return set(re.findall(r'cp clickhouse/(\S+) "\$INSTALL_DIR/clickhouse/"', text))


def _mounted_clickhouse_files() -> set[str]:
    """Basenames the airgap compose bind-mounts out of `./clickhouse/`."""
    text = AIRGAP_COMPOSE.read_text()
    return set(re.findall(r"- \./clickhouse/(\S+?):", text))


def test_bundle_stages_the_memory_ceiling():
    assert "memory.xml" in _staged_clickhouse_files()


def test_installer_copies_every_file_the_bundle_stages():
    """The regression itself: a staged file the installer forgets is a mount
    source that does not exist, and Docker turns that into an empty directory
    rather than an error."""
    missing = _staged_clickhouse_files() - _installed_clickhouse_files()
    assert not missing, f"install.sh never copies: {sorted(missing)}"


def test_every_mounted_file_is_one_the_installer_copies():
    missing = _mounted_clickhouse_files() - _installed_clickhouse_files()
    assert not missing, f"compose mounts files install.sh never places: {sorted(missing)}"


def test_installer_asserts_the_memory_ceiling_is_a_regular_file():
    """A directory at that path is the silent-failure shape, so the check has to
    be `-f`, not `-e`."""
    text = INSTALL_SCRIPT.read_text()
    assert "clickhouse/memory.xml" in text
    assert re.search(r'\[ -f (clickhouse/memory\.xml|"\$INSTALL_DIR/clickhouse/memory\.xml") \]', text), (
        "install.sh must test memory.xml with -f before compose up"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_airgap_bundle_parity.py -v`
Expected: FAIL on `test_installer_copies_every_file_the_bundle_stages`
(`install.sh never copies: ['memory.xml']`) and on
`test_installer_asserts_the_memory_ceiling_is_a_regular_file`.

- [ ] **Step 3: Fix the installer's copy step**

In `deploy/airgap/install.sh`, immediately after the `allow-default-network.xml` line (`:207`):

```sh
cp clickhouse/allow-default-network.xml "$INSTALL_DIR/clickhouse/"
# Not optional, and its absence is silent: the compose file bind-mounts
# ./clickhouse/memory.xml, and a missing bind-mount source becomes an empty
# *directory* rather than an error. ClickHouse then starts, merges no ceiling,
# and derives 0.9 x whatever RAM a limit-less container sees — the unbounded
# condition this file exists to prevent. This copy was missing from every
# release that shipped memory.xml; tests/test_airgap_bundle_parity.py is what
# stops the next drop-in repeating it.
cp clickhouse/memory.xml "$INSTALL_DIR/clickhouse/"
```

- [ ] **Step 4: Add the pre-flight assertion before `compose up`**

Find the `compose up -d` invocation in `deploy/airgap/install.sh` and put this immediately
above it:

```sh
# The one mount whose absence does not fail. Checked with -f, not -e: the
# failure shape is a *directory* Docker created at the target on a previous run
# with a missing source, and -e would pass on exactly that.
if [ ! -f clickhouse/memory.xml ]; then
  if [ -d clickhouse/memory.xml ]; then
    die "clickhouse/memory.xml is a directory — a previous run started the stack without it, and Docker created it as a mount target. Remove it (rm -rf clickhouse/memory.xml), then re-run this installer."
  fi
  die "clickhouse/memory.xml is missing. Without it ClickHouse runs with no memory ceiling and is eventually OOM-killed by the kernel with nothing in its own log. Re-copy the bundle and re-run."
fi
```

- [ ] **Step 5: Cover it in `--check`**

In the `--check` block (`deploy/airgap/install.sh` around `:50-91`), before the
"bundle is complete and consistent" line, add:

```sh
  if [ -f clickhouse/memory.xml ]; then
    say "clickhouse/memory.xml present ($(wc -c < clickhouse/memory.xml) bytes)"
  else
    warn "clickhouse/memory.xml missing from the bundle — install would leave ClickHouse with no memory ceiling"
  fi
```

- [ ] **Step 6: Point the bundle script at the parity test**

In `scripts/airgap-bundle.sh`, extend the comment above `:140`:

```sh
# Ships enabled, not as an example to copy: the server-side memory ceiling is
# what keeps ClickHouse failing one query instead of being OOM-killed, and an
# opt-in guardrail is one that production runs without. Staging it here is only
# half of it — install.sh must copy it into the install directory too, which it
# did not for several releases; tests/test_airgap_bundle_parity.py asserts the
# two halves stay in step.
cp deploy/clickhouse/memory.xml "$BUNDLE/clickhouse/"
```

- [ ] **Step 7: Run the tests and shell-check the installer**

Run:
```bash
uv run pytest tests/test_airgap_bundle_parity.py -v
sh -n deploy/airgap/install.sh && echo "install.sh parses"
```
Expected: PASS, and `install.sh parses`.

- [ ] **Step 8: Commit**

```bash
git add deploy/airgap/install.sh scripts/airgap-bundle.sh tests/test_airgap_bundle_parity.py
git commit -m "fix(airgap): install memory.xml, and refuse to start without it

install.sh copied clickhouse/allow-default-network.xml into the install
directory and never clickhouse/memory.xml, so the airgap compose's bind-mount
source did not exist. Docker materialises a missing source as an empty
directory, so ClickHouse started, merged no ceiling and derived 0.9 x the RAM a
limit-less container saw — unbounded, on the deployment that reaches
production, since the file first shipped.

Copies it, asserts it is a regular file (not a directory) before compose up,
reports it under --check, and adds a parity test so the next drop-in cannot
repeat it.

Refs #303"
```

---

### Task 5: Surface `scan_budget.risk` in the admin UI

**Files:**
- Modify: `frontend/src/api/types.ts:1073-1103` (`HealthResponse`, new `ScanBudget`)
- Create: `frontend/src/components/admin/ScanBudgetCard.tsx`
- Modify: `frontend/src/pages/admin/AdminSettingsPage.tsx:273-291` (render it above the
  `scans` group)
- Create: `frontend/src/components/admin/ScanBudgetCard.test.tsx`

**Interfaces:**
- Consumes: the `scan_budget` block `/api/health` returns after Tasks 2 and 3 —
  `risk`, `per_query_bytes`, `total_bytes`, `cache_bytes`, `headroom_bytes`,
  `clickhouse_ceiling_bytes`, `concurrency`, `max_threads`, `max_threads_source`.
- Produces: `ScanBudgetCard` (default export absent; named export `ScanBudgetCard`).

- [ ] **Step 1: Add the types**

In `frontend/src/api/types.ts`, above `HealthResponse`:

```ts
/**
 * `/api/health`'s `scan_budget` block: how the heavy-scan memory budget
 * resolved and against what. `risk` is the field an operator acts on — a
 * misconfiguration here has no symptom until ClickHouse is OOM-killed, and the
 * kernel does that without writing anything to ClickHouse's own log.
 */
export interface ScanBudget {
  risk: "ok" | "over_budget" | "unbounded";
  per_query_bytes: number;
  total_bytes: number;
  /** Cache maxima under the same ClickHouse ceiling, unavailable to scans. */
  cache_bytes: number;
  cache_breakdown: Record<string, number>;
  /** What is left under the ceiling for merges and allocator slack. */
  headroom_bytes: number | null;
  clickhouse_ceiling_bytes: number | null;
  clickhouse_ceiling_is_explicit: boolean;
  budget_ceiling_bytes: number | null;
  local_detected_bytes: number | null;
  source: "pinned" | "clickhouse" | "local";
  concurrency: number;
  pending_concurrency: number | null;
  max_threads: number;
  max_threads_source: "pinned" | "clickhouse" | "fallback";
  detected_cores: number | null;
}
```

and add the field to `HealthResponse`:

```ts
  /** How the heavy-scan budget resolved. Authenticated responses only — it
   * describes the host's memory layout. */
  scan_budget?: ScanBudget;
```

- [ ] **Step 2: Write the failing component test**

Create `frontend/src/components/admin/ScanBudgetCard.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ScanBudget } from "../../api/types";
import { ScanBudgetCard } from "./ScanBudgetCard";

const base: ScanBudget = {
  risk: "ok",
  per_query_bytes: 2 * 1024 ** 3,
  total_bytes: 4 * 1024 ** 3,
  cache_bytes: 3.5 * 1024 ** 3,
  cache_breakdown: { mark_cache_size: 2 * 1024 ** 3 },
  headroom_bytes: 2 * 1024 ** 3,
  clickhouse_ceiling_bytes: 9.5 * 1024 ** 3,
  clickhouse_ceiling_is_explicit: true,
  budget_ceiling_bytes: 9.5 * 1024 ** 3,
  local_detected_bytes: 64 * 1024 ** 3,
  source: "clickhouse",
  concurrency: 2,
  pending_concurrency: null,
  max_threads: 10,
  max_threads_source: "clickhouse",
  detected_cores: 20,
};

describe("ScanBudgetCard", () => {
  it("states the resolved numbers when everything fits", () => {
    render(<ScanBudgetCard budget={base} />);
    expect(screen.getByText(/fits under ClickHouse/i)).toBeInTheDocument();
    expect(screen.getByText(/10 threads per scan/i)).toBeInTheDocument();
  });

  it("names the caches when scans plus caches exceed the ceiling", () => {
    render(<ScanBudgetCard budget={{ ...base, risk: "over_budget" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/caches/i);
  });

  it("says the kernel is the only backstop when ClickHouse is unbounded", () => {
    render(<ScanBudgetCard budget={{ ...base, risk: "unbounded" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/kernel/i);
  });

  it("discloses a concurrency edit waiting for a restart", () => {
    render(<ScanBudgetCard budget={{ ...base, pending_concurrency: 4 }} />);
    expect(screen.getByText(/restart/i)).toBeInTheDocument();
  });

  it("renders nothing without a budget — an anonymous health response has none", () => {
    const { container } = render(<ScanBudgetCard budget={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run (from `frontend/`): `npm run test -- ScanBudgetCard`
Expected: FAIL — cannot resolve `./ScanBudgetCard`

- [ ] **Step 4: Implement the card**

Create `frontend/src/components/admin/ScanBudgetCard.tsx`:

```tsx
import { AlertTriangle, Check } from "lucide-react";

import type { ScanBudget } from "../../api/types";

/** GiB with one decimal — the unit every sizing doc and log line already uses. */
function gib(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

const COPY: Record<ScanBudget["risk"], { title: string; body: (b: ScanBudget) => string }> = {
  ok: {
    title: "Scan budget fits under ClickHouse's ceiling",
    body: (b) =>
      `${gib(b.total_bytes)} of scans (${gib(b.per_query_bytes)} × ${b.concurrency}) plus ` +
      `${gib(b.cache_bytes)} of server caches under a ${gib(b.clickhouse_ceiling_bytes)} ceiling, ` +
      `leaving ${gib(b.headroom_bytes)} for background merges.`,
  },
  over_budget: {
    title: "Scans and caches exceed what ClickHouse may use",
    body: (b) =>
      `${gib(b.total_bytes)} of scans plus ${gib(b.cache_bytes)} of caches against a ` +
      `${gib(b.clickhouse_ceiling_bytes)} ceiling. Admitting a full set of scans can only end in ` +
      `a memory error or an OOM kill. Lower the scan budget, shrink the caches in ` +
      `deploy/clickhouse/memory.xml, or raise max_server_memory_usage.`,
  },
  unbounded: {
    title: "ClickHouse reports no memory ceiling of its own",
    body: () =>
      `Nothing bounds its merges, caches or allocator slack, and the kernel is the only ` +
      `backstop — it kills the server without writing anything to ClickHouse's log. Mount ` +
      `deploy/clickhouse/memory.xml and set a container memory limit. See docs/DEPLOYMENT.md ` +
      `"Resource sizing".`,
  },
};

/**
 * The `risk` verdict, where an operator is already standing when they change
 * the numbers it describes.
 *
 * It has always been on `/api/health` and in a startup log line, and a startup
 * warning is exactly what nobody reads — which is how an airgap install ran
 * unbounded for a release without anyone seeing it.
 */
export function ScanBudgetCard({ budget }: { budget: ScanBudget | undefined }) {
  if (!budget) return null;
  const copy = COPY[budget.risk];
  const bad = budget.risk !== "ok";

  return (
    <div
      role={bad ? "alert" : undefined}
      className={
        bad
          ? "mb-3 flex items-start gap-2 rounded border border-[var(--color-danger)]/40 bg-[var(--color-danger-dim)] p-3 text-xs text-[var(--color-danger)]"
          : "mb-3 flex items-start gap-2 rounded border border-[var(--color-border)] p-3 text-xs text-[var(--color-fg-muted)]"
      }
    >
      {bad ? (
        <AlertTriangle size={14} className="mt-0.5 shrink-0" />
      ) : (
        <Check size={14} className="mt-0.5 shrink-0" />
      )}
      <div className="space-y-1">
        <p className="font-medium">{copy.title}</p>
        <p>{copy.body(budget)}</p>
        <p>
          {budget.max_threads} threads per scan (
          {budget.max_threads_source === "pinned"
            ? "pinned"
            : budget.max_threads_source === "clickhouse"
              ? `from ${budget.detected_cores} cores ClickHouse reports`
              : "detection failed — fallback"}
          ), budget from {budget.source} detection.
        </p>
        {budget.pending_concurrency !== null && (
          <p>
            Concurrent scans is set to {budget.pending_concurrency} but still running at{" "}
            {budget.concurrency} — it takes effect on restart, and both halves of the budget keep
            using the old value until then.
          </p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run the component test**

Run (from `frontend/`): `npm run test -- ScanBudgetCard`
Expected: PASS (5 tests)

- [ ] **Step 6: Render it above the `scans` group**

In `frontend/src/pages/admin/AdminSettingsPage.tsx`, add the imports:

```tsx
import { useHealth } from "../../api/health";
import { ScanBudgetCard } from "../../components/admin/ScanBudgetCard";
```

inside `AdminSettingsPage`, beside the other hooks:

```tsx
  const health = useHealth();
```

and inside the `data.groups.map` render (`:277-291`), between the group's description and the
settings box:

```tsx
          <section key={group.key}>
            <h2 className="text-sm font-semibold text-[var(--color-fg-primary)]">{group.label}</h2>
            <p className="mb-2 text-xs text-[var(--color-fg-muted)]">{group.description}</p>
            {/* The verdict belongs next to the knobs that move it: an operator
                editing the scan budget is the person who needs to know it does
                not currently fit. */}
            {group.key === "scans" && <ScanBudgetCard budget={health.data?.scan_budget} />}
            <div className="rounded-lg border border-[var(--color-border)] px-4">
```

- [ ] **Step 7: Typecheck, lint, test, commit**

```bash
cd frontend && npm run typecheck && npm run lint && npm run test && cd ..
git add frontend/src/api/types.ts frontend/src/components/admin/ScanBudgetCard.tsx frontend/src/components/admin/ScanBudgetCard.test.tsx frontend/src/pages/admin/AdminSettingsPage.tsx
git commit -m "feat(admin): show the scan-budget risk verdict beside the scan settings

risk has been on /api/health and in a startup log line since 1.15, and a startup
warning is exactly what nobody reads. The card renders above the 'scans' group
on the admin settings page — where an operator already stands when they change
the numbers it describes — as a warning for over_budget/unbounded and a quiet
summary for ok.

Refs #303"
```

---

### Task 6: Re-size the shipped defaults and update the operator docs

**Files:**
- Modify: `deploy/clickhouse/memory.xml` (pin `index_mark_cache_size`, `primary_index_cache_size`)
- Modify: `docs/DEPLOYMENT.md:218-360` (§"Resource sizing": the three ceilings, the
  `scan_budget` sample, both worked examples, the airgap path)
- Modify: `docs/ROADMAP.md` (drop the closed items from any "Open defects" section)
- Modify: `docs/PROGRESS.md` (new entry on top)
- Test: `tests/test_reference_stack_budget.py` (create)

**Interfaces:**
- Consumes: `resolve_cache_bytes`, `resolve_clickhouse_ceiling`, `_resolve_scan_memory_budget`
  (Task 2).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing test that the shipped defaults fit**

Create `tests/test_reference_stack_budget.py`:

```python
"""The configuration we ship must report a truthful `risk`.

Issue #302's acceptance criterion, as a test rather than an assertion in a
comment: parse the numbers out of the file the compose stack actually mounts,
run them through the same pure functions the startup probe uses, and require
that scans plus caches fit under the ceiling.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from vestigo.db._scan import (
    _resolve_scan_memory_budget,
    resolve_cache_bytes,
    resolve_clickhouse_ceiling,
)

MEMORY_XML = Path(__file__).resolve().parents[1] / "deploy" / "clickhouse" / "memory.xml"

# `mem_limit: 12g` in docker-compose.yml — the container the file is sized for.
REFERENCE_CONTAINER_LIMIT = 12 * 1024**3
# ClickHouse 26.6 defaults for the caches memory.xml does not pin.
CLICKHOUSE_DEFAULT_CACHES = {
    "index_mark_cache_size": 5 * 1024**3,
    "primary_index_cache_size": 5 * 1024**3,
    "index_uncompressed_cache_size": 0,
}


def _shipped_facts() -> dict[str, float]:
    """memory.xml, as the probe would see it after ClickHouse applied it."""
    root = ET.parse(MEMORY_XML).getroot()
    facts: dict[str, float] = {
        "cgroup_memory_total": float(REFERENCE_CONTAINER_LIMIT),
        "os_memory_total": float(32 * 1024**3),
        **{k: float(v) for k, v in CLICKHOUSE_DEFAULT_CACHES.items()},
    }
    for child in root:
        # `<!-- ... -->` nodes are elements too, and their `.tag` is a callable
        # rather than a string — skip them or the first comment kills the parse.
        if not isinstance(child.tag, str):
            continue
        facts[child.tag] = float(child.text or 0)
    return facts


def test_shipped_memory_xml_pins_every_cache_we_count():
    """A cache left at its default is 5 GiB under a 9.5 GiB ceiling."""
    root = ET.parse(MEMORY_XML).getroot()
    pinned = {child.tag for child in root if isinstance(child.tag, str)}
    for name in ("mark_cache_size", "uncompressed_cache_size",
                 "index_mark_cache_size", "primary_index_cache_size"):
        assert name in pinned, f"memory.xml leaves {name} at its ClickHouse default"


def test_shipped_defaults_fit_under_their_own_ceiling():
    """Scans plus caches, against the ceiling the same file pins."""
    facts = _shipped_facts()
    ceiling, bounded = resolve_clickhouse_ceiling(facts)
    caches, _ = resolve_cache_bytes(facts)

    assert bounded, "the shipped file pins an explicit ceiling"
    per_query = _resolve_scan_memory_budget(0, 0.8, ceiling, concurrency=2, caches=caches)
    total = per_query * 2

    assert total + caches <= ceiling, (
        f"scans {total / 1024**3:.1f} GiB + caches {caches / 1024**3:.1f} GiB "
        f"exceed the {ceiling / 1024**3:.1f} GiB ceiling the file pins"
    )
    # Real headroom for merges, not a rounding sliver.
    assert (ceiling - total - caches) >= 1024**3
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_reference_stack_budget.py -v`
Expected: FAIL on `test_shipped_memory_xml_pins_every_cache_we_count`
(`memory.xml leaves index_mark_cache_size at its ClickHouse default`).

- [ ] **Step 3: Pin the two missing caches**

In `deploy/clickhouse/memory.xml`, replace the cache block at the end with:

```xml
  <!-- Caches count against the ceiling above, so their defaults spend the
       budget on cache instead of on queries. At 26.6 defaults that is not a
       trim but a rout: mark 2 GiB + index-mark 5 GiB + primary-index 5 GiB =
       12 GiB of cache maxima under a 9.5 GiB ceiling, i.e. the ceiling is
       already exceeded before one query is admitted. Vestigo's own budget is
       now taken of (ceiling - these), and /api/health reports the figure, so
       leaving one at its default visibly shrinks the scan budget instead of
       silently overcommitting the server.

       The uncompressed caches are off. Stated rather than left at the default
       so an upgrade cannot turn them on underneath this budget. -->
  <mark_cache_size>2147483648</mark_cache_size>
  <uncompressed_cache_size>0</uncompressed_cache_size>
  <!-- 512 MiB / 1 GiB against 5 GiB each by default. Vestigo's scans are
       whole-corpus GROUP BYs over a handful of columns: the marks and the
       primary index for the parts in flight, not a working set that rewards a
       5 GiB cache. -->
  <index_mark_cache_size>536870912</index_mark_cache_size>
  <index_uncompressed_cache_size>0</index_uncompressed_cache_size>
  <primary_index_cache_size>1073741824</primary_index_cache_size>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_reference_stack_budget.py -v`
Expected: PASS. Caches total 3.5 GiB; ceiling 9.5 GiB; budget 0.8 × 6 GiB = 4.8 GiB total,
2.4 GiB per query; headroom 9.5 − 4.8 − 3.5 = 1.2 GiB.

- [ ] **Step 5: Update `docs/DEPLOYMENT.md` §"Resource sizing"**

Three edits.

(a) In "The three ceilings", extend item 2 (`docs/DEPLOYMENT.md:254-269`) with a paragraph after
"…which makes it the layer that matters after an enrichment partition rewrite.":

```markdown
   **In an airgap install this file is at `./clickhouse/memory.xml`, relative to the
   install directory — not `deploy/clickhouse/memory.xml`.** They are the same file
   with two locations (`scripts/airgap-bundle.sh` copies one to the other), and only
   the bundle-relative one is mounted. Editing the repo path on an airgap host has no
   effect at all.

   **The file on disk is not proof of the file in effect.** A missing bind-mount source
   becomes an empty *directory*, which ClickHouse skips without complaint. Verify
   server-side, never with `grep`:

   ```sql
   SELECT name, value FROM system.server_settings
   WHERE name LIKE 'max_server_memory%' OR name LIKE '%cache_size';
   ```

   A `max_server_memory_usage` of 0 and a ratio of 0.9 mean nothing was merged.
```

(b) Replace the `scan_budget` sample and the `risk` bullets (`:288-317`) with:

```markdown
```json
{"risk": "ok", "per_query_bytes": 2576980377, "total_bytes": 5153960755,
 "cache_bytes": 3758096384, "cache_breakdown": {"mark_cache_size": 2147483648,
 "index_mark_cache_size": 536870912, "primary_index_cache_size": 1073741824,
 "uncompressed_cache_size": 0, "index_uncompressed_cache_size": 0},
 "headroom_bytes": 1288490190, "clickhouse_ceiling_bytes": 10200547328,
 "clickhouse_ceiling_is_explicit": true, "budget_ceiling_bytes": 10200547328,
 "local_detected_bytes": 34359738368, "source": "clickhouse", "concurrency": 2,
 "pending_concurrency": null, "max_threads": 6, "max_threads_source": "clickhouse",
 "detected_cores": 12}
```

`risk` is what to act on, and it is rendered on the admin **Settings** page above the
"Scans" group as well as served here:

- `ok` — scans *and* ClickHouse's own caches fit under its ceiling, with
  `headroom_bytes` left for background merges.
- `over_budget` — `total_bytes + cache_bytes` exceeds the ceiling. Lower
  `VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`, shrink the caches in `memory.xml`, or raise
  `max_server_memory_usage`. The caches are counted because they are configured maxima
  under the same ceiling: at 26.6 defaults `index_mark_cache_size` and
  `primary_index_cache_size` are 5 GiB *each*, which alone exceeds the ceiling the
  reference stack pins.
- `unbounded` — ClickHouse reports no ceiling an operator set. Nothing bounds its
  merges, caches or allocator slack, and the kernel is the only backstop. Mount
  `memory.xml` and set a container limit. The budget still uses that derived ceiling,
  but capped by what the *app's* own container can see — two guesses, so the lower one
  — which is what `budget_ceiling_bytes` reports when it differs from
  `clickhouse_ceiling_bytes`.

`max_threads` is per-scan thread width. At `VESTIGO_STAT_SCAN_MAX_THREADS=0` (the
default) it is `detected_cores ÷ concurrency`, floor 2, where `detected_cores` is what
ClickHouse resolves for itself — cgroup-CPU-quota aware, so a `--cpus=2` container
reports 2. `max_threads_source` is `pinned`, `clickhouse`, or `fallback` (the probe
failed; the width is the former constant 8).
```

(c) Update both worked-example tables. In "32 GiB host, full-docker (the shipped defaults)":

```markdown
| Setting | Value | Where |
| --- | --- | --- |
| ClickHouse container limit | 12 GiB | `mem_limit: 12g` |
| ClickHouse server limit | 9.5 GiB | `deploy/clickhouse/memory.xml` (under 0.8 × 12 GiB) |
| ClickHouse caches (mark + index-mark + primary-index) | 3.5 GiB | `memory.xml` |
| Vestigo scan budget (total) | 4.8 GiB | auto: 0.8 × (9.5 − 3.5) GiB |
| → per-query cap | 2.4 GiB | budget ÷ concurrency (2) |
| → merge headroom | 1.2 GiB | 9.5 − 4.8 − 3.5, reported as `headroom_bytes` |
| Postgres | 4 GiB | `mem_limit: 4g` |
| Qdrant | 4 GiB | `mem_limit: 4g` (only with embeddings) |
| App | 4 GiB | `mem_limit: 4g` |
```

In "64 GiB host, one analyst, ~700M-event timelines", replace the threads row and add caches:

```markdown
| ClickHouse caches | 3.5 GiB | `memory.xml`, unchanged from the shipped values |
| Threads per scan | 10 | auto: 20 cores ÷ concurrency (2); was the constant 8 |
```

and adjust the scan-budget rows to `0.8 × (27 − 3.5) = 18.8 GiB` total / `9.4 GiB` per query if
the budget is left on auto, noting that the pinned `VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES=17179869184`
shown is still honoured verbatim and bypasses the cache subtraction.

- [ ] **Step 6: Add the `PROGRESS.md` entry (newest on top)**

```markdown
## 2026-08-25 — Scan-budget truthfulness (#301, #302, #303)

`scan_budget.risk` reported `ok` for a configuration that does not fit, including the one
we ship. Three defects, one probe.

- **Caches were never counted** (#302). The ratio is now taken of
  `ceiling − caches`, which is what `stat_scan_memory_ratio`'s own help text always
  claimed, and `risk` compares `scans + caches` against the ceiling. The issue's own
  table understated it: at 26.6 defaults `index_mark_cache_size` and
  `primary_index_cache_size` are 5 GiB *each*, so the reference stack's caches (12 GiB)
  exceeded its whole 9.5 GiB ceiling. `memory.xml` now pins both; caches total 3.5 GiB
  and the stack fits, with 1.2 GiB of merge headroom, asserted by
  `tests/test_reference_stack_budget.py`.
- **Thread width was a constant** (#301). `stat_scan_max_threads` defaults to 0 = auto:
  cores ÷ concurrency, floor 2. The issue proposed reading CPU counts from
  `system.asynchronous_metrics`; 26.6 has none — no `CGroupMaxCPU`, no `OSNProcessors`.
  The real source is `system.settings.max_threads`, which reports `auto(N)` and *is*
  cgroup-quota aware (verified: `--cpus=2` → `auto(2)`).
- **The airgap install never had a ceiling** (#303). `install.sh` copied
  `allow-default-network.xml` and not `memory.xml`, so the compose bind-mount source did
  not exist and Docker made an empty directory of it. Fixed, plus a `-f` pre-flight
  assertion, `--check` coverage, and a parity test between the bundle script, the
  installer and the compose mounts. `risk` is now rendered on the admin Settings page,
  since a startup log line is what nobody reads.

Issue #300 (foreground histograms queuing behind detector sweeps in the shared gate)
stays open: it changes hot-path query behaviour and gets its own round.
```

- [ ] **Step 7: Prune `docs/ROADMAP.md`**

Remove #301/#302/#303 from any "Open defects" section (CLAUDE.md: delete items once fixed rather
than marking them done). Leave #300 listed, pointing at the issue.

- [ ] **Step 8: Full suite, then commit**

```bash
podman compose up -d
uv run pytest
uv run ruff format --check . && uv run ruff check .
cd frontend && npm run typecheck && npm run lint && npm run test && cd ..
git add deploy/clickhouse/memory.xml docs/DEPLOYMENT.md docs/PROGRESS.md docs/ROADMAP.md tests/test_reference_stack_budget.py
git commit -m "fix(deploy): size the shipped caches so the reference stack fits its ceiling

memory.xml pinned mark_cache_size and uncompressed_cache_size and left
index_mark_cache_size and primary_index_cache_size at their 26.6 defaults —
5 GiB each, so the shipped caches alone (12 GiB) exceeded the 9.5 GiB ceiling
the same file pins. Pins both; caches total 3.5 GiB, the auto budget resolves to
4.8 GiB, and 1.2 GiB is left for merges.

DEPLOYMENT.md gains the airgap path, the server-side verification query that
replaces grepping the file on disk, and recomputed worked examples.

Fixes #302, #303"
```

- [ ] **Step 9: Open the PR**

```bash
git push -u origin fix/scan-budget-truthfulness
gh pr create --title "Scan-budget truthfulness (#301, #302, #303)" --body "$(cat <<'BODY'
Batch A of the four scan-budget issues opened against 1.15.0. #300 is deliberately not
here — it changes hot-path query behaviour and needs its own regression evidence; this
round is the diagnosis surface it will be debugged against.

Two of the three issues were written from the repo rather than a running server, and
probing ClickHouse 26.6.1 corrected them. See
`docs/superpowers/specs/2026-08-25-scan-budget-truthfulness.md`.

- #302: caches were never counted against the ceiling they live under, and the shipped
  defaults were 12 GiB of cache maxima under a 9.5 GiB ceiling.
- #301: `system.asynchronous_metrics` has no CPU-count metric on 26.6; the core count
  comes from `system.settings.max_threads` (`auto(N)`, cgroup-quota aware).
- #303: the airgap installer never copied `memory.xml`, so every airgap install since it
  shipped ran with no ceiling.

Fixes #301, fixes #302, fixes #303.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01UQBK6dRJ4Q1DKPy4pvcDFM
BODY
)"
```

---

### Task 7: Sizing calculator, published on GitHub Pages

**Files:**
- Create: `docs/sizing/index.html` (self-contained page: markup, styles, script)
- Create: `docs/sizing/sizing-constants.json` (generated, checked in)
- Create: `scripts/gen_sizing_constants.py`
- Create: `tests/test_sizing_constants.py`
- Modify: `README.md` (link the published page)
- Modify: `docs/DEPLOYMENT.md` §"Resource sizing" (link it as the starting point)

**Interfaces:**
- Consumes: `stat_scan_memory_ratio`, `stat_scan_concurrency`, `_FALLBACK_MAX_THREADS`,
  `_COUNTED_CACHES` and the shipped `memory.xml` cache values (Tasks 2, 3, 6).
- Produces: nothing other tasks depend on.

**Decisions taken with the user:** GitHub Pages under `docs/sizing/`, linked from the README —
the audience is someone sizing a box *before* installing, so an in-app route would be too late.
**Sizing numbers only**: recommended hardware and a table of setting values, no generated `.env`
or `memory.xml` (an operator who pastes a generated config has skipped the file that explains
what the numbers mean). Formula constants are **generated** from the Python source with a parity
test, so the page cannot drift from what the app does.

**Enabling GitHub Pages is the user's call** — it publishes the repo's `docs/` folder to a public
URL. Do not enable it; hand the setting over at the end (Settings → Pages → source `main`,
folder `/docs`).

- [ ] **Step 1: Write the failing parity test**

Create `tests/test_sizing_constants.py`:

```python
"""The sizing calculator's constants are the app's constants.

`docs/sizing/index.html` computes recommended hardware and setting values from
a checked-in JSON. That JSON is generated from `core/config.py` and
`db/_scan.py`, and this test fails when the two drift — which is the only thing
standing between a public sizing page and advice the app stopped following.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONSTANTS = REPO / "docs" / "sizing" / "sizing-constants.json"
GENERATOR = REPO / "scripts" / "gen_sizing_constants.py"


def test_checked_in_constants_match_the_generator():
    fresh = subprocess.run(
        [sys.executable, str(GENERATOR), "--stdout"],
        capture_output=True, text=True, check=True, cwd=REPO,
    ).stdout
    assert json.loads(fresh) == json.loads(CONSTANTS.read_text()), (
        "docs/sizing/sizing-constants.json is stale — run "
        "`uv run python scripts/gen_sizing_constants.py`"
    )


def test_constants_carry_what_the_page_needs():
    data = json.loads(CONSTANTS.read_text())
    assert data["memory_ratio"] > 0
    assert data["default_concurrency"] >= 1
    assert data["fallback_max_threads"] == 8
    assert set(data["shipped_caches"]) <= set(data["counted_caches"])
    assert data["reference"]["clickhouse_mem_limit_bytes"] > 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_sizing_constants.py -v`
Expected: FAIL — the generator and the JSON do not exist.

- [ ] **Step 3: Write the generator**

Create `scripts/gen_sizing_constants.py`:

```python
"""Emit the constants `docs/sizing/index.html` sizes a deployment with.

The calculator is a static page on GitHub Pages, so it cannot import anything
from the app. Generating its constants — rather than transcribing them — is
what keeps a public sizing page from recommending values the app stopped
using. `tests/test_sizing_constants.py` fails when the checked-in JSON is
stale.

Run: `uv run python scripts/gen_sizing_constants.py` (writes the file), or
`--stdout` (what the parity test compares against).
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from vestigo.core.config import Settings  # noqa: E402
from vestigo.db._scan import _COUNTED_CACHES, _FALLBACK_MAX_THREADS  # noqa: E402

MEMORY_XML = REPO / "deploy" / "clickhouse" / "memory.xml"


def _shipped_caches() -> dict[str, int]:
    """The cache values the reference stack actually pins."""
    root = ET.parse(MEMORY_XML).getroot()
    return {
        child.tag: int(child.text or 0)
        for child in root
        if isinstance(child.tag, str) and child.tag in _COUNTED_CACHES
    }


def build() -> dict[str, object]:
    defaults = Settings.model_fields
    return {
        "memory_ratio": defaults["stat_scan_memory_ratio"].default,
        "default_concurrency": defaults["stat_scan_concurrency"].default,
        "fallback_max_threads": _FALLBACK_MAX_THREADS,
        "min_threads_per_scan": 2,
        "counted_caches": list(_COUNTED_CACHES),
        "shipped_caches": _shipped_caches(),
        # The ceiling the reference stack pins, and the container it is sized
        # for. The page scales both together, exactly as memory.xml's comment
        # tells an operator to.
        "reference": {
            "clickhouse_mem_limit_bytes": 12 * 1024**3,
            "clickhouse_ceiling_bytes": int(
                ET.parse(MEMORY_XML).getroot().findtext("max_server_memory_usage") or 0
            ),
            "ceiling_to_limit_ratio": float(
                ET.parse(MEMORY_XML).getroot().findtext(
                    "max_server_memory_usage_to_ram_ratio"
                )
                or 0.8
            ),
            "postgres_mem_limit_bytes": 4 * 1024**3,
            "qdrant_mem_limit_bytes": 4 * 1024**3,
            "app_mem_limit_bytes": 4 * 1024**3,
        },
        # Measured on the 300M-event corpus the scan guardrails were sized
        # against; used to scale ClickHouse's share with dataset size.
        "bytes_per_event_on_disk": 220,
        "scan_working_set_bytes_per_million_events": 12 * 1024**2,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args()
    text = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if args.stdout:
        sys.stdout.write(text)
    else:
        out = REPO / "docs" / "sizing" / "sizing-constants.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"wrote {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Generate the JSON and run the test**

```bash
uv run python scripts/gen_sizing_constants.py
uv run pytest tests/test_sizing_constants.py -v
```
Expected: PASS.

- [ ] **Step 5: Build the page**

Create `docs/sizing/index.html`: one self-contained file, no external requests (an operator
sizing an airgapped deployment may well be offline). Requirements:

- **Inputs**, as sliders with live numeric readouts: expected events (rows) in the largest
  timeline; number of concurrent analysts; deployment shape (full-docker / app-native with
  containerized services / ClickHouse on its own host); whether embeddings are used (decides
  whether Qdrant gets RAM at all); whether enrichment/`REPLACE PARTITION` applies are expected.
- **Outputs**, sizing numbers only: recommended total RAM and cores, a per-service RAM table
  (ClickHouse container limit, its `max_server_memory_usage`, its cache total, Postgres, Qdrant,
  app, page-cache reserve), and a table of `VESTIGO_*` / `memory.xml` keys with the value each
  should take. No copyable `.env`, no generated XML.
- Each output row states **why** in one line, and links the relevant `docs/DEPLOYMENT.md`
  anchor. The page is a doorway into the sizing docs, not a replacement for them.
- Math mirrors `db/_scan.py` exactly, read from `sizing-constants.json` via `fetch`:
  `ceiling = ratio_to_limit x container_limit`, `caches` from `shipped_caches` scaled with the
  ceiling, `scan_total = memory_ratio x (ceiling - caches)`, `per_query = scan_total /
  concurrency`, `threads = max(min_threads, cores // concurrency)`.
- A visible **"these are starting points"** note naming `/api/health`'s `scan_budget` as the
  only authority on what actually resolved.
- Styled like Vestigo: reuse the CSS custom-property names from `frontend/src/index.css`
  (`--color-bg-base`, `--color-fg-primary`, `--color-fg-muted`, `--color-border`,
  `--color-accent`, `--color-danger`), inlined, with both light and dark via
  `prefers-color-scheme`. Inline the logo as SVG from `frontend/public/` — read whatever file
  is there and embed it rather than linking, so the page has no external requests.

Read `frontend/src/index.css` for the actual token values and `frontend/public/` for the logo
before writing the file; do not invent either.

- [ ] **Step 6: Verify the page renders and the math matches the app**

```bash
python3 -m http.server -d docs/sizing 8099 &
```
Open `http://localhost:8099/`, set the sliders to the reference stack (12 GiB ClickHouse
container, 2 analysts) and confirm the page reports the same numbers
`tests/test_reference_stack_budget.py` asserts: 3.5 GiB caches, 4.8 GiB scan total, 2.4 GiB per
query, 1.2 GiB headroom. Kill the server afterwards.

- [ ] **Step 7: Link it**

In `README.md`, near the install/deployment section:

```markdown
**Sizing a box first?** The [sizing calculator](https://<owner>.github.io/vestigo/sizing/)
turns an expected dataset size and analyst count into recommended RAM, cores and
`VESTIGO_*` values. It is a starting point — `/api/health`'s `scan_budget` block reports
what actually resolved on the machine you deploy to.
```

In `docs/DEPLOYMENT.md`, at the top of §"Resource sizing", add one line pointing at the same URL
before the worked examples.

- [ ] **Step 8: Commit**

```bash
uv run ruff format . && uv run ruff check .
uv run pytest tests/test_sizing_constants.py -q
git add docs/sizing scripts/gen_sizing_constants.py tests/test_sizing_constants.py README.md docs/DEPLOYMENT.md
git commit -m "feat(docs): sizing calculator for hardware and scan settings

A self-contained page under docs/sizing/, published via GitHub Pages, that
turns expected dataset size, analyst count and deployment shape into
recommended RAM, cores and VESTIGO_* values. Its constants are generated from
core/config.py, db/_scan.py and deploy/clickhouse/memory.xml, with a parity
test, so a public sizing page cannot recommend values the app stopped using.

Sizing numbers only, deliberately: an operator who pastes a generated config
has skipped the file that explains what the numbers mean."
```

- [ ] **Step 9: Hand over the Pages switch**

Tell the user to enable it: **Settings → Pages → Source: Deploy from a branch → `main` / `/docs`**.
Do not enable it yourself — it publishes the repo's docs to a public URL.

---

## Self-review

**Spec coverage.** D1 → Task 2 step 3. D2 → Task 2 steps 1–3. D3 → Task 6 steps 3–4. D4 →
Task 3. D5 → Task 4. D6 → Task 5. Acceptance criteria: #301's four bullets → Task 3 steps 1,
3, 4 and Task 6 step 5(b); #302's four → Task 6 step 1, Task 2 step 1, Task 6 step 5(b), Task 2
step 1; #303's three → Task 4 steps 3–5, Task 6 step 5(a), Task 5.

**Type consistency.** `server_resource_facts` is used under that name in Task 1 (definition),
Task 1 step 6 (call site) and Task 2 step 4. `resolve_cache_bytes` returns
`tuple[int, dict[str, int]]` in Task 2 and is destructured that way in Task 2 step 4 and Task 6
step 1. `configure_scan_budget`'s four-argument form is used in Task 2 step 4 and the fixture in
Task 2 step 1. `ScanBudget`'s fields match `scan_budget_report`'s keys after Tasks 2 and 3.

**Known follow-up, not a gap.** `_resolve_scan_memory_budget` gains a fifth positional parameter;
its four existing call sites in `tests/test_scan_budget.py` pass three or four arguments and stay
correct, since `caches` defaults to 0.
