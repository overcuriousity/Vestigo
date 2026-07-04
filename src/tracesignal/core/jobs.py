"""Lightweight in-memory job tracker for long-running background tasks."""

from __future__ import annotations

import threading
import uuid
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
    # ID of the user who started this job, or None for jobs created before
    # auth existed. Used to scope job-status reads so one analyst can't poll
    # another's job by guessing its ID.
    created_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation."""
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
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
        self._terminal_order: list[str] = []
        self._lock = threading.Lock()

    def create(
        self, kind: str, progress: dict[str, Any] | None = None, created_by: str | None = None
    ) -> Job:
        """Create a new job and return it."""
        job_id = uuid.uuid4().hex[:16]
        job = Job(id=job_id, kind=kind, progress=progress or {}, created_by=created_by)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Return a job by ID, or None if not found."""
        return self._jobs.get(job_id)

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
            old_id = self._terminal_order.pop(0)
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
