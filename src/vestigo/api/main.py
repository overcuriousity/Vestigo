"""FastAPI application factory and API routers."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from vestigo import __version__
from vestigo.api import scan_exec
from vestigo.api.deps import get_store, resolve_user_optional
from vestigo.api.request_context import RequestContextMiddleware
from vestigo.api.routers import (
    admin,
    agent,
    agent_tokens,
    analysis,
    auth,
    baselines,
    cases,
    converters,
    demo,
    dispositions,
    enrichers,
    events,
    jobs,
    sigma,
    stories,
    stream,
    transfer,
    viz,
)
from vestigo.core.capabilities import get_capabilities
from vestigo.core.config import env_layer_value, get_settings
from vestigo.core.demo_case import cancel_pending_seeds
from vestigo.core.runtime_settings import load_runtime_settings
from vestigo.core.security import hash_password
from vestigo.db._scan import scan_budget_report
from vestigo.db.postgres import EnrichmentJobRun, PostgresStore, User, generate_id
from vestigo.db.queries import QueryMemoryExceededError, QueryRequestTooLargeError

logger = logging.getLogger(__name__)

_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

# API paths reachable without an authenticated session. Everything else under
# /api/* requires a valid session cookie (enforced by the middleware below);
# the SPA catch-all route serves static files only, so it stays exempt too.
_AUTH_EXEMPT_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/oidc/",
    "/api/docs",
    "/api/openapi.json",
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


def _requires_password_current(path: str, method: str) -> bool:
    """Whether ``method path`` should be blocked while a password rotation is pending.

    Every mutating ``/api/*`` request is gated except the self-service
    ``/api/auth/*`` routes (login, logout, profile update, and the
    change-password endpoint itself) — a user stuck in forced rotation must
    still be able to log out or change their password. Case/events routers
    also apply ``deps.require_password_current`` directly as defense in
    depth; this is the actual enforcement boundary, closing the gap where
    ``admin.py`` never opted in to that per-route dependency (PR #7 review
    finding #1).
    """
    return method in _MUTATING_METHODS and not path.startswith("/api/auth/")


async def _seed_admin() -> None:
    """Seed the first administrator on startup if no users exist yet.

    The seeded password is one-time: ``must_change_password=True`` forces a
    rotation on first login, which invalidates ``VESTIGO_ADMIN_PASSWORD`` the
    moment it's changed (see ``auth.change_my_password``).
    """
    settings = get_settings()
    store = get_store()
    if await store.list_users():
        return
    if not settings.admin_password:
        logger.error(
            "No users exist yet and VESTIGO_ADMIN_PASSWORD is not set. Set it and "
            "restart to bootstrap the first administrator account."
        )
        return
    password_hash = await asyncio.to_thread(hash_password, settings.admin_password)
    await store.create_user(
        user_id=generate_id("user"),
        username=settings.admin_username,
        password_hash=password_hash,
        is_admin=True,
        must_change_password=True,
    )
    logger.info(
        "Seeded administrator account %r (password must be changed on first login).",
        settings.admin_username,
    )


async def _reconcile_orphaned_ingests() -> None:
    """Clean up sources stuck in "ingesting" from a mid-ingest restart.

    Ingestion jobs live in the in-memory JobStore, so on a fresh boot any
    source still marked "ingesting" has no job that will ever finish it.
    Its partial ClickHouse events and its row are removed — the same cleanup
    a failed ingest performs — so the file can simply be re-uploaded (the
    duplicate check is keyed on file_hash and would otherwise reject the
    retry forever). Each removal is recorded in the audit log.

    Best-effort: ClickHouse being unreachable at startup must not prevent
    the app from booting; the orphan stays "ingesting" (still invisible to
    queries) and is retried on the next restart.
    """
    store = get_store()
    orphans = await store.list_ingesting_sources()
    if not orphans:
        return
    from vestigo.api.routers.cases import _retention_path
    from vestigo.db.clickhouse import ClickHouseStore

    try:
        clickhouse = ClickHouseStore()
    except Exception:
        logger.exception(
            "Failed to reach ClickHouse while reconciling %d orphaned ingesting source(s); "
            "retrying on next restart.",
            len(orphans),
        )
        return

    # Each orphan is cleaned up independently — a failure on one (e.g. a
    # transient ClickHouse error on its partition) must not stop the rest
    # of the batch from being reconciled this boot.
    for source in orphans:
        try:
            await asyncio.to_thread(clickhouse.delete_source_events, source.case_id, source.id)
            await store.delete_source(source.case_id, source.id)
            if not await store.source_hash_in_use(source.file_hash, exclude_source_id=source.id):
                _retention_path(source.file_hash).unlink(missing_ok=True)
            await store.record_audit(
                action="source.ingest_interrupted",
                case_id=source.case_id,
                target_type="source",
                target_id=source.id,
                detail={"filename": source.filename, "file_hash": source.file_hash},
            )
            logger.warning(
                "Removed source %r (case %s): its ingestion was interrupted by a restart. "
                "Re-upload the file to ingest it.",
                source.name,
                source.case_id,
            )
        except Exception:
            logger.exception(
                "Failed to reconcile orphaned source %r (case %s); it stays 'ingesting' and "
                "will be retried on the next restart.",
                source.name,
                source.case_id,
            )


async def _sweep_stale_transfer_archives() -> None:
    """Export archives live in temp storage and the job store is in-memory —
    after a restart every leftover is orphaned by definition.

    Age-independent, unlike the per-export sweep: this assumes one process per
    configured ``transfer_temp_path``, which the in-memory JobStore already
    requires. In-flight archives are additionally expired by
    ``archive.sweep_stale``'s TTL on each export, so a long-running process
    does not accumulate them.

    It removes only what a transfer job writes (see ``is_transfer_artifact``),
    never the directory itself: ``transfer_temp_path`` is operator-configurable
    and pointing it at, say, ``/data`` must not wipe ``/data`` on every boot.

    Failures are swallowed here rather than left to the caller's handler:
    ``temp_root`` refuses a misowned directory, and a misconfigured
    ``transfer_temp_path`` must cost no more than this sweep — not the ingest
    reconciliation and session purge that follow it."""
    from vestigo.transfer.archive import sweep_stale

    try:
        sweep_stale(max_age_seconds=None)
    except Exception:
        logger.exception("Transfer archive sweep failed; leftover export archives remain.")


async def _reconcile_stale_converter_generations(store: PostgresStore) -> None:
    """Fail every converter script still ``generating`` — its job did not survive the restart.

    Same reasoning as the ingest reconciliation above: the JobStore is
    in-memory, so a row in this state on boot has nothing left to finish it,
    and it would sit in the panel as "generating" forever, neither reusable
    nor regenerable as a failed draft. The row and its attempts stay; only the
    status flips, and the trail records why.

    Runs *before* the lifespan yields, like
    ``_settle_orphaned_column_recommendations`` and for the same two reasons:
    it is one fast Postgres statement that touches no external service, and it
    fails *every* ``generating`` row — so it must run before the app can
    accept a conversion, or (queued behind a slow ClickHouse sweep in
    ``_startup_recovery``) it would flip a live generation to ``failed`` and
    plant a bogus "interrupted by a server restart" attempt on it.
    """
    try:
        rows = await store.fail_stale_converter_generations()
    except Exception:  # noqa: BLE001 — reconciliation must never block startup
        logger.exception("Converter generation reconciliation failed")
        return
    for row in rows:
        logger.warning(
            "Marked converter script %s (%s v%s, case %s) failed: still generating on startup",
            row.id,
            row.name,
            row.version,
            row.case_id,
        )


async def _reconcile_orphaned_enrichment_jobs() -> list[EnrichmentJobRun]:
    """Recover enrichment jobs left running by a mid-run restart. See ``enrichers/jobs.py``.

    Returns the recovered runs so the lifespan can schedule re-runs once
    enricher availability has been refreshed.
    """
    from vestigo.db.clickhouse import ClickHouseStore
    from vestigo.enrichers.jobs import reconcile_orphaned_enrichment_jobs

    store = get_store()
    try:
        ch_store = ClickHouseStore()
        # A crash mid-apply can leave tmp_enrich_* scratch tables behind;
        # they carry no state (Postgres staging is the source of truth).
        dropped = await asyncio.to_thread(ch_store.drop_stale_enrichment_scratch_tables)
        if dropped:
            logger.info("Dropped %d stale enrichment scratch table(s).", dropped)
        return await reconcile_orphaned_enrichment_jobs(store, ch_store)
    except Exception:
        logger.exception("Failed to reconcile orphaned enrichment jobs; retrying on next restart.")
        return []


def _redact_url(url: str | None) -> str:
    """Strip credentials from a URL for logging."""
    if not url:
        return "(unset)"
    parsed = urlsplit(url if "://" in url else f"//{url}")
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        netloc = f"***@{host}" + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit(parsed._replace(netloc=netloc))
    return url


# Query parameters whose *value* is a credential. The OIDC redirect carries an
# authorization code and its CSRF state in the URL — that is the protocol, not
# a flaw — but uvicorn's access log writes the full query string, so without
# this every login puts a live credential into the system journal, which is
# routinely readable by more people than the session store is.
#
# This is a *name list*, not a heuristic: a parameter whose name is not here is
# logged in full. Anything added to the API that puts a secret in a query string
# has to be added here too — lowercase, since matching folds case.
#
# Scope stops at this process. A fronting reverse proxy writes its own access
# log from the request it received, unredacted — `nginx-tls.conf` ships in this
# repo and `docs/DEPLOYMENT.md` §"TLS reverse proxy (nginx)" is the deployment
# it describes. An operator terminating TLS upstream has to scrub or disable
# that log separately; nothing here can reach it.
_SECRET_QUERY_PARAMS = frozenset(
    {
        "code",
        "state",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "session_state",
        "client_secret",
        "agent_token",
        "api_key",
        "apikey",
        "password",
        "signature",
    }
)
REDACTED = "***"


def redact_query(target: str) -> str:
    """Replace the value of every sensitive query parameter in ``target``.

    Matching is on the whole parameter name, percent-decoded and folded to
    lowercase — a sensitive name appearing as a *substring* of another
    parameter is not a match, a provider that capitalizes ``Code`` does not
    slip through, and neither does one that sends ``%63ode``. Only names in
    ``_SECRET_QUERY_PARAMS`` are redacted; nothing is inferred from the value.

    The name is *emitted* exactly as it arrived, decoded only to decide. An
    operator reading the journal should see what the client actually sent.

    Args:
        target: A request target, with or without a query string.

    Returns:
        The same target with sensitive values replaced by ``***``. Parameter
        names, order and the path are preserved, so the log stays readable and
        an operator can still see *that* a callback carried a code.
    """
    path, sep, query = target.partition("?")
    if not sep or not query:
        return target
    parts = []
    for pair in query.split("&"):
        name, eq, _value = pair.partition("=")
        secret = bool(eq) and unquote_plus(name).lower() in _SECRET_QUERY_PARAMS
        parts.append(f"{name}={REDACTED}" if secret else pair)
    return f"{path}?{'&'.join(parts)}"


class AccessLogRedactor(logging.Filter):
    """Scrub credentials out of uvicorn's access log records.

    Uvicorn formats access lines from the record's ``args``, where the third
    item is the request target including its query string. Rewriting the tuple
    here catches every route at once, which is what makes this hold for
    whatever query parameter the next subsystem adds.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_query(args[2]), *args[3:])
        return True


def _log_config_report() -> None:
    """One startup block stating the resolved deployment-critical config."""
    settings = get_settings()
    logger.info(
        "Config: environment=%s offline=%s (VESTIGO_ALLOW_ONLINE=%s%s) "
        "postgres=%s clickhouse=%s qdrant=%s audit_enabled=%s oidc_enabled=%s "
        "auth_cookie_secure=%s",
        settings.environment,
        not settings.allow_online,
        settings.allow_online,
        "" if settings.allow_online else ", HF_HUB_OFFLINE forced for embedding models",
        _redact_url(settings.postgres_url),
        _redact_url(settings.clickhouse_url),
        settings.qdrant_path or _redact_url(settings.qdrant_url),
        settings.audit_enabled,
        settings.oidc_enabled,
        settings.auth_cookie_secure,
    )
    if settings.environment == "production" and not settings.auth_cookie_secure:
        logger.warning(
            "environment=production but VESTIGO_AUTH_COOKIE_SECURE=false — session cookies "
            "will be sent over plain HTTP. Set VESTIGO_AUTH_COOKIE_SECURE=1 behind TLS."
        )
    # Retired in 0033 along with the separate agent settings row. Settings
    # ignores unknown VESTIGO_* variables, so an operator who pinned this to
    # env-only would otherwise see the LLM key silently become DB-storable
    # again with nothing in the log to say why. Asked of the whole environment
    # layer, .env included: that file is how .env.example tells operators to
    # set it, so an os.environ check alone would stay quiet for exactly the
    # deployments the warning is for.
    if env_layer_value("VESTIGO_AGENT_SECRET_MODE") is not None:
        logger.warning(
            "VESTIGO_AGENT_SECRET_MODE is set but no longer read — the LLM API key now "
            "follows the instance-wide VESTIGO_SECRETS_MODE. Set that instead and unset this."
        )


async def _refresh_enricher_availability() -> None:
    """Fill the enricher availability cache before the first request.

    Deliberately *not* part of ``_startup_recovery``: an enricher's
    ``check_availability()`` is a local filesystem check, so it neither needs
    nor deserves the ClickHouse-deferred treatment, and the ``enrichers``
    capability reads that cache — leaving it cold made the whole Enrichment UI
    disappear whenever a recovery step above it raised. Recovery still refreshes
    it again before scheduling re-runs; the call is idempotent.
    """
    try:
        from vestigo.enrichers.registry import refresh_availability

        await asyncio.to_thread(refresh_availability)
    except Exception:
        logger.exception("Could not determine enricher availability at startup.")


async def _settle_orphaned_column_recommendations(store: PostgresStore) -> None:
    """Relabel column recommendations a restart left mid-flight (issue #213).

    Deliberately *not* part of ``_startup_recovery``, for the same reason as
    ``_refresh_enricher_availability``: this is one fast Postgres statement
    that touches no external service, and a ClickHouse-dependent step failing
    above it must not leave a timeline polling forever for a job that died
    with the previous process.
    """
    try:
        settled = await store.clear_stale_running_recommendations()
        if settled:
            logger.info("Settled %d column recommendation(s) orphaned by a restart.", settled)
    except Exception:
        logger.exception("Could not settle orphaned column recommendations.")


async def _probe_embeddings_availability() -> None:
    """Fill the embeddings availability cache before the first health poll.

    Deliberately *not* awaited in the lifespan the way
    ``_refresh_enricher_availability`` is: that one is a local filesystem
    check, while this opens a socket to Qdrant and possibly to a remote
    embedding endpoint. Booting the HTTP server must not depend on either
    answering — the same rule ``_startup_recovery`` exists for.

    Skipping it would not break anything: ``embeddings_operational`` probes on
    a cold cache anyway, so the first ``/api/health`` poll would pay for it.
    Doing it here means that poll is a dict read, and that the capability is
    already truthful when the first browser asks.
    """
    try:
        from vestigo.models.availability import embeddings_operational, unavailable_detail

        available = await embeddings_operational(force=True)
        if not available:
            # The reason, not a guess at it: "did not answer" was the only line
            # this ever logged, and it is the wrong one for the two commonest
            # cases — the operator switch is off, or the local weights are not
            # on an airgapped host. Neither involved anything failing to answer.
            logger.info(
                "Embedding features are unavailable on this instance, so nothing "
                "embedding-related is offered in the UI. %s",
                unavailable_detail(),
            )
    except Exception:
        logger.exception("Could not determine embeddings availability at startup.")


async def _probe_scan_budget() -> None:
    """Size the heavy-scan budget from ClickHouse's ceiling, not the app's host.

    Until this runs, ``db/_scan.py`` falls back to the memory *this process*
    can see, which in a full-docker stack is the whole host — the app then
    authorizes a per-query cap larger than the ClickHouse container is allowed
    to use in total, every guardrail reads as satisfied, and the kernel does
    the enforcing (session-186). Asking the server what it may use removes the
    guess.

    Runs here, in the background task, rather than in the lifespan proper: it
    is the first thing that touches ClickHouse, and booting the HTTP server
    must not depend on ClickHouse answering. A scan that beats this probe uses
    the fallback budget, which is the same one every release before this used.
    """
    from vestigo.db._scan import (
        configure_scan_budget,
        configure_scan_threads,
        resolve_cache_bytes,
        resolve_clickhouse_ceiling,
        scan_budget_report,
    )
    from vestigo.db.clickhouse import ClickHouseStore

    facts = await asyncio.to_thread(ClickHouseStore().server_resource_facts)
    ceiling, bounded = resolve_clickhouse_ceiling(facts)
    cache_bytes, cache_breakdown = resolve_cache_bytes(facts)
    configure_scan_budget(ceiling, bounded, cache_bytes, cache_breakdown)
    configure_scan_threads(
        int(facts.get("resolved_max_threads", 0) or 0) or None,
        # A server that answered but did not say which kind of value it gave is
        # treated as auto, which is what every unpinned ClickHouse reports.
        is_auto=bool(facts.get("max_threads_is_auto", 1)),
    )
    report = scan_budget_report()
    if report["risk"] == "unbounded":
        logger.warning(
            "ClickHouse reports no memory ceiling of its own (%s). Background merges and "
            "caches are bounded by nothing but the kernel, which kills the server without "
            "logging anything. Set max_server_memory_usage on the server (and a container "
            "memory limit) — see docs/DEPLOYMENT.md 'Resource sizing'. Scan budget resolved "
            "to %.1f GiB total across %d slot(s) from %s detection.",
            "no max_server_memory_usage and no container limit"
            if facts
            else "the probe could not read system.server_settings",
            report["total_bytes"] / (1 << 30),
            report["concurrency"],
            report["source"],
        )
    elif report["risk"] == "over_budget":
        logger.error(
            "Scan budget (%.1f GiB across %d heavy slot(s) plus the chart lane) plus ClickHouse's own caches "
            "(%.1f GiB) exceeds what ClickHouse is allowed to use in total (%.1f GiB). "
            "Admitting a full set of scans can only end in a memory error or an OOM kill. "
            "Lower VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES, shrink the caches in "
            "deploy/clickhouse/memory.xml, or raise max_server_memory_usage.",
            report["total_bytes"] / (1 << 30),
            report["concurrency"],
            report["cache_bytes"] / (1 << 30),
            report["clickhouse_ceiling_bytes"] / (1 << 30),
        )
    else:
        logger.info(
            "Scan budget: %.1f GiB total (%.1f GiB per heavy query x %d, plus %d chart queries "
            "at %.1f GiB) under ClickHouse's %.1f GiB ceiling, with %.1f GiB of server caches "
            "counted; %d threads per scan (%s).",
            report["total_bytes"] / (1 << 30),
            report["per_query_bytes"] / (1 << 30),
            report["concurrency"],
            report["foreground"]["concurrency"],
            report["foreground"]["per_query_bytes"] / (1 << 30),
            report["clickhouse_ceiling_bytes"] / (1 << 30),
            report["cache_bytes"] / (1 << 30),
            report["max_threads"],
            report["max_threads_source"],
        )


async def _startup_recovery(store: PostgresStore) -> None:
    """Best-effort recovery + housekeeping, run *after* the app is serving.

    Deliberately not awaited inside the lifespan before ``yield``: every step
    here touches ClickHouse (orphan reconciliation applies staged rows, re-runs
    query timelines), and a slow or unreachable ClickHouse would otherwise wedge
    the ASGI lifespan startup — uvicorn never begins accepting connections and
    the reverse proxy returns 502. Booting the HTTP server must not depend on
    ClickHouse being reachable; each step below already self-heals on the next
    restart if it fails, so running them in the background is safe.
    """
    try:
        # First: every step below, and every request already being served, can
        # start a heavy scan, and each one reads the budget this sets.
        try:
            await _probe_scan_budget()
        except Exception:
            logger.exception("Could not size the scan budget from ClickHouse; using detection.")
        await _probe_embeddings_availability()
        await _sweep_stale_transfer_archives()
        await _reconcile_orphaned_ingests()
        enrichment_reruns = await _reconcile_orphaned_enrichment_jobs()

        from vestigo.enrichers.registry import refresh_availability

        # Re-runs are scheduled only after availability is refreshed — they skip
        # enrichers whose runtime requirements (e.g. GeoIP database) are missing.
        await asyncio.to_thread(refresh_availability)
        if enrichment_reruns:
            from vestigo.core.jobs import get_job_store
            from vestigo.enrichers.jobs import schedule_enrichment_reruns

            try:
                await schedule_enrichment_reruns(enrichment_reruns, get_job_store(), store)
            except Exception:
                logger.exception("Failed to schedule enrichment re-runs after recovery.")
        # No cron/scheduler in this single-process deployment (see JobStore),
        # so a startup-only sweep is the simple option — good enough to keep
        # `sessions` from growing unbounded across restarts without adding a
        # background task loop for a purely housekeeping concern.
        purged = await store.purge_expired_sessions()
        if purged:
            logger.info("Purged %d expired session(s) on startup.", purged)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Startup recovery failed; it retries on the next restart.")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = get_store()
    # Only Postgres schema + settings + admin seeding block startup — all fast
    # and required before the first request. Everything ClickHouse-dependent is
    # deferred to a background task so booting can't hang behind it (502s).
    await store.init_schema()
    # Before anything reads configuration: the DB-backed override layer is part
    # of the effective settings, and the config report below should state what
    # the process will actually use.
    await load_runtime_settings()
    _log_config_report()
    await _seed_admin()
    await _refresh_enricher_availability()
    await _settle_orphaned_column_recommendations(store)
    await _reconcile_stale_converter_generations(store)

    recovery_task = asyncio.create_task(_startup_recovery(store))
    try:
        yield
    finally:
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task
        # Demo seeds tear their partial case down when cancelled, but only if
        # someone cancels them — an unattended shutdown mid-ingest would
        # otherwise leave a half-populated case in a user's list.
        await cancel_pending_seeds()


class AuthAuditMiddleware:
    """Gate unauthenticated access to /api/* and append one audit row per request.

    Deliberately a plain ASGI middleware, **not** ``@app.middleware("http")``
    (Starlette's ``BaseHTTPMiddleware``) — that wrapper buffers/re-frames the
    response through an in-memory stream, which breaks disconnect detection
    and effectively hangs long-lived ``StreamingResponse``s (this app's SSE
    live-collaboration endpoint being exactly that case). A pure ASGI
    middleware passes ``receive``/``send`` straight through, so streaming and
    client-disconnect propagation both work correctly.

    Authorization (which case/admin actions a given user may take) still
    happens in the route dependencies (``deps.get_current_user``,
    ``deps.require_case``); this middleware only establishes *who* is calling
    (if anyone) and enforces that a session exists at all for non-exempt API
    paths. Resolving the user here means route handlers reuse the cached
    value via ``request.state.user`` instead of re-querying the session store.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        settings = get_settings()

        user = None
        if path.startswith("/api/"):
            user = await resolve_user_optional(request)
            if user is None and not _is_exempt(path):
                response = JSONResponse(status_code=401, content={"detail": "Not authenticated"})
                await response(scope, receive, send)
                return
            if (
                user is not None
                and user.must_change_password
                and _requires_password_current(path, request.method)
            ):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Password change required before continuing"},
                )
                await response(scope, receive, send)
                return

        status_holder: dict[str, int] = {}

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["status_code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            # /api/auth/* handlers write their own enriched audit row on
            # success, but an exception raised before that call leaves zero
            # trace otherwise — for a forensic platform, a baseline row here
            # (even without the handler's semantic detail) is the safer
            # contract than silence.
            if settings.audit_enabled and path.startswith("/api/auth/"):
                fallback_user = user or getattr(request.state, "user", None)
                try:
                    await get_store().record_audit(
                        action="api.request_failed",
                        actor=fallback_user,
                        method=request.method,
                        path=path,
                        ip=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"),
                    )
                except Exception:
                    logger.exception(
                        "Failed to write fallback audit log row for %s %s", request.method, path
                    )
            raise

        should_audit = (
            settings.audit_enabled
            and path.startswith("/api/")
            and not path.startswith("/api/auth/")
            and request.method in _MUTATING_METHODS
        )
        if should_audit:
            # /api/auth/* actions (login, logout, password change, OIDC) write
            # their own enriched audit rows with a semantic action label;
            # logging them again here would duplicate with less detail. GETs
            # are excluded too — polling (JobTray, TopBar, list refetches)
            # otherwise buries the security-relevant mutating rows this audit
            # log exists to surface.
            user = user or getattr(request.state, "user", None)
            route = scope.get("route")
            route_path = getattr(route, "path", path)
            case_id = (scope.get("path_params") or {}).get("case_id")
            try:
                await get_store().record_audit(
                    action="api.request",
                    actor=user,
                    method=request.method,
                    path=path,
                    route=route_path,
                    case_id=case_id,
                    status_code=status_holder.get("status_code"),
                    ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
            except Exception:
                # Audit logging must never take down the actual request.
                logger.exception("Failed to write audit log row for %s %s", request.method, path)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Root logging was never configured anywhere, so every app-level
    # logger.info (session purges, recovery sweeps, the config report below)
    # silently vanished — only uvicorn's own loggers were visible. basicConfig
    # is a no-op if the embedding process already configured logging.
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(levelname)s:     %(name)s — %(message)s",
    )
    # Attached to the logger rather than to a handler: uvicorn owns the
    # handler, and an embedding process may replace it.
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactor) for f in access_logger.filters):
        access_logger.addFilter(AccessLogRedactor())
    app = FastAPI(
        title="Vestigo",
        description="Local-first forensic log investigation platform.",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # Starlette applies middleware in reverse of registration order (last
    # added = outermost), so AuthAuditMiddleware is added first here — that
    # makes CORSMiddleware outermost, so it always gets a chance to answer
    # (and stamp CORS headers on) cross-origin preflight OPTIONS requests
    # and 401 responses, instead of AuthAuditMiddleware short-circuiting
    # them first with a bare, header-less 401.
    app.add_middleware(AuthAuditMiddleware)
    # Binds the Request into a contextvar for every HTTP request so
    # scan_exec.run_scan can watch it for a disconnect. Order relative to the
    # auth gate is immaterial — it sets a variable and passes through.
    app.add_middleware(RequestContextMiddleware)
    scan_exec.install(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:8080"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(QueryRequestTooLargeError)
    async def _query_too_large(_request: Request, exc: QueryRequestTooLargeError) -> JSONResponse:
        """Answer 413 instead of leaking a Poco form-parser 500.

        Reached when a filter resolves to a value list ClickHouse refuses to
        accept in one request (issue #181). Large membership lists now travel
        as external data, so this is the backstop for whatever still overflows
        — and it tells the analyst to narrow the filter rather than showing a
        ClickHouse-internal message.

        413 rather than 400: the analyst's own request is well-formed and
        small, but the request *Vestigo* must make of the event store to
        answer it exceeds a payload limit. 413 is the only status that names
        size as the problem, which is the one thing the analyst can act on;
        400 would suggest the filter itself is malformed.

        Streaming exports reach this handler because the route pre-flights a
        ``count()`` over the same ``EventQuery`` before constructing the
        ``StreamingResponse`` (``routers/events.py``) — once the response
        headers are flushed no handler can run, so that pre-flight is what
        keeps an over-large export filter a clean 413 instead of a truncated
        200. Covered by ``tests/test_query_too_large_handler.py``.
        """
        return JSONResponse(
            status_code=413,
            content={
                "detail": (
                    f"This filter is too large for the event store to process ({exc}). "
                    "Narrow it — a shorter time range or fewer selected events — and retry."
                )
            },
        )

    @app.exception_handler(QueryMemoryExceededError)
    async def _query_out_of_memory(
        _request: Request, exc: QueryMemoryExceededError
    ) -> JSONResponse:
        """Answer 413 instead of a 500 when a scan hits its memory cap.

        The cap is ours, not ClickHouse's default: ``db/_scan.py`` pins
        ``max_memory_usage`` per query precisely so a scan too broad for the
        box fails alone rather than OOM-killing the server for everyone. That
        makes hitting it an expected outcome with an obvious remedy — ask for
        less — and a 500 tells the analyst none of that.

        413 for the same reason as :func:`_query_too_large`: the request is
        well-formed, it is the work it implies that does not fit. Streaming
        exports reach this handler only via their pre-flight count, which is
        why that pre-flight is aggregated through the spillable path
        (``count_field_inventory``) — so the common high-cardinality case
        succeeds rather than arriving here at all.
        """
        return JSONResponse(status_code=413, content={"detail": str(exc)})

    @app.get("/api/health", response_class=JSONResponse)
    async def health(user: User | None = Depends(resolve_user_optional)) -> dict:
        """Liveness, plus what this instance can actually do.

        The route is exempt from the auth gate because the login page needs it
        (``oidc_enabled`` decides whether the SSO button renders), so the body
        is split: anonymous callers learn the app is up and which login methods
        exist, and nothing else. Which optional subsystems an instance runs is
        an inventory of its attack surface, so `capabilities` — and the three
        flat aliases that predate it — need a session.

        The middleware already resolved and cached this user on
        ``request.state``, so the dependency costs no extra query.
        """
        body: dict = {
            "status": "ok",
            "version": __version__,
            "oidc_enabled": get_settings().oidc_enabled,
        }
        if user is None:
            return body
        # `capabilities` is the general form: one entry per optional subsystem,
        # false when the subsystem is unconfigured, and the frontend renders no
        # entry point for a false one (core/capabilities.py). The flat keys
        # below predate it and are kept as aliases so an older client keeps
        # working.
        caps = await get_capabilities()
        body["capabilities"] = caps
        # Served rather than mirrored: the tag is a filter token the resolver
        # and the grid must name identically, and a copy hardcoded in the
        # frontend would drift silently — a renamed tag stops matching without
        # raising anything. Outside `capabilities`, which is bool-only.
        body["annotated_tag"] = events.ANNOTATED_TAG
        body["embeddings_available"] = caps["embeddings"]
        body["agent_available"] = caps["agent"]
        body["mcp_enabled"] = caps["mcp"]
        # How the heavy-scan memory budget resolved, and against what. A
        # misconfiguration here has no symptom until ClickHouse is OOM-killed,
        # and the kernel does that without writing anything to ClickHouse's own
        # log — so the startup warning is the only signal, and a startup
        # warning is exactly what nobody reads. Serving it makes the answer
        # reachable at any time. Authenticated-only: it describes the host's
        # memory layout.
        # Off the event loop: the report re-detects local memory, which is
        # three blocking reads (`/sys/fs/cgroup/memory.max`, `/proc/meminfo`,
        # `os.sysconf`), and this is the app's most frequently polled route.
        body["scan_budget"] = await run_in_threadpool(scan_budget_report)
        return body

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(enrichers.router)
    app.include_router(cases.router)
    app.include_router(baselines.router)
    app.include_router(dispositions.router)
    app.include_router(events.router)
    app.include_router(viz.router)
    app.include_router(analysis.router)
    app.include_router(jobs.router)
    app.include_router(sigma.router)
    app.include_router(stories.router)
    app.include_router(stream.router)
    app.include_router(converters.router)
    app.include_router(converters.case_router)
    app.include_router(agent.router)
    app.include_router(agent.info_router)
    app.include_router(agent_tokens.router)
    app.include_router(transfer.router)
    app.include_router(demo.router)

    # External streamable-HTTP MCP endpoint (Bearer-token-gated), off by default.
    # Registered outside /api/, so AuthAuditMiddleware's session gate does not
    # apply — the endpoint's own Bearer auth is the sole gate. Registered
    # unconditionally (the endpoint itself 404s when VESTIGO_MCP_ENABLED is off)
    # so a disabled deployment answers a clean 404 instead of the SPA catch-all's
    # 405. A bare Mount("/mcp") only matches "/mcp/…"; clients POST to "/mcp", so
    # the exact path is routed explicitly too (both dispatch to the same app).
    from starlette.routing import Mount, Route

    from vestigo.agent.mcp_http import MCPEndpoint

    mcp_endpoint = MCPEndpoint()
    app.router.routes.append(Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]))
    app.router.routes.append(Mount("/mcp", app=mcp_endpoint))

    # Serve the built frontend when frontend/dist exists.
    # Run `npm run build` inside frontend/ once; vestigo-web then serves everything.
    # For development with HMR, run `npm run dev` (port 5173) alongside vestigo-web instead.
    if _FRONTEND_DIST.is_dir():
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        # Resolved once: every candidate must land inside this directory.
        dist_root = _FRONTEND_DIST.resolve()
        index_html = dist_root / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_frontend(full_path: str) -> FileResponse:
            # `full_path` is attacker-controlled and arrives unnormalized: a
            # request line may carry a literal `..`, and neither uvicorn's
            # parser nor Starlette's router collapses it. Joining it onto the
            # dist directory and calling `.is_file()` lets the *kernel* resolve
            # the `..`, which turns this unauthenticated catch-all into an
            # arbitrary-file read of anything the service account can open
            # (CVE-class path traversal; the deployment's own .env among them).
            #
            # So: resolve first, then require the result to sit under
            # `dist_root` — which also rejects a symlink inside dist that
            # points out of it. Anything else falls through to the SPA shell,
            # exactly as an unknown client-side route does.
            candidate = (dist_root / full_path).resolve()
            if candidate.is_relative_to(dist_root) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app
