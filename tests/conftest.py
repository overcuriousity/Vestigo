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

from vestigo.api import deps
from vestigo.api.main import create_app
from vestigo.core.config import get_settings
from vestigo.core.login_backoff import reset_login_backoff
from vestigo.db.postgres import PostgresStore, User


@pytest_asyncio.fixture()
async def store(tmp_path, monkeypatch):
    """In-memory SQLite store shared by every router via api.deps.get_store()."""
    db_path = tmp_path / "test_auth.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    monkeypatch.setattr(deps, "_store", s)
    yield s
    await s.engine.dispose()


@pytest.fixture(autouse=True)
def transfer_temp(tmp_path, monkeypatch):
    """Point export archives at the test's tmp dir.

    ``transfer_temp_path`` defaults to ``data/transfer`` relative to the
    working directory, so without this any test that touches the transfer
    router would write archives into the repo. Autouse because the app's
    startup sweep calls ``temp_root()`` before any test body runs.
    """
    monkeypatch.setenv("VESTIGO_TRANSFER_TEMP_PATH", str(tmp_path / "transfer"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fresh_job_store(monkeypatch):
    """Give every test its own job store.

    ``get_job_store()`` is a process-wide singleton, so without this a test
    that leaves a job queued or running holds an admission slot for every test
    after it — the demo seeder's cap is one, which makes that immediately
    visible as a seed that silently never dispatches.
    """
    from vestigo.core import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "_default_store", None)
    yield


@pytest.fixture()
def admin_bootstrap(monkeypatch):
    """Seed VESTIGO_ADMIN_* env vars and clear the settings cache so the app
    bootstraps a fresh administrator on startup. Cache is cleared again on
    teardown so later tests aren't affected by this test's env."""
    monkeypatch.setenv("VESTIGO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("VESTIGO_ADMIN_PASSWORD", "bootstrap-pass-123")
    get_settings.cache_clear()
    yield {"username": "admin", "password": "bootstrap-pass-123"}
    get_settings.cache_clear()


@pytest.fixture()
def client(store, admin_bootstrap):
    """A TestClient over the real app (lifespan seeds the admin on entry)."""
    # The login-backoff singleton is process-wide; reset it so failed-login
    # tests can't rate-limit each other across test boundaries.
    reset_login_backoff()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_login_backoff()


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


@pytest.fixture(autouse=True)
def no_demo_seed(monkeypatch):
    """Don't seed a demo case for tests that aren't about demo seeding.

    Every login dispatches a background build of a 251k-event case against a
    real ClickHouse. For the ~700 tests that only need *a* logged-in session
    that is minutes of generation per run and an unrelated task competing for
    the app's loop, which is exactly the kind of load that turns a seeding race
    into an intermittent failure elsewhere.

    Patched at the call site in ``routers.auth``, which imports the name
    directly. Tests that *are* about seeding take ``builds``, which puts the
    real dispatch path back.
    """
    from vestigo.api.routers import auth as auth_mod

    async def _no_seed(user):
        return None

    monkeypatch.setattr(auth_mod, "maybe_seed_demo_case", _no_seed)
    yield


@pytest.fixture()
def seeds_on_login(monkeypatch, no_demo_seed):
    """Put the real login-time seeder back for tests that are about seeding.

    Take this (directly, or via ``builds``) whenever the dispatch that a login
    triggers is part of what's under test — including tests that only need the
    slot it occupies. Fake the build underneath it, or the case is generated
    for real.
    """
    from vestigo.api.routers import auth as auth_mod
    from vestigo.core import demo_case as demo_mod

    monkeypatch.setattr(auth_mod, "maybe_seed_demo_case", demo_mod.maybe_seed_demo_case)


@pytest.fixture()
def builds(monkeypatch, seeds_on_login):
    """Record demo builds instead of generating and ingesting a real case.

    The build itself is exercised against live services in
    ``tests/test_demo_build_clickhouse.py``; everything here is about the
    dispatch path around it, so this fakes only the expensive build underneath
    the real seeder.

    Creates a real (empty) case flagged ``is_demo`` rather than only recording
    the call: the flag is what the case list filters on and what the restore
    endpoint refuses against, so a fake that skips it would leave both
    untested.
    """
    from vestigo.core import demo_case as demo_mod
    from vestigo.demo.build import DemoBuildResult

    calls = []

    async def _fake_build(store, clickhouse, owner_id, progress=None):
        # Keyed by owner, not by call count: seeds for two users can overlap,
        # and a count read before either appends gives both the same id.
        case_id = f"case_demo_{owner_id}"
        await store.create_case(
            case_id=case_id, name="Demo (test)", owner_id=owner_id, is_demo=True
        )
        # Recorded only once the case exists, so a test that waits on the call
        # count can then query for the case without racing it.
        calls.append({"owner": owner_id})
        return DemoBuildResult(case_id=case_id, events=3, sources=4, annotations=25)

    monkeypatch.setattr(demo_mod, "build_demo_case", _fake_build)
    return calls
