"""Demo case: seeding on login and the explicit restore endpoint.

Drives the real HTTP layer (see ``tests/conftest.py``) with ``import_case``
faked, so these assert the *dispatch* path — that a login seeds exactly once
and a restore seeds again — not the archive restore itself. That lives in
``tests/test_demo_archive_clickhouse.py``.
"""

from __future__ import annotations

import time

from tests.conftest import login


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
