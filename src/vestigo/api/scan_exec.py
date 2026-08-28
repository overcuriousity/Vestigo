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
import threading
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

#: Guards the lazy build of :data:`_store`. ``_kill`` runs on a bare daemon
#: thread (see ``_kill_detached``), and the case this path exists for — a full
#: lane — is exactly the one that produces simultaneous disconnects. Without
#: the lock each of them builds its own store and all but one is leaked, on
#: the very path that is meant to be cheap.
_store_lock = threading.Lock()


def _client() -> Any:
    global _store
    with _store_lock:
        if _store is None:
            _store = ClickHouseStore()
        return _store.client


def _kill(token: str) -> None:
    """Best-effort KILL for *token*, including the connect that reaches it.

    ``kill_scan_queries`` swallows its own failures, but the store behind
    ``_client()`` is built lazily on the *first* disconnect — so a ClickHouse
    that is restarting, rotated credentials or an exhausted socket pool used
    to raise out of here instead. On the cancel path that turned a 499 into a
    500 and left the scan task with nobody to retrieve its result. A scan we
    could not kill simply finishes on its own, as it did before cancellation
    existed.
    """
    try:
        client = _client()
    except Exception:  # noqa: BLE001 — best effort by design
        logger.warning("cannot reach ClickHouse to kill scan %s; it will finish", token)
        logger.debug("scan kill client failed", exc_info=True)
        return
    kill_scan_queries(client, token)


def _discard(task: asyncio.Future) -> None:
    """Retrieve the result of a scan nobody is waiting for any more.

    Without this, a task abandoned on the cancellation path ends with
    ``ScanCancelled`` and asyncio logs "Task exception was never retrieved"
    on every disconnect.
    """

    def _done(finished: asyncio.Future) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            logger.debug("scan ended after cancel", exc_info=exc)

    task.add_done_callback(_done)


def _kill_detached(token: str) -> None:
    """Send the KILL for *token* on a plain daemon thread.

    Never ``run_in_threadpool``: the scenario a cancel exists for is a full
    lane, and every scan parked in ``acquire_scan_slot`` is holding one of
    anyio's limited threadpool tokens for the whole of its wait. Queueing the
    KILL behind them is how it goes out minutes late or not at all — and
    until it does, ``run_scan`` cannot finish cancelling, so the orphan this
    path exists to kill survives. A raw thread needs neither a token nor a
    running loop, which is also what makes it safe while one is being torn
    down.
    """
    threading.Thread(
        target=_kill, args=(token,), name=f"scan-kill-{token[:8]}", daemon=True
    ).start()


def _cancel_detached(ctx: Any, task: asyncio.Future) -> None:
    """Cancel a scan without awaiting anything — safe while unwinding.

    Used from the ``CancelledError`` path, where the event loop is being torn
    down (shutdown, a failing ``gather`` sibling) and any further ``await``
    may never resume. The scan thread then fails with ClickHouse's 394
    (running) or notices the flag (parked) and releases its gate slot either
    way. Without this, the scan outlives the request holding both — the
    orphan the whole cancellation path exists to prevent.
    """
    ctx.cancelled.set()
    _discard(task)
    _kill_detached(ctx.token)


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
                    _kill_detached(ctx.token)
                    # The thread ends with ScanCancelled (parked) or a 394 from
                    # ClickHouse (running); either way it releases its slot.
                    try:
                        await task
                    except Exception:  # noqa: BLE001 — the client is gone
                        logger.debug("scan %s ended after cancel", ctx.token, exc_info=True)
                    raise ScanCancelledResponse()
            return task.result()
        except asyncio.CancelledError:
            _cancel_detached(ctx, task)
            raise
        except ScanBusy as exc:
            raise ScanBusyResponse(exc) from exc
        except ScanCancelled:
            raise ScanCancelledResponse() from None
