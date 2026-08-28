"""run_scan: threadpool scans that notice a gone client and a busy lane (#300)."""

from __future__ import annotations

import asyncio
import threading

import anyio.to_thread
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from vestigo.api import scan_exec
from vestigo.api.request_context import RequestContextMiddleware, current_request
from vestigo.db import _scan
from vestigo.db._scan import ScanBusy, scan_context


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
    """Without a request there is nobody to poll: the scan just runs to completion."""
    monkeypatch.setattr(scan_exec, "current_request", lambda: None)
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.001)

    def work():
        return "done"

    assert await scan_exec.run_scan(work) == "done"


# ── Cancel paths that must not leave a scan (or a 500) behind (PR #305 review) ──


@pytest.mark.asyncio
async def test_an_unreachable_clickhouse_still_answers_499(monkeypatch):
    """The kill is best effort *including the connect it needs*.

    The store behind `_client()` is built lazily on the first disconnect, so a
    ClickHouse that is down at that moment used to raise out of `_kill` — the
    handler died with a 500 instead of the intended 499, and the scan task was
    abandoned mid-await with nobody to retrieve its result.
    """
    req = _Request(disconnect_after=1)
    monkeypatch.setattr(scan_exec, "current_request", lambda: req)
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)

    def _no_client():
        raise ConnectionError("clickhouse is away")

    monkeypatch.setattr(scan_exec, "_client", _no_client)
    gate = threading.BoundedSemaphore(1)
    gate.acquire()

    def work():
        with _scan.acquire_scan_slot(gate, wait=None):
            raise AssertionError("must not be admitted")

    with pytest.raises(scan_exec.ScanCancelledResponse):
        await scan_exec.run_scan(work)
    gate.release()
    assert gate.acquire(blocking=False), "the parked scan noticed the flag and left"
    gate.release()


@pytest.mark.asyncio
async def test_cancelling_the_request_coroutine_still_kills_the_scan(monkeypatch):
    """Server shutdown (or a failing `gather` sibling) cancels the awaiter.

    Without a handler for that, the threadpool scan runs on holding its gate
    slot and its ClickHouse process with nobody left to set the flag or issue
    the KILL — exactly the orphan this module exists to prevent.
    """
    monkeypatch.setattr(scan_exec, "current_request", lambda: None)
    killed: list[str] = []
    done = threading.Event()
    monkeypatch.setattr(scan_exec, "_kill", lambda token: (killed.append(token), done.set()))
    gate = threading.BoundedSemaphore(1)
    gate.acquire()
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.01)
    admitted = threading.Event()

    def work():
        admitted.set()
        with _scan.acquire_scan_slot(gate, wait=None):
            raise AssertionError("must not be admitted")

    task = asyncio.ensure_future(scan_exec.run_scan(work))
    await asyncio.to_thread(admitted.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(done.wait, 5), "the KILL went out without the loop"
    assert len(killed) == 1
    gate.release()
    assert await asyncio.to_thread(gate.acquire, True, 5), "the parked scan left too"
    gate.release()


@pytest.mark.asyncio
async def test_disconnect_kills_even_when_the_threadpool_is_starved(monkeypatch):
    """The KILL must not queue behind the scans it is trying to free.

    The situation a disconnect exists for is a full lane: other requests'
    scans, parked in `acquire_scan_slot`, each hold one of anyio's threadpool
    tokens for the whole of their wait. A KILL sent through
    `run_in_threadpool` waits for a token none of them will give up, the
    running scan never gets its 394, `run_scan` never finishes cancelling,
    and the orphan survives (#305).
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    limiter.total_tokens = 2
    foreign_holds = threading.Event()
    release_foreign = threading.Event()
    killed_evt = threading.Event()
    killed: list[str] = []
    monkeypatch.setattr(scan_exec, "_kill", lambda token: (killed.append(token), killed_evt.set()))

    def _foreign_scan() -> None:  # another request's scan, parked on its gate
        foreign_holds.set()
        release_foreign.wait(10)

    foreign = asyncio.ensure_future(scan_exec.run_in_threadpool(_foreign_scan))
    await asyncio.to_thread(foreign_holds.wait, 5)

    req = _Request(disconnect_after=1)
    monkeypatch.setattr(scan_exec, "current_request", lambda: req)
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.01)

    def work():  # a *running* scan: it ends when ClickHouse kills it
        killed_evt.wait(30)
        raise _scan.ScanCancelled("killed")

    try:
        # Fails as a timeout if the KILL queued behind the foreign scan: the
        # only thing that can free a token is the answer this is waiting for.
        with pytest.raises(scan_exec.ScanCancelledResponse):
            await asyncio.wait_for(scan_exec.run_scan(work), timeout=5)
        assert len(killed) == 1
    finally:
        release_foreign.set()
        await foreign
        limiter.total_tokens = original
