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
    """Opting in to AI column suggestions has to outlive one browser."""
    as_admin(client, admin_bootstrap)
    assert (client.get("/api/auth/me").json()["user"]["preferences"] or {}) == {}

    resp = client.put(
        "/api/auth/me/preferences",
        json={"preferences": {"column_advisor_optin": {"tl-1": True}}},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["preferences"]["column_advisor_optin"] == {"tl-1": True}
    assert client.get("/api/auth/me").json()["user"]["preferences"] == {
        "column_advisor_optin": {"tl-1": True}
    }


def test_update_my_preferences_merges_dict_values_one_level_down(client, admin_bootstrap):
    """A second tab adding its own timeline must not drop the first one's."""
    as_admin(client, admin_bootstrap)
    client.put(
        "/api/auth/me/preferences",
        json={"preferences": {"column_advisor_optin": {"tl-1": True}}},
    )

    resp = client.put(
        "/api/auth/me/preferences",
        json={"preferences": {"column_advisor_optin": {"tl-2": True}}},
    )

    assert resp.json()["user"]["preferences"]["column_advisor_optin"] == {
        "tl-1": True,
        "tl-2": True,
    }


def test_update_my_preferences_records_a_declined_answer(client, admin_bootstrap):
    """``false`` is an answer, not an absent one.

    The Explorer offers the AI column suggestion once per timeline and has to
    tell "said no" apart from "not asked yet" — otherwise the disclosure comes
    back on every visit, which is how people learn to dismiss a consent dialog
    unread. Same key as the opt-in, so one merge and one bound cover both.
    """
    as_admin(client, admin_bootstrap)
    client.put(
        "/api/auth/me/preferences",
        json={"preferences": {"column_advisor_optin": {"tl-1": True}}},
    )

    resp = client.put(
        "/api/auth/me/preferences",
        json={"preferences": {"column_advisor_optin": {"tl-2": False}}},
    )

    assert resp.status_code == 200
    assert resp.json()["user"]["preferences"]["column_advisor_optin"] == {
        "tl-1": True,
        "tl-2": False,
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
            json={"preferences": {"column_advisor_optin": "yes"}},
        ).status_code
        == 422
    )
    # The whitelist has to reach inside a dict value too, or it is exactly the
    # arbitrary key/value store it exists to prevent.
    assert (
        client.put(
            "/api/auth/me/preferences",
            json={"preferences": {"column_advisor_optin": {"tl-1": "sure"}}},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/auth/me/preferences",
            json={"preferences": {"column_advisor_optin": {str(i): True for i in range(501)}}},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/auth/me/preferences",
            json={"preferences": {"column_advisor_optin": {"t" * 129: True}}},
        ).status_code
        == 422
    )
    assert (client.get("/api/auth/me").json()["user"]["preferences"] or {}) == {}


def test_update_my_preferences_bounds_the_merged_blob(client, admin_bootstrap):
    """The cap has to survive the merge, or repeated calls grow the row forever.

    Over the cap the *oldest* entries go, and the write still succeeds: a hard
    refusal would mean an analyst past the ceiling can never record a consent
    again, and a consent that cannot be recorded is a disclosure dialog that
    reappears forever.
    """
    as_admin(client, admin_bootstrap)

    for batch in range(2):
        resp = client.put(
            "/api/auth/me/preferences",
            json={
                "preferences": {
                    "column_advisor_optin": {f"tl-{batch}-{i}": True for i in range(300)}
                }
            },
        )
        # 300 already stored + 300 fresh = 600, over the ceiling — accepted, trimmed.
        assert resp.status_code == 200, resp.text

    stored = client.get("/api/auth/me").json()["user"]["preferences"]["column_advisor_optin"]
    assert len(stored) == 500
    # Everything this request recorded survived — it is the consent being given.
    assert all(f"tl-1-{i}" in stored for i in range(300))
    # The 100 oldest went, in insertion order.
    assert not any(f"tl-0-{i}" in stored for i in range(100))
    assert all(f"tl-0-{i}" in stored for i in range(100, 300))


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


def test_access_log_redacts_the_oidc_authorization_code():
    """The callback's code and state are credentials in a URL uvicorn logs.

    They are single-use and short-lived, but the journal is readable by more
    people than the session store is, so they must not land there in the
    clear.
    """
    import logging

    from vestigo.api.main import AccessLogRedactor, redact_query

    target = "/api/auth/oidc/callback?state=Fb8wUjkc&code=BKnwFkdaDaZxDQ"
    assert redact_query(target) == "/api/auth/oidc/callback?state=***&code=***"

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.13.1:0", "GET", target, "1.1", 307),
        exc_info=None,
    )
    assert AccessLogRedactor().filter(record) is True
    assert record.args is not None
    assert record.args[2] == "/api/auth/oidc/callback?state=***&code=***"
    # The path and the parameter names survive: an operator can still see that
    # a callback happened and that it carried a code.
    assert "/api/auth/oidc/callback" in record.args[2]
    assert "BKnwFkdaDaZxDQ" not in record.args[2]


