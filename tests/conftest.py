"""Shared fixtures for the authentication/RBAC/audit test suite.

Every router now shares a single ``PostgresStore`` via ``api.deps.get_store``
(see ``tests/test_uploads.py``/``test_events_router.py`` for the same
monkeypatch pattern used against individual router modules before that
centralization). These fixtures spin up a full FastAPI app against a private,
already-migrated PostgreSQL database so auth/session/RBAC/audit behavior can be
exercised end-to-end through the real HTTP layer rather than by calling handlers
directly.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vestigo.api import deps
from vestigo.api.main import create_app
from vestigo.core import security
from vestigo.core.config import get_settings
from vestigo.core.login_backoff import reset_login_backoff
from vestigo.db.postgres import PostgresStore, User

# ---------------------------------------------------------------------------
# Backing services
# ---------------------------------------------------------------------------

#: How long the probe waits before calling ClickHouse down. Deliberately short:
#: this runs against a local container, and the whole point is to answer before
#: anyone has time to walk away from the terminal.
_PROBE_TIMEOUT_SECONDS = 1.5


def _probe_clickhouse(url: str) -> str | None:
    """Return None if ClickHouse answers a real query, else a one-line reason.

    Uses ``urllib`` rather than ``clickhouse_connect`` on purpose. The driver
    retries, and its failure surfaces as a paragraph of connection-pool
    traceback — which is exactly how a stopped container used to read as a
    mysterious test failure minutes into a run.

    It is a ``SELECT 1`` as the configured user and not ``/ping``, because
    ``/ping`` is answered before any user is resolved. A server whose ``default``
    user is restricted to localhost — what the stock ``users.d/default-user.xml``
    in images >= ~25.x does, so every ClickHouse reached across a container
    bridge — answers ``Ok.`` to the ping and refuses every query with
    REQUIRED_PASSWORD/194. That configuration cost a CI run six hours of red
    that this probe existed to prevent.
    """
    settings = get_settings()
    query = urllib.parse.urljoin(url.rstrip("/") + "/", "?query=SELECT+1")
    request = urllib.request.Request(
        query,
        headers={
            "X-ClickHouse-User": settings.clickhouse_username,
            "X-ClickHouse-Key": settings.clickhouse_password,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return f"HTTP {resp.status} from {query}"
            body = resp.read(64).decode("utf-8", "replace").strip()
        if body != "1":
            return f"unexpected answer to SELECT 1 from {query}: {body!r}"
        return None
    except urllib.error.HTTPError as exc:
        # ClickHouse puts its own diagnosis in the body — "not allowed from
        # network" names a fix, a bare 403 does not.
        detail = exc.read(200).decode("utf-8", "replace").strip().replace("\n", " ")
        return f"HTTP {exc.code} from {query}: {detail}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"{reason} ({query})"


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to start a run that cannot possibly pass.

    ClickHouse and PostgreSQL are both genuinely required (Qdrant is faked, so
    it is not probed), and a large share of the suite reaches them indirectly
    through the app rather than through anything marked ``clickhouse``. With a
    container stopped those tests do not skip: they each retry, then fail with a
    driver traceback that names a connection pool rather than the actual
    problem, and an eight-minute run ends in a wall of red that says nothing
    about the one thing that was wrong.

    So the reachability question is asked once, up front, in about a second,
    and answered with the command that fixes it. There is no opt-out: a run
    that cannot pass is not worth starting, and "most of it passed" is a worse
    outcome than a clear stop.
    """
    if config.option.collectonly or config.option.help:
        return

    settings = get_settings()
    started = time.monotonic()
    down = [
        (name, problem)
        for name, problem in (
            ("ClickHouse", _probe_clickhouse(settings.clickhouse_url)),
            ("PostgreSQL", _probe_postgres()),
        )
        if problem is not None
    ]
    elapsed = time.monotonic() - started
    if not down:
        return

    detail = "\n".join(f"  {name} — {problem}" for name, problem in down)
    pytest.exit(
        "\n"
        f"Backing services are not reachable (probed in {elapsed:.1f}s):\n"
        f"{detail}\n"
        "\n"
        "  The test suite needs them: most tests reach ClickHouse and Postgres\n"
        "  through the app, and without them they fail slowly with driver\n"
        "  tracebacks instead of saying this. Start the dev stack and re-run:\n"
        "\n"
        "      podman compose up -d\n",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )


# ---------------------------------------------------------------------------
# Per-test PostgreSQL databases
# ---------------------------------------------------------------------------
#
# Tests run against the real thing, not SQLite. The two dialects disagree about
# exactly the things this model leans on — `JSONB` equality, `json` having no
# equality operator at all, server defaults — and a suite that cannot see those
# differences cannot see the bugs that live in them: the `confirmed` disposition
# path shipped broken on PostgreSQL while every SQLite test passed.
#
# It is also *faster*. Alembic runs per test either way; against PostgreSQL a
# template database replays the migrations once per session and every test
# clones it (~55ms vs ~101ms replaying them into a fresh SQLite file).

