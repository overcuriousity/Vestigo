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

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from tracevector.api.deps import require_case_read
from tracevector.api.routers.events import (
    _get_query_service,
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
