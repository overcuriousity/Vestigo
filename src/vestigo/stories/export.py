"""Server-authoritative Story export resolution (W7).

``resolve_story_snapshot`` freezes a story into the versioned ``"v": 1``
snapshot bundle: every block resolved against live data at export time,
each individually wrapped so a dangling reference or query failure becomes
an explicit ``resolution.error`` — visible in the artifact, never a
silently dropped block. Truncation is always flagged (a report that shows
200 of 14203 rows says so).

Query execution reuses the existing paths — ``EventQueryService.query``
for view/event blocks (via the agent's ``_build_query``, so field
mappings, tag filters and clock-skew offsets apply exactly as in the
Explorer) and ``execute_chart_spec`` for chart blocks. The keyword hooks
(``run_event_query``, ``run_chart``, ``resolve_scope``, ``now``) exist for
tests; production callers pass none of them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from vestigo.db.postgres import Story, StoryBlock, User, View


def _view_filter_to_spec(view: View):
    """Map a stored ``View.view_filter`` payload onto the agent's FilterSpec.

    The payload is ``filtersToViewPayload``'s shape
    (``frontend/src/lib/queryParams.ts``); FilterSpec is the same Explorer
    filter vocabulary, so this is a key rename plus dropping the empties
    (FilterSpec rejects explicit empty selections as select-nothing traps).
    """
    from vestigo.agent.tools import FilterSpec

    p = view.view_filter or {}

    def _list(key: str) -> list[str] | None:
        values = p.get(key) or None
        return list(values) if values else None

    artifacts = _list("artifacts") or []
    if p.get("artifact") and p["artifact"] not in artifacts:
        artifacts.append(p["artifact"])
    tags_include = _list("tagsInclude") or []
    if p.get("tag") and p["tag"] not in tags_include:
        tags_include.append(p["tag"])
    tags_exclude = _list("tagsExclude") or []
    if p.get("excludeTag") and p["excludeTag"] not in tags_exclude:
        tags_exclude.append(p["excludeTag"])

    filters = {k: list(v) for k, v in (p.get("filters") or {}).items() if v}
    exclusions = {k: list(v) for k, v in (p.get("exclusions") or {}).items() if v}

    def _dt(key: str) -> datetime | None:
        raw = p.get(key)
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))

    return FilterSpec(
        q=p.get("q") or None,
        q_regex=bool(p.get("qRegex")),
        artifacts=artifacts or None,
        source_id=p.get("sourceId") or None,
        start=_dt("start"),
        end=_dt("end"),
        filters=filters,
        exclusions=exclusions,
        filter_modes={k: v for k, v in (p.get("filterModes") or {}).items() if k in filters},
        exclusion_modes={
            k: v for k, v in (p.get("exclusionModes") or {}).items() if k in exclusions
        },
        tags_include=tags_include or None,
        tags_exclude=tags_exclude or None,
        annotated=_list("annotated"),
        annotation_tag_value=p.get("annotationTagValue") or None,
    )


async def resolve_story_snapshot(
    story: Story,
    blocks: list[StoryBlock],
    *,
    user: User,
    store: Any = None,
    run_event_query: Callable[..., Any] | None = None,
    run_chart: Any = None,
    resolve_scope: Any = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Resolve every block of *story* into the frozen ``"v": 1`` snapshot.

    Never raises for a bad block — failures freeze as ``resolution.error``.
    """
    from vestigo.agent.tools import AgentScope, ChartSpec, _build_query

    if store is None:
        from vestigo.api.deps import get_store

        store = get_store()
    if now is None:
        now = lambda: datetime.now(UTC)  # noqa: E731

    if resolve_scope is None:

        async def _resolve_scope(case_id: str, timeline_id: str):
            from vestigo.api.routers.events import _resolve_timeline_scope

            return await _resolve_timeline_scope(case_id, timeline_id)

    else:
        _sync_resolve = resolve_scope

        async def _resolve_scope(case_id: str, timeline_id: str):
            return _sync_resolve(case_id, timeline_id)

    if run_event_query is None:
        from vestigo.api.routers.events import _get_query_service

        service = _get_query_service()

        async def _run_event_query(query):
            return await run_in_threadpool(service.query, query)

    else:
        _sync_query = run_event_query

        async def _run_event_query(query):
            return _sync_query(query)

    if run_chart is None:
        from vestigo.agent.chart_exec import execute_chart_spec

        run_chart = execute_chart_spec

    scope_cache: dict[str, AgentScope] = {}

    async def _scope_for(timeline_id: str) -> AgentScope:
        if timeline_id not in scope_cache:
            source_ids, field_mappings, source_offsets = await _resolve_scope(
                story.case_id, timeline_id
            )
            scope_cache[timeline_id] = AgentScope(
                case_id=story.case_id,
                timeline_id=timeline_id,
                user=user,
                source_ids=source_ids,
                field_mappings=field_mappings,
                source_offsets=source_offsets,
            )
        return scope_cache[timeline_id]

    async def _resolve_markdown(content: dict) -> tuple[dict, dict]:
        return {"text": content.get("text", "")}, {}

    async def _resolve_view(content: dict) -> tuple[dict | None, dict]:
        view = await store.get_view(story.case_id, content["view_id"])
        if view is None:
            raise LookupError(f"view {content['view_id']!r} not found (deleted before export)")
        timeline_id = content["timeline_id"]
        scope = await _scope_for(timeline_id)
        display = content.get("display") or {}
        limit = int(display.get("limit") or 200)
        query = await _build_query(scope, _view_filter_to_spec(view))
        # _build_query clamps limit to the agent's per-search context cap;
        # export blocks carry their own (validated ≤ VIEW_BLOCK_ROW_CAP).
        query.limit = limit
        page = await _run_event_query(query)
        rows = page.events
        total = page.total if page.total is not None else len(rows)
        data = {
            "rows": rows,
            "row_count_total": total,
            "rows_included": len(rows),
            "truncated": total > len(rows),
            "columns": display.get("columns"),
        }
        ref_extra = {
            "name": view.name,
            "query": view.query,
            "filter": view.view_filter or {},
        }
        return data, {"timeline_id": timeline_id, "ref_extra": ref_extra}

    async def _resolve_chart(content: dict) -> tuple[dict | None, dict]:
        chart = await store.get_saved_chart(
            story.case_id, content["timeline_id"], content["chart_id"]
        )
        if chart is None:
            raise LookupError(
                f"saved chart {content['chart_id']!r} not found (deleted before export)"
            )
        try:
            spec = ChartSpec.model_validate(chart.config or {})
        except Exception as exc:
            raise ValueError(
                f"saved chart config (v={((chart.config or {}).get('v'))}) "
                f"did not parse as a chart spec: {exc}"
            ) from exc
        scope = await _scope_for(content["timeline_id"])
        envelope = await run_chart(scope, spec)
        data = {
            "name": chart.name,
            "config": chart.config or {},
            "resolved": envelope.get("resolved"),
            "warnings": envelope.get("warnings", []),
            "chart": envelope.get("result"),
        }
        return data, {"timeline_id": content["timeline_id"], "ref_extra": {"name": chart.name}}

    async def _resolve_event(content: dict) -> tuple[dict | None, dict]:
        from vestigo.db.queries import EventQuery

        query = EventQuery(
            case_id=story.case_id,
            source_ids=[content["source_id"]],
            event_ids=[content["event_id"]],
            limit=1,
        )
        page = await _run_event_query(query)
        if not page.events:
            raise LookupError(
                f"event {content['event_id']!r} not found (source deleted or re-scoped)"
            )
        return {"event": page.events[0], "caption": content.get("caption")}, {}

    resolvers = {
        "markdown": _resolve_markdown,
        "view_ref": _resolve_view,
        "chart_ref": _resolve_chart,
        "event_ref": _resolve_event,
    }

    frozen_blocks: list[dict[str, Any]] = []
    for block in blocks:
        resolution: dict[str, Any] = {"executed_at": now().isoformat(), "error": None}
        ref = dict(block.content or {})
        data: dict | None = None
        try:
            resolver = resolvers.get(block.kind)
            if resolver is None:
                raise ValueError(f"unknown block kind {block.kind!r}")
            data, extra = await resolver(block.content or {})
            if extra.get("timeline_id"):
                resolution["timeline_id"] = extra["timeline_id"]
            ref.update(extra.get("ref_extra") or {})
        except Exception as exc:  # noqa: BLE001 — every failure freezes as an error block
            data = None
            resolution["error"] = str(exc)
        frozen_blocks.append(
            {
                "id": block.id,
                "kind": block.kind,
                "origin": block.origin,
                "ref": ref,
                "data": data,
                "resolution": resolution,
            }
        )

    return {
        "v": 1,
        "story": {
            "id": story.id,
            "title": story.title,
            "case_id": story.case_id,
            "exported_at": now().isoformat(),
            "exported_by": user.username,
        },
        "blocks": frozen_blocks,
    }
