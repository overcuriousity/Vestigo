"""Server-Sent Events endpoint for live case collaboration.

Lets analysts viewing the same case see each other's annotations/tags show
up without a manual refresh. Events carry only IDs and the acting user (no
event content), so a subscriber never receives anything they couldn't
already fetch themselves through the normal, authorized endpoints — the
stream is purely an invalidation signal, not a data channel.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from tracesignal.api.deps import (
    AccessLevel,
    require_case_read,
    resolve_case_access,
    resolve_user_optional,
)
from tracesignal.core.events_bus import get_event_bus
from tracesignal.db.postgres import Case

router = APIRouter(prefix="/api/cases", tags=["stream"])

_KEEPALIVE_SECONDS = 20


async def _event_stream(request: Request, case: Case) -> AsyncGenerator[str]:
    bus = get_event_bus()
    queue = bus.subscribe(case.id)
    try:
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                yield f"data: {json.dumps(event)}\n\n"
            except TimeoutError:
                # Auth/case-access was only checked once, at connect time, via
                # the route dependency below. Re-validate here on every
                # keepalive tick so a revoked session, deactivation, or team
                # removal actually stops the stream instead of leaking
                # activity metadata to a subscriber who's lost access.
                user = await resolve_user_optional(request)
                if user is None or await resolve_case_access(user, case) < AccessLevel.READ:
                    break
                yield ": keepalive\n\n"
    finally:
        bus.unsubscribe(case.id, queue)


@router.get("/{case_id}/stream")
async def stream_case_events(
    request: Request,
    case: Case = Depends(require_case_read),
) -> StreamingResponse:
    """Subscribe to live change events (annotations/tags) for a case."""
    return StreamingResponse(
        _event_stream(request, case),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
