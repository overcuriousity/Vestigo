"""A filter ClickHouse refuses to parse must surface as an actionable 4xx.

The raw failure is a Poco `code: 1000` "Field value too long" DatabaseError
raised deep inside the query layer, which reached the client as an opaque 500
with a ClickHouse-internal message (issue #181). The query layer translates it
into `QueryRequestTooLargeError`; this covers the app-level mapping to 413.
"""

from __future__ import annotations

from clickhouse_connect.driver.exceptions import DatabaseError
from fastapi.testclient import TestClient

from vestigo.api.main import create_app
from vestigo.db.queries import (
    QueryMemoryExceededError,
    QueryRequestTooLargeError,
    _mapped_database_error,
)


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


def test_query_memory_exceeded_maps_to_413() -> None:
    """The sibling failure: a scan killed at the cap `db/_scan.py` pins.

    That cap exists so one over-broad scan dies instead of the server, which
    makes hitting it an ordinary outcome with an obvious remedy — the analyst
    has to be told to ask for less, not shown ClickHouse's allocator accounting
    in a 500.
    """
    app = create_app()

    @app.get("/api/health/_out_of_memory", include_in_schema=False)
    async def _boom() -> None:
        raise QueryMemoryExceededError("this query needs more memory than allowed — narrow it")

    app.router.routes.insert(0, app.router.routes.pop())

    response = TestClient(app).get("/api/health/_out_of_memory")

    assert response.status_code == 413
    assert "memory" in response.json()["detail"].lower()


def test_clickhouse_memory_error_is_translated() -> None:
    """`code: 241` is recognized by code *and* by name.

    clickhouse-connect's message text has changed shape across releases, so
    matching only one of the two would make this mapping quietly regress into
    the 500 it exists to prevent.
    """
    by_name = _mapped_database_error(
        DatabaseError("Code: 241. DB::Exception: Memory limit (for query) exceeded")
    )
    by_code = _mapped_database_error(DatabaseError("HTTPDriver received code: 241, some detail"))
    unrelated = _mapped_database_error(DatabaseError("code: 62. Syntax error"))

    assert isinstance(by_name, QueryMemoryExceededError)
    assert isinstance(by_code, QueryMemoryExceededError)
    assert unrelated is None
