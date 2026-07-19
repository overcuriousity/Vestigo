"""Read-only investigation tools for the AI agent, exposed as an MCP server.

The tools are defined once on a standard MCP server (``mcp.server.fastmcp``)
so the same definitions serve the built-in pydantic-ai loop today (in-memory
transport) and an externally mounted MCP endpoint later (roadmap). Every tool
is scoped to one case + timeline at server-build time — the model never
supplies IDs, so it can never read outside the conversation's scope.

Query behavior deliberately reuses the events router's building blocks
(`_resolve_timeline_scope`, `_resolve_tags_filter`, `_run_stat_detector`, …)
instead of re-implementing filter semantics: an agent finding applied to the
Explorer must match exactly what the agent saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi.concurrency import run_in_threadpool
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from vestigo.db.postgres import User
from vestigo.db.queries import EventQuery, TagFilter
from vestigo.models.embeddings import embeddings_available

# Result budgets: tool output goes into the model's context window, so every
# list is capped and every string truncated. The analyst inspects full events
# in the Explorer — the agent only needs enough to reason and to hand back
# filters.
MAX_EVENTS_PER_SEARCH = 50
MESSAGE_TRUNCATE = 500
ATTR_VALUE_TRUNCATE = 200
MAX_ATTRS_PER_EVENT = 40


class FilterSpec(BaseModel):
    """Event filters, mirroring the Explorer's filter shape.

    This is the contract that makes findings applicable: the exact same spec
    the agent used for a search is handed to the frontend, which maps it onto
    Explorer URL params (`frontend/src/lib/queryParams.ts`).
    """

    q: str | None = Field(default=None, description="Free-text search across all fields.")
    q_regex: bool = Field(
        default=False,
        description="Treat q as an RE2 regex (case-sensitive; prefix (?i) to ignore case).",
    )
    artifacts: list[str] | None = Field(
        default=None, description="Artifact types to include (OR'd)."
    )
    source_id: str | None = Field(default=None, description="Restrict to one source.")
    start: datetime | None = Field(default=None, description="Events at/after this time (ISO).")
    end: datetime | None = Field(default=None, description="Events before this time (ISO).")
    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Field filters: {field: [values]}. Values under one field are OR'd, "
            "distinct fields AND'ed. Use attribute keys as reported by list_fields."
        ),
    )
    exclusions: dict[str, list[str]] = Field(
        default_factory=dict, description="Field exclusions, same shape as filters, negated."
    )
    filter_modes: dict[str, str] = Field(
        default_factory=dict,
        description='Per-field match mode for filters: "exact" (default) | "wildcard" | "regex".',
    )
    exclusion_modes: dict[str, str] = Field(
        default_factory=dict, description="Per-field match mode for exclusions."
    )
    tags_include: list[str] | None = Field(
        default=None, description="Only events carrying any of these tags."
    )
    tags_exclude: list[str] | None = Field(
        default=None, description="Drop events carrying any of these tags."
    )


@dataclass
class AgentScope:
    """Frozen case/timeline scope a tool server operates in."""

    case_id: str
    timeline_id: str
    user: User
    source_ids: list[str]
    field_mappings: dict[str, list[str]] | None
    source_offsets: dict[str, int] | None


async def build_scope(case_id: str, timeline_id: str, user: User) -> AgentScope:
    """Resolve the timeline's source scope once for a conversation turn."""
    from vestigo.api.routers.events import _resolve_timeline_scope

    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    return AgentScope(
        case_id=case_id,
        timeline_id=timeline_id,
        user=user,
        source_ids=source_ids,
        field_mappings=field_mappings,
        source_offsets=source_offsets,
    )


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _slim_event(event: dict[str, Any]) -> dict[str, Any]:
    """Compact an event row for model consumption."""
    slim: dict[str, Any] = {}
    for key in ("event_id", "timestamp", "source_id", "artifact", "display_name"):
        if event.get(key) not in (None, ""):
            slim[key] = event[key]
    if event.get("message"):
        slim["message"] = _truncate(event["message"], MESSAGE_TRUNCATE)
    attrs = event.get("attributes") or {}
    if isinstance(attrs, dict) and attrs:
        slim_attrs = {}
        for i, (k, v) in enumerate(attrs.items()):
            if i >= MAX_ATTRS_PER_EVENT:
                slim_attrs["…"] = f"{len(attrs) - MAX_ATTRS_PER_EVENT} more keys omitted"
                break
            if v in (None, ""):
                continue
            slim_attrs[k] = _truncate(v, ATTR_VALUE_TRUNCATE)
        slim["attributes"] = slim_attrs
    return slim


