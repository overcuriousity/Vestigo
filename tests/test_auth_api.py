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


def test_login_backoff_returns_429_after_threshold(client, admin_bootstrap):
    """After VESTIGO_LOGIN_BACKOFF_THRESHOLD (default 5) consecutive failures the
    next attempt is rejected with 429 and a Retry-After header — even with the
    correct password, until the delay elapses."""
    for _ in range(5):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": admin_bootstrap["password"]},
    )
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_login_backoff_does_not_leak_username_existence(client, admin_bootstrap):
    """Unknown usernames and wrong passwords for a real account must produce
    identical status sequences (401...401, then 429)."""

    def sequence(username: str) -> list[int]:
        return [
            client.post(
                "/api/auth/login", json={"username": username, "password": "wrong"}
            ).status_code
            for _ in range(6)
        ]

    assert sequence("admin") == sequence("no-such-user")


def test_successful_login_resets_backoff(client, admin_bootstrap):
    """Failures below the threshold are cleared by a successful login."""
    for _ in range(4):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    # Counter reset: four more failures stay below the threshold again.
    for _ in range(4):
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
    VESTIGO_ADMIN_PASSWORD. The gate now lives in AuthAuditMiddleware, applied to
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


def test_update_me_onboarding_flag(client, admin_bootstrap):
    """Onboarding flag: defaults false, PATCHable both ways without touching other fields."""
    as_admin(client, admin_bootstrap)
    me = client.get("/api/auth/me").json()["user"]
    assert me["onboarding_completed"] is False

    resp = client.patch("/api/auth/me", json={"onboarding_completed": True})
    assert resp.status_code == 200
    updated = resp.json()["user"]
    assert updated["onboarding_completed"] is True
    assert updated["username"] == me["username"]
    assert updated["display_name"] == me["display_name"]

    # Reset path (Settings → restart tour).
    resp = client.patch("/api/auth/me", json={"onboarding_completed": False})
    assert resp.json()["user"]["onboarding_completed"] is False


def test_update_my_preferences_merges_a_whitelisted_key(client, admin_bootstrap):
    """Acknowledging the column-suggestion disclosure has to outlive one browser."""
    as_admin(client, admin_bootstrap)
    assert (client.get("/api/auth/me").json()["user"]["preferences"] or {}) == {}

    resp = client.put(
        "/api/auth/me/preferences",
        json={"preferences": {"column_advisor_notice_ack": True}},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["preferences"]["column_advisor_notice_ack"] is True
    assert client.get("/api/auth/me").json()["user"]["preferences"] == {
        "column_advisor_notice_ack": True
    }


def test_update_my_preferences_refuses_anything_not_whitelisted(client, admin_bootstrap):
    """The blob is feature state, not a key/value store every session can write."""
    as_admin(client, admin_bootstrap)

    assert (
        client.put(
            "/api/auth/me/preferences", json={"preferences": {"arbitrary": "value"}}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/auth/me/preferences",
            json={"preferences": {"column_advisor_notice_ack": "yes"}},
        ).status_code
        == 422
    )
    assert (client.get("/api/auth/me").json()["user"]["preferences"] or {}) == {}


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


def test_user_directory_requires_auth_and_maps_ids(client, admin_bootstrap):
    resp = client.get("/api/auth/users")
    assert resp.status_code == 401

    as_admin(client, admin_bootstrap)
    client.patch("/api/auth/me", json={"display_name": "Root Admin"})
    resp = client.get("/api/auth/users")
    assert resp.status_code == 200
    users = resp.json()["users"]
    admin_row = next(u for u in users if u["username"] == "admin")
    assert admin_row["id"].startswith("user_")
    assert admin_row["display_name"] == "Root Admin"
    assert set(admin_row) == {"id", "username", "display_name"}


@pytest.mark.asyncio
async def test_oidc_discovery_follows_redirects():
    """Nextcloud 301s /.well-known/openid-configuration to /index.php/...

    Refusing to follow it turned a perfectly working IdP into an unhandled
    HTTPStatusError and a 500 on /api/auth/oidc/login.
    """
    import httpx

    from vestigo.api.routers.auth import _oidc_metadata

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                301, headers={"Location": "/index.php/.well-known/openid-configuration"}
            )
        return httpx.Response(200, json={"authorization_endpoint": "https://idp.example/auth"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    httpx.AsyncClient = patched  # type: ignore[misc]
    try:
        metadata = await _oidc_metadata("https://cloud.example.org")
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert metadata["authorization_endpoint"] == "https://idp.example/auth"
    assert seen[-1].endswith("/index.php/.well-known/openid-configuration")


@pytest.mark.asyncio
async def test_oidc_discovery_failure_is_a_502_not_a_traceback():
    import httpx
    from fastapi import HTTPException

    from vestigo.api.routers.auth import _oidc_metadata

    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    httpx.AsyncClient = patched  # type: ignore[misc]
    try:
        with pytest.raises(HTTPException) as excinfo:
            await _oidc_metadata("https://cloud.example.org")
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert excinfo.value.status_code == 502
    assert "cloud.example.org" in excinfo.value.detail
