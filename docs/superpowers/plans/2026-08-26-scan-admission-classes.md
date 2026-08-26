# Scan Admission Classes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Interactive chart aggregations stop queuing behind detector sweeps, a queued chart tells the client it is waiting, and a request that disconnects releases its gate slot and kills its ClickHouse query.

**Architecture:** `db/_scan.py` grows a second semaphore (`FOREGROUND_SCAN_GATE`, 4 slots) fed by one heavy slot's worth of the memory budget (divisor `N+1`), a `ScanContext` contextvar that tags every scan's `SETTINGS` clause with `log_comment = 'vestigo-scan/<token>'`, and a cancellable `acquire_scan_slot()`. A new `api/scan_exec.py::run_scan` replaces `run_in_threadpool` at the three request-side choke points, polls `request.is_disconnected()` and issues `KILL QUERY` by that tag. The frontend treats a 503 carrying `queued_ahead` as "waiting", not "failed".

**Tech Stack:** Python 3.13, FastAPI/Starlette, `threading.BoundedSemaphore`, `contextvars`, clickhouse-connect 1.7 (`log_comment` setting, `KILL QUERY WHERE Settings['log_comment'] = …`), React 19 + TanStack Query, vitest, pytest against real ClickHouse/PostgreSQL.

**Spec:** `docs/superpowers/specs/2026-08-26-scan-admission-classes-design.md`

## Global Constraints

- Tests need reachable ClickHouse + PostgreSQL (`podman compose up -d`); never add `pytest.skip` around store construction.
- `uv run ruff check .` **and** `uv run ruff format --check .` must pass; line length 100, `E501` ignored.
- `_FOREGROUND_CONCURRENCY = 4` is a module constant, **not** a `Settings` field (no `SettingSpec`).
- The memory invariant: `N × heavy_cap + 4 × foreground_cap ≤ total budget`, with `heavy_cap = total // (N + 1)` and `foreground_cap = heavy_cap // 4`.
- Foreground bounded wait: `30.0` seconds; 503 body `{"detail": …, "queued_ahead": n}` with header `Retry-After: 5`.
- Detectors keep an unbounded wait. Background jobs, CLI and agent tools run without a request context and must behave exactly as before.
- Commits: identity `overcuriousity <overcuriousity@posteo.org>`, GPG-signed (repo config already does this); trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_019hNUHRTWQ5zpDqUaXCxLUv`.
- Docs updated in the same commit as the behaviour they describe (`docs/ANOMALY_DETECTION.md` for scan-cost changes).

---

## File map

| File | Responsibility |
|---|---|
| `src/vestigo/db/_scan.py` | budget split, both gates, `ScanContext`, `acquire_scan_slot`, `foreground_scan_settings`, `kill_scan_queries`, report |
| `src/vestigo/db/queries.py` | `_foreground_scan` decorator on the 12 chart aggregations; inventory keeps heavy |
| `src/vestigo/db/anomaly_stats.py`, `db/clickhouse.py`, `sigma/runner.py` | heavy holders acquire through `acquire_scan_slot` |
| `src/vestigo/api/request_context.py` (new) | pure ASGI middleware + `current_request()` contextvar |
| `src/vestigo/api/scan_exec.py` (new) | `run_scan()` — threadpool + disconnect polling + KILL + 503 mapping |
| `src/vestigo/api/routers/events.py`, `analysis.py` | choke points switched to `run_scan` |
| `src/vestigo/agent/chart_exec.py` | `ScanBusy` → `ValueError` for the tool |
| `frontend/src/api/client.ts`, `lib/queryClient.ts` | `ApiError.queuedAhead/retryAfter`, `busyRetry` |
| `frontend/src/components/explorer/TimelineHistogram.tsx`, `viz/FieldHistogramModal.tsx`, `viz/ChartCanvas.tsx` | waiting copy |
| `frontend/src/api/types.ts`, `components/admin/ScanBudgetCard.tsx` | foreground block |
| `scripts/gen_sizing_constants.py`, `docs/sizing/index.html`, `docs/sizing/sizing-constants.json` | `N+1` divisor, foreground row |
| `docs/ANOMALY_DETECTION.md`, `docs/DEPLOYMENT.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md` | reference docs |

---

### Task 1: Budget split — `N+1` divisor, foreground gate and cap

**Files:**
- Modify: `src/vestigo/db/_scan.py` (`_resolve_scan_memory_budget` call in `detect_scan_memory_budget`, `heavy_scan_settings`, gate block at ~578, `scan_budget_report`)
- Test: `tests/test_scan_budget.py`

**Interfaces:**
- Produces: `_FOREGROUND_CONCURRENCY: int = 4`, `FOREGROUND_SCAN_GATE: threading.BoundedSemaphore`, `detect_foreground_memory_budget() -> int`, `foreground_scan_settings() -> str`, `scan_budget_report()["foreground"] == {"concurrency": 4, "per_query_bytes": int}`.
- `detect_scan_memory_budget()` keeps its name; it now returns `total // (N + 1)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_scan_budget.py`:

```python
# ── Admission classes (#300) ─────────────────────────────────────────────────


def test_heavy_cap_reserves_one_slot_for_the_foreground_class(monkeypatch):
    """The divisor is N + 1: one heavy slot's worth is the foreground share."""
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 64 << 30)
    settings = _scan.get_settings()
    total = int((64 << 30) * settings.stat_scan_memory_ratio)
    assert _scan.detect_scan_memory_budget() == total // (_scan._GATE_CONCURRENCY + 1)


def test_foreground_cap_is_a_quarter_of_a_heavy_slot(monkeypatch):
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 64 << 30)
    heavy = _scan.detect_scan_memory_budget()
    assert _scan._FOREGROUND_CONCURRENCY == 4
    assert _scan.detect_foreground_memory_budget() == heavy // 4
    # The invariant the module exists for: both classes fully admitted fit the budget.
    settings = _scan.get_settings()
    total = int((64 << 30) * settings.stat_scan_memory_ratio)
    assert (
        _scan._GATE_CONCURRENCY * heavy
        + _scan._FOREGROUND_CONCURRENCY * _scan.detect_foreground_memory_budget()
        <= total
    )


def test_foreground_clause_carries_the_smaller_cap(monkeypatch):
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 64 << 30)
    clause = _scan.foreground_scan_settings()
    cap = _scan.detect_foreground_memory_budget()
    assert clause.startswith("SETTINGS max_threads = ")
    assert f"max_memory_usage = {cap}" in clause
    assert f"max_bytes_before_external_group_by = {min(_scan.get_settings().stat_scan_external_group_by_bytes, cap // 2)}" in clause


def test_foreground_gate_has_four_slots():
    taken = 0
    try:
        while _scan.FOREGROUND_SCAN_GATE.acquire(blocking=False):
            taken += 1
        assert taken == 4
    finally:
        for _ in range(taken):
            _scan.FOREGROUND_SCAN_GATE.release()


def test_report_discloses_the_foreground_class(monkeypatch):
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 64 << 30)
    report = _scan.scan_budget_report()
    assert report["foreground"] == {
        "concurrency": 4,
        "per_query_bytes": _scan.detect_foreground_memory_budget(),
    }
    # `total_bytes` is what both classes may hold at once, not just the heavy gate.
    assert report["total_bytes"] == report["per_query_bytes"] * (_scan._GATE_CONCURRENCY + 1)
```

