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
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from tracevector.api.deps import require_case_read
from tracevector.api.routers.events import (
    _get_query_service,
    _get_stat_anomaly_service,
    _parse_exclusions_object,
    _parse_json_object,
    _parse_str_list,
    _resolve_event_id_filters,
    _resolve_timeline_source_ids,
)
from tracevector.db.postgres import Case
from tracevector.db.queries import EventQuery

router = APIRouter(prefix="/api/cases", tags=["viz"])


async def _resolve_event_query(
    case_id: str,
    timeline_id: str,
    *,
    q: str | None,
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
) -> EventQuery:
    """Resolve the shared filter query params into an :class:`EventQuery`.

    Mirrors ``events.py::get_histogram``'s param-resolution sequence
    (timeline → source_ids, then the annotated/tags_include/tags_exclude/ids
    combo via ``_resolve_event_id_filters``) so every viz endpoint below
    builds an identical ``EventQuery`` from identical inputs — one place to
    keep in sync with ``list_events``/``get_histogram`` instead of three.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
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
        artifact=artifact,
        artifacts=_parse_str_list(artifacts),
        source_id=source_id,
        tag=tag,
        exclude_tag=exclude_tag,
        start=start,
        end=end,
        field_filters=_parse_json_object(filters),
        field_exclusions=_parse_exclusions_object(exclusions),
        event_ids=event_ids,
        tags_include=tags_include_filter,
        tags_exclude=tags_exclude_filter,
    )


# Shared `Query(...)` declarations for the filter params every endpoint below
# accepts — FastAPI needs each param redeclared per-route for docs/validation,
# but the *values* immediately flow into `_resolve_event_query` above so the
# resolution logic itself is written once.
_Q = Query(default=None, description="Free-text search, broadened across all fields")
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
_ANNOTATED = Query(default=None)
_ANNOTATION_TAG_VALUE = Query(default=None)
_RUN_ID = Query(default=None)


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-terms")
async def get_field_terms(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'artifact' or 'attr:status_code'"),
    limit: int = Query(default=50, ge=1, le=500),
    q: str | None = _Q,
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
    annotated: str | None = _ANNOTATED,
    annotation_tag_value: str | None = _ANNOTATION_TAG_VALUE,
    run_id: str | None = _RUN_ID,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a top-N terms aggregation (value → count) for *field*.

    Powers the per-value histogram modal's top-list and nominal/ordinal
    chart types (bar, pie, treemap) on the Visualization page.
    """
    query = await _resolve_event_query(
        case_id,
        timeline_id,
        q=q,
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
    )
    service = _get_query_service()
    return await run_in_threadpool(service.field_terms, query, field, limit)


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-numeric")
async def get_field_numeric_stats(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:bytes_sent'"),
    bins: int = Query(default=30, ge=1, le=200),
    q: str | None = _Q,
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
    )
    service = _get_query_service()
    return await run_in_threadpool(service.field_numeric_stats, query, field, bins)


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-timeseries")
async def get_field_value_timeseries(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:status_code'"),
    buckets: int = Query(default=60, ge=10, le=200),
    series_limit: int = Query(default=12, ge=1, le=50),
    q: str | None = _Q,
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
    )
    service = _get_query_service()
    return await run_in_threadpool(
        service.field_value_timeseries, query, field, buckets, series_limit
    )


class CompareFilters(BaseModel):
    """One comparison layer's filter set — field-for-field the same names and
    string encodings as the shared viz/events filter *query params*, so the
    frontend's ``serializeEventFilterParams`` output maps 1:1 into a body
    object and resolution reuses ``_resolve_event_query`` unchanged.
    """

    q: str | None = None
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

    if body.comparison.mode == "baseline":
        # All events of the timeline: filters dropped, timeline scope and
        # explicit time window kept — "the whole" the primary is a part of.
        comparison = replace(
            primary,
            q=None,
            artifact=None,
            artifacts=None,
            source_id=None,
            tag=None,
            exclude_tag=None,
            field_filters={},
            field_exclusions={},
            event_ids=None,
            exclude_event_ids=None,
            tags_include=None,
            tags_exclude=None,
        )
    else:
        if body.comparison.filters is None:
            raise HTTPException(status_code=422, detail="mode='custom' requires 'filters'")
        comparison = await _resolve_body_query(case_id, timeline_id, body.comparison.filters)
        # Comparability invariant: both layers share the primary's explicit
        # time window (the union of data ranges handles the implicit case).
        comparison = replace(comparison, start=primary.start, end=primary.end)

    service = _get_query_service()
    if body.kind == "time":
        return await run_in_threadpool(
            service.compare_time_histogram, primary, comparison, body.buckets
        )
    if body.kind == "terms":
        return await run_in_threadpool(
            service.compare_field_terms, primary, comparison, body.field, body.limit
        )
    return await run_in_threadpool(
        service.compare_field_numeric, primary, comparison, body.field, body.bins
    )


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

    Sorted by coverage descending, then token — the first entry is the
    frontend's default field pick.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    svc = _get_stat_anomaly_service()
    inventory, total = await run_in_threadpool(svc.field_inventory, case_id, source_ids)
    if total == 0:
        return {"fields": []}
    fields = [
        {"token": token, "distinct": distinct, "coverage": round(cov_count / total, 4)}
        for token, distinct, cov_count in inventory
    ]
    fields.sort(key=lambda f: (-f["coverage"], f["token"]))
    return {"fields": fields}
