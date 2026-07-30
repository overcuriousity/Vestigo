"""Seeding the fabricated demo case into a user's case list.

The case is *generated and ingested* per user (``vestigo.demo.build``) rather
than restored from a shipped archive: the generator is deterministic, so every
copy is identical anyway, and the repository carries a few hundred lines of
code instead of a 146 MiB binary that would need regenerating on every schema
change. Once seeded it is an ordinary case — annotate it, export it, delete it;
nothing here treats it specially afterwards.

Seeding is claimed atomically per user (``PostgresStore.claim_demo_seed``) and
runs as a background job, so a login never waits on it and never fails because
of it. The claim is stamped before the build starts: a user whose build fails
sees a failed job and can restore explicitly, which beats rebuilding on every
subsequent login.
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

#: Concurrent seeds allowed instance-wide. Each one ingests a quarter of a
#: million events, so a burst of first logins after an upgrade should queue
#: behind itself rather than compete for the whole box.
MAX_CONCURRENT_SEEDS = 2

#: Background seed tasks, kept referenced so the event loop cannot collect a
#: running task mid-import (asyncio holds only weak references to tasks).
_pending: set[asyncio.Task] = set()


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

        if not await get_store().claim_demo_seed(user.id):
            return None
        return _dispatch(user)
    except Exception:  # noqa: BLE001 — seeding must never break a login
        logger.exception("demo seeding failed to start for user %s", user.id)
        return None


async def seed_demo_case(user: User) -> str:
    """Seed unconditionally — the explicit "restore demo case" action.

    Args:
        user: The account asking for a fresh copy.

    Returns:
        The background job's id.

    Raises:
        RuntimeError: When too many seeds are already running.
    """
    from vestigo.api.deps import get_store

    # Best-effort stamp so a restore also closes out a never-seeded account;
    # already-claimed is the normal case here and is not an error.
    await get_store().claim_demo_seed(user.id)
    return _dispatch(user)


def _dispatch(user: User) -> str:
    """Create the job row and start its background task.

    Raises:
        RuntimeError: When ``MAX_CONCURRENT_SEEDS`` builds are already running.
    """
    job = get_job_store().create_if_under(
        kinds=(JOB_KIND,),
        limit=MAX_CONCURRENT_SEEDS,
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
    except Exception as exc:  # noqa: BLE001 — job error surface
        logger.exception("demo seed job %s failed", job_id)
        jobs.update(job_id, status="failed", error=str(exc))


async def _await_pending_seeds() -> None:
    """Await in-flight seed tasks. Test helper — the app never calls this."""
    while _pending:
        await asyncio.gather(*tuple(_pending), return_exceptions=True)