#: Prefix for per-test clones, and what the orphan sweep matches on.
_TEST_DB_PREFIX = "vestigo_test_"
#: Prefix for session templates, kept distinct from the clone prefix so the
#: clone sweep cannot reach one.
_TEMPLATE_DB_PREFIX = "vestigo_tpl_"
#: Migrated once per session; every test's database is a clone of it.
#:
#: Unique per run, because the name used to be a constant and two suites
#: against one PostgreSQL then shared it: the second run's `CREATE DATABASE`
#: collided, and its teardown dropped the template out from under the first,
#: which failed every remaining test with "template database does not exist" —
#: a message that names neither the cause nor the other run.
_TEMPLATE_DB = f"{_TEMPLATE_DB_PREFIX}{uuid.uuid4().hex[:12]}"


def _admin_url() -> URL:
    """The configured server, pointed at `postgres` so we can create databases."""
    return make_url(get_settings().postgres_url).set(database="postgres")


def _db_url(name: str) -> str:
    # `str(URL)` masks the password as `***`; the rendered form is what a driver
    # can actually connect with.
    return (
        make_url(get_settings().postgres_url)
        .set(database=name)
        .render_as_string(hide_password=False)
    )


def _admin_sql(*statements: str) -> list:
    """Run DDL against the server itself, returning the last result's rows.

    Synchronous by design — every caller is a sync fixture — but built on the
    project's only PostgreSQL driver rather than pulling in psycopg2 for this.
    AUTOCOMMIT because CREATE/DROP DATABASE cannot run inside a transaction, and
    the engine is disposed before returning: a lingering pooled connection would
    block the next DROP against whatever it was attached to.
    """

    async def _run() -> list:
        engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                rows: list = []
                for sql in statements:
                    result = await conn.exec_driver_sql(sql)
                    # DDL returns no rows at all, and asking a closed result for
                    # them raises rather than yielding an empty list.
                    rows = list(result.scalars().all()) if result.returns_rows else []
                return rows
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _probe_postgres() -> str | None:
    """Return None if the configured PostgreSQL accepts a connection."""
    try:
        _admin_sql("SELECT 1")
        return None
    except Exception as exc:  # noqa: BLE001 — any failure is the same answer here
        return f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"


@pytest.fixture(scope="session", autouse=True)
def _cheap_password_hashing() -> Iterator[None]:
    """Argon2 at test cost, not production cost.

    The default `PasswordHasher()` is tuned to be slow on purpose — ~50 ms to
    hash and ~45 ms to verify on this hardware. That is the right number in
    production and pure overhead here: a single API test file spent two of its
    seventeen seconds inside argon2, and the suite logs in thousands of times.

    Same library and same API, so every hash produced is still a real argon2
    hash and `verify_password` still exercises the real verifier — only the
    work factors change. Nothing asserts on the parameters, and the production
    ones are the module default this replaces, so a test cannot pass because
    of this and then fail in production.
    """
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)
    original, security._hasher = security._hasher, cheap
    try:
        yield
    finally:
        security._hasher = original


@pytest.fixture(scope="session", autouse=True)
def _pg_template():
    """Build the template database once, and clean up after killed runs.

    The sweep matters more than it looks: an interrupted run leaves its clone
    behind, and without this they accumulate silently until someone wonders why
    the dev server has four hundred databases.
    """
    clones = _TEST_DB_PREFIX.replace("_", r"\_") + "%"
    templates = _TEMPLATE_DB_PREFIX.replace("_", r"\_") + "%"
    # Only databases nobody is connected to. A second suite running right now
    # holds connections to its own clones, and dropping those out from under it
    # would fail that run with something it has no way to explain.
    #
    # An idle template is the case that check cannot see: a live run's template
    # carries no connection between clones, so it looks exactly like an orphan.
    # So templates are swept only when nothing at all is connected to a test
    # database — i.e. when no other suite is running. Ours is created after,
    # and named per run, so a concurrent suite can never take it.
    stale = _admin_sql(
        "SELECT d.datname FROM pg_database d "
        f"WHERE d.datname LIKE '{clones}' "
        "AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)"
    )
    busy = _admin_sql(
        "SELECT 1 FROM pg_stat_activity a JOIN pg_database d ON d.datname = a.datname "
        f"WHERE d.datname LIKE '{clones}' OR d.datname LIKE '{templates}' LIMIT 1"
    )
    if not busy:
        stale += _admin_sql(
            "SELECT d.datname FROM pg_database d "
            f"WHERE d.datname LIKE '{templates}' "
            "AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)"
        )
    for name in stale:
        _admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    _admin_sql(f'CREATE DATABASE "{_TEMPLATE_DB}"')

    # Migrate it exactly once. Every test then pays a clone, not an upgrade.
    async def _migrate():
        s = PostgresStore(url=_db_url(_TEMPLATE_DB))
        await s.init_schema()
        await s.engine.dispose()

    asyncio.run(_migrate())
    yield
    _admin_sql(f'DROP DATABASE IF EXISTS "{_TEMPLATE_DB}" WITH (FORCE)')


