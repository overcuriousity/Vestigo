"""Referent-scope checks for story blocks (W7).

``vestigo.stories.schemas.validate_block_content`` checks a block's *shape*;
this module checks that what it points at actually exists inside the case.
Both gates run on every write path — the HTTP router, and the agent's
``propose_story_block`` (at propose time, so the model can correct itself)
plus its confirm handler (again, because a referent can be deleted in
between). Catching a foreign or mistyped id here turns what would otherwise
surface much later as a "cannot be drawn" card or a frozen
``resolution.error`` in an export into an error at the point the mistake was
made.

Raises ``ValueError`` so callers map it exactly like a schema failure.
"""

from __future__ import annotations

from typing import Any


async def validate_block_scope(
    case_id: str, kind: str, content: dict[str, Any], *, store: Any = None
) -> None:
    """Check that a block's referents exist inside *case_id*.

    ``content`` must already have passed ``validate_block_content`` — the
    per-kind keys are read directly.
    """
    if store is None:
        from vestigo.api.deps import get_store

        store = get_store()
    timeline_id = content.get("timeline_id")
    if timeline_id and await store.get_timeline(case_id, timeline_id) is None:
        raise ValueError(f"timeline {timeline_id!r} is not in this case")
    if kind == "view_ref":
        view = await store.get_view(case_id, content["view_id"])
        # A hidden view (deleted while some block still referenced it) keeps
        # its existing block working, but must not become the referent of a
        # new one — that would resurrect an artifact the analyst deleted.
        if view is None or view.deleted_at is not None:
            raise ValueError(f"view {content['view_id']!r} is not in this case")
    if kind == "chart_ref":
        chart = await store.get_saved_chart(case_id, timeline_id, content["chart_id"])
        if chart is None:
            raise ValueError(f"chart {content['chart_id']!r} is not in this case/timeline")
    if kind == "event_ref":
        # An event block freezes one row of one source. The source is the only
        # part of it that is case-scoped metadata we can check up front; the
        # event id itself is only resolvable against ClickHouse at export time.
        source_id = content["source_id"]
        if await store.get_source(case_id, source_id) is None:
            raise ValueError(f"source {source_id!r} is not in this case")
