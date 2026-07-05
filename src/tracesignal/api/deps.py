"""Shared FastAPI dependencies: store access, authentication, and case RBAC.

Before this module, each router (``cases.py``, ``events.py``) defined its own
module-global ``PostgresStore`` singleton and there was no ``Depends``-based
injection anywhere in the app. This module is the single DI seam: one shared
store instance, the current-user resolver, and the case-access-level checks
that every case-scoped endpoint now goes through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import IntEnum

from fastapi import Depends, HTTPException, Request

from tracesignal.core.config import get_settings
from tracesignal.db._dt import ensure_utc
from tracesignal.db.postgres import Case, PostgresStore, User

_store: PostgresStore | None = None


def get_store() -> PostgresStore:
    """Return the process-wide cached PostgresStore instance."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = PostgresStore()
    return _store


class AccessLevel(IntEnum):
    """Ordered case-access levels; a higher level implies every lower one."""

    NONE = 0
    READ = 1
    CONTRIBUTE = 2
    MANAGE = 3


# Every /api/* hit re-validates the session, so an unconditional touch_session
# UPDATE+commit on each request is one Postgres write per request just to
# refresh a "last seen" timestamp. Debouncing to once/minute keeps session
# activity current enough for admin visibility without the per-request write.
_SESSION_TOUCH_INTERVAL = timedelta(seconds=60)


async def resolve_user_optional(request: Request) -> User | None:
    """Resolve the session cookie to a user, or return None if unauthenticated.

    Non-raising counterpart to :func:`get_current_user`, used by the auth
    middleware (which can't cleanly propagate an ``HTTPException`` from
    outside the routed exception-handling stack). On success, caches the
    result on ``request.state`` so a subsequent ``Depends(get_current_user)``
    in the route handler reuses it instead of re-querying the store.
    """
    settings = get_settings()
    session_id = request.cookies.get(settings.auth_cookie_name)
    if not session_id:
        return None

    store = get_store()
    session = await store.get_session(session_id)
    if session is None or session.revoked or ensure_utc(session.expires_at) < datetime.now(UTC):
        return None

    user = await store.get_user(session.user_id)
    if user is None or not user.is_active:
        return None

    if datetime.now(UTC) - ensure_utc(session.last_seen_at) > _SESSION_TOUCH_INTERVAL:
        await store.touch_session(session_id)
    request.state.user = user
    request.state.session_id = session_id
    return user


async def get_current_user(request: Request) -> User:
    """Resolve the authenticated user from the session cookie.

    Raises 401 if there is no session, it is expired/revoked, or the user
    has been deactivated. Reuses ``request.state.user`` if the auth
    middleware already resolved it for this request.
    """
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    user = await resolve_user_optional(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require the current user to be an administrator."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator privileges required")
    return user


async def require_password_current(user: User = Depends(get_current_user)) -> User:
    """Block mutating actions until a forced password rotation is complete.

    Kept as an explicit, in-route dependency on the ``cases.py``/``events.py``
    endpoints that already used it (defense in depth, and a clear 403 in the
    OpenAPI schema for those routes). The actual enforcement boundary is
    ``AuthAuditMiddleware`` in ``main.py``, which blocks every mutating
    ``/api/*`` request (including admin endpoints, which never opted in to
    this dependency — see PR #7 review finding #1) except ``/api/auth/*``
    self-service routes (login, logout, profile update, and the
    change-password endpoint itself, so a user stuck in forced rotation can
    still reach it).
    """
    if user.must_change_password:
        raise HTTPException(
            status_code=403,
            detail="Password change required before continuing",
        )
    return user


async def resolve_case_access(user: User, case: Case) -> AccessLevel:
    """Return the caller's access level for a specific case.

    - Admins: MANAGE on every case.
    - Team case (``case.team_id`` set): membership role decides — team
      managers get MANAGE, team members get CONTRIBUTE, non-members get NONE.
    - Personal case (``case.team_id`` is None): the owner gets MANAGE;
      everyone else gets NONE.
    """
    if user.is_admin:
        return AccessLevel.MANAGE
    if case.team_id:
        store = get_store()
        membership = await store.get_membership(case.team_id, user.id)
        if membership is None:
            return AccessLevel.NONE
        return AccessLevel.MANAGE if membership.role == "manager" else AccessLevel.CONTRIBUTE
    if case.owner_id == user.id:
        return AccessLevel.MANAGE
    return AccessLevel.NONE


async def has_case_access(user: User, case: Case | None, level: AccessLevel) -> bool:
    """Return whether *user* has at least *level* access to *case*.

    For callers that need a boolean check outside the ``Depends`` chain (e.g.
    a fallback path after another check, or a re-check mid-generator) instead
    of raising 403/404 — those should prefer ``require_case``/
    ``require_case_read`` etc.
    """
    if case is None:
        return False
    return await resolve_case_access(user, case) >= level


def require_case(level: AccessLevel):
    """Return a FastAPI dependency requiring at least ``level`` access to ``case_id``.

    The dependency reads ``case_id`` from the path, 404s if the case doesn't
    exist, 403s if the caller's access is below ``level``, and otherwise
    returns the loaded ``Case`` so handlers don't need to re-fetch it.

    Prefer the pre-built ``require_case_read``/``require_case_contribute``/
    ``require_case_manage`` singletons below in route signatures — calling
    ``require_case(...)`` directly in a ``Depends(...)`` default triggers
    flake8-bugbear B008 (a fresh closure on every route registration).
    """

    async def _dependency(case_id: str, user: User = Depends(get_current_user)) -> Case:
        store = get_store()
        case = await store.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found")
        access = await resolve_case_access(user, case)
        if access < level:
            raise HTTPException(status_code=403, detail="Insufficient access to this case")
        return case

    return _dependency


# Pre-built module-level singletons for the three access levels, so route
# signatures can write `Depends(require_case_read)` etc. instead of calling
# `require_case(...)` inline (which flake8-bugbear flags as B008 since it
# builds a new closure at import time rather than reading one).
require_case_read = require_case(AccessLevel.READ)
require_case_contribute = require_case(AccessLevel.CONTRIBUTE)
require_case_manage = require_case(AccessLevel.MANAGE)