Also update the existing `test_clause_follows_the_configured_ceiling` expectation:
```python
    expected = int((8 << 30) * settings.stat_scan_memory_ratio) // (settings.stat_scan_concurrency + 1)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scan_budget.py -q -k "foreground or reserves or clause_follows"`
Expected: FAIL — `AttributeError: module 'vestigo.db._scan' has no attribute '_FOREGROUND_CONCURRENCY'` etc.

- [ ] **Step 3: Implement in `src/vestigo/db/_scan.py`**

In `detect_scan_memory_budget()` replace the `_GATE_CONCURRENCY,` argument with `_GATE_CONCURRENCY + 1,` and amend the comment above it:

```python
        # Deliberately the gate's own size *plus one*, not the live setting.
        # The extra slot is the foreground class's share (see
        # FOREGROUND_SCAN_GATE): N heavy scans plus four foreground charts at a
        # quarter-slot each is exactly the total, so both gates fully admitted
        # still fit. The divisor and the semaphore describe one budget from
        # two sides, and this is the only value both can agree on: ...
        _GATE_CONCURRENCY + 1,
```

Add after `detect_scan_memory_budget`:

```python
def detect_foreground_memory_budget() -> int:
    """Per-query ``max_memory_usage`` for a foreground (chart) scan.

    One heavy slot's worth, divided across the foreground gate — so charts
    have their own lane without adding to the total the heavy class already
    accounts for. Never 0, for the same reason as the heavy cap.
    """
    return max(detect_scan_memory_budget() // _FOREGROUND_CONCURRENCY, 1)
```

Refactor `heavy_scan_settings` into a shared builder and add the foreground variant:

```python
def _scan_settings_clause(budget: int) -> str:
    s = get_settings()
    # Spill must engage well before the cap kills the query — a configured
    # threshold at or above the per-query cap would never fire.
    group_by_spill = min(s.stat_scan_external_group_by_bytes, budget // 2)
    sort_spill = min(s.stat_scan_external_sort_bytes, budget // 2)
    return (
        f"SETTINGS max_threads = {detect_scan_max_threads()}, "
        f"max_bytes_before_external_group_by = {group_by_spill}, "
        # Plain ORDER BY sorts spill at this threshold. Window-function sorts
        # cannot spill at all (see docs/ANOMALY_DETECTION.md) — bound those
        # scans structurally (per source / slim columns) instead.
        f"max_bytes_before_external_sort = {sort_spill}, "
        f"max_memory_usage = {budget}"
    )


def heavy_scan_settings() -> str:
    """The SETTINGS clause every whole-corpus scan must carry.

    Built per call rather than frozen at import — see the module docstring.
    Cheap: a few f-string formats over cached inputs, against a query that is
    about to read the whole corpus.
    """
    return _scan_settings_clause(detect_scan_memory_budget())


def foreground_scan_settings() -> str:
    """The SETTINGS clause for an interactive chart aggregation.

    Same shape as :func:`heavy_scan_settings` with the foreground cap. Thread
    width is the heavy width on purpose: a chart is short and latency-bound,
    and the foreground gate's size, not the thread width, bounds its CPU.
    """
    return _scan_settings_clause(detect_foreground_memory_budget())
```

Below `HEAVY_SCAN_GATE = ...` add:

```python
# Admission gate for *foreground* scans: the chart aggregations an analyst is
# looking at while they wait (histogram, top terms, numeric stats, …). Its own
# lane so a 60-bucket GROUP BY never queues behind a whole-corpus detector
# sweep (issue #300). Sized as one heavy slot split four ways — see
# detect_foreground_memory_budget — so it adds nothing to the total. A
# constant rather than a setting: how finely the reserved slot is sliced is
# not a number an operator needs to reason about.
_FOREGROUND_CONCURRENCY = 4
FOREGROUND_SCAN_GATE = threading.BoundedSemaphore(_FOREGROUND_CONCURRENCY)
```

In `scan_budget_report()`:
```python
    per_query = detect_scan_memory_budget()
    foreground = detect_foreground_memory_budget()
    # Both classes fully admitted: N heavy slots plus the one slot the
    # foreground gate shares.
    total = per_query * (_GATE_CONCURRENCY + 1)
```
and add to the returned dict, after `"pending_concurrency"`:
```python
        # The foreground class (charts): its own gate, fed by the slot the
        # heavy divisor reserves. Disclosed so "why is my chart capped at X"
        # has an answer on the same page as the heavy cap.
        "foreground": {"concurrency": _FOREGROUND_CONCURRENCY, "per_query_bytes": foreground},
```

Update the module docstring paragraph "Each query's ``max_memory_usage`` is budget / ``VESTIGO_STAT_SCAN_CONCURRENCY``" to say "budget / (``VESTIGO_STAT_SCAN_CONCURRENCY`` + 1) — the extra slot is the foreground class, see :data:`FOREGROUND_SCAN_GATE`".

- [ ] **Step 4: Run the file**

Run: `uv run pytest tests/test_scan_budget.py tests/test_reference_stack_budget.py tests/test_scan_facts.py -q`
Expected: PASS. If `test_reference_stack_budget.py` asserts a per-query figure derived from `/ concurrency`, update it to `/ (concurrency + 1)` with a comment naming #300.

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/db/_scan.py tests/test_scan_budget.py tests/test_reference_stack_budget.py
git commit -m "feat(scan): reserve one heavy slot for a foreground admission class (#300)"
```

---

### Task 2: Scan context, cancellable admission, KILL by tag

**Files:**
- Modify: `src/vestigo/db/_scan.py`
- Test: `tests/test_scan_admission.py` (new)

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class ScanContext:
      token: str
      cancelled: threading.Event
  class ScanBusy(RuntimeError):       # .ahead: int, .wait: float
  class ScanCancelled(RuntimeError)
  def scan_context() -> ScanContext | None
  def bind_scan_context() -> contextlib.AbstractContextManager[ScanContext]   # sets the contextvar; resets on exit
  def acquire_scan_slot(gate: threading.BoundedSemaphore, *, wait: float | None) -> contextlib.AbstractContextManager[None]
  def scan_log_comment(token: str) -> str          # 'vestigo-scan/<token>'
  def kill_scan_queries(client, token: str) -> None
  ```
- `heavy_scan_settings()` / `foreground_scan_settings()` append `, log_comment = '<scan_log_comment(token)>'` when `scan_context()` is not None.

- [ ] **Step 1: Write failing tests** — create `tests/test_scan_admission.py`:

```python
"""Cancellable admission and query tagging for scans (db/_scan.py, #300)."""

from __future__ import annotations

import threading
import time

import pytest

from vestigo.db import _scan
from vestigo.db._scan import (
    ScanBusy,
    ScanCancelled,
    acquire_scan_slot,
    bind_scan_context,
    scan_context,
)


def _held(n: int) -> threading.BoundedSemaphore:
    gate = threading.BoundedSemaphore(n)
    for _ in range(n):
        gate.acquire()
    return gate


def test_acquire_without_context_behaves_like_the_bare_gate():
    gate = threading.BoundedSemaphore(1)
    with acquire_scan_slot(gate, wait=None):
        assert not gate.acquire(blocking=False)
    assert gate.acquire(blocking=False)
    gate.release()


def test_bounded_wait_raises_busy_with_the_queue_depth(monkeypatch):
    gate = _held(1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)
    with pytest.raises(ScanBusy) as info, acquire_scan_slot(gate, wait=0.05):
        pass
    assert info.value.ahead == 0
    assert info.value.wait == 0.05


def test_ahead_counts_callers_parked_before_this_one(monkeypatch):
    gate = _held(1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)
    started = threading.Event()

    def park():
        started.set()
        with pytest.raises(ScanBusy), acquire_scan_slot(gate, wait=0.3):
            pass

    t = threading.Thread(target=park)
    t.start()
    started.wait()
    time.sleep(0.02)
    with pytest.raises(ScanBusy) as info, acquire_scan_slot(gate, wait=0.05):
        pass
    assert info.value.ahead == 1
    t.join()


def test_cancel_while_queued_raises_and_takes_no_slot(monkeypatch):
    gate = _held(1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)
    with bind_scan_context() as ctx:
        threading.Timer(0.03, ctx.cancelled.set).start()
        with pytest.raises(ScanCancelled), acquire_scan_slot(gate, wait=None):
            pass
    gate.release()
    # The slot the holder releases is the only one; nothing was taken by us.
    assert gate.acquire(blocking=False)
    gate.release()


def test_cancel_after_admission_is_the_callers_problem():
    """A slot already held is released by the `with` exit, not by the event."""
    gate = threading.BoundedSemaphore(1)
    with bind_scan_context() as ctx, acquire_scan_slot(gate, wait=None):
        ctx.cancelled.set()
        assert not gate.acquire(blocking=False)
    assert gate.acquire(blocking=False)
    gate.release()


def test_settings_clause_is_tagged_only_under_a_context(monkeypatch):
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 8 << 30)
    assert "log_comment" not in _scan.heavy_scan_settings()
    assert "log_comment" not in _scan.foreground_scan_settings()
    with bind_scan_context() as ctx:
        assert scan_context() is ctx
        tag = _scan.scan_log_comment(ctx.token)
        assert tag == f"vestigo-scan/{ctx.token}"
        assert f"log_comment = '{tag}'" in _scan.heavy_scan_settings()
        assert f"log_comment = '{tag}'" in _scan.foreground_scan_settings()
    assert scan_context() is None


def test_kill_targets_the_tag_and_never_raises():
    seen: list[tuple[str, dict]] = []

    class _Client:
        def command(self, sql, parameters=None):
            seen.append((sql, parameters))
            raise RuntimeError("clickhouse is away")

    _scan.kill_scan_queries(_Client(), "abc")
    sql, params = seen[0]
    assert "KILL QUERY WHERE Settings['log_comment'] = {tag:String} ASYNC" in sql
    assert params == {"tag": "vestigo-scan/abc"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scan_admission.py -q`
Expected: FAIL — `ImportError: cannot import name 'ScanBusy'`.

- [ ] **Step 3: Implement in `src/vestigo/db/_scan.py`**

Imports to add: `import contextlib`, `import logging`, `import uuid`, `from collections.abc import Iterator`, `from contextvars import ContextVar`, `from dataclasses import dataclass, field`. `logger = logging.getLogger(__name__)` if absent.

Add (before the settings-clause functions so they can reference it):

```python
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
    """A bounded wait for a gate slot expired. ``ahead`` is the queue depth at entry."""

    def __init__(self, *, ahead: int, wait: float) -> None:
        self.ahead = ahead
        self.wait = wait
        super().__init__(
            f"scan lane busy: {ahead} waiting ahead after {wait:g}s — retry shortly"
        )


class ScanCancelled(RuntimeError):
    """The request that started this scan went away while it waited or ran."""


#: How often a parked acquire re-checks its cancel flag. Module-level so tests
#: can shorten it; one second is invisible to a human and cheap for a thread.
_ACQUIRE_POLL_SECONDS = 1.0

_waiting_lock = threading.Lock()
_waiting: dict[int, int] = {}


def _waiting_count(gate: threading.BoundedSemaphore) -> int:
    with _waiting_lock:
        return _waiting.get(id(gate), 0)


def _adjust_waiting(gate: threading.BoundedSemaphore, delta: int) -> None:
    with _waiting_lock:
        _waiting[id(gate)] = max(_waiting.get(id(gate), 0) + delta, 0)


@contextlib.contextmanager
def acquire_scan_slot(
    gate: threading.BoundedSemaphore, *, wait: float | None
) -> Iterator[None]:
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
    ahead = _waiting_count(gate)
    _adjust_waiting(gate, +1)
    deadline = None if wait is None else time.monotonic() + wait
    try:
        while True:
            if ctx is not None and ctx.cancelled.is_set():
                raise ScanCancelled("cancelled while waiting for a scan slot")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise ScanBusy(ahead=ahead, wait=wait or 0.0)
            timeout = _ACQUIRE_POLL_SECONDS if remaining is None else min(_ACQUIRE_POLL_SECONDS, remaining)
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
        logger.warning("KILL QUERY for scan %s failed; it will finish on its own", token, exc_info=True)
```

`import time` and `from typing import Any` if not already imported. Then in `_scan_settings_clause` append the tag:

```python
    ctx = scan_context()
    tag = f", log_comment = '{scan_log_comment(ctx.token)}'" if ctx is not None else ""
    return (
        f"SETTINGS max_threads = {detect_scan_max_threads()}, "
        ...
        f"max_memory_usage = {budget}{tag}"
    )
```
(`token` is a uuid hex, so no quoting concerns.)

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_scan_admission.py tests/test_scan_budget.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/db/_scan.py tests/test_scan_admission.py
git commit -m "feat(scan): scan context, cancellable gate acquire and KILL-by-tag (#300)"
```

---

### Task 3: Switch every holder to `acquire_scan_slot`; charts go foreground

**Files:**
- Modify: `src/vestigo/db/queries.py:1119-1136` (`_gated_scan`), the 29 `heavy_scan_settings()` call sites inside the 12 chart methods, `iter_field_inventory` (~2318-2340)
- Modify: `src/vestigo/db/anomaly_stats.py:1577-1592`
- Modify: `src/vestigo/db/clickhouse.py:896`
- Modify: `src/vestigo/sigma/runner.py:103`
- Test: `tests/test_scan_budget.py`, `tests/test_queries.py`

**Interfaces:**
- Consumes: `acquire_scan_slot`, `FOREGROUND_SCAN_GATE`, `foreground_scan_settings`, `ScanBusy` from Task 1–2.
- Produces: `queries._foreground_scan` decorator; every `EventQueryService` chart method uses the foreground gate and clause. Heavy holders unchanged in behaviour.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scan_budget.py`:

```python
def test_chart_aggregations_take_the_foreground_gate(monkeypatch):
    """A histogram must never wait for a detector slot (#300)."""
    from vestigo.db import queries

    class _Gate:
        def __init__(self):
            self.entered = 0

        def acquire(self, timeout=None):
            self.entered += 1
            return True

        def release(self):
            pass

    heavy, fg = _Gate(), _Gate()
    monkeypatch.setattr(queries, "HEAVY_SCAN_GATE", heavy)
    monkeypatch.setattr(queries, "FOREGROUND_SCAN_GATE", fg)
    svc = queries.EventQueryService.__new__(queries.EventQueryService)

    @queries._foreground_scan
    def probe(self):
        return "ok"

    assert probe(svc) == "ok"
    assert (fg.entered, heavy.entered) == (1, 0)


def test_every_chart_aggregation_is_foreground():
    from vestigo.db.queries import EventQueryService

    charts = [
        "histogram", "field_terms", "field_numeric_stats", "field_correlation",
        "field_numeric_grouped", "field_value_timeseries", "compare_time_histogram",
        "compare_field_terms", "compare_field_numeric", "time_punchcard", "field_pivot",
        "field_scatter",
    ]
    for name in charts:
        fn = getattr(EventQueryService, name)
        assert getattr(fn, "_scan_class", None) == "foreground", f"{name} is not foreground"
    for name in ("count_field_inventory",):
        assert getattr(getattr(EventQueryService, name), "_scan_class", None) != "foreground"


def test_foreground_wait_is_bounded(monkeypatch):
    from vestigo.db import queries

    class _Full:
        def acquire(self, timeout=None):
            return False

        def release(self):
            raise AssertionError("never acquired")

    monkeypatch.setattr(queries, "FOREGROUND_SCAN_GATE", _Full())
    monkeypatch.setattr(queries, "FOREGROUND_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)

    @queries._foreground_scan
    def probe(self):
        raise AssertionError("must not run")

    with pytest.raises(_scan.ScanBusy):
        probe(object())
```

