"""Cancellable admission and query tagging for scans (db/_scan.py, #300)."""

from __future__ import annotations

import gc
import threading
import time

import pytest

from vestigo.db import _scan
from vestigo.db._scan import (
    ScanBusy,
    ScanCancelled,
    acquire_scan_slot,
    bind_scan_context,
    foreground_wait_seconds,
    scan_context,
    scan_fanout,
    unbounded_foreground_wait,
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


# ── Queue bookkeeping the analyst is shown (PR #305 review) ──────────────────


def test_waiting_counts_do_not_outlive_their_gate():
    """A gate that has been collected must not lend its count to the next one.

    The counter used to be a module-level `dict[int, int]` keyed by `id(gate)`
    whose entries were never removed. CPython reuses an id once its object is
    gone, so a throwaway gate — a test substituting `FOREGROUND_SCAN_GATE`, a
    per-request gate — could hand a later gate a dead one's depth, which is the
    number the UI renders as "waiting behind N scans".
    """
    gc.collect()  # settle entries other tests' gates left behind
    before = len(_scan._waiting)
    dead = _held(1)
    _scan._adjust_waiting(dead, +1)
    assert _scan._waiting_count(dead) == 1
    assert len(_scan._waiting) == before + 1

    del dead
    gc.collect()
    assert len(_scan._waiting) == before, "the entry outlived the gate it counted"


def test_ahead_is_counted_when_the_wait_expires_not_at_entry(monkeypatch):
    """The depth reported is the depth *now*, not up to `wait` seconds ago.

    A chart that entered behind a parked sweep and timed out anyway used to
    report the entry queue, so the UI could say "waiting behind 1 scan" about a
    queue that had drained entirely.
    """
    gate = _held(1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)
    parked = threading.Event()

    def park():
        parked.set()
        with pytest.raises(ScanBusy), acquire_scan_slot(gate, wait=0.05):
            pass

    t = threading.Thread(target=park)
    t.start()
    parked.wait()
    time.sleep(0.02)

    # Enters behind the parked caller; that one gives up long before we do.
    with pytest.raises(ScanBusy) as info, acquire_scan_slot(gate, wait=0.4):
        pass
    t.join()
    assert info.value.ahead == 0, "the queue it named had drained"


def _max_memory(clause: str) -> int:
    return int(clause.split("max_memory_usage = ")[1].split(",")[0].strip())


def test_a_fan_out_splits_its_slot_share_rather_than_doubling_it(monkeypatch):
    """The over-commit factor is the fan-out width, and no gate sizing absorbs it.

    Both gates fully admitted have to fit under one budget; a chart that puts
    two queries in flight at the full per-query cap commits twice what its
    slot reserved (#305).
    """
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 8 << 30)
    solo = _max_memory(_scan.foreground_scan_settings())
    with scan_fanout(2):
        assert _max_memory(_scan.foreground_scan_settings()) == solo // 2
        # Nested waves multiply — the violin chart's two-by-two.
        with scan_fanout(2):
            assert _max_memory(_scan.foreground_scan_settings()) == solo // 4
    assert _max_memory(_scan.foreground_scan_settings()) == solo


def test_the_heavy_class_splits_a_fan_out_the_same_way(monkeypatch):
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 8 << 30)
    solo = _max_memory(_scan.heavy_scan_settings())
    with scan_fanout(2):
        assert _max_memory(_scan.heavy_scan_settings()) == solo // 2


def test_a_fan_out_cap_never_reaches_zero(monkeypatch):
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 8 << 30)
    with scan_fanout(1 << 40):
        assert _max_memory(_scan.foreground_scan_settings()) == 1


def test_the_reserved_lane_still_covers_a_fully_admitted_gate(monkeypatch):
    """Foreground queries in flight, fan-out included, stay inside the reservation."""
    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 8 << 30)
    reserved = _scan.detect_scan_memory_budget() * _scan._FOREGROUND_SLOTS
    with scan_fanout(2):
        per_query = _max_memory(_scan.foreground_scan_settings())
    in_flight = per_query * 2 * _scan._FOREGROUND_CONCURRENCY
    assert in_flight <= reserved


def test_a_background_caller_waits_instead_of_being_told_busy():
    assert foreground_wait_seconds(30.0) == 30.0
    with unbounded_foreground_wait():
        assert foreground_wait_seconds(30.0) is None
    assert foreground_wait_seconds(30.0) == 30.0


def test_the_fan_out_width_reaches_the_worker_threads(monkeypatch):
    """`_run_parallel` copies the context into each thread — the width goes too."""
    from vestigo.db.queries import EventQueryService

    monkeypatch.setattr(_scan, "detect_local_memory_total", lambda: 8 << 30)
    service = EventQueryService(store=object())
    solo = _max_memory(_scan.foreground_scan_settings())
    caps = service._run_parallel(
        lambda: _max_memory(_scan.foreground_scan_settings()),
        lambda: _max_memory(_scan.foreground_scan_settings()),
    )
    assert caps == [solo // 2, solo // 2]
    assert _max_memory(_scan.foreground_scan_settings()) == solo, "restored for the caller"
