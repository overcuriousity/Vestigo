"""A filter ClickHouse refuses to parse must surface as an actionable 4xx.

The raw failure is a Poco `code: 1000` "Field value too long" DatabaseError
raised deep inside the query layer, which reached the client as an opaque 500
with a ClickHouse-internal message (issue #181). The query layer translates it
into `QueryRequestTooLargeError`; this covers the app-level mapping to 413.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from vestigo.api.main import create_app
from vestigo.db.queries import QueryRequestTooLargeError


def test_query_request_too_large_maps_to_413() -> None:
    app = create_app()

    # Registered under an auth-exempt prefix so the test exercises the
    # exception handler rather than the auth middleware, and inserted ahead of
    # the SPA catch-all, which otherwise answers 200 with the app shell when
    # `frontend/dist` happens to be built.
    @app.get("/api/health/_too_large", include_in_schema=False)
    async def _boom() -> None:
        raise QueryRequestTooLargeError("the filter is too large")

    app.router.routes.insert(0, app.router.routes.pop())

    # No lifespan: startup would connect to the real backing services, and
    # this test only needs the exception handler wired into the app.
    response = TestClient(app).get("/api/health/_too_large")

    assert response.status_code == 413
    assert "filter" in response.json()["detail"].lower()