In `tests/test_queries.py` add next to `test_histogram_buckets_over_corrected_timestamp`:

```python
def test_histogram_carries_the_foreground_clause(monkeypatch) -> None:
    from vestigo.db import _scan

    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 64 << 30)
    svc = EventQueryService(store=FakeClickHouseStore())
    svc.histogram(EventQuery(case_id="case-1", source_ids=["s1"]))
    query, _ = svc.store.client.queries[-1]
    assert f"max_memory_usage = {_scan.detect_foreground_memory_budget()}" in query
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scan_budget.py tests/test_queries.py -q -k "foreground or chart"`
Expected: FAIL — `AttributeError: ... '_foreground_scan'`.

- [ ] **Step 3: Implement**

`src/vestigo/db/queries.py`: change the import to
```python
from vestigo.db._scan import (
    EXPORT_SCAN_GATE,
    FOREGROUND_SCAN_GATE,
    HEAVY_SCAN_GATE,
    acquire_scan_slot,
    foreground_scan_settings,
    heavy_scan_settings,
)
```
Replace `_gated_scan` with:

```python
#: How long a chart waits for a foreground slot before the API answers 503
#: "busy" instead of leaving a spinner. Module-level so tests can shorten it.
FOREGROUND_WAIT_SECONDS = 30.0


def _foreground_scan(fn):
    """Admit at most ``_FOREGROUND_CONCURRENCY`` chart aggregations at once.

    The foreground class (#300): its own gate so a histogram never queues
    behind a detector sweep, its own (smaller) per-query cap so the total
    budget still holds, and a *bounded* wait — a chart the analyst is looking
    at must say "busy" rather than spin. Applied to the public aggregation
    entry points only; internal helpers (``_field_terms_impl``, the
    ``_compare`` layer scans) run while the caller holds the slot, and gating
    them too would deadlock. Callers run in FastAPI's threadpool, so blocking
    on the semaphore is safe. The gate is looked up at call time so tests can
    substitute it.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with acquire_scan_slot(FOREGROUND_SCAN_GATE, wait=FOREGROUND_WAIT_SECONDS):
            return fn(*args, **kwargs)

    wrapper._scan_class = "foreground"  # type: ignore[attr-defined]
    return wrapper
```
Change every `@_gated_scan` in `queries.py` to `@_foreground_scan` (12 methods). Inside those 12 methods and the helpers they call while holding the slot (`_field_terms_impl`, `_compare_*`), replace `heavy_scan_settings()` with `foreground_scan_settings()` — all occurrences in the file **except** those in `count_field_inventory` and `iter_field_inventory`. Verify with `grep -n "heavy_scan_settings()" src/vestigo/db/queries.py` → exactly the two inventory sites remain.

`count_field_inventory`: `with HEAVY_SCAN_GATE:` → `with acquire_scan_slot(HEAVY_SCAN_GATE, wait=None):`.

`iter_field_inventory` keeps its hand-over-hand release (slot dropped once rows flow). Replace the bare `HEAVY_SCAN_GATE.acquire()` / `.release()` pair with the cancellable context manager driven explicitly, since the release point is inside the generator loop:
```python
        with EXPORT_SCAN_GATE if hold_export_slot else contextlib.nullcontext():
            slot = acquire_scan_slot(HEAVY_SCAN_GATE, wait=None)
            slot.__enter__()  # cancellable admission; may raise ScanCancelled
            scanning = True
            try:
                for block in self._select_row_blocks(sql, parameters=parameters):
                    if scanning:
                        slot.__exit__(None, None, None)
                        scanning = False
                    for value, count, first_seen, last_seen in block:
                        yield {...}  # unchanged
            finally:
                if scanning:
                    slot.__exit__(None, None, None)
```

`src/vestigo/db/anomaly_stats.py` `_gated_scan`:
```python
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with acquire_scan_slot(HEAVY_SCAN_GATE, wait=None):
            return fn(*args, **kwargs)

    wrapper._scan_class = "heavy"  # type: ignore[attr-defined]
    return wrapper
```
(import `acquire_scan_slot` alongside `HEAVY_SCAN_GATE`). Keep `__wrapped__` — `test_every_detector_entry_point_is_gated` relies on it.

`src/vestigo/db/clickhouse.py:896`: `with HEAVY_SCAN_GATE:` → `with acquire_scan_slot(HEAVY_SCAN_GATE, wait=None):` (import it). Check `test_enrichment_partition_rewrite_takes_a_gate_slot` — it substitutes a `_Gate` with `__enter__/__exit__`; change that fake to expose `acquire(timeout=None) -> True` / `release()` and record on those instead.

`src/vestigo/sigma/runner.py:103`:
```python
        with (
            acquire_scan_slot(HEAVY_SCAN_GATE, wait=None),
            ch.client.query_rows_stream(query, parameters=params) as stream,
        ):
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_scan_budget.py tests/test_scan_admission.py tests/test_queries.py tests/test_anomaly_stats.py tests/test_viz_router.py -q`
Expected: PASS. Then `uv run ruff check . && uv run ruff format --check .`.

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/db/queries.py src/vestigo/db/anomaly_stats.py src/vestigo/db/clickhouse.py src/vestigo/sigma/runner.py tests/test_scan_budget.py tests/test_queries.py
git commit -m "feat(scan): chart aggregations take the foreground gate; heavy holders acquire cancellably (#300)"
```

---

### Task 4: Request contextvar middleware and `run_scan`

**Files:**
- Create: `src/vestigo/api/request_context.py`
- Create: `src/vestigo/api/scan_exec.py`
- Modify: `src/vestigo/api/main.py:712` (add middleware)
- Test: `tests/test_scan_exec.py` (new), `tests/test_scan_exec_clickhouse.py` (new)

**Interfaces:**
- Produces:
  ```python
  # request_context.py
  class RequestContextMiddleware: (pure ASGI)
  def current_request() -> Request | None
  # scan_exec.py
  class ScanCancelledResponse(HTTPException)   # status 499
  async def run_scan(fn, /, *args, **kwargs) -> Any
  ```
- `run_scan` maps `ScanBusy` → `ScanBusyResponse` (503, `Retry-After: 5`, body `{"detail", "queued_ahead"}` via a registered exception handler — FastAPI would otherwise nest a dict detail under `detail`) and `ScanCancelled`/disconnect → `ScanCancelledResponse` (499).
- `install(app)` registers that handler; `main.create_app` calls it.

- [ ] **Step 1: Write failing tests** — `tests/test_scan_exec.py`:

```python
"""run_scan: threadpool scans that notice a gone client and a busy lane (#300)."""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from vestigo.api import scan_exec
from vestigo.api.request_context import RequestContextMiddleware, current_request
from vestigo.db import _scan
from vestigo.db._scan import ScanBusy, ScanCancelled, scan_context


