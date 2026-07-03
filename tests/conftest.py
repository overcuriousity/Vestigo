"""Shared fixtures for the authentication/RBAC/audit test suite.

Every router now shares a single ``PostgresStore`` via ``api.deps.get_store``
(see ``tests/test_uploads.py``/``test_events_router.py`` for the same
monkeypatch pattern used against individual router modules before that
centralization). These fixtures spin up a full FastAPI app against an
in-memory SQLite store so auth/session/RBAC/audit behavior can be exercised
end-to-end through the real HTTP layer rather than by calling handlers
directly.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tracesignal.api import deps
from tracesignal.api.main import create_app
from tracesignal.core.config import get_settings
from tracesignal.db.postgres import PostgresStore, User


@pytest_asyncio.fixture()
async def store(tmp_path, monkeypatch):
    """In-memory SQLite store shared by every router via api.deps.get_store()."""
    db_path = tmp_path / "test_auth.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    monkeypatch.setattr(deps, "_store", s)
    yield s
    await s.engine.dispose()


@pytest.fixture()
def admin_bootstrap(monkeypatch):
    """Seed TS_ADMIN_* env vars and clear the settings cache so the app
    bootstraps a fresh administrator on startup. Cache is cleared again on
    teardown so later tests aren't affected by this test's env."""
    monkeypatch.setenv("TS_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("TS_ADMIN_PASSWORD", "bootstrap-pass-123")
    get_settings.cache_clear()
    yield {"username": "admin", "password": "bootstrap-pass-123"}
    get_settings.cache_clear()


@pytest.fixture()
def client(store, admin_bootstrap):
    """A TestClient over the real app (lifespan seeds the admin on entry)."""
    app = create_app()
    with TestClient(app) as c:
        yield c


def login(client: TestClient, username: str, password: str) -> dict:
    """Log in, returning the response JSON. Cookies persist on `client`."""
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fake_user(user_id: str = "u1") -> User:
    """A non-persisted User for calling route handlers directly (bypassing FastAPI DI)."""
    return User(id=user_id, username="tester", is_admin=True, is_active=True)


def as_admin(client: TestClient, admin_bootstrap: dict) -> dict:
    """Log in as the bootstrapped admin and complete the forced password change.

    Returns the post-change user payload. Most tests need this before they
    can do anything mutating, since the seeded admin always starts with
    ``must_change_password=True``.
    """
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    resp = client.post(
        "/api/auth/me/password",
        json={"current_password": admin_bootstrap["password"], "new_password": "rotated-pass-456"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["user"]
