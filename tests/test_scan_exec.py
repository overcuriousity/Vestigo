"""run_scan: threadpool scans that notice a gone client and a busy lane (#300)."""

from __future__ import annotations

import threading

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
