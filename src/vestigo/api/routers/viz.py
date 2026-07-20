"""API routes for field-value visualization/statistics aggregations.

Powers two frontend features (see ``docs/`` roadmap / CLAUDE.md): the
per-value histogram modal in the Explorer's event detail panel, and the
full Visualization page. Every endpoint here accepts the same filter query
params as ``GET .../events`` and ``GET .../histogram`` (see
``events.py::list_events``/``get_histogram``) so a chart always reflects
exactly the currently-filtered Explorer view — never a separate, drifting
notion of "current filters".
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from vestigo.api.deps import (
    get_store,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from vestigo.api.routers.events import (
    _get_query_service,
    _get_stat_anomaly_service,
    _parse_modes_object,
    _parse_multivalue_object,
    _parse_str_list,
    _resolve_event_id_filters,
    _resolve_timeline_scope,
    _resolve_timeline_source_ids,
    _run_regex_guarded,
    _uses_regex,
    _validate_field_regexes,
    _validate_regex,
)
from vestigo.db._time_fields import TIME_FIELD_SPECS
from vestigo.db.field_stats import (
    ensure_source_field_stats,
    merged_field_terms,
    merged_inventory,
)
from vestigo.db.postgres import Case, User, generate_id
from vestigo.db.queries import EventQuery

router = APIRouter(prefix="/api/cases", tags=["viz"])


async def _resolve_event_query(
    case_id: str,
    timeline_id: str,
    *,
    q: str | None,
    q_regex: bool,
    artifact: str | None,
    artifacts: str | None,
    source_id: str | None,
    tag: str | None,
    exclude_tag: str | None,
    tags_include: str | None,
    tags_exclude: str | None,
    ids: str | None,
    start: datetime | None,
    end: datetime | None,
    filters: str | None,
    exclusions: str | None,
    annotated: str | None,
    annotation_tag_value: str | None,
    run_id: str | None,
    filter_modes: str | None,
    exclusion_modes: str | None,
) -> EventQuery:
    """Resolve the shared filter query params into an :class:`EventQuery`.

    Mirrors ``events.py::get_histogram``'s param-resolution sequence
    (timeline → source_ids, then the annotated/tags_include/tags_exclude/ids
    combo via ``_resolve_event_id_filters``) so every viz endpoint below
    builds an identical ``EventQuery`` from identical inputs — one place to
    keep in sync with ``list_events``/``get_histogram`` instead of three.
    """
    _validate_regex(q, q_regex)
    parsed_filters = _parse_multivalue_object(filters)
    parsed_exclusions = _parse_multivalue_object(exclusions)
    parsed_filter_modes = _parse_modes_object(filter_modes)
    parsed_exclusion_modes = _parse_modes_object(exclusion_modes)
    _validate_field_regexes(parsed_filters, parsed_filter_modes)
    _validate_field_regexes(parsed_exclusions, parsed_exclusion_modes)
    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    event_ids, tags_include_filter, tags_exclude_filter = await _resolve_event_id_filters(
        case_id,
        source_ids,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
    )
    return EventQuery(
        case_id=case_id,
        source_ids=source_ids,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=_parse_str_list(artifacts),
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        start=start,
        end=end,
        field_filters=parsed_filters,
        field_exclusions=parsed_exclusions,
        filter_modes=parsed_filter_modes,
        exclusion_modes=parsed_exclusion_modes,
        event_ids=event_ids,
        tags_include=tags_include_filter,
        tags_exclude=tags_exclude_filter,
        field_mappings=field_mappings,
        source_offsets=source_offsets,
    )


# Shared `Query(...)` declarations for the filter params every endpoint below
# accepts — FastAPI needs each param redeclared per-route for docs/validation,
# but the *values* immediately flow into `_resolve_event_query` above so the
# resolution logic itself is written once.
_Q = Query(default=None, description="Free-text search, broadened across all fields")
_Q_REGEX = Query(default=False, description="Treat q as an RE2 regular expression.")
_ARTIFACT = Query(default=None)
_ARTIFACTS = Query(default=None, description="Comma-separated artifact values (OR'd)")
_SOURCE_ID = Query(default=None)
_TAG = Query(default=None, description="Deprecated single-value form — prefer tags_include.")
_EXCLUDE_TAG = Query(
    default=None, description="Deprecated single-value form — prefer tags_exclude."
)
_TAGS_INCLUDE = Query(default=None)
_TAGS_EXCLUDE = Query(default=None)
_IDS = Query(default=None)
_START = Query(default=None)
_END = Query(default=None)
_FILTERS = Query(default=None)
_EXCLUSIONS = Query(default=None)
_FILTER_MODES = Query(default=None, description="JSON match-mode map for `filters`.")
_EXCLUSION_MODES = Query(default=None, description="JSON match-mode map for `exclusions`.")
_ANNOTATED = Query(default=None)
_ANNOTATION_TAG_VALUE = Query(default=None)
_RUN_ID = Query(default=None)


def _is_unfiltered(query: EventQuery) -> bool:
    """True when *query* restricts nothing beyond the timeline's own scope.

    The cache-eligibility check for M24a: an unfiltered first-load
    aggregation depends only on (timeline sources, field), which the
    per-source ``field_stats`` cache can answer without a ClickHouse scan.
    ``source_ids`` (timeline scope) is deliberately not a filter here — the
    cached merge runs over exactly those sources.
    """
    return (
        not any(
            [
                query.q,
                query.artifact,
                query.artifacts,
                query.source_id,
                query.tag,
                query.exclude_tag,
                query.start,
                query.end,
                query.field_filters,
                query.field_exclusions,
                query.tags_include,
                query.tags_exclude,
            ]
        )
        and query.event_ids is None
        and query.exclude_event_ids is None
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-terms")
async def get_field_terms(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'artifact' or 'attr:status_code'"),
    limit: int = Query(default=50, ge=1, le=500),
    q: str | None = _Q,
    q_regex: bool = _Q_REGEX,
    artifact: str | None = _ARTIFACT,
    artifacts: str | None = _ARTIFACTS,
    source_id: str | None = _SOURCE_ID,
    tag: str | None = _TAG,
    exclude_tag: str | None = _EXCLUDE_TAG,
    tags_include: str | None = _TAGS_INCLUDE,
    tags_exclude: str | None = _TAGS_EXCLUDE,
    ids: str | None = _IDS,
    start: datetime | None = _START,  # noqa: B008
    end: datetime | None = _END,  # noqa: B008
    filters: str | None = _FILTERS,
    exclusions: str | None = _EXCLUSIONS,
    filter_modes: str | None = _FILTER_MODES,
    exclusion_modes: str | None = _EXCLUSION_MODES,
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a top-N terms aggregation (value → count) for *field*.

    Powers the per-value histogram modal's top-list and nominal/ordinal
    chart types (bar, pie) on the Visualization page.
    """
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=artifacts,
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
        start=start,
        end=end,
        filters=filters,
        exclusions=exclusions,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        filter_modes=filter_modes,
        exclusion_modes=exclusion_modes,
    )
    # M24a: an unfiltered first load is answerable from the per-source
    # field_stats cache — no ClickHouse scan, no HEAVY_SCAN_GATE slot. A
    # canonical mapped field must stay live (coalesce over several raw keys
    # dedupes per event; not derivable from per-key caches), and any filter
    # or a cache gap falls through to the live path below.
    if _is_unfiltered(query) and not (query.field_mappings and field in query.field_mappings):
        stats = await ensure_source_field_stats(
            get_store(), _get_stat_anomaly_service().ch, case_id, query.source_ids or []
        )
        cached = merged_field_terms(stats, field, limit)
        if cached is not None:
            return {**cached, "cached": True}
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_terms,
        query,
        field,
        limit,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-numeric")
