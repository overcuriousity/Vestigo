"""Seeding the fabricated demo case into a user's case list.

The case is *generated and ingested* per user (``vestigo.demo.build``) rather
than restored from a shipped archive: the generator is deterministic, so every
copy is identical anyway, and the repository carries a few hundred lines of
code instead of a 146 MiB binary that would need regenerating on every schema
change. Once seeded it is an ordinary case — annotate it, export it, delete it —
except that it is flagged ``is_demo``, which keeps other users' copies out of an
administrator's case list and bounds the restore endpoint to one per account.

Seeding is claimed atomically per user (``PostgresStore.claim_demo_seed``) and
runs as a background job, so a login never waits on it and never fails because
of it. The claim is stamped before the build starts: a user whose build fails
sees a failed job and can restore explicitly, which beats rebuilding on every
subsequent login. A claim that never became a running build is given back
(``release_demo_seed``), so a full concurrency cap costs a user nothing but a
wait until their next login.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from vestigo.core.config import get_settings
from vestigo.core.jobs import get_job_store
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.postgres import User
from vestigo.demo.build import build_demo_case

logger = logging.getLogger(__name__)

JOB_KIND = "demo_seed"

#: Background seed tasks, kept referenced so the event loop cannot collect a
#: running task mid-import (asyncio holds only weak references to tasks).
_pending: set[asyncio.Task] = set()


class DemoCaseExists(Exception):
    """The caller already has a demo case, so there is nothing to restore."""


async def maybe_seed_demo_case(user: User) -> str | None:
    """Seed the demo case for ``user`` if they have never been seeded.

    Args:
        user: The account whose session was just issued.

    Returns:
        The background job's id, or None when seeding is disabled or this user
        already had their turn.

    Never raises: a login must not fail because of the demo case.
    """
    if not get_settings().demo_case_enabled:
        return None
    try:
        from vestigo.api.deps import get_store

        store = get_store()
        if not await store.claim_demo_seed(user.id):
            return None
        try:
            return _dispatch(user)
        except Exception:
            # The claim is spent only once a build is actually running. A
            # dispatch that never got that far — the concurrency cap is full
            # during a post-upgrade burst of logins, say — must give the stamp
            # back, or this user is marked seeded and never offered the case
            # again. Their next login retries.
            await store.release_demo_seed(user.id)
            raise
    except Exception:  # seeding must never break a login
        logger.exception("demo seeding failed to start for user %s", user.id)
        return None


async def seed_demo_case(user: User) -> str:
    """Seed on request — the explicit "restore demo case" action.

    Args:
        user: The account asking for a fresh copy.

    Returns:
        The background job's id.

    Raises:
        DemoCaseExists: When the caller still has the one they were given.
        RuntimeError: When too many seeds are already running.
    """
    from vestigo.api.deps import get_store

    store = get_store()
    # One demo case per account at a time. Without this an authenticated user
    # can loop this endpoint and write a quarter of a million ClickHouse rows
    # per call; with it, a fresh copy costs a deliberate deletion first.
    if await store.find_demo_case_for_owner(user.id) is not None:
        raise DemoCaseExists("this user already has a demo case")
    # Best-effort stamp so a restore also closes out a never-seeded account;
    # already-claimed is the normal case here and is not an error.
    await store.claim_demo_seed(user.id)
    return _dispatch(user)


def _dispatch(user: User) -> str:
    """Create the job row and start its background task.

    Raises:
        RuntimeError: When ``demo_max_concurrent`` builds are already running.
    """
    job = get_job_store().create_if_under(
        kinds=(JOB_KIND,),
        limit=get_settings().demo_max_concurrent,
        kind=JOB_KIND,
        progress={"phase": "queued", "processed": 0, "total": None},
        created_by=user.id,
    )
    if job is None:
        raise RuntimeError("too many demo cases are being prepared right now; try again shortly")
    task = asyncio.create_task(_run_seed_job(job.id, user))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return job.id


async def _run_seed_job(job_id: str, user: User) -> None:
    """Generate, ingest and annotate the demo case for one user."""
    from vestigo.api.deps import get_store

    jobs = get_job_store()
    jobs.update(job_id, status="running")
    store = get_store()
    try:
        result = await build_demo_case(
            store,
            ClickHouseStore(),
            owner_id=user.id,
            progress=lambda p: jobs.update(job_id, progress=p),
        )
        counts = {
            "events": result.events,
            "sources": result.sources,
            "annotations": result.annotations,
        }
        await store.record_audit(
            action="case.demo_seeded",
            actor=user,
            case_id=result.case_id,
            target_type="case",
            target_id=result.case_id,
            detail={"job_id": job_id, "counts": counts, "at": datetime.now(UTC).isoformat()},
        )
        jobs.update(
            job_id,
            status="completed",
            result={"case_id": result.case_id, "counts": counts},
        )
    except asyncio.CancelledError:
        # Shutdown. ``build_demo_case`` has already torn its partial case down
        # by the time this arrives; the job store is in-memory and dies with
        # the process, so there is nothing further to record.
        jobs.update(job_id, status="failed", error="cancelled at shutdown")
        raise
    except Exception as exc:  # job error surface
        logger.exception("demo seed job %s failed", job_id)
        jobs.update(job_id, status="failed", error=str(exc))


async def cancel_pending_seeds() -> None:
    """Cancel in-flight seed tasks and wait for their cleanup to finish.

    Called from the app's lifespan shutdown. Without it a seed interrupted
    mid-ingest leaves a half-populated case sitting in someone's list: the
    process exits while the build is between sources, and nothing ever runs
    the teardown that a failed build does.
    """
    for task in tuple(_pending):
        task.cancel()
    while _pending:
        await asyncio.gather(*tuple(_pending), return_exceptions=True)


async def _await_pending_seeds() -> None:
    """Await in-flight seed tasks. Test helper — the app never calls this."""
    while _pending:
        await asyncio.gather(*tuple(_pending), return_exceptions=True)
