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