@pytest.fixture()
def pg_database(_pg_template) -> Iterator[str]:
    """A private, already-migrated database URL for one test."""
    name = f"{_TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    _admin_sql(f'CREATE DATABASE "{name}" TEMPLATE "{_TEMPLATE_DB}"')
    try:
        yield _db_url(name)
    finally:
        # FORCE, because a test that failed mid-request can leave a pooled
        # connection open and a plain DROP would fail the teardown instead of
        # the test — reporting the wrong thing as broken.
        _admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def module_pg_database(_pg_template) -> Iterator[str]:
    """One database shared by a module, for fixtures that seed once and reuse.

    Same guarantees as :func:`pg_database`, different lifetime — the modules
    that build an expensive corpus (the demo case) cannot afford to rebuild it
    per test.
    """
    name = f"{_TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    _admin_sql(f'CREATE DATABASE "{name}" TEMPLATE "{_TEMPLATE_DB}"')
    try:
        yield _db_url(name)
    finally:
        _admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def blank_pg_database(_pg_template) -> Iterator[str]:
    """An *empty* database — no tables, no `alembic_version`.

    For the tests that are about schema management itself, which have to start
    from nothing and drive Alembic themselves.
    """
    name = f"{_TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    _admin_sql(f'CREATE DATABASE "{name}"')
    try:
        yield _db_url(name)
    finally:
        _admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest_asyncio.fixture()
async def store(pg_database, request, monkeypatch):
    """The store every router shares via api.deps.get_store(), on real Postgres.

    Pooled by default. asyncpg binds a connection to the event loop that opened
    it, so a test that drives work on more than one loop — a `TestClient` built
    outside a `with` block gets its own portal loop, and the CLI runs each
    command in its own ``asyncio.run`` — cannot reuse a pooled connection and
    fails with "Event loop is closed", which reads like a failure about
    whatever the test was actually asserting.

    ``NullPool`` fixes that by never reusing a connection, but it costs a
    connect per operation: applied to every test it made the suite roughly
    three times slower. So it is opt-in, by marker, for the handful of tests
    that genuinely need it::

        @pytest.mark.multiloop
        def test_two_sessions(client, store): ...
    """
    # `TestClient` drives the app from its own portal thread and loop, and a
    # test may hold two of them at once (two logged-in sessions). asyncpg binds
    # a connection to the loop that opened it, so a pooled connection crossing
    # that boundary raises "Event loop is closed" — a failure about plumbing
    # that reads like a failure about whatever was being asserted.
    #
    # Not pooling costs a connect per operation, so it is applied where it is
    # needed rather than everywhere: any test that takes `client`, plus anything
    # marked `multiloop` (the CLI, which runs each command in its own
    # `asyncio.run`). Tests that only await the store directly stay pooled.
    unpooled = "client" in request.fixturenames or request.node.get_closest_marker("multiloop")
    s = PostgresStore(url=pg_database, **({"poolclass": NullPool} if unpooled else {}))
    monkeypatch.setattr(deps, "_store", s)
    yield s
    # No `engine.dispose()`. Its connections may belong to a `TestClient`'s
    # portal loop rather than this one, and disposing across that boundary
    # raises "attached to a different loop" *during teardown* — failing a test
    # that already passed. Nothing leaks: `pg_database` drops the whole
    # database WITH (FORCE) a moment later, which closes them server-side.


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
def stub_embeddings_probe(monkeypatch):
    """Keep the embeddings availability probe off the network.

    ``models/availability.py`` decides the ``embeddings`` capability by asking
    the vector store to list its collections (and, with a remote endpoint
    configured, by embedding a token against it). Qdrant is faked per test
    rather than run — the suite deliberately requires only ClickHouse and
    Postgres — so a real probe would make every ``/api/health`` call depend on
    a service that is up on one developer's box and not the next.

    The stub answers with the configuration half, which is what the suite's
    existing assertions are about. Tests that are about the probe patch its
    arms themselves.
    """
    from vestigo.models import availability

    async def _configured_only() -> bool:
        return availability.model_configured() and availability.vector_store_configured()

    monkeypatch.setattr(availability, "_probe", _configured_only)
    availability.reset_probe_cache()
    yield
    availability.reset_probe_cache()


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

    Raises the concurrency cap off its default of 1. A refused seed is dropped,
    not queued, so at the default a test that seeds two users races: if the
    first build is still in flight when the second logs in, the second never
    happens. Tests that are about the cap set their own value.
    """
    from vestigo.core import demo_case as demo_mod
    from vestigo.demo.build import DemoBuildResult

    monkeypatch.setenv("VESTIGO_DEMO_MAX_CONCURRENT", "8")
    get_settings.cache_clear()

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