def test_access_log_redactor_survives_uvicorn_logging_config():
    """The filter must outlive uvicorn configuring its own loggers.

    `create_app()` attaches the redactor at import time; uvicorn calls
    `dictConfig(LOGGING_CONFIG)` afterwards, when the server starts. That
    ordering only holds because `dictConfig` clears a configured logger's
    *handlers* and not its *filters* — a CPython implementation detail, not a
    documented contract, and the difference between "OIDC codes are redacted"
    and "they are silently back in the journal after a dependency bump".
    """
    import logging

    from uvicorn.config import LOGGING_CONFIG

    from vestigo.api.main import AccessLogRedactor, create_app

    access_logger = logging.getLogger("uvicorn.access")
    for existing in [f for f in access_logger.filters if isinstance(f, AccessLogRedactor)]:
        access_logger.removeFilter(existing)

    create_app()
    assert any(isinstance(f, AccessLogRedactor) for f in access_logger.filters)

    # Exactly what `uvicorn.Config.configure_logging()` does.
    logging.config.dictConfig(LOGGING_CONFIG)
    assert any(isinstance(f, AccessLogRedactor) for f in access_logger.filters), (
        "uvicorn's logging config dropped the access-log redactor — "
        "OIDC authorization codes would reach the journal in the clear"
    )

    # And it still redacts once uvicorn owns the handler.
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.13.1:0", "GET", "/api/auth/oidc/callback?code=BKnwFkdaDaZxDQ", "1.1", 307),
        exc_info=None,
    )
    for log_filter in access_logger.filters:
        log_filter.filter(record)  # type: ignore[union-attr]
    assert record.args is not None
    assert "BKnwFkdaDaZxDQ" not in record.args[2]


def test_access_log_redactor_is_attached_once():
    """Repeated `create_app()` calls must not stack filters on the shared logger."""
    import logging

    from vestigo.api.main import AccessLogRedactor, create_app

    create_app()
    create_app()
    access_logger = logging.getLogger("uvicorn.access")
    attached = [f for f in access_logger.filters if isinstance(f, AccessLogRedactor)]
    assert len(attached) == 1


def test_access_log_leaves_ordinary_query_strings_alone():
    from vestigo.api.main import redact_query

    assert redact_query("/api/cases/") == "/api/cases/"
    assert redact_query("/api/events?limit=50&offset=100") == "/api/events?limit=50&offset=100"
    # A sensitive name as a *substring* of another parameter is not a match.
    assert redact_query("/api/x?encoded=1") == "/api/x?encoded=1"


def test_access_log_redaction_decodes_the_parameter_name():
    """Matching on the raw name would let ``%63ode`` carry a live credential.

    The value is what must not be logged, and the name is only how we find it
    — so the name is decoded to *decide* and emitted verbatim, leaving the
    journal an honest record of what the client actually sent.
    """
    from vestigo.api.main import redact_query

    assert redact_query("/cb?%63ode=BKnwFkda") == "/cb?%63ode=***"
    assert redact_query("/cb?%43ODE=BKnwFkda") == "/cb?%43ODE=***"
    # Still not a substring match once decoded.
    assert redact_query("/api/x?en%63oded=1") == "/api/x?en%63oded=1"


def test_access_log_redaction_folds_case_and_covers_more_than_oidc():
    """The guarantee is a name list, so the list has to be the wide one.

    An IdP that capitalizes its parameter, or any endpoint that grows a
    secret-bearing query, would otherwise log the value verbatim — the same
    failure as the OIDC code, discovered again later.
    """
    from vestigo.api.main import redact_query

    assert redact_query("/cb?Code=abc&STATE=def") == "/cb?Code=***&STATE=***"
    assert redact_query("/x?client_secret=s&api_key=k") == "/x?client_secret=***&api_key=***"
    # A bare flag with no `=` is left alone rather than gaining a value.
    assert redact_query("/x?code") == "/x?code"


def test_every_secret_bearing_query_parameter_is_on_the_redaction_list():
    """The list is a name list, so the obligation to extend it needs a ratchet.

    ``_SECRET_QUERY_PARAMS`` only redacts what it is told about, and until now
    the only thing saying "add your new one here" was a comment. This walks the
    app's own OpenAPI schema and fails when a route declares a query parameter
    whose *name* reads like a credential and is not on the list — which is how
    the OIDC code reached the journal in the first place, one subsystem at a
    time.

    Deliberately name-shaped rather than exhaustive: it cannot know that some
    innocuously-named parameter carries a secret, but it does catch every
    parameter a reviewer would have flagged by reading its name.
    """
    import re

    from vestigo.api.main import _SECRET_QUERY_PARAMS, create_app

    suspicious = re.compile(
        r"(token|secret|password|passwd|credential|api_?key|signature|^code$|^state$)"
    )
    offenders = []
    for path, operations in create_app().openapi()["paths"].items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "query":
                    continue
                name = parameter["name"].lower()
                if suspicious.search(name) and name not in _SECRET_QUERY_PARAMS:
                    offenders.append(f"{method.upper()} {path} ?{parameter['name']}")
    assert offenders == [], (
        "these query parameters look like credentials but would be logged in full; "
        "add them to _SECRET_QUERY_PARAMS in api/main.py: " + ", ".join(offenders)
    )


def test_listing_cases_does_not_run_migrations(client, admin_bootstrap, monkeypatch):
    """Schema upgrades belong to startup, not to the hottest endpoint.

    ``init_schema`` runs ``alembic upgrade head``, which opens a connection and
    takes a migration lock. On every ``GET /api/cases/`` that is a per-request
    cost and a contention point on the path the UI hits most.
    """
    from vestigo.api import deps

    store = deps.get_store()
    calls = []
    original = store.init_schema

    async def _counting():
        calls.append(1)
        return await original()

    monkeypatch.setattr(store, "init_schema", _counting)

    as_admin(client, admin_bootstrap)
    assert client.get("/api/cases/").status_code == 200
    assert client.post("/api/cases/", json={"name": "c1"}).status_code == 200
    assert client.get("/api/cases/").status_code == 200
    assert calls == []