class _Request:
    def __init__(self, disconnect_after: int):
        self.calls = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.disconnect_after


@pytest.mark.asyncio
async def test_scan_runs_under_a_bound_context(monkeypatch):
    monkeypatch.setattr(scan_exec, "current_request", lambda: None)
    seen = {}

    def work():
        seen["ctx"] = scan_context()
        return 42

    assert await scan_exec.run_scan(work) == 42
    assert seen["ctx"] is not None and seen["ctx"].token
    assert scan_context() is None


@pytest.mark.asyncio
async def test_disconnect_cancels_a_queued_scan_and_kills_by_tag(monkeypatch):
    req = _Request(disconnect_after=1)
    monkeypatch.setattr(scan_exec, "current_request", lambda: req)
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)
    killed: list[str] = []
    monkeypatch.setattr(scan_exec, "_kill", lambda token: killed.append(token))
    gate = threading.BoundedSemaphore(1)
    gate.acquire()

    def work():
        with _scan.acquire_scan_slot(gate, wait=None):
            raise AssertionError("must not be admitted")

    with pytest.raises(scan_exec.ScanCancelledResponse):
        await scan_exec.run_scan(work)
    assert len(killed) == 1
    gate.release()


@pytest.mark.asyncio
async def test_busy_maps_to_503_with_queue_depth(monkeypatch):
    monkeypatch.setattr(scan_exec, "current_request", lambda: None)

    def work():
        raise ScanBusy(ahead=3, wait=30.0)

    with pytest.raises(scan_exec.ScanBusyResponse) as info:
        await scan_exec.run_scan(work)
    assert info.value.queued_ahead == 3


def test_busy_response_shape():
    app = FastAPI()
    scan_exec.install(app)

    @app.get("/x")
    async def x():
        raise scan_exec.ScanBusyResponse(ScanBusy(ahead=2, wait=30.0))

    res = TestClient(app).get("/x")
    assert res.status_code == 503
    assert res.headers["retry-after"] == "5"
    body = res.json()
    assert body["queued_ahead"] == 2
    assert "busy" in body["detail"]


def test_middleware_exposes_the_request_to_the_endpoint():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/x")
    async def x():
        req = current_request()
        return {"path": req.url.path if req else None}

    assert TestClient(app).get("/x").json() == {"path": "/x"}
    assert current_request() is None


@pytest.mark.asyncio
async def test_no_request_means_no_polling(monkeypatch):
    monkeypatch.setattr(scan_exec, "current_request", lambda: None)
    polled = []
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.001)

    def work():
        return "done"

    assert await scan_exec.run_scan(work) == "done"
    assert polled == []
```

`tests/test_scan_exec_clickhouse.py` (real ClickHouse — the suite already refuses to start without it):

```python
"""A disconnect kills the ClickHouse query the scan is running (#300)."""

from __future__ import annotations

import time

import pytest

from vestigo.api import scan_exec
from vestigo.db import _scan
from vestigo.db.clickhouse import ClickHouseStore


class _Request:
    def __init__(self, after: float):
        self.t0 = time.monotonic()
        self.after = after

    async def is_disconnected(self) -> bool:
        return time.monotonic() - self.t0 > self.after


@pytest.mark.asyncio
async def test_disconnect_kills_the_running_query(monkeypatch):
    store = ClickHouseStore()
    monkeypatch.setattr(scan_exec, "current_request", lambda: _Request(after=1.0))
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.2)
    monkeypatch.setattr(scan_exec, "_client", lambda: store.client)
    tags: list[str] = []

    def work():
        ctx = _scan.scan_context()
        tags.append(_scan.scan_log_comment(ctx.token))
        # Same clause every real scan carries, so the tag rides log_comment.
        store.client.query(
            f"SELECT sleepEachRow(1) FROM numbers(60) {_scan.heavy_scan_settings()}, max_block_size = 1"
        )

    t0 = time.monotonic()
    with pytest.raises(scan_exec.ScanCancelledResponse):
        await scan_exec.run_scan(work)
    assert time.monotonic() - t0 < 15
    # Give the ASYNC kill a moment, then the process must be gone.
    for _ in range(50):
        rows = store.client.query(
            "SELECT count() FROM system.processes WHERE Settings['log_comment'] = {t:String}",
            parameters={"t": tags[0]},
        ).result_rows
        if rows[0][0] == 0:
            break
        time.sleep(0.1)
    assert rows[0][0] == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scan_exec.py tests/test_scan_exec_clickhouse.py -q`
Expected: FAIL — `ModuleNotFoundError: vestigo.api.scan_exec`.

- [ ] **Step 3: Implement**

`src/vestigo/api/request_context.py`:

```python
"""The current HTTP request, reachable from anywhere in its call chain.

A pure ASGI middleware (not ``BaseHTTPMiddleware`` — see
``main.AuthAuditMiddleware`` for why) binds the :class:`Request` into a
contextvar for the duration of the request. ``scan_exec.run_scan`` reads it
to watch for a client disconnect without every endpoint having to thread a
``Request`` parameter down to the query layer.
"""

from __future__ import annotations

from contextvars import ContextVar

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

_request_var: ContextVar[Request | None] = ContextVar("vestigo_request", default=None)


def current_request() -> Request | None:
    return _request_var.get()


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _request_var.set(Request(scope, receive))
        try:
            await self.app(scope, receive, send)
        finally:
            _request_var.reset(token)
