"""In-process pub/sub for live collaboration (SSE).

Mirrors the intentionally in-memory, single-process design of
``core.jobs.JobStore``: subscriptions live only as long as the process and a
client's open connection, and are lost on restart. That's fine here — this
bus carries advisory "something changed, go refetch" signals, not the data
itself, so a missed event just means a client refreshes a beat later than
otherwise (or on its next navigation).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracesignal.db.postgres import User

_MAX_QUEUE = 100


@dataclass
class CaseEventBus:
    """Fan-out of change events to subscribers of a single case."""

    _subscribers: dict[str, list[asyncio.Queue]] = field(default_factory=dict)

    def subscribe(self, case_id: str) -> asyncio.Queue:
        """Register a new subscriber queue for ``case_id`` and return it."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._subscribers.setdefault(case_id, []).append(queue)
        return queue

    def unsubscribe(self, case_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue, e.g. when its client disconnects."""
        subscribers = self._subscribers.get(case_id)
        if not subscribers:
            return
        with contextlib.suppress(ValueError):
            subscribers.remove(queue)
        if not subscribers:
            self._subscribers.pop(case_id, None)

    def publish(self, case_id: str, event: dict[str, Any]) -> None:
        """Push ``event`` to every current subscriber of ``case_id``.

        Best-effort: a full subscriber queue (a stalled/very slow client)
        just drops the event for that one subscriber rather than blocking
        the writer that triggered it.
        """
        for queue in self._subscribers.get(case_id, []):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


_bus: CaseEventBus | None = None


def get_event_bus() -> CaseEventBus:
    """Return the process-wide event bus singleton."""
    global _bus  # noqa: PLW0603
    if _bus is None:
        _bus = CaseEventBus()
    return _bus


def publish_annotation_change(
    case_id: str,
    timeline_id: str | None,
    event_id: str | None,
    actor: User,
    kind: str = "annotation.changed",
) -> None:
    """Notify live subscribers of this case that annotations/tags changed.

    Shared by every annotation write path (cases.py, events.py) so the
    payload shape lives in one place instead of being re-declared per
    call site and silently drifting.

    Advisory only: payload carries IDs and the acting user, never event
    content, so a subscriber never learns anything they couldn't already
    fetch themselves under their own case-access grant.
    """
    get_event_bus().publish(
        case_id,
        {
            "type": kind,
            "case_id": case_id,
            "timeline_id": timeline_id,
            "event_id": event_id,
            "actor": actor.username,
        },
    )
