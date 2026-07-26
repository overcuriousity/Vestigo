"""Lightweight in-memory job tracker for long-running background tasks."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_TERMINAL_STATUSES = {"completed", "failed"}

# How many finished jobs to keep around for status polling before the oldest
# are evicted. Sizing detail, not an operator tunable.
_DEFAULT_MAX_TERMINAL_JOBS = 200


@dataclass
class Job:
    """A tracked background job."""

    id: str
    kind: str
    status: str = "queued"  # queued | running | completed | failed
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    # ID of the user who started this job, or None for system-triggered jobs
    # (e.g. automatic enrichment). Used to scope job-status reads so one
    # analyst can't poll another's job by guessing its ID.
    created_by: str | None = None
    # Case this job operates on. When set, any user with READ access to the
    # case may poll the job — job visibility follows case RBAC, matching the
    # rest of the access model.
    case_id: str | None = None
    created_at: float = field(default_factory=time.time)
    # Guards the two mutable payloads (`progress`, `result`) against the
    # worker-thread-writes / request-thread-serializes race. Held by
    # ``JobStore.update`` around the mutation and by ``to_dict`` around the
    # snapshot; always taken *inside* the store lock, never the other way.
    _payload_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation.

        ``progress`` and ``result`` are snapshotted under the payload lock:
        jobs are updated from FastAPI's threadpool while the polling request
        serializes them, so handing out the live dicts lets a response change
        mid-encode (or raise "dictionary changed size during iteration").
        """
        with self._payload_lock:
            progress = dict(self.progress)
            result = dict(self.result) if self.result is not None else None
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": progress,
            "result": result,
            "error": self.error,
            "case_id": self.case_id,
            "created_at": self.created_at,
        }


class JobStore:
    """Thread-safe in-memory store for background jobs.

    Jobs are intentionally ephemeral: they are lost when the server process
    restarts. This is sufficient for the current single-process deployment.

    Terminal (completed/failed) jobs are retained for status polling but
    capped at ``max_terminal``; the oldest-finished are evicted first.
    Queued/running jobs are never evicted.
    """

    def __init__(self, max_terminal: int = _DEFAULT_MAX_TERMINAL_JOBS) -> None:
        self._jobs: dict[str, Job] = {}
        self._max_terminal = max_terminal
        # Job IDs in completion order (dict insertion order is creation order,
        # which is not the same thing).
        self._terminal_order: deque[str] = deque()
        self._lock = threading.Lock()

    def create(
        self,
        kind: str,
        progress: dict[str, Any] | None = None,
        created_by: str | None = None,
        case_id: str | None = None,
    ) -> Job:
        """Create a new job and return it."""
        with self._lock:
            return self._create(
                kind=kind, progress=progress, created_by=created_by, case_id=case_id
            )

    def _create(
        self,
        kind: str,
        progress: dict[str, Any] | None,
        created_by: str | None,
        case_id: str | None,
    ) -> Job:
        """``create`` without the lock. Callers must already hold ``_lock``."""
        job_id = uuid.uuid4().hex[:16]
        job = Job(
            id=job_id, kind=kind, progress=progress or {}, created_by=created_by, case_id=case_id
        )
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Return a job by ID, or None if not found."""
        return self._jobs.get(job_id)

    def create_if_under(
        self,
        kinds: tuple[str, ...],
        limit: int,
        kind: str,
        progress: dict[str, Any] | None = None,
        created_by: str | None = None,
        case_id: str | None = None,
    ) -> Job | None:
        """Create a job only if fewer than ``limit`` of ``kinds`` are active.

        Returns None when the cap is already reached; ``limit`` of 0 disables
        the cap entirely.

        Counting and creating happen under one lock acquisition, which is the
        whole point: checking with ``count_active`` and then calling ``create``
        lets two simultaneous requests both pass at limit-1, so a cap of 1
        admits 2. The caller is admission control for work that reserves real
        resources, so the cap has to actually hold.
        """
        with self._lock:
            if limit and self._count_active(kinds) >= limit:
                return None
            return self._create(
                kind=kind, progress=progress, created_by=created_by, case_id=case_id
            )

    def count_active(self, kinds: tuple[str, ...]) -> int:
        """Queued or running jobs of the given kinds.

        Admission control for work that reserves real resources before it
        starts (case transfers hold a multi-GiB upload plus its expansion on
        disk). Locked like the other readers — jobs are updated from FastAPI's
        threadpool, so an unlocked scan could see a torn dict.

        Read-only: to *act* on the count, use ``create_if_under``, which does
        both under one lock.
        """
        with self._lock:
            return self._count_active(kinds)

    def _count_active(self, kinds: tuple[str, ...]) -> int:
        """``count_active`` without the lock. Callers must already hold ``_lock``."""
        return sum(
            1
            for job in self._jobs.values()
            if job.kind in kinds and job.status in ("queued", "running")
        )

    def list_by_case(self, case_id: str) -> list[Job]:
        """Return jobs scoped to a case, newest-first."""
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.case_id == case_id]
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return jobs

    def update(
        self,
        job_id: str,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job | None:
        """Update a job's status/progress/result/error."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if status is not None:
                was_terminal = job.status in _TERMINAL_STATUSES
                job.status = status
                if status in _TERMINAL_STATUSES and not was_terminal:
                    self._terminal_order.append(job_id)
                    self._evict_locked()
            if progress is not None or result is not None:
                with job._payload_lock:  # noqa: SLF001 - same-module dataclass
                    if progress is not None:
                        job.progress.update(progress)
                    if result is not None:
                        job.result = result
            if error is not None:
                job.error = error
            return job

    def _evict_locked(self) -> None:
        """Drop the oldest-finished jobs beyond the cap (caller holds the lock)."""
        while len(self._terminal_order) > self._max_terminal:
            old_id = self._terminal_order.popleft()
            self._jobs.pop(old_id, None)


# Global singleton used by the web app. In-memory is fine for the current
# single-process deployment; replace with a persistent store if horizontal
# scaling is needed.
_default_store: JobStore | None = None


def get_job_store() -> JobStore:
    """Return the global job store instance."""
    global _default_store  # noqa: PLW0603
    if _default_store is None:
        _default_store = JobStore()
    return _default_store
