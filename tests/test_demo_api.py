"""Demo case: seeding on login and the explicit restore endpoint.

Drives the real HTTP layer (see ``tests/conftest.py``) with ``import_case``
faked, so these assert the *dispatch* path — that a login seeds exactly once
and a restore seeds again — not the archive restore itself. That lives in
``tests/test_demo_archive_clickhouse.py``.
"""

from __future__ import annotations

import time

from tests.conftest import as_admin, login
from vestigo.core.config import get_settings


def _wait_for_imports(imports, expected, timeout=5.0):
    """Wait for the background seed task to run in the app's event loop.

    The seed job is dispatched with ``asyncio.create_task`` inside the request,
    so it completes shortly *after* the login response returns — polling is the
    honest way to observe it from a synchronous TestClient.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(imports) >= expected:
            return
        time.sleep(0.02)


def test_login_dispatches_a_seed_job_once(client, admin_bootstrap, fake_archive, imports):
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    _wait_for_imports(imports, 1)
    assert len(imports) == 1

    client.post("/api/auth/logout")
    login(client, admin_bootstrap["username"], admin_bootstrap["password"])
    _wait_for_imports(imports, 2, timeout=1.0)
    assert len(imports) == 1, "second login must not seed again"


def test_login_still_works_without_an_archive(client, admin_bootstrap, imports):
    """No packaged archive is not an error — it is simply no demo case."""
    resp = client.post(
        "/api/auth/login",
        json={"username": admin_bootstrap["username"], "password": admin_bootstrap["password"]},
    )
    assert resp.status_code == 200
    _wait_for_imports(imports, 1, timeout=0.5)
    assert imports == []


def test_restore_endpoint_seeds_again(client, admin_bootstrap, fake_archive, imports):
    # as_admin also rotates the bootstrap password, which every mutating
    # endpoint requires before it will answer.
    as_admin(client, admin_bootstrap)
    _wait_for_imports(imports, 1)
    assert len(imports) == 1

    resp = client.post("/api/demo/seed")
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"]
    _wait_for_imports(imports, 2)
    assert len(imports) == 2, "restore is explicit, so the once-only claim does not apply"


def test_restore_endpoint_requires_auth(client, fake_archive):
    assert client.post("/api/demo/seed").status_code == 401


def test_restore_endpoint_503_when_disabled(client, admin_bootstrap, fake_archive, monkeypatch):
    as_admin(client, admin_bootstrap)
    monkeypatch.setenv("VESTIGO_DEMO_CASE_ENABLED", "false")
    get_settings.cache_clear()
    assert client.post("/api/demo/seed").status_code == 503
    monkeypatch.delenv("VESTIGO_DEMO_CASE_ENABLED")
    get_settings.cache_clear()


def test_restore_endpoint_503_without_an_archive(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    assert client.post("/api/demo/seed").status_code == 503