async def _build_query(
    scope: AgentScope,
    spec: FilterSpec | None,
    *,
    limit: int = MAX_EVENTS_PER_SEARCH,
    offset: int = 0,
    order: str = "desc",
) -> EventQuery:
    from vestigo.api.routers.events import _resolve_tags_filter

    spec = spec or FilterSpec()
    tags_include: TagFilter | None = None
    tags_exclude: TagFilter | None = None
    if spec.tags_include:
        tags_include = await _resolve_tags_filter(
            scope.case_id, scope.source_ids, spec.tags_include
        )
    if spec.tags_exclude:
        tags_exclude = await _resolve_tags_filter(
            scope.case_id, scope.source_ids, spec.tags_exclude
        )
    return EventQuery(
        case_id=scope.case_id,
        source_ids=scope.source_ids,
        q=spec.q,
        q_regex=spec.q_regex,
        artifacts=spec.artifacts,
        source_id=spec.source_id,
        start=spec.start,
        end=spec.end,
        field_filters=spec.filters,
        field_exclusions=spec.exclusions,
        filter_modes=spec.filter_modes,
        exclusion_modes=spec.exclusion_modes,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        limit=min(limit, MAX_EVENTS_PER_SEARCH),
        offset=offset,
        order=order if order in ("asc", "desc") else "desc",  # type: ignore[arg-type]
        field_mappings=scope.field_mappings,
        source_offsets=scope.source_offsets,
    )