async def get_field_numeric_stats(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:bytes_sent'"),
    bins: int = Query(default=30, ge=1, le=200),
    q: str | None = _Q,
    q_regex: bool = _Q_REGEX,
    artifact: str | None = _ARTIFACT,
    artifacts: str | None = _ARTIFACTS,
    source_id: str | None = _SOURCE_ID,
    tag: str | None = _TAG,
    exclude_tag: str | None = _EXCLUDE_TAG,
    tags_include: str | None = _TAGS_INCLUDE,
    tags_exclude: str | None = _TAGS_EXCLUDE,
    ids: str | None = _IDS,
    start: datetime | None = _START,  # noqa: B008
    end: datetime | None = _END,  # noqa: B008
    filters: str | None = _FILTERS,
    exclusions: str | None = _EXCLUSIONS,
    filter_modes: str | None = _FILTER_MODES,
    exclusion_modes: str | None = _EXCLUSION_MODES,
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return summary statistics and a fixed-width histogram for a numeric field.

    ``count == 0`` in the response means the field has no numeric values in
    the current filter set — the Visualization page falls back to treating
    it as categorical. Powers histogram/box/violin/ECDF chart types.
    """
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=artifacts,
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
        start=start,
        end=end,
        filters=filters,
        exclusions=exclusions,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        filter_modes=filter_modes,
        exclusion_modes=exclusion_modes,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_numeric_stats,
        query,
        field,
        bins,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-timeseries")
async def get_field_value_timeseries(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:status_code'"),
    buckets: int = Query(default=60, ge=10, le=200),
    series_limit: int = Query(default=12, ge=1, le=50),
    q: str | None = _Q,
    q_regex: bool = _Q_REGEX,
    artifact: str | None = _ARTIFACT,
    artifacts: str | None = _ARTIFACTS,
    source_id: str | None = _SOURCE_ID,
    tag: str | None = _TAG,
    exclude_tag: str | None = _EXCLUDE_TAG,
    tags_include: str | None = _TAGS_INCLUDE,
    tags_exclude: str | None = _TAGS_EXCLUDE,
    ids: str | None = _IDS,
    start: datetime | None = _START,  # noqa: B008
    end: datetime | None = _END,  # noqa: B008
    filters: str | None = _FILTERS,
    exclusions: str | None = _EXCLUSIONS,
    filter_modes: str | None = _FILTER_MODES,
    exclusion_modes: str | None = _EXCLUSION_MODES,
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return per-value event counts bucketed over time for *field*.

    Restricted to the top ``series_limit`` values by overall count (see
    ``EventQueryService.field_value_timeseries``). Powers the multi-series
    line/area chart and the value×time heatmap on the Visualization page.
    """
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=artifacts,
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
        start=start,
        end=end,
        filters=filters,
        exclusions=exclusions,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        filter_modes=filter_modes,
        exclusion_modes=exclusion_modes,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_value_timeseries,
        query,
        field,
        buckets,
        series_limit,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/time-punchcard")
async def get_time_punchcard(
    case_id: str,
    timeline_id: str,
    q: str | None = _Q,
    q_regex: bool = _Q_REGEX,
    artifact: str | None = _ARTIFACT,
    artifacts: str | None = _ARTIFACTS,
    source_id: str | None = _SOURCE_ID,
    tag: str | None = _TAG,
    exclude_tag: str | None = _EXCLUDE_TAG,
    tags_include: str | None = _TAGS_INCLUDE,
    tags_exclude: str | None = _TAGS_EXCLUDE,
    ids: str | None = _IDS,
    start: datetime | None = _START,  # noqa: B008
    end: datetime | None = _END,  # noqa: B008
    filters: str | None = _FILTERS,
    exclusions: str | None = _EXCLUSIONS,
    filter_modes: str | None = _FILTER_MODES,
    exclusion_modes: str | None = _EXCLUSION_MODES,
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return event counts by (day-of-week × hour-of-day), UTC.

    Field-free like ``GET .../histogram``. ``dow`` is ISO (1 = Monday …
    7 = Sunday); extraction is pinned to UTC (see
    ``EventQueryService.time_punchcard``). Powers the punch-card chart —
    the "does activity happen outside working hours?" view.
    """
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=artifacts,
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
        start=start,
        end=end,
        filters=filters,
        exclusions=exclusions,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        filter_modes=filter_modes,
        exclusion_modes=exclusion_modes,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.time_punchcard,
        query,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-pivot")
async def get_field_pivot(
    case_id: str,
    timeline_id: str,
    field_x: str = Query(..., description="X-axis field token, e.g. 'attr:username'"),
    field_y: str = Query(..., description="Y-axis field token, e.g. 'attr:workstation'"),
    limit_x: int = Query(default=10, ge=1, le=50),
    limit_y: int = Query(default=10, ge=1, le=50),
    q: str | None = _Q,
    q_regex: bool = _Q_REGEX,
    artifact: str | None = _ARTIFACT,
    artifacts: str | None = _ARTIFACTS,
    source_id: str | None = _SOURCE_ID,
    tag: str | None = _TAG,
    exclude_tag: str | None = _EXCLUDE_TAG,
    tags_include: str | None = _TAGS_INCLUDE,
    tags_exclude: str | None = _TAGS_EXCLUDE,
    ids: str | None = _IDS,
    start: datetime | None = _START,  # noqa: B008
    end: datetime | None = _END,  # noqa: B008
    filters: str | None = _FILTERS,
    exclusions: str | None = _EXCLUSIONS,
    filter_modes: str | None = _FILTER_MODES,
    exclusion_modes: str | None = _EXCLUSION_MODES,
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a top-X × top-Y co-occurrence matrix for two categorical fields.

    ``""`` on either axis of a cell means "outside that axis's top-N"
    (truthful Other rollup). Powers the field×field heatmap and the flow
    (Sankey) chart on the Visualization page.
    """
    if field_x == field_y:
        raise HTTPException(status_code=422, detail="field_x and field_y must differ")
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=artifacts,
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
        start=start,
        end=end,
        filters=filters,
        exclusions=exclusions,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        filter_modes=filter_modes,
        exclusion_modes=exclusion_modes,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_pivot,
        query,
        field_x,
        field_y,
        limit_x,
        limit_y,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-scatter")
async def get_field_scatter(
    case_id: str,
    timeline_id: str,
    field_x: str = Query(..., description="X-axis numeric field token, e.g. 'attr:bytes_sent'"),
    field_y: str = Query(..., description="Y-axis numeric field token, e.g. 'attr:duration_ms'"),
    limit: int = Query(default=5000, ge=100, le=20000, description="Max sampled points"),
    q: str | None = _Q,
    q_regex: bool = _Q_REGEX,
    artifact: str | None = _ARTIFACT,
    artifacts: str | None = _ARTIFACTS,
    source_id: str | None = _SOURCE_ID,
    tag: str | None = _TAG,
    exclude_tag: str | None = _EXCLUDE_TAG,
    tags_include: str | None = _TAGS_INCLUDE,
    tags_exclude: str | None = _TAGS_EXCLUDE,
    ids: str | None = _IDS,
    start: datetime | None = _START,  # noqa: B008
    end: datetime | None = _END,  # noqa: B008
    filters: str | None = _FILTERS,
    exclusions: str | None = _EXCLUSIONS,
    filter_modes: str | None = _FILTER_MODES,
    exclusion_modes: str | None = _EXCLUSION_MODES,
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a uniform random sample of (x, y) numeric pairs for a scatter plot.

    ``total`` is the full pair count and the per-axis min/max describe the
    full data (not the sample), so the frontend caption can state "showing
    N of M points (uniform random sample)" truthfully. ``total == 0`` means
    one or both fields have no numeric values under the current filters —
    the frontend falls back to a categorical hint, mirroring field-numeric.
    """
    if field_x == field_y:
        raise HTTPException(status_code=422, detail="field_x and field_y must differ")
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
        q_regex=q_regex,
        artifact=artifact,
        artifacts=artifacts,
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        ids=ids,
        start=start,
        end=end,
        filters=filters,
        exclusions=exclusions,
        annotated=annotated,
        annotation_tag_value=annotation_tag_value,
        run_id=run_id,
        filter_modes=filter_modes,
        exclusion_modes=exclusion_modes,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_scatter,
        query,
        field_x,
        field_y,
        limit,
    )


class CompareFilters(BaseModel):
    """One comparison layer's filter set — field-for-field the same names and
    string encodings as the shared viz/events filter *query params*, so the
    frontend's ``serializeEventFilterParams`` output maps 1:1 into a body
    object and resolution reuses ``_resolve_event_query`` unchanged.
    """

    q: str | None = None
    q_regex: bool = False
    artifact: str | None = None
    artifacts: str | None = None
    source_id: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    tags_include: str | None = None
    tags_exclude: str | None = None
    ids: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    filters: str | None = None
    exclusions: str | None = None
    filter_modes: str | None = None
    exclusion_modes: str | None = None
    annotated: str | None = None
    annotation_tag_value: str | None = None
    run_id: str | None = None


class ComparisonSpec(BaseModel):
    """The comparison layer: all timeline events (baseline) or a second filter set."""

    mode: Literal["baseline", "custom"]
    filters: CompareFilters | None = None


class CompareRequest(BaseModel):
    """Body for ``POST .../viz/compare`` — two filter sets don't fit query params."""

    kind: Literal["time", "terms", "numeric"]
    field: str | None = None
    primary: CompareFilters = Field(default_factory=CompareFilters)
    comparison: ComparisonSpec
    buckets: int = Field(default=60, ge=10, le=200)
    bins: int = Field(default=30, ge=1, le=200)
    limit: int = Field(default=50, ge=1, le=500)


async def _resolve_body_query(case_id: str, timeline_id: str, body: CompareFilters):
    return await _resolve_event_query(
        case_id,
        timeline_id,
        q=body.q,
        q_regex=body.q_regex,
        artifact=body.artifact,
        artifacts=body.artifacts,
        source_id=body.source_id,
        tag=body.tag,
        exclude_tag=body.exclude_tag,
        tags_include=body.tags_include,
        tags_exclude=body.tags_exclude,
        ids=body.ids,
        start=body.start,
        end=body.end,
        filters=body.filters,
        exclusions=body.exclusions,
        annotated=body.annotated,
        annotation_tag_value=body.annotation_tag_value,
        run_id=body.run_id,
        filter_modes=body.filter_modes,
        exclusion_modes=body.exclusion_modes,
    )


@router.post("/{case_id}/timelines/{timeline_id}/viz/compare")
async def compare_layers(
    case_id: str,
    timeline_id: str,
    body: CompareRequest,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Compare a primary filter layer against a baseline or custom second layer.

    Comparability is enforced server-side: both layers are evaluated against
    one shared grid (same resolved time range, same bucket interval / bin
    edges, same top-N category list — see ``EventQueryService.compare_*``),
    so the returned series are comparable by construction. The response
    carries raw counts only — derived metrics (delta / rate / % of baseline /
    cumulative) are pure frontend transforms, keeping counts the forensic
    ground truth.
    """
    if body.kind in ("terms", "numeric") and not body.field:
        raise HTTPException(status_code=422, detail=f"kind={body.kind!r} requires 'field'")

    primary = await _resolve_body_query(case_id, timeline_id, body.primary)

    baseline_token: tuple | None = None
    if body.comparison.mode == "baseline":
        # All events of the timeline: filters dropped, timeline scope and
        # explicit time window kept — "the whole" the primary is a part of.
        # Built via the same all-keyword _resolve_event_query used for every
        # other filter resolution (rather than a hand-listed `replace(...)` of
        # EventQuery fields) so a filter field added there without a matching
        # `None` here is a TypeError, not a silently leaked baseline filter.
        comparison = await _resolve_event_query(
            case_id,
            timeline_id,
            q=None,
            q_regex=False,
            artifact=None,
            artifacts=None,
            source_id=None,
            tag=None,
            exclude_tag=None,
            tags_include=None,
            tags_exclude=None,
            ids=None,
            start=primary.start,
            end=primary.end,
            filters=None,
            exclusions=None,
            annotated=None,
            annotation_tag_value=None,
            run_id=None,
            filter_modes=None,
            exclusion_modes=None,
        )
        # M24c: freshness fingerprint for the baseline-layer cache. The
        # comparison layer here is a strict superset of the primary (same
        # timeline sources + explicit window, all filters dropped) — the
        # compare_* methods' cache paths and their primary-range-scan skip
        # both rest on that invariant; anything that could make a primary
        # filter *add* rows outside timeline scope breaks it. computed_at
        # moves on exactly the two source-mutation events (ingest,
        # enrichment apply); a source without a stats row disables caching
        # for this render (token stays None — always safe).
        rows = await get_store().get_source_field_stats(comparison.source_ids or [])
        by_source = {row.source_id: row for row in rows}
        if comparison.source_ids and all(sid in by_source for sid in comparison.source_ids):
            baseline_token = (
                case_id,
                tuple(
                    sorted(
                        (sid, by_source[sid].computed_at.isoformat(), by_source[sid].events_total)
                        for sid in comparison.source_ids
                    )
                ),
            )
    else:
        if body.comparison.filters is None:
            raise HTTPException(status_code=422, detail="mode='custom' requires 'filters'")
        comparison = await _resolve_body_query(case_id, timeline_id, body.comparison.filters)
        # Comparability invariant: both layers share the primary's explicit
        # time window (the union of data ranges handles the implicit case).
        comparison = replace(comparison, start=primary.start, end=primary.end)

    service = _get_query_service()
    q_regex = _uses_regex(
        primary.q_regex or comparison.q_regex,
        primary.filter_modes,
        primary.exclusion_modes,
        comparison.filter_modes,
        comparison.exclusion_modes,
    )
    if body.kind == "time":
        return await _run_regex_guarded(
            q_regex,
            service.compare_time_histogram,
            primary,
            comparison,
            body.buckets,
            baseline_cache_token=baseline_token,
        )
    if body.kind == "terms":
        return await _run_regex_guarded(
            q_regex,
            service.compare_field_terms,
            primary,
            comparison,
            body.field,
            body.limit,
            baseline_cache_token=baseline_token,
        )
    return await _run_regex_guarded(
        q_regex,
        service.compare_field_numeric,
        primary,
        comparison,
        body.field,
        body.bins,
        baseline_cache_token=baseline_token,
    )


class SavedChartCreate(BaseModel):
    """Body for creating a saved chart."""

    name: str = Field(min_length=1, max_length=255)
    config: dict[str, Any]


class SavedChartRename(BaseModel):
    """Body for renaming a saved chart."""

    name: str = Field(min_length=1, max_length=255)


@router.get("/{case_id}/timelines/{timeline_id}/viz/charts")
async def list_saved_charts(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List a timeline's saved charts (newest first)."""
    store = get_store()
    charts = await store.list_saved_charts(case_id, timeline_id)
    return {"charts": [c.to_dict() for c in charts]}


@router.post("/{case_id}/timelines/{timeline_id}/viz/charts")
async def create_saved_chart(
    case_id: str,
    timeline_id: str,
    payload: SavedChartCreate,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Save the current chart config under a name.

    ``config`` is the frontend's versioned ``ChartConfig`` and is stored as
    opaque JSON — the backend round-trips it without interpretation, exactly
    like a View's filter payload.
    """
    store = get_store()
    chart = await store.create_saved_chart(
        case_id=case_id,
        timeline_id=timeline_id,
        chart_id=generate_id(payload.name),
        name=payload.name,
        config=payload.config,
    )
    return {"chart": chart.to_dict()}


@router.patch("/{case_id}/timelines/{timeline_id}/viz/charts/{chart_id}")
async def rename_saved_chart(
    case_id: str,
    timeline_id: str,
    chart_id: str,
    payload: SavedChartRename,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Rename a saved chart (the stored config itself is immutable)."""
    store = get_store()
    chart = await store.rename_saved_chart(case_id, timeline_id, chart_id, payload.name)
    if chart is None:
        raise HTTPException(status_code=404, detail="Saved chart not found")
    return {"chart": chart.to_dict()}


@router.delete("/{case_id}/timelines/{timeline_id}/viz/charts/{chart_id}")
async def delete_saved_chart(
    case_id: str,
    timeline_id: str,
    chart_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Delete a saved chart."""
    store = get_store()
    deleted = await store.delete_saved_chart(case_id, timeline_id, chart_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved chart not found")
    return {"deleted": True, "chart_id": chart_id}


@router.get("/{case_id}/timelines/{timeline_id}/viz/fields")
async def list_viz_fields(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return every chartable field for the Visualization page's field picker.

    Unlike ``GET .../anomalies/fields`` this applies **no** novelty-detection
    heuristics — charting a constant or identifier-like field is a legitimate
    analyst choice, and coupling the picker to anomaly tuning would let
    detector changes silently reshape this list. Each entry carries:

    - ``token``    — field token to pass to the viz endpoints' ``field`` param.
    - ``distinct`` — number of distinct non-empty values.
    - ``coverage`` — fraction of events with a non-empty value (0-1).
    - ``label``    — display name, for virtual fields whose token is not
      self-explanatory; absent for ordinary data fields.

    Sorted by coverage descending, then token — the first entry is the
    frontend's default field pick.

    The virtual ``time:`` fields (:mod:`vestigo.db._time_fields`) are appended
    **after** that sort rather than merged into it. They are defined for every
    dated event, so a coverage-ranked merge would put them above every real
    field and hand the picker's default pick to an hour-of-day axis. They are
    listed here at all — not just exposed to the agent's charting tools —
    because the analyst and the agent must be able to name the same fields;
    anything the agent can chart the analyst has to be able to rebuild by hand.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    svc = _get_stat_anomaly_service()
    stats = await ensure_source_field_stats(get_store(), svc.ch, case_id, source_ids)
    inventory, total = merged_inventory(stats)
    if total == 0:
        return {"fields": []}
    fields = [
        {"token": token, "distinct": distinct, "coverage": round(cov_count / total, 4)}
        for token, distinct, cov_count in inventory
    ]
    fields.sort(key=lambda f: (-f["coverage"], f["token"]))
    fields.extend(
        {
            "token": token,
            "distinct": len(spec.domain) if spec.domain else 0,
            "coverage": 1.0,
            "label": spec.label,
        }
        for token, spec in TIME_FIELD_SPECS.items()
    )
    return {"fields": fields}
