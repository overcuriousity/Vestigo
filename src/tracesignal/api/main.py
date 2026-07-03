"""FastAPI application factory and API routers."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from tracesignal import __version__
from tracesignal.api.deps import get_store, resolve_user_optional
from tracesignal.api.routers import admin, auth, cases, events, jobs, stream, viz
from tracesignal.core.config import get_settings
from tracesignal.core.security import hash_password
from tracesignal.db.postgres import generate_id

logger = logging.getLogger(__name__)

_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

# API paths reachable without an authenticated session. Everything else under
# /api/* requires a valid session cookie (enforced by the middleware below);
# the SPA catch-all route serves static files only, so it stays exempt too.
_AUTH_EXEMPT_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/oidc/",
    "/api/docs",
    "/api/openapi.json",
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


def _requires_password_current(path: str, method: str) -> bool:
    """Whether ``method path`` should be blocked while a password rotation is pending.

    Every mutating ``/api/*`` request is gated except the self-service
    ``/api/auth/*`` routes (login, logout, profile update, and the
    change-password endpoint itself) — a user stuck in forced rotation must
    still be able to log out or change their password. Case/events routers
    also apply ``deps.require_password_current`` directly as defense in
    depth; this is the actual enforcement boundary, closing the gap where
    ``admin.py`` never opted in to that per-route dependency (PR #7 review
    finding #1).
    """
    return method in _MUTATING_METHODS and not path.startswith("/api/auth/")


async def _seed_admin() -> None:
    """Seed the first administrator on startup if no users exist yet.

    The seeded password is one-time: ``must_change_password=True`` forces a
    rotation on first login, which invalidates ``TS_ADMIN_PASSWORD`` the
    moment it's changed (see ``auth.change_my_password``).
    """
    settings = get_settings()
    store = get_store()
    if await store.list_users():
        return
    if not settings.admin_password:
        logger.error(
            "No users exist yet and TS_ADMIN_PASSWORD is not set. Set it and "
            "restart to bootstrap the first administrator account."
        )
        return
    password_hash = await asyncio.to_thread(hash_password, settings.admin_password)
    await store.create_user(
        user_id=generate_id("user"),
        username=settings.admin_username,
        password_hash=password_hash,
        is_admin=True,
        must_change_password=True,
    )
    logger.info(
        "Seeded administrator account %r (password must be changed on first login).",
        settings.admin_username,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = get_store()
    await store.init_schema()
    await _seed_admin()
    # No cron/scheduler in this single-process deployment (see JobStore),
    # so a startup-only sweep is the simple option — good enough to keep
    # `sessions` from growing unbounded across restarts without adding a
    # background task loop for a purely housekeeping concern.
    purged = await store.purge_expired_sessions()
    if purged:
        logger.info("Purged %d expired session(s) on startup.", purged)
    yield


class AuthAuditMiddleware:
    """Gate unauthenticated access to /api/* and append one audit row per request.

    Deliberately a plain ASGI middleware, **not** ``@app.middleware("http")``
    (Starlette's ``BaseHTTPMiddleware``) — that wrapper buffers/re-frames the
    response through an in-memory stream, which breaks disconnect detection
    and effectively hangs long-lived ``StreamingResponse``s (this app's SSE
    live-collaboration endpoint being exactly that case). A pure ASGI
    middleware passes ``receive``/``send`` straight through, so streaming and
    client-disconnect propagation both work correctly.

    Authorization (which case/admin actions a given user may take) still
    happens in the route dependencies (``deps.get_current_user``,
    ``deps.require_case``); this middleware only establishes *who* is calling
    (if anyone) and enforces that a session exists at all for non-exempt API
    paths. Resolving the user here means route handlers reuse the cached
    value via ``request.state.user`` instead of re-querying the session store.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        settings = get_settings()

        user = None
        if path.startswith("/api/"):
            user = await resolve_user_optional(request)
            if user is None and not _is_exempt(path):
                response = JSONResponse(status_code=401, content={"detail": "Not authenticated"})
                await response(scope, receive, send)
                return
            if (
                user is not None
                and user.must_change_password
                and _requires_password_current(path, request.method)
            ):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Password change required before continuing"},
                )
                await response(scope, receive, send)
                return

        status_holder: dict[str, int] = {}

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["status_code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            # /api/auth/* handlers write their own enriched audit row on
            # success, but an exception raised before that call leaves zero
            # trace otherwise — for a forensic platform, a baseline row here
            # (even without the handler's semantic detail) is the safer
            # contract than silence.
            if settings.audit_enabled and path.startswith("/api/auth/"):
                fallback_user = user or getattr(request.state, "user", None)
                try:
                    await get_store().record_audit(
                        action="api.request_failed",
                        actor=fallback_user,
                        method=request.method,
                        path=path,
                        ip=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"),
                    )
                except Exception:
                    logger.exception(
                        "Failed to write fallback audit log row for %s %s", request.method, path
                    )
            raise

        should_audit = (
            settings.audit_enabled
            and path.startswith("/api/")
            and not path.startswith("/api/auth/")
            and request.method in _MUTATING_METHODS
        )
        if should_audit:
            # /api/auth/* actions (login, logout, password change, OIDC) write
            # their own enriched audit rows with a semantic action label;
            # logging them again here would duplicate with less detail. GETs
            # are excluded too — polling (JobTray, TopBar, list refetches)
            # otherwise buries the security-relevant mutating rows this audit
            # log exists to surface.
            user = user or getattr(request.state, "user", None)
            route = scope.get("route")
            route_path = getattr(route, "path", path)
            case_id = (scope.get("path_params") or {}).get("case_id")
            try:
                await get_store().record_audit(
                    action="api.request",
                    actor=user,
                    method=request.method,
                    path=path,
                    route=route_path,
                    case_id=case_id,
                    status_code=status_holder.get("status_code"),
                    ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
            except Exception:
                # Audit logging must never take down the actual request.
                logger.exception("Failed to write audit log row for %s %s", request.method, path)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="TraceSignal",
        description="Local-first forensic log investigation platform.",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # Starlette applies middleware in reverse of registration order (last
    # added = outermost), so AuthAuditMiddleware is added first here — that
    # makes CORSMiddleware outermost, so it always gets a chance to answer
    # (and stamp CORS headers on) cross-origin preflight OPTIONS requests
    # and 401 responses, instead of AuthAuditMiddleware short-circuiting
    # them first with a bare, header-less 401.
    app.add_middleware(AuthAuditMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:8080"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_class=JSONResponse)
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "oidc_enabled": get_settings().oidc_enabled,
        }

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(cases.router)
    app.include_router(events.router)
    app.include_router(viz.router)
    app.include_router(jobs.router)
    app.include_router(stream.router)

    # Serve the built frontend when frontend/dist exists.
    # Run `npm run build` inside frontend/ once; tsig-web then serves everything.
    # For development with HMR, run `npm run dev` (port 5173) alongside tsig-web instead.
    if _FRONTEND_DIST.is_dir():
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_frontend(full_path: str) -> FileResponse:
            candidate = _FRONTEND_DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_FRONTEND_DIST / "index.html")

    return app
