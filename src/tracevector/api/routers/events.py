"""API routes for querying events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tracevector.db.postgres import PostgresStore
from tracevector.db.queries import EventQuery, EventQueryService

router = APIRouter(prefix="/api/cases", tags=["events"])

_store: PostgresStore | None = None


def get_store() -> PostgresStore:
    """Return a cached PostgresStore instance."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = PostgresStore()
    return _store


@router.get("/{case_id}/timelines/{timeline_id}/events")
async def list_events(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(default=None, description="Full-text search in message"),
    source: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List events for a timeline with optional filters."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    service = EventQueryService()
    page = service.query(
        EventQuery(
            case_id=case_id,
            timeline_id=timeline_id,
            q=q,
            source=source,
            tag=tag,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
    )
    return {
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "events": page.events,
    }
