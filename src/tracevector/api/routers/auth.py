"""Login/logout, self-service account management, and optional OIDC SSO."""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Generator
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from tracevector.api.deps import get_current_user, get_store
from tracevector.core.config import get_settings
from tracevector.core.security import (
    hash_password,
    new_session_token,
    session_expiry,
    verify_password,
)
from tracevector.db.postgres import User, generate_id

router = APIRouter(prefix="/api/auth", tags=["auth"])

_OIDC_STATE_COOKIE = "tv_oidc_state"


class LoginRequest(BaseModel):
    """Payload to log in with a local username/password."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    """Payload to change the current user's own password."""

    current_password: str | None = Field(default=None)
    new_password: str = Field(..., min_length=8, max_length=255)


class UpdateMeRequest(BaseModel):
    """Payload to update the current user's own profile."""

    username: str | None = Field(default=None, min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)


def _user_response(user: User, teams: list[dict[str, Any]]) -> dict[str, Any]:
    payload = user.to_dict()
    payload["teams"] = teams
    return payload


async def _teams_for_user(user: User) -> list[dict[str, Any]]:
    store = get_store()
    return [
        {"id": team.id, "name": team.name, "role": role}
        for team, role in await store.list_teams_for_user(user.id)
    ]


def _set_session_cookie(response: Response, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=session_id,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


async def _issue_session(user: User, request: Request, response: Response) -> None:
    settings = get_settings()
    store = get_store()
    session = await store.create_session(
        session_id=new_session_token(),
        user_id=user.id,
        expires_at=session_expiry(settings.session_ttl_hours),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, session.id)
    await store.touch_last_login(user.id)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    """Authenticate with a local username/password and start a session."""
    store = get_store()
    user = await store.get_user_by_username(payload.username)
    if user is None or not await asyncio.to_thread(
        verify_password, payload.password, user.password_hash
    ):
        await store.record_audit(
            action="auth.login_failed",
            username_snapshot=payload.username,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    await _issue_session(user, request, response)
    await store.record_audit(
        action="auth.login",
        actor=user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"user": _user_response(user, await _teams_for_user(user))}


@router.post("/logout")
async def logout(
    response: Response,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Revoke the current session and clear its cookie."""
    store = get_store()
    session_id = getattr(request.state, "session_id", None)
    if session_id:
        await store.revoke_session(session_id)
    settings = get_settings()
    response.delete_cookie(settings.auth_cookie_name, path="/")
    await store.record_audit(action="auth.logout", actor=user)
    return {"logged_out": True}


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return the authenticated user's profile, teams, and roles."""
    return {"user": _user_response(user, await _teams_for_user(user))}


@router.patch("/me")
async def update_me(
    payload: UpdateMeRequest, user: User = Depends(get_current_user)
) -> dict[str, Any]:
    """Change the current user's own username and/or display name."""
    store = get_store()
    if payload.username and payload.username != user.username:
        existing = await store.get_user_by_username(payload.username)
        if existing is not None and existing.id != user.id:
            raise HTTPException(status_code=409, detail="Username already taken")
    updated = await store.update_user(
        user.id, username=payload.username, display_name=payload.display_name
    )
    await store.record_audit(action="auth.update_profile", actor=user)
    return {"user": _user_response(updated, await _teams_for_user(updated))}


@router.post("/me/password")
async def change_my_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Change the current user's own password.

    Verifies ``current_password`` unless the account has never had a local
    password (a fresh OIDC account gaining a local password for the first
    time). Clears ``must_change_password`` — this is how the seeded admin
    bootstrap credential (``TV_ADMIN_PASSWORD``) gets permanently
    invalidated. Rotates the session: every other outstanding session for
    this user is revoked and a fresh one is issued for the current request,
    so a stolen old cookie stops working the moment the password changes.
    """
    store = get_store()
    if user.password_hash is not None and not await asyncio.to_thread(
        verify_password, payload.current_password or "", user.password_hash
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_hash = await asyncio.to_thread(hash_password, payload.new_password)
    await store.set_password(user.id, new_hash, must_change_password=False)
    await store.revoke_user_sessions(user.id)
    await _issue_session(user, request, response)
    await store.record_audit(action="auth.change_password", actor=user)
    refreshed = await store.get_user(user.id)
    return {"user": _user_response(refreshed, await _teams_for_user(refreshed))}


def _audit_rows_to_csv(rows: list[dict[str, Any]]) -> Generator[str]:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "timestamp",
            "action",
            "method",
            "route",
            "case_id",
            "target_type",
            "target_id",
            "status_code",
        ],
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate(0)
        writer.writerow(row)
        yield buf.getvalue()


@router.get("/me/audit")
async def get_my_audit(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    limit: int = Query(default=1000, ge=1, le=10000),
    user: User = Depends(get_current_user),
) -> Any:
    """Return (or download) the current user's own audit trail.

    Self-service reproducibility: an analyst can pull exactly what they did
    and when, without needing admin access to the global audit log.
    """
    store = get_store()
    rows = [r.to_dict() for r in await store.query_audit(user_id=user.id, limit=limit)]
    if format == "csv":
        return StreamingResponse(
            _audit_rows_to_csv(rows),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="audit-{user.username}.csv"'},
        )
    return {"audit": rows}


# ---------------------------------------------------------------------------
# OIDC SSO (optional; gated on TV_OIDC_ENABLED)
# ---------------------------------------------------------------------------


async def _oidc_metadata(issuer: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        resp.raise_for_status()
        return resp.json()


def _require_oidc_configured() -> None:
    settings = get_settings()
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")
    if not (settings.oidc_issuer and settings.oidc_client_id and settings.oidc_redirect_url):
        raise HTTPException(status_code=500, detail="OIDC is enabled but not fully configured")


@router.get("/oidc/login")
async def oidc_login() -> RedirectResponse:
    """Redirect the browser to the configured OIDC provider's authorization endpoint."""
    _require_oidc_configured()
    settings = get_settings()
    metadata = await _oidc_metadata(settings.oidc_issuer)  # type: ignore[arg-type]
    state = new_session_token()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_url,
        "scope": settings.oidc_scopes,
        "state": state,
    }
    response = RedirectResponse(url=f"{metadata['authorization_endpoint']}?{urlencode(params)}")
    response.set_cookie(_OIDC_STATE_COOKIE, state, httponly=True, max_age=600, samesite="lax")
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str,
    state: str,
) -> RedirectResponse:
    """Handle the OIDC provider's redirect back: exchange the code, provision/find the user.

    New subjects land in the "default pool" (no team) — visible only to
    themselves until an admin assigns them to a team via the admin console.
    """
    _require_oidc_configured()
    settings = get_settings()
    expected_state = request.cookies.get(_OIDC_STATE_COOKIE)
    if not expected_state or expected_state != state:
        raise HTTPException(status_code=400, detail="Invalid or expired OIDC state")

    metadata = await _oidc_metadata(settings.oidc_issuer)  # type: ignore[arg-type]
    async with AsyncOAuth2Client(
        settings.oidc_client_id,
        settings.oidc_client_secret,
        redirect_uri=settings.oidc_redirect_url,
    ) as client:
        await client.fetch_token(
            metadata["token_endpoint"], code=code, grant_type="authorization_code"
        )
        userinfo_resp = await client.get(metadata["userinfo_endpoint"])
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

    subject = userinfo.get("sub")
    if not subject:
        raise HTTPException(status_code=502, detail="OIDC provider did not return a subject claim")

    store = get_store()
    user = await store.get_user_by_oidc_subject(subject)
    if user is None:
        username = (
            userinfo.get("preferred_username") or userinfo.get("email") or f"oidc_{subject[:12]}"
        )
        # Guard against a local account already using this username.
        if await store.get_user_by_username(username) is not None:
            username = f"{username}_{subject[:6]}"
        user = await store.create_user(
            user_id=generate_id("user"),
            username=username,
            auth_provider="oidc",
            oidc_subject=subject,
            display_name=userinfo.get("name"),
            email=userinfo.get("email"),
        )
        await store.record_audit(action="auth.oidc_provisioned", actor=user)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    response = RedirectResponse(url="/")
    await _issue_session(user, request, response)
    response.delete_cookie(_OIDC_STATE_COOKIE)
    await store.record_audit(
        action="auth.login",
        actor=user,
        detail={"provider": "oidc"},
    )
    return response