```

`src/vestigo/api/scan_exec.py`:

```python
"""Run a blocking ClickHouse scan for a request: cancellable, and honest when busy.

Every request-driven scan (charts, detectors, log templates) passes through
:func:`run_scan` instead of a bare ``run_in_threadpool``. It binds a
``ScanContext`` so the query is tagged, watches the request for a disconnect
while the thread waits or runs, and on disconnect kills the tagged query and
lets the parked acquire notice its cancel flag — so a page reload frees its
gate slot and its ClickHouse process within about a second instead of leaving
nine ghosts holding the lane (#300).

A foreground scan that could not get a slot in its bounded wait surfaces as a
503 with the queue depth and ``Retry-After``, which the UI renders as
"waiting behind N scans" and retries; that is the whole difference between a
spinner and an answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from vestigo.api.request_context import current_request
from vestigo.db._scan import ScanBusy, ScanCancelled, bind_scan_context, kill_scan_queries
from vestigo.db.clickhouse import ClickHouseStore

logger = logging.getLogger(__name__)

#: How often the request is checked for a disconnect while its scan runs.
_POLL_SECONDS = 1.0
_RETRY_AFTER_SECONDS = 5


class ScanBusyResponse(HTTPException):
    """503: the foreground lane stayed full for the whole bounded wait."""

    def __init__(self, exc: ScanBusy) -> None:
        self.queued_ahead = exc.ahead
        super().__init__(
            status_code=503, detail=str(exc), headers={"Retry-After": str(_RETRY_AFTER_SECONDS)}
        )


class ScanCancelledResponse(HTTPException):
    """499 (nginx's client-closed-request): nobody is listening for this answer."""

    def __init__(self) -> None:
        super().__init__(status_code=499, detail="client disconnected; scan cancelled")


def install(app: FastAPI) -> None:
    """Register the handler that puts ``queued_ahead`` beside ``detail`` in the body."""

    @app.exception_handler(ScanBusyResponse)
    async def _busy(_request: Request, exc: ScanBusyResponse) -> JSONResponse:
        return JSONResponse(
            {"detail": exc.detail, "queued_ahead": exc.queued_ahead},
            status_code=503,
            headers=exc.headers,
        )


_store: ClickHouseStore | None = None


def _client() -> Any:
    global _store
    if _store is None:
        _store = ClickHouseStore()
    return _store.client


def _kill(token: str) -> None:
    kill_scan_queries(_client(), token)


async def run_scan(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run *fn* in the threadpool under a scan context, watching the client."""
    request = current_request()
    with bind_scan_context() as ctx:
        task = asyncio.ensure_future(run_in_threadpool(fn, *args, **kwargs))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_POLL_SECONDS if request else None)
                if done:
                    break
                if request is not None and await request.is_disconnected():
                    ctx.cancelled.set()
                    await run_in_threadpool(_kill, ctx.token)
                    # The thread ends with ScanCancelled (parked) or a 394 from
                    # ClickHouse (running); either way it releases its slot.
                    try:
                        await task
                    except Exception:  # noqa: BLE001 — the client is gone
                        logger.debug("scan %s ended after cancel", ctx.token, exc_info=True)
                    raise ScanCancelledResponse()
            return task.result()
        except ScanBusy as exc:
            raise ScanBusyResponse(exc) from exc
        except ScanCancelled:
            raise ScanCancelledResponse() from None
```

`src/vestigo/api/main.py`: import `RequestContextMiddleware` and `scan_exec`; after `app.add_middleware(AuthAuditMiddleware)` add `app.add_middleware(RequestContextMiddleware)` and `scan_exec.install(app)`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_scan_exec.py tests/test_scan_exec_clickhouse.py -q`
Expected: PASS (the ClickHouse test takes ~2 s).

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/api/request_context.py src/vestigo/api/scan_exec.py src/vestigo/api/main.py tests/test_scan_exec.py tests/test_scan_exec_clickhouse.py
git commit -m "feat(api): run_scan — disconnect-aware scans with KILL QUERY and a 503 busy answer (#300)"
```

---

### Task 5: Wire `run_scan` at the choke points; agent tool maps busy

**Files:**
- Modify: `src/vestigo/api/routers/events.py:160-175` (`_run_regex_guarded`), the 8 `run_in_threadpool(` calls inside `_run_stat_detector` (lines ~2141-2328)
- Modify: `src/vestigo/api/routers/analysis.py:452` (`_run_log_templates`)
- Modify: `src/vestigo/agent/chart_exec.py` (all `run_in_threadpool(service.` sites)
- Test: `tests/test_events_router.py`, `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `scan_exec.run_scan`, `ScanBusyResponse`.

- [ ] **Step 1: Write failing tests**

`tests/test_events_router.py` — add:

```python
@pytest.mark.asyncio
async def test_regex_guard_runs_scans_through_run_scan(monkeypatch):
    """Every chart/detector endpoint must be cancellable and busy-aware (#300)."""
    from vestigo.api import scan_exec

    calls = []

    async def fake_run_scan(fn, *args, **kwargs):
        calls.append(fn)
        return "ran"

    monkeypatch.setattr(events, "run_scan", fake_run_scan)
    assert await events._run_regex_guarded(False, lambda: None) == "ran"
    assert len(calls) == 1
    assert scan_exec.run_scan is not fake_run_scan  # the module itself is untouched


@pytest.mark.asyncio
async def test_busy_scan_surfaces_as_503_not_500(monkeypatch):
    from vestigo.api import scan_exec
    from vestigo.db._scan import ScanBusy

    def busy():
        raise ScanBusy(ahead=4, wait=30.0)

    monkeypatch.setattr(scan_exec, "current_request", lambda: None)
    with pytest.raises(scan_exec.ScanBusyResponse):
        await events._run_regex_guarded(False, busy)
```

`tests/test_agent_tools.py` — add (find the existing `execute_chart_spec` test helper/fixture in that file and reuse its `scope`/`service` construction):

```python
@pytest.mark.asyncio
async def test_chart_tool_reports_a_busy_lane_as_a_tool_error(monkeypatch, ...):
    from vestigo.db._scan import ScanBusy

    def busy(*a, **k):
        raise ScanBusy(ahead=2, wait=30.0)

    monkeypatch.setattr(service, "histogram", busy)
    with pytest.raises(ValueError, match="busy"):
        await execute_chart_spec(scope, spec_for_time_chart, service=service, ...)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_events_router.py -q -k "run_scan or busy"`
Expected: FAIL — `AttributeError: module ... has no attribute 'run_scan'`.

- [ ] **Step 3: Implement**

`events.py`: `from vestigo.api.scan_exec import run_scan`; in `_run_regex_guarded` replace `return await run_in_threadpool(fn, *args, **kwargs)` with `return await run_scan(fn, *args, **kwargs)`; in `_run_stat_detector`, replace each of the 8 `await run_in_threadpool(` with `await run_scan(` (leave every other `run_in_threadpool` in the file alone — `list_events` etc. are not gated). Add to the docstring of `_run_regex_guarded`: "Runs through ``scan_exec.run_scan`` so the scan is cancelled when the client disconnects and a full foreground lane answers 503 (#300)."

`analysis.py`: `from vestigo.api.scan_exec import run_scan`; in `_run_log_templates` replace `await run_in_threadpool(` with `await run_scan(`. `get_analysis_findings` needs no change: `ScanCancelledResponse` propagates before `cache_put`.

`chart_exec.py`: add
```python
from vestigo.db._scan import ScanBusy


async def _scan(fn, *args):
    """Run a chart aggregation; a busy foreground lane is a tool error, not a crash."""
    try:
        return await run_in_threadpool(fn, *args)
    except ScanBusy as exc:
        raise ValueError(f"{exc}. Tell the analyst the chart lane is busy and try again.") from exc
```
and replace every `await run_in_threadpool(service.…` with `await _scan(service.…`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_events_router.py tests/test_agent_tools.py tests/test_analysis_cache.py tests/test_viz_router.py tests/test_timeline_muted_methods_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vestigo/api/routers/events.py src/vestigo/api/routers/analysis.py src/vestigo/agent/chart_exec.py tests/test_events_router.py tests/test_agent_tools.py
git commit -m "feat(api): charts, detectors and templates run through run_scan (#300)"
```

---

### Task 6: End-to-end — histogram unaffected by a full heavy gate; 503 when foreground is full

**Files:**
- Test: `tests/test_scan_admission_api_clickhouse.py` (new)

**Interfaces:** consumes the `client` fixture (`tests/conftest.py:345`), `as_admin`, and the ingestion path used by `tests/test_demo_detector_coverage_clickhouse.py` (read that file's setup for how a timeline with events is created through the API; reuse the same helper).

- [ ] **Step 1: Write the tests**

```python
"""The foreground lane is independent of the heavy gate, and says so when full (#300)."""

from __future__ import annotations

import pytest

from vestigo.db import _scan, queries
from tests.conftest import as_admin


def _hold(gate, n):
    taken = 0
    while n and gate.acquire(blocking=False):
        taken += 1
        n -= 1
    return taken


@pytest.fixture
def timeline(client, admin_bootstrap):
    """A case + timeline with a handful of ingested events.

    Build it exactly the way tests/test_demo_detector_coverage_clickhouse.py
    builds its corpus (copy that helper's calls here); return (case_id, timeline_id).
    """
    ...


def test_histogram_does_not_wait_for_the_heavy_gate(client, admin_bootstrap, timeline):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = timeline
    taken = _hold(_scan.HEAVY_SCAN_GATE, _scan._GATE_CONCURRENCY)
    try:
        res = client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/histogram")
        assert res.status_code == 200, res.text
        assert "buckets" in res.json()
    finally:
        for _ in range(taken):
            _scan.HEAVY_SCAN_GATE.release()


def test_histogram_answers_busy_when_the_foreground_lane_is_full(
    client, admin_bootstrap, timeline, monkeypatch
):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = timeline
    monkeypatch.setattr(queries, "FOREGROUND_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.02)
    taken = _hold(_scan.FOREGROUND_SCAN_GATE, _scan._FOREGROUND_CONCURRENCY)
    try:
        res = client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/histogram")
        assert res.status_code == 503, res.text
        assert res.headers["retry-after"] == "5"
        assert res.json()["queued_ahead"] == 0
    finally:
        for _ in range(taken):
            _scan.FOREGROUND_SCAN_GATE.release()
```

Replace the `...` in the fixture with the concrete case/source/timeline creation copied from the demo-coverage test's setup (it goes through `register_source_for_ingest` / the ingest endpoint; keep the corpus tiny).

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_scan_admission_api_clickhouse.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scan_admission_api_clickhouse.py
git commit -m "test(api): histogram ignores a full heavy gate and reports a full foreground lane (#300)"
```

---

### Task 7: Frontend — `ApiError` busy fields, `busyRetry`, waiting copy, admin card

**Files:**
- Modify: `frontend/src/api/client.ts:13-30, 60-80`
- Modify: `frontend/src/lib/queryClient.ts`
- Modify: `frontend/src/components/explorer/TimelineHistogram.tsx:137-143` and its spinner/empty render
- Modify: `frontend/src/components/viz/FieldHistogramModal.tsx:66-105`
- Modify: `frontend/src/components/viz/ChartCanvas.tsx:82-100`
- Modify: `frontend/src/api/types.ts:1078-1097`, `frontend/src/components/admin/ScanBudgetCard.tsx`
- Test: `frontend/src/test/client.test.ts`, `frontend/src/test/busyRetry.test.ts` (new), `frontend/src/test/scanBudgetCard.test.tsx`

**Interfaces:**
- `ApiError` gains `queuedAhead?: number` and `retryAfterMs?: number`.
- `lib/queryClient.ts` exports `isScanBusy(error: unknown): error is ApiError`, `busyRetry: { retry: (count, err) => boolean; retryDelay: (count, err) => number }` and `busyMessage(error: unknown): string | null` ("Waiting behind N scans…").

- [ ] **Step 1: Write failing tests**

`frontend/src/test/client.test.ts` — add:
```ts
import { apiErrorFromBody } from "@/api/client";

describe("apiErrorFromBody", () => {
  it("carries queued_ahead and Retry-After from a busy 503", () => {
    const err = apiErrorFromBody(
      503,
      "Service Unavailable",
      JSON.stringify({ detail: "scan lane busy: 3 waiting ahead", queued_ahead: 3 }),
      "/x",
      { "retry-after": "5" },
    );
    expect(err.status).toBe(503);
    expect(err.queuedAhead).toBe(3);
    expect(err.retryAfterMs).toBe(5000);
  });
});
```

`frontend/src/test/busyRetry.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { ApiError } from "@/api/client";
import { busyMessage, busyRetry, isScanBusy } from "@/lib/queryClient";

function busy(ahead: number): ApiError {
  const e = new ApiError(503, "scan lane busy");
  e.queuedAhead = ahead;
  e.retryAfterMs = 5000;
  return e;
}

describe("busyRetry", () => {
  it("keeps retrying a busy lane and stops on anything else", () => {
    expect(isScanBusy(busy(2))).toBe(true);
    expect(isScanBusy(new ApiError(503, "down"))).toBe(false);
    expect(busyRetry.retry(7, busy(2))).toBe(true);
    expect(busyRetry.retry(1, new ApiError(500, "x"))).toBe(false);
    expect(busyRetry.retry(0, new ApiError(500, "x"))).toBe(true); // default single retry kept
  });
  it("honours Retry-After and names the queue", () => {
    expect(busyRetry.retryDelay(3, busy(2))).toBe(5000);
    expect(busyMessage(busy(2))).toBe("Waiting behind 2 scans…");
    expect(busyMessage(busy(0))).toBe("Waiting for a scan slot…");
    expect(busyMessage(new Error("x"))).toBeNull();
  });
});
```

`frontend/src/test/scanBudgetCard.test.tsx` — add `foreground: { concurrency: 4, per_query_bytes: 0.5 * 1024 ** 3 }` to `base` and a test:
```ts
  it("discloses the foreground chart lane", () => {
    render(<ScanBudgetCard budget={base} />);
    expect(screen.getByText(/4 chart queries at 0\.5 GiB each/i)).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && npm run test -- client busyRetry scanBudgetCard`
Expected: FAIL (missing exports / property).

- [ ] **Step 3: Implement**

`client.ts`:
```ts
export class ApiError extends Error {
  status: number;
  /** Set on a 503 from a full scan lane: callers parked ahead of this request. */
  queuedAhead?: number;
  /** `Retry-After` in milliseconds, when the server sent one. */
  retryAfterMs?: number;
  ...
}

export function apiErrorFromBody(
  status: number,
  statusText: string,
  bodyText: string,
  path: string,
  headers?: Record<string, string> | Headers,
): ApiError {
  ...
  const err = new ApiError(status, detail);
  try {
    const json = JSON.parse(bodyText) as { queued_ahead?: unknown };
    if (typeof json?.queued_ahead === "number") err.queuedAhead = json.queued_ahead;
  } catch { /* non-JSON */ }
  const ra = headers instanceof Headers ? headers.get("retry-after") : headers?.["retry-after"];
  if (ra && /^\d+$/.test(ra)) err.retryAfterMs = Number(ra) * 1000;
  return err;
}
```
Pass `res.headers` from `checkResponse` and the XHR path's `getResponseHeader("retry-after")` (build a `{ "retry-after": value }` record).

`queryClient.ts` — add above `queryClient`:
```ts
export function isScanBusy(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 503 && error.queuedAhead !== undefined;
}

/** A full scan lane is "still waiting", never "failed": keep asking at the
 * server's pace. Everything else keeps the app's single retry. */
export const busyRetry = {
  retry: (count: number, error: unknown): boolean => (isScanBusy(error) ? true : count < 1),
  retryDelay: (count: number, error: unknown): number =>
    isScanBusy(error) ? (error.retryAfterMs ?? 5000) : Math.min(1000 * 2 ** count, 30_000),
};

export function busyMessage(error: unknown): string | null {
  if (!isScanBusy(error)) return null;
  const n = error.queuedAhead ?? 0;
  return n > 0 ? `Waiting behind ${n} scan${n === 1 ? "" : "s"}…` : "Waiting for a scan slot…";
}
```
and in `QueryCache.onError`: `if (query.meta?.silentError || isUnauthorized(error) || isScanBusy(error)) return;` — a busy lane must not toast.

`TimelineHistogram.tsx`: spread `...busyRetry` into the `useQuery` options, read `error` from the result, and where the component renders its loading state show `busyMessage(error) ?? <existing spinner>` (a small muted `<p>` with the text). `FieldHistogramModal.tsx`: same for all three queries; render the message for `histogramQuery`/`termsQuery` in their loading slots. `ChartCanvas.tsx`: spread `...busyRetry`; in the `chartQuery.isLoading` block render `busyMessage(chartQuery.error)` beside the spinner when non-null. Note: with `retry: true` the query stays in `isLoading`/`isFetching`, and `error` is only populated after retries stop — so read `chartQuery.failureReason` (TanStack v5) for the in-flight error: `busyMessage(chartQuery.failureReason)`. Use `failureReason` in all three components.

`types.ts` `ScanBudget`: add `foreground: { concurrency: number; per_query_bytes: number };`. `ScanBudgetCard.tsx`: after the threads paragraph add
```tsx
        <p>
          Charts have their own lane: {budget.foreground.concurrency} chart queries at{" "}
          {gib(budget.foreground.per_query_bytes)} each, never queued behind a detector sweep.
        </p>
```

- [ ] **Step 4: Run**

Run: `cd frontend && npm run typecheck && npm run lint && npm run test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/lib/queryClient.ts frontend/src/components/explorer/TimelineHistogram.tsx frontend/src/components/viz/FieldHistogramModal.tsx frontend/src/components/viz/ChartCanvas.tsx frontend/src/api/types.ts frontend/src/components/admin/ScanBudgetCard.tsx frontend/src/test/client.test.ts frontend/src/test/busyRetry.test.ts frontend/src/test/scanBudgetCard.test.tsx
git commit -m "feat(frontend): a busy scan lane waits visibly instead of spinning (#300)"
```

---

### Task 8: Sizing calculator, reference docs, roadmap and progress

**Files:**
- Modify: `scripts/gen_sizing_constants.py:34-40`, `docs/sizing/sizing-constants.json`, `docs/sizing/index.html:265-335`
- Modify: `docs/ANOMALY_DETECTION.md:70-100`, `docs/DEPLOYMENT.md` §Resource sizing, `docs/ROADMAP.md:14-20`, `docs/PROGRESS.md` (top), `.env.example:250-280`
- Test: `tests/test_sizing_constants.py`

- [ ] **Step 1: Write the failing test** — in `tests/test_sizing_constants.py::test_constants_carry_what_the_page_needs` add `assert data["foreground_concurrency"] == 4`; in `test_page_makes_no_external_requests`' neighbour add a new test:
```python
def test_page_divides_by_concurrency_plus_one():
    html = (Path(__file__).parent.parent / "docs" / "sizing" / "index.html").read_text()
    assert "scanTotal / (concurrency + 1)" in html
    assert "foreground per-chart cap" in html
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_sizing_constants.py -q` → FAIL.

- [ ] **Step 3: Implement**

`gen_sizing_constants.py`: import `_FOREGROUND_CONCURRENCY` from `vestigo.db._scan`; add `"foreground_concurrency": _FOREGROUND_CONCURRENCY,` to `build()`. Regenerate: `uv run python scripts/gen_sizing_constants.py`.

`docs/sizing/index.html`:
```js
  // One heavy slot is reserved for the foreground class (charts), so the
  // heavy cap divides by concurrency + 1 — see docs/ANOMALY_DETECTION.md.
  const perQuery = Math.floor(scanTotal / (concurrency + 1));
  const perChart = Math.floor(perQuery / K.foreground_concurrency);
```
Rows: change the budget row text to `shared across ${concurrency} concurrent scans plus a chart lane.`; per-query "why" to `budget ÷ (concurrency + 1), as max_memory_usage on every heavy scan. One query fails instead of the server.`; add after it:
```js
  row(mem, "→ foreground per-chart cap", gib(perChart),
    `${K.foreground_concurrency} chart queries share the reserved slot, in their own lane — a histogram never waits behind a detector sweep.`);
```
Also update the `VESTIGO_STAT_SCAN_CONCURRENCY` "why" to "Heavy scans admitted at once; the rest queue. Charts use a separate lane. Also the divisor (+1) on the memory budget, so it needs a restart."

`docs/ANOMALY_DETECTION.md` "Query-cost discipline": amend the `HEAVY_SCAN_SETTINGS` bullet — `max_memory_usage` = total budget / (concurrency + 1) — and add a bullet:

> - **Two admission classes (#300).** `HEAVY_SCAN_GATE` (N = `VESTIGO_STAT_SCAN_CONCURRENCY`) admits detectors, Sigma, the value inventory and the enrichment rewrite; `FOREGROUND_SCAN_GATE` (4 slots, a constant) admits the chart aggregations an analyst is looking at — histogram, top terms, numeric stats, compare layers, punchcard, pivot, scatter. The heavy cap divides the budget by N + 1 and the reserved slot is split four ways for charts, so both gates fully admitted still fit. A chart waits at most 30 s for a slot and then answers 503 with `queued_ahead` and `Retry-After`; detectors queue indefinitely. Every request-driven scan carries `log_comment = 'vestigo-scan/<token>'`; when the request disconnects the app sets the scan's cancel flag (a parked acquire notices within a second) and issues `KILL QUERY WHERE Settings['log_comment'] = …`, so a page reload releases its slots instead of leaving orphaned sweeps ahead of the next one. Background jobs and the CLI bind no context and are unaffected.

`docs/DEPLOYMENT.md` §Resource sizing: after the `/api/health` sentence add a paragraph: the `scan_budget.foreground` block reports the chart lane (4 queries, cap = heavy cap / 4); raising `VESTIGO_STAT_SCAN_CONCURRENCY` widens the heavy lane and shrinks every cap (divisor N + 1) — it does not change how charts are admitted.

`.env.example` line ~256: `budget / (VESTIGO_STAT_SCAN_CONCURRENCY + 1) as its max_memory_usage — the extra slot is the chart lane`.

`docs/ROADMAP.md`: delete the `#300` bullet and the "Open defects" heading if it is now empty; fix the intro line that mentions it.

`docs/PROGRESS.md`: new top entry `## Session 189 — 2026-08-26: scan admission classes (#300)` summarising the three defects and the fix, pointing at the spec and plan, and noting the double-scan half had already shipped in `6c69855`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_sizing_constants.py -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_sizing_constants.py docs/sizing docs/ANOMALY_DETECTION.md docs/DEPLOYMENT.md docs/ROADMAP.md docs/PROGRESS.md .env.example tests/test_sizing_constants.py
git commit -m "docs(scan): admission classes in the calculator, reference docs and roadmap (#300)"
```

---

### Task 9: Full verification

- [ ] **Step 1:** `uv run pytest -q` (full suite, needs the compose services). Expected: all pass, including `tests/test_demo_detector_coverage_clickhouse.py`.
- [ ] **Step 2:** `uv run ruff check . && uv run ruff format --check .` → clean.
- [ ] **Step 3:** `cd frontend && npm run typecheck && npm run lint && npm run test && npm run build` → clean.
- [ ] **Step 4:** Manual check with the `verify` skill: start the app, open a timeline, hold the heavy gate by launching an Investigate sweep on the demo case, open the per-value histogram — it renders; reload mid-sweep and watch `SELECT count() FROM system.processes WHERE Settings['log_comment'] LIKE 'vestigo-scan/%'` drop to the new sweep's count within ~2 s. Check `/api/health` → `scan_budget.foreground`.
- [ ] **Step 5:** Open the PR from `fix/300-scan-admission-classes` (`gh pr create`), body ending with the Claude Code trailer. Do not merge — that is the user's call.
