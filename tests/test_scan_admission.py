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
