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
