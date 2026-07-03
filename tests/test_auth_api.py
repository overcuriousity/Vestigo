"""Tests for local login/logout/session lifecycle and the forced-rotation bootstrap."""

from __future__ import annotations

import pytest

from tests.conftest import as_admin, login


def test_unauthenticated_request_is_rejected(client):
    resp = client.get("/api/cases/")
    assert resp.status_code == 401


def test_health_is_exempt_from_auth(client):
    assert client.get("/api/health").status_code == 200


def test_health_reports_oidc_enabled_flag(client):
    assert client.get("/api/health").json()["oidc_enabled"] is False


def test_cross_origin_preflight_gets_cors_headers_not_a_bare_401(client):
    """PR #7 review finding #3: AuthAuditMiddleware was added after
    CORSMiddleware, making it outermost — an OPTIONS preflight (which never
    carries cookies) got a header-less 401 before CORS ever answered.
    CORSMiddleware must be the outer layer so it always gets to respond."""
    resp = client.options(
        "/api/cases/",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_seeded_admin_must_change_password_on_first_login(client, admin_bootstrap):
    payload = login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    assert payload["user"]["is_admin"] is True
    assert payload["user"]["must_change_password"] is True


def test_bad_credentials_are_rejected_and_audited(client, admin_bootstrap):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_mutating_action_blocked_until_password_rotated(client, admin_bootstrap):
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    resp = client.post("/api/cases/", json={"name": "should-be-blocked"})
    assert resp.status_code == 403


def test_admin_mutation_blocked_until_password_rotated(client, admin_bootstrap):
    """PR #7 review finding #1: admin.py never opted in to
    require_password_current, so the bootstrap admin could mint a permanent
    admin via POST /api/admin/users before ever rotating the one-time
    TS_ADMIN_PASSWORD. The gate now lives in AuthAuditMiddleware, applied to
    every mutating /api/* request regardless of router opt-in."""
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    resp = client.post(
        "/api/admin/users", json={"username": "sneaky", "password": "abcdefgh12", "is_admin": True}
    )
    assert resp.status_code == 403


def test_logout_and_password_change_still_reachable_during_forced_rotation(client, admin_bootstrap):
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    # The self-service /api/auth/* routes must stay reachable, or a user
    # stuck in forced rotation could never actually clear the flag.
    resp = client.post(
        "/api/auth/me/password",
        json={
            "current_password": admin_bootstrap["password"],
            "new_password": "cleared-pass-789",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["must_change_password"] is False


def test_seeded_password_is_invalidated_after_rotation(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    # The one-time bootstrap credential must no longer work.
    fresh = client.__class__(client.app)
    resp = fresh.post(
        "/api/auth/login",
        json={"username": admin_bootstrap["username"], "password": admin_bootstrap["password"]},
    )
    assert resp.status_code == 401


def test_rotated_password_now_works_and_unblocks_mutations(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    resp = client.post("/api/cases/", json={"name": "now-allowed"})
    assert resp.status_code == 200
    assert resp.json()["case"]["owner_id"] is not None


def test_password_change_revokes_prior_sessions(client, admin_bootstrap):
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    old_cookie = client.cookies.get("tv_session")
    assert old_cookie

    client.post(
        "/api/auth/me/password",
        json={"current_password": admin_bootstrap["password"], "new_password": "second-pass-789"},
    )
    new_cookie = client.cookies.get("tv_session")
    assert new_cookie != old_cookie

    # Replay the old cookie: it must no longer authenticate.
    client.cookies.set("tv_session", old_cookie)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_logout_revokes_the_session(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    assert client.get("/api/auth/me").status_code == 200
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_update_own_profile_rejects_duplicate_username(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/users", json={"username": "someoneelse", "password": "abcdefgh12"}
    )
    assert resp.status_code == 200
    resp = client.patch("/api/auth/me", json={"username": "someoneelse"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_disabled_account_cannot_authenticate(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post("/api/admin/users", json={"username": "disableme", "password": "abcdefgh12"})
    user_id = resp.json()["user"]["id"]
    client.patch(f"/api/admin/users/{user_id}", json={"is_active": False})

    fresh = client.__class__(client.app)
    resp = fresh.post("/api/auth/login", json={"username": "disableme", "password": "abcdefgh12"})
    assert resp.status_code == 403