def build_tool_server(scope: AgentScope) -> FastMCP:
    """Build an MCP server whose tools are bound to *scope*.

    A fresh (cheap) server instance per conversation turn keeps scoping
    airtight without threading per-call context through the MCP protocol.
    """
    from vestigo.api.routers.events import (
        _get_query_service,
        _get_similarity_service,
        _persist_detector_run,
        _run_stat_detector,
        _serialize_stat_result,
        _validate_field_regexes,
        _validate_regex,
    )

    server = FastMCP(
        "vestigo-investigation",
        instructions=(
            "Read-only forensic log investigation tools, scoped to one case "
            "timeline. Iterate: inspect fields, search, aggregate, then "
            "return refined filters as findings."
        ),
    )
    service = _get_query_service()

    def _validated(spec: FilterSpec | None) -> FilterSpec:
        spec = spec or FilterSpec()
        _validate_regex(spec.q, spec.q_regex)
        _validate_field_regexes(spec.filters, spec.filter_modes)
        _validate_field_regexes(spec.exclusions, spec.exclusion_modes)
        return spec

    @server.tool()
    async def search_events(
        filters: FilterSpec | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str = "desc",
    ) -> dict[str, Any]:
        """Search events with Explorer-equivalent filters.

        Returns the total match count plus up to `limit` (max 50) compacted
        events. Iterate by refining `filters` rather than paging deeply —
        aggregations (field_terms, histogram) summarize better than paging.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec, limit=limit, offset=offset, order=order)
        page = await run_in_threadpool(service.query, query)
        return {
            "total": page.total,
            "returned": len(page.events),
            "events": [_slim_event(e) for e in page.events],
        }

    @server.tool()
    async def get_event(event_id: str) -> dict[str, Any]:
        """Fetch a single event by its event_id (full attribute set, truncated values)."""
        query = await _build_query(scope, FilterSpec(), limit=1)
        query.event_ids = [event_id]
        page = await run_in_threadpool(service.query, query)
        if not page.events:
            return {"error": f"event {event_id} not found in this timeline"}
        return _slim_event(page.events[0])

    @server.tool()
    async def list_fields() -> dict[str, Any]:
        """List queryable fields: fixed top-level columns and dynamic attribute keys."""
        return await run_in_threadpool(
            service.list_fields, scope.case_id, scope.source_ids, scope.field_mappings
        )

    @server.tool()
    async def list_artifacts() -> list[str]:
        """List the distinct artifact types present in this timeline."""
        return await run_in_threadpool(
            service.list_distinct_artifacts, scope.case_id, scope.source_ids
        )

    @server.tool()
    async def field_terms(
        field: str, filters: FilterSpec | None = None, limit: int = 30
    ) -> dict[str, Any]:
        """Top-N value distribution for a field, honoring optional filters.

        The primary primitive for spotting dominant/rare values. `other_count`
        is the tail beyond the top N.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        return await run_in_threadpool(service.field_terms, query, field, min(limit, 100))

    @server.tool()
    async def field_numeric_stats(field: str, filters: FilterSpec | None = None) -> dict[str, Any]:
        """Summary stats + fixed-width histogram for a numeric field. count==0 means non-numeric."""
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        return await run_in_threadpool(service.field_numeric_stats, query, field)

    @server.tool()
    async def histogram(filters: FilterSpec | None = None, buckets: int = 48) -> dict[str, Any]:
        """Time-bucketed event counts honoring optional filters — the timeline's shape."""
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        return await run_in_threadpool(service.histogram, query, min(max(buckets, 4), 120))

    @server.tool()
    async def run_anomaly_detector(
        detector: str,
        fields: str | None = None,
        series_field: str = "artifact",
        baseline_id: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Run a statistical anomaly detector over the timeline.

        Detectors: value_novelty (rare/first-seen values), value_combo,
        frequency (volume spikes/silences), timestamp_order, numeric_range,
        charset, entropy, proportion_shift, interval_periodicity,
        sequence_novelty, sequence_motif, value_distribution_drift.
        `fields` is a comma-separated field list for value detectors (omit to
        auto-recommend); `series_field` groups frequency/sequence detectors.
        Returns findings plus a persisted run_id the analyst can open.
        """
        result, resolution = await _run_stat_detector(
            scope.case_id,
            scope.timeline_id,
            scope.source_ids,
            detector=detector,
            fields=fields,
            series_field=series_field,
            z_threshold=None,
            baseline_id=baseline_id,
            limit=min(limit, 100),
            field_mappings=scope.field_mappings,
            source_offsets=scope.source_offsets,
        )
        payload = _serialize_stat_result(result)
        run_id = None
        if result.status == "ok":
            run_id = await _persist_detector_run(
                scope.case_id,
                scope.timeline_id,
                detector=detector,
                fields=fields,
                series_field=series_field,
                z_threshold=None,
                limit=min(limit, 100),
                payload=payload,
                resolution=resolution,
                source_offsets=scope.source_offsets,
            )
        payload["run_id"] = run_id
        return payload

    @server.tool()
    async def propose_finding(title: str, description: str, filters: FilterSpec) -> dict[str, Any]:
        """Propose a distilled finding to the analyst.

        Call this when a filter set isolates something worth attention. The
        analyst sees a card with your title/description and an "apply to
        Explorer" button carrying exactly these filters. The returned total
        is the filter's current hit count — verify it is what you expect.
        Propose only filters you have actually run via search_events.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec, limit=1)
        page = await run_in_threadpool(service.query, query)
        return {"accepted": True, "title": title, "total": page.total}

    @server.tool()
    async def semantic_search(q: str, limit: int = 10) -> dict[str, Any]:
        """Find events semantically similar to free text (needs embeddings)."""
        if not embeddings_available():
            return {"error": "embeddings are not available in this installation"}
        svc = _get_similarity_service()
        result = await run_in_threadpool(
            svc.find_similar_by_text, scope.case_id, scope.source_ids, q, limit=min(limit, 50)
        )
        return {
            "status": result.status,
            "results": [
                {"event_id": r.event_id, "score": r.score, "event": _slim_event(r.event or {})}
                for r in result.results
            ],
        }

    @server.tool()
    async def similar_events(event_id: str, limit: int = 10) -> dict[str, Any]:
        """Find events semantically similar to an existing event (needs embeddings)."""
        svc = _get_similarity_service()
        result = await run_in_threadpool(
            svc.find_similar, scope.case_id, scope.source_ids, event_id, limit=min(limit, 50)
        )
        return {
            "status": result.status,
            "results": [
                {"event_id": r.event_id, "score": r.score, "event": _slim_event(r.event or {})}
                for r in result.results
            ],
        }

    return server
