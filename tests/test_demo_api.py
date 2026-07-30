"""Demo case: seeding on login and the explicit restore endpoint.

Drives the real HTTP layer (see ``tests/conftest.py``) with the build faked, so
these assert the *dispatch* path — that a login seeds exactly once and a restore
seeds again — not the build itself. That lives in
``tests/test_demo_build_clickhouse.py``.
"""

from __future__ import annotations

import time

from tests.conftest import as_admin, login
from vestigo.core.config import get_settings


def _wait_for_builds(builds, expected, timeout=5.0):
    """Wait for the background seed task to run in the app's event loop.

    The seed job is dispatched with ``asyncio.create_task`` inside the request,
    so it completes shortly *after* the login response returns — polling is the
    honest way to observe it from a synchronous TestClient.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(builds) >= expected:
            return
        time.sleep(0.02)


def test_login_dispatches_a_seed_job_once(client, admin_bootstrap, builds):
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    _wait_for_builds(builds, 1)
    assert len(builds) == 1

    client.post("/api/auth/logout")
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    _wait_for_builds(builds, 2, timeout=1.0)
    assert len(builds) == 1, "second login must not seed again"


def test_login_still_works_when_seeding_is_off(client, admin_bootstrap, builds, monkeypatch):
    """Seeding off is not an error — it is simply no demo case."""
    monkeypatch.setenv("VESTIGO_DEMO_CASE_ENABLED", "false")
    get_settings.cache_clear()
    resp = client.post(
        "/api/auth/login",
        json={"username": admin_bootstrap["username"], "password": admin_bootstrap["password"]},
    )
    assert resp.status_code == 200
    _wait_for_builds(builds, 1, timeout=0.5)
    assert builds == []


def test_restore_endpoint_seeds_again(client, admin_bootstrap, builds):
    # as_admin also rotates the bootstrap password, which every mutating
    # endpoint requires before it will answer.
    as_admin(client, admin_bootstrap)
    _wait_for_builds(builds, 1)
    assert len(builds) == 1

    resp = client.post("/api/demo/seed")
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"]
    _wait_for_builds(builds, 2)
    assert len(builds) == 2, "restore is explicit, so the once-only claim does not apply"


def test_restore_endpoint_requires_auth(client):
    assert client.post("/api/demo/seed").status_code == 401


def test_restore_endpoint_503_when_disabled(client, admin_bootstrap, monkeypatch):
    as_admin(client, admin_bootstrap)
    monkeypatch.setenv("VESTIGO_DEMO_CASE_ENABLED", "false")
    get_settings.cache_clear()
    assert client.post("/api/demo/seed").status_code == 503
    monkeypatch.delenv("VESTIGO_DEMO_CASE_ENABLED")
    get_settings.cache_clear()


def test_restore_endpoint_429_when_too_many_seeds_are_running(client, admin_bootstrap, monkeypatch):
    """The build is CPU-heavy, so the cap is a real limit with its own code.

    The fake build never finishes, which is the only way to hold jobs in a
    running state long enough for the cap to be observable from a synchronous
    client.
    """
    import asyncio

    from vestigo.core import demo_case as demo_mod

    async def _never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(demo_mod, "build_demo_case", _never_finishes)
    as_admin(client, admin_bootstrap)  # consumes one slot via the first-login seed

    responses = [client.post("/api/demo/seed").status_code for _ in range(3)]
    assert 429 in responses, responses
    assert responses.count(200) <= demo_mod.MAX_CONCURRENT_SEEDS
