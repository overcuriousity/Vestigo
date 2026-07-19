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

import asyncio
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


def _slim_annotation(row: Any) -> dict[str, Any]:
    """Compact an Annotation row for model consumption."""
    return {
        "event_id": row.event_id,
        "source_id": row.source_id,
        "type": row.annotation_type,
        "content": _truncate(row.content, MESSAGE_TRUNCATE),
        "origin": row.origin,
        "detector": row.detector,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


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

    @server.tool()
    async def list_baselines() -> dict[str, Any]:
        """List saved baseline definitions (baseline range + suspect windows).

        Pass a baseline's id as `baseline_id` to run_anomaly_detector to run
        temporal detection (proportion_shift, interval_periodicity,
        sequence_novelty, frequency, value_distribution_drift) against it.
        """
        from vestigo.api.deps import get_store

        rows = await get_store().list_baseline_definitions(scope.case_id, scope.timeline_id)
        return {
            "total": len(rows),
            "baselines": [
                {
                    "id": r.id,
                    "name": r.name,
                    **r.windows_payload(),
                    "created_by": r.created_by,
                }
                for r in rows
            ],
        }

    @server.tool()
    async def list_dispositions(
        kind: str | None = None, detector: str | None = None
    ) -> dict[str, Any]:
        """List analyst verdicts on anomaly findings visible from this timeline.

        Kinds: 'normal' (expected behavior, suppresses detection), 'dismissed'
        (noise), 'confirmed' (escalated true positive), 'routine' (recurring
        expected motif). Use these to avoid re-reporting what the analyst has
        already judged.
        """
        from vestigo.api.deps import get_store

        rows = await get_store().list_dispositions(
            scope.case_id,
            timeline_id=scope.timeline_id,
            source_ids=scope.source_ids,
            kinds=[kind] if kind else None,
            detector=detector,
        )
        return {
            "total": len(rows),
            "dispositions": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "detector": r.detector,
                    "field": r.field,
                    "value": _truncate(r.value, ATTR_VALUE_TRUNCATE),
                    "source_id": r.source_id,
                    "event_id": r.event_id,
                    "note": _truncate(r.note, MESSAGE_TRUNCATE),
                    "created_by": r.created_by,
                }
                for r in rows
            ],
        }

    @server.tool()
    async def list_saved_views() -> dict[str, Any]:
        """List the analyst's saved filter views for this case (name, query, filter payload)."""
        from vestigo.api.deps import get_store

        rows = await get_store().list_views(scope.case_id)
        return {
            "total": len(rows),
            "views": [
                {"id": r.id, "name": r.name, "query": r.query, "filter": r.view_filter or {}}
                for r in rows
            ],
        }

    @server.tool()
    async def list_annotations(annotation_type: str | None = None) -> dict[str, Any]:
        """List annotations (tags/comments/system anomaly marks) across this timeline's sources.

        `annotation_type` filters to 'tag', 'comment', or 'anomaly'. Results
        are capped at 200 rows, oldest first — use get_event_annotations for
        one event's full detail.
        """
        from vestigo.api.deps import get_store

        rows = await get_store().list_source_annotations(scope.case_id, scope.source_ids)
        if annotation_type:
            rows = [r for r in rows if r.annotation_type == annotation_type]
        return {
            "total": len(rows),
            "annotations": [_slim_annotation(r) for r in rows[:200]],
        }

    @server.tool()
    async def get_event_annotations(source_id: str, event_id: str) -> dict[str, Any]:
        """List all annotations attached to one event (full content, oldest first)."""
        from vestigo.api.deps import get_store

        if source_id not in scope.source_ids:
            return {"error": f"source {source_id} is not part of this timeline"}
        rows = await get_store().list_annotations(scope.case_id, source_id, event_id)
        return {"total": len(rows), "annotations": [_slim_annotation(r) for r in rows]}

    @server.tool()
    async def list_sigma_rules() -> dict[str, Any]:
        """List Sigma detection rules available to this case (metadata only).

        Covers both the global rule directory and case-uploaded rules. Use
        get_sigma_rule with a case rule's id to read its YAML body.
        """
        from vestigo.api.deps import get_store
        from vestigo.api.routers.sigma import _global_rule_dict, _load_global

        global_rules, case_rows = await asyncio.gather(
            _load_global(), get_store().list_sigma_rules(scope.case_id)
        )
        rules = [_global_rule_dict(r) for r in global_rules]
        for row in case_rows:
            rules.append(
                {
                    "origin": "case",
                    "id": row.id,
                    "rule_key": row.rule_key,
                    "title": row.title,
                    "level": row.level,
                    "logsource": row.logsource,
                    "enabled": row.enabled,
                }
            )
        return {"total": len(rules), "rules": rules}

    @server.tool()
    async def get_sigma_rule(rule_id: str) -> dict[str, Any]:
        """Fetch one case-uploaded Sigma rule including its full YAML content."""
        from vestigo.api.deps import get_store

        row = await get_store().get_sigma_rule(scope.case_id, rule_id)
        if row is None:
            return {"error": f"no case-uploaded sigma rule with id {rule_id}"}
        return row.to_dict()

    @server.tool()
    async def list_sigma_runs() -> dict[str, Any]:
        """List past Sigma evaluations over this timeline (newest first, no per-rule detail)."""
        from vestigo.api.deps import get_store

        rows = await get_store().list_sigma_runs(scope.case_id)
        rows = [r for r in rows if r.timeline_id == scope.timeline_id]
        return {
            "total": len(rows),
            "runs": [
                {
                    "id": r.id,
                    "status": r.status,
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "rule_count": len(r.results or []),
                }
                for r in rows
            ],
        }

    @server.tool()
    async def get_sigma_run(run_id: str) -> dict[str, Any]:
        """Fetch one Sigma run's full per-rule results (match counts, statuses, compiled SQL)."""
        from vestigo.api.deps import get_store

        row = await get_store().get_sigma_run(scope.case_id, run_id)
        if row is None or row.timeline_id != scope.timeline_id:
            return {"error": f"no sigma run with id {run_id} in this timeline"}
        return row.to_dict()

    return server
