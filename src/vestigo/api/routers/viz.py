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
from pydantic import BaseModel, Field, ValidationError

from vestigo.api.deps import (
    get_current_user,
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
    _resolve_routine_collapse,
    _resolve_timeline_scope,
    _resolve_timeline_source_ids,
    _run_regex_guarded,
    _uses_regex,
    _validate_field_modes,
    _validate_regex,
)
from vestigo.core.config import get_settings
from vestigo.db._time_fields import TIME_FIELD_SPECS, resolve_time_field
from vestigo.db.derive import DeriveSpec, parse_derive
from vestigo.db.field_stats import (
    ensure_source_field_stats,
    merged_field_terms,
    merged_inventory,
)
from vestigo.db.postgres import Case, User, generate_id
from vestigo.db.queries import EventQuery, EventQueryService

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
    collapse_routine: bool,
) -> EventQuery:
    """Resolve the shared filter query params into an :class:`EventQuery`.

    Mirrors ``events.py::get_histogram``'s param-resolution sequence
    (timeline → source_ids, then the annotated/tags_include/tags_exclude/ids
    combo via ``_resolve_event_id_filters``, then the routine-collapse scope)
    so every viz endpoint below builds an identical ``EventQuery`` from
    identical inputs — one place to keep in sync with
    ``list_events``/``get_histogram`` instead of three.
    """
    _validate_regex(q, q_regex)
    parsed_filters = _parse_multivalue_object(filters)
    parsed_exclusions = _parse_multivalue_object(exclusions)
    parsed_filter_modes = _parse_modes_object(filter_modes)
    parsed_exclusion_modes = _parse_modes_object(exclusion_modes)
    _validate_field_modes(parsed_filters, parsed_filter_modes)
    _validate_field_modes(parsed_exclusions, parsed_exclusion_modes)
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
    # A chart must aggregate exactly the set the grid displays (#147): the
    # frontend has always sent `collapse_routine` here, and FastAPI silently
    # dropped it — charts summed the uncollapsed superset while the grid
    # collapsed. Same silent-drop shape as bulk-annotate's pydantic
    # `extra="ignore"` bug; see the invariant note in ANOMALY_DETECTION.md.
    routine_scope = await _resolve_routine_collapse(
        case_id, timeline_id, source_ids, collapse_routine
    )
    return EventQuery(
        case_id=case_id,
        source_ids=source_ids,
        exclude_routine_disposition_ids=routine_scope.motif_disposition_ids,
        exclude_template_hashes=routine_scope.template_hashes,
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
_COLLAPSE_ROUTINE = Query(
    default=False,
    description=(
        "Exclude events covered by an active routine disposition (muted "
        "templates, motifs marked routine), matching what the grid shows. "
        "Must mirror the flag the caller's event list was rendered with."
    ),
)


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
        # An active routine collapse is a filter: the per-source field_stats
        # cache aggregates the *uncollapsed* timeline, so serving it here
        # would resurrect the muted events in the chart (#147).
        and query.exclude_routine_disposition_ids is None
        and query.exclude_template_hashes is None
    )


def _derive_query() -> Any:
    """A fresh ``Query`` per endpoint — never a shared module-level one.

    FastAPI stamps the parameter name onto the ``Query`` object's ``alias`` the
    first time it is bound, so one object shared between ``derive`` and
    ``derive_x`` would make the pivot endpoint read the query key ``derive``
    while advertising ``derive_x`` (found during verification of #viz-step-2).
    The shared filter ``Query`` objects above are safe only because every
    endpoint binds them under the same name.
    """
    return Query(
        default=None,
        description=(
            "Optional derivation of the charted field, as a JSON object: "
            '{"kind":"bins","mode":"width"|"log","count":N} | '
            '{"kind":"bins","mode":"custom","edges":[…]} | '
            '{"kind":"time_part","part":"hour"|"weekday"|"day"|"week"|"month"}. '
            "A change of scale — the result is ordered categories."
        ),
    )


def _parse_derive_param(raw: str | None, field: str | None) -> DeriveSpec | None:
    """422 with the validator's own words on a bad derivation.

    A virtual ``time:`` field is already a calendar part; deriving it again is
    refused here with the same sentence the agent's ``propose_chart`` uses,
    rather than surfacing as a 500 from the query service.
    """
    if not isinstance(raw, str):
        # Absent — or, when a handler is called directly outside HTTP, the
        # unresolved ``Query`` default object itself.
        return None
    try:
        spec = parse_derive(raw)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"derive: {exc}") from exc
    if spec is not None and field and resolve_time_field(field) is not None:
        raise HTTPException(
            status_code=422,
            detail=f"derive: {field} is already a calendar part — chart it directly, without derive.",
        )
    return spec


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-terms")
async def get_field_terms(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'artifact' or 'attr:status_code'"),
    limit: int = Query(default=50, ge=1, le=500),
    totals: bool = Query(
        default=True,
        description=(
            "Also scan for the field's whole distribution, so total/distinct/other_count "
            "cover the tail beyond the top-N. Pass false from a caller that reads only the "
            "values (e.g. autocomplete) to halve the scan."
        ),
    ),
    derive: str | None = _derive_query(),  # noqa: B008
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a top-N terms aggregation (value → count) for *field*.

    Powers the per-value histogram modal's top-list and nominal/ordinal
    chart types (bar, pie) on the Visualization page.

    ``derive`` (:mod:`vestigo.db.derive`) groups the field's bins or calendar
    part instead of its raw values; such a request is never answered from the
    field-stats cache, which holds raw values only.
    """
    derive_spec = _parse_derive_param(derive, field)
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
        collapse_routine=collapse_routine,
    )
    # M24a: an unfiltered first load is answerable from the per-source
    # field_stats cache — no ClickHouse scan, no HEAVY_SCAN_GATE slot. A
    # canonical mapped field must stay live (coalesce over several raw keys
    # dedupes per event; not derivable from per-key caches), and any filter
    # or a cache gap falls through to the live path below.
    if (
        derive_spec is None
        and _is_unfiltered(query)
        and not (query.field_mappings and field in query.field_mappings)
    ):
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
        totals=totals,
        derive=derive_spec,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-numeric")
async def get_field_numeric_stats(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:bytes_sent'"),
    bins: int | None = Query(default=None, ge=1, le=200),
    points: bool = Query(default=False, description="Include a reproducible raw-value sample"),
    points_limit: int = Query(default=1000, ge=10, le=1000),
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return summary statistics and a fixed-width histogram for a numeric field.

    ``count == 0`` in the response means the field has no numeric values in
    the current filter set — the Visualization page falls back to treating
    it as categorical. Powers histogram/box/violin/ECDF chart types.

    ``bins`` omitted selects the Freedman–Diaconis automatic bin count; the
    response's ``bin_rule`` records which path was taken.
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_numeric_stats,
        query,
        field,
        bins,
        points,
        points_limit,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-correlation")
async def get_field_correlation(
    case_id: str,
    timeline_id: str,
    fields: list[str] = Query(  # noqa: B008
        ..., description="2–8 numeric field tokens; repeat the param per field"
    ),
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Pairwise Pearson/Spearman correlations across several numeric fields.

    Powers the correlation-matrix chart. Each pair reports the number of
    events where **both** fields are numeric (pairwise-complete), so a field
    with sparse coverage cannot silently shrink the other pairs. 422 on
    fewer than two or more than eight fields, or on duplicates.
    """
    unique = list(dict.fromkeys(fields))
    if len(unique) != len(fields):
        raise HTTPException(status_code=422, detail="fields must be distinct")
    if not 2 <= len(unique) <= EventQueryService.CORRELATION_MAX_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=(
                "a correlation matrix needs between 2 and "
                f"{EventQueryService.CORRELATION_MAX_FIELDS} fields"
            ),
        )
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_correlation,
        query,
        unique,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-numeric-grouped")
async def get_field_numeric_grouped(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Numeric field token, e.g. 'attr:bytes_sent'"),
    group_field: str = Query(..., description="Categorical grouping field token"),
    groups: int = Query(default=8, ge=2, le=8),
    bins: int = Query(default=30, ge=1, le=200),
    points: bool = Query(default=False, description="Include a reproducible raw-value sample"),
    points_limit: int = Query(default=1000, ge=10, le=1000),
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Per-group numeric distributions — grouped box/violin plots.

    One numeric response field split by a categorical grouping field: top-N
    groups by numeric-value count, per-group quantiles + fixed-width bins
    over the global value range, honest omission counts, and an optional
    uniform point sample (stable across reruns). 422 when the two fields
    are the same.
    """
    if field == group_field:
        raise HTTPException(
            status_code=422, detail="field and group_field must differ for a grouped chart"
        )
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_numeric_grouped,
        query,
        field,
        group_field,
        groups,
        bins,
        points,
        points_limit,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-timeseries")
async def get_field_value_timeseries(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:status_code'"),
    buckets: int = Query(default=60, ge=10, le=200),
    series_limit: int = Query(default=12, ge=1, le=50),
    derive: str | None = _derive_query(),  # noqa: B008
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return per-value event counts bucketed over time for *field*.

    Restricted to the top ``series_limit`` values by overall count (see
    ``EventQueryService.field_value_timeseries``). Powers the multi-series
    line/area chart and the value×time heatmap on the Visualization page.
    """
    derive_spec = _parse_derive_param(derive, field)
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_value_timeseries,
        query,
        field,
        buckets,
        series_limit,
        derive=derive_spec,
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.time_punchcard,
        query,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/cumulative")
async def get_cumulative(
    case_id: str,
    timeline_id: str,
    field: str | None = Query(default=None, description="Optional field token."),
    quantity: Literal["events", "sum", "distinct"] | None = Query(
        default=None,
        description=(
            'What accumulates. Omitted: "events" without a field, "distinct" with one; '
            '"sum" must be asked for.'
        ),
    ),
    buckets: int = Query(default=60, ge=4, le=200),
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a running total over time (the cumulative step figure).

    See ``EventQueryService.cumulative``. The endpoint knows no scale, so a
    ``sum`` is never assumed — the page asks for it when the field is
    treated as a measure.
    """
    resolved_quantity = quantity or ("distinct" if field else "events")
    if resolved_quantity != "events" and not field:
        raise HTTPException(status_code=422, detail=f'quantity="{resolved_quantity}" needs field')
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.cumulative,
        query,
        field=field,
        quantity=resolved_quantity,
        buckets=buckets,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/calendar")
async def get_calendar(
    case_id: str,
    timeline_id: str,
    field: str | None = Query(
        default=None, description="Optional: count only events whose field is non-empty."
    ),
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return event counts per UTC day, latest 53 weeks (the calendar heatmap).

    See ``EventQueryService.calendar``; the week cap is
    ``ANALYST_CHART_LIMITS.calendar_weeks`` so the page and a Story export
    draw the same weeks.
    """
    from vestigo.agent.chart_exec import ANALYST_CHART_LIMITS

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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.calendar,
        query,
        field=field,
        max_weeks=ANALYST_CHART_LIMITS.calendar_weeks,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-pivot")
async def get_field_pivot(
    case_id: str,
    timeline_id: str,
    field_x: str = Query(..., description="X-axis field token, e.g. 'attr:username'"),
    field_y: str = Query(..., description="Y-axis field token, e.g. 'attr:workstation'"),
    limit_x: int = Query(default=10, ge=1, le=50),
    limit_y: int = Query(default=10, ge=1, le=50),
    derive_x: str | None = _derive_query(),  # noqa: B008
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a top-X × top-Y co-occurrence matrix for two categorical fields.

    ``""`` on either axis of a cell means "outside that axis's top-N"
    (truthful Other rollup). Powers the field×field heatmap and the flow
    (Sankey) chart on the Visualization page.

    ``derive_x`` derives the x axis (:mod:`vestigo.db.derive`); a derived
    axis is a bounded domain — every range or part, in order, empty or not.
    """
    if field_x == field_y:
        raise HTTPException(status_code=422, detail="field_x and field_y must differ")
    derive_x_spec = _parse_derive_param(derive_x, field_x)
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
        collapse_routine=collapse_routine,
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
        derive_x=derive_x_spec,
    )


@router.get("/{case_id}/timelines/{timeline_id}/viz/field-table")
async def get_field_table(
    case_id: str,
    timeline_id: str,
    field: str = Query(..., description="Field token, e.g. 'attr:username'"),
    second_field: str | None = Query(
        default=None,
        description="Optional second field: each row also counts its distinct values (uniqExact).",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    sort_by: Literal[
        "value", "count", "share", "first_seen", "last_seen", "distinct_second"
    ] = Query(
        default="count",
        description="Row order. 'share' orders like 'count'; 'distinct_second' needs second_field.",
    ),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    derive: str | None = _derive_query(),  # noqa: B008
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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return the top-N values of *field* as table rows (the table figure).

    Count, share of the filtered slice, first/last seen and — with
    ``second_field`` — the distinct count of that field per row; a
    ``remainder`` row whenever values were cut. See
    ``EventQueryService.field_table``.
    """
    if second_field is not None and second_field == field:
        raise HTTPException(status_code=422, detail="second_field must differ from field")
    if sort_by == "distinct_second" and not second_field:
        raise HTTPException(status_code=422, detail="sort_by='distinct_second' needs second_field")
    derive_spec = _parse_derive_param(derive, field)
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
        collapse_routine=collapse_routine,
    )
    service = _get_query_service()
    return await _run_regex_guarded(
        _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
        service.field_table,
        query,
        field,
        limit,
        second_field=second_field,
        sort_by=sort_by,
        sort_dir=sort_dir,
        derive=derive_spec,
    )


class MarksRequest(BaseModel):
    """Body for ``POST …/viz/marks``: the chart config's ``marks`` list, verbatim.

    The stored ``MarkSource`` shape (camelCase, ``filters`` as a view payload)
    — the same bytes the frontend keeps in ``c_marks`` and a saved chart, so
    the page posts what it holds and nothing is re-encoded on the way.
    """

    marks: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


@router.post("/{case_id}/timelines/{timeline_id}/viz/marks")
async def resolve_viz_marks(
    case_id: str,
    timeline_id: str,
    body: MarksRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Resolve mark sources into instants and ranges with provenance.

    See ``agent/marks.py``. Reads only; the per-source cap is the
    ``viz_marks_max`` setting and is echoed as ``cap``.
    """
    from vestigo.agent.marks import resolve_marks
    from vestigo.agent.tools import AgentScope, ChartMarkSpec, FilterSpec
    from vestigo.stories.export import _stored_marks_to_spec

    try:
        specs = [ChartMarkSpec.model_validate(m) for m in (_stored_marks_to_spec(body.marks) or [])]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    scope = AgentScope(
        case_id=case_id,
        timeline_id=timeline_id,
        user=user,
        source_ids=source_ids,
        field_mappings=field_mappings,
        source_offsets=source_offsets,
    )

    # An events mark is a foreground scan like every GET above: the same
    # regex / match-mode pre-checks (a bad pattern is a 400, not a ClickHouse
    # 500) and the same runner (a full lane answers 503, which is what the
    # page's busy-retry waits for; a client that left cancels the scan).
    def validated(fspec: FilterSpec | None) -> FilterSpec:
        fspec = fspec or FilterSpec()
        _validate_regex(fspec.q, fspec.q_regex)
        _validate_field_modes(fspec.filters, fspec.filter_modes)
        _validate_field_modes(fspec.exclusions, fspec.exclusion_modes)
        return fspec

    # Per mark, off the query that is about to run: an events mark can carry a
    # regex in `filter_modes` rather than in `q_regex`, and a `view` mark's
    # pattern lives in the saved view and is not known until `resolve_marks`
    # has loaded it. Deciding once, from `q_regex` alone, left both of those
    # unguarded — an RE2-only rejection surfaced as a 500 where every other viz
    # endpoint answers 400.
    async def run_mark_scan(fn: Any, query: Any, /, *args: Any, **kwargs: Any) -> Any:
        return await _run_regex_guarded(
            _uses_regex(query.q_regex, query.filter_modes, query.exclusion_modes),
            fn,
            query,
            *args,
            **kwargs,
        )

    try:
        return await resolve_marks(
            scope,
            specs,
            service=_get_query_service(),
            store=get_store(),
            cap=get_settings().viz_marks_max,
            run=run_mark_scan,
            validated=validated,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    collapse_routine: bool = _COLLAPSE_ROUTINE,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a uniform, reproducible sample of (x, y) numeric pairs for a scatter plot.

    ``total`` is the full pair count and the per-axis min/max describe the
    full data (not the sample), so the frontend caption can state "showing
    N of M points (uniform sample)" truthfully. The sample is drawn in a
    stable hash order, so re-running identical filters redraws identical
    points. ``total == 0`` means
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
        collapse_routine=collapse_routine,
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
    collapse_routine: bool = False


class ComparisonSpec(BaseModel):
    """The comparison layer: all timeline events (baseline) or a second filter set."""

    mode: Literal["baseline", "custom"]
    filters: CompareFilters | None = None


class CompareRequest(BaseModel):
    """Body for ``POST .../viz/compare`` — two filter sets don't fit query params."""

    kind: Literal["time", "terms", "numeric", "change"]
    field: str | None = None
    primary: CompareFilters = Field(default_factory=CompareFilters)
    comparison: ComparisonSpec
    buckets: int = Field(default=60, ge=10, le=200)
    bins: int = Field(default=30, ge=1, le=200)
    limit: int = Field(default=50, ge=1, le=500)
    #: ``kind="terms"`` and ``kind="change"``: derive ``field`` (bins / calendar
    #: part) before counting — both layers on the primary's edges.
    derive: DeriveSpec | None = None


class LanesRequest(BaseModel):
    """Body for ``POST …/viz/lanes`` — three filter sets don't fit query params.

    ``primary`` is the current filters; ``start_filter``/``end_filter`` are
    the events that open and close an interval under ``pairing="next_end"``
    — each ANDed with the primary and pinned to its time range, so an
    analyst filtering to one host gets that host's starts.
    """

    field: str
    pairing: Literal["first_last", "next_end"] = "first_last"
    primary: CompareFilters = Field(default_factory=CompareFilters)
    start_filter: CompareFilters | None = None
    end_filter: CompareFilters | None = None
    limit_y: int = Field(default=10, ge=1, le=500)


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
        collapse_routine=body.collapse_routine,
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
    ground truth. ``kind="change"`` is the ranked change figure — the union of
    both windows' top-N values with each value's share of its own window
    (``EventQueryService.field_change``); it rides on this endpoint because it
    needs exactly the two-layer resolution done here.
    """
    if body.kind in ("terms", "numeric", "change") and not body.field:
        raise HTTPException(status_code=422, detail=f"kind={body.kind!r} requires 'field'")
    if body.derive is not None:
        if body.kind not in ("terms", "change"):
            raise HTTPException(
                status_code=422, detail="derive applies to kind='terms' and kind='change' only"
            )
        if body.field and resolve_time_field(body.field) is not None:
            raise HTTPException(
                status_code=422,
                detail=f"derive: {body.field} is already a calendar part — chart it directly, without derive.",
            )

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
            # The baseline layer is "the whole the primary is a part of" —
            # deliberately uncollapsed like every other dropped filter, so
            # the superset invariant the M24c cache rests on keeps holding.
            collapse_routine=False,
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
                # A derived request never reads an underived layer (the
                # service keys on the resolved expression too — belt and braces).
                body.derive.model_dump_json() if body.derive is not None else None,
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
            derive=body.derive,
        )
    if body.kind == "change":
        from vestigo.agent.chart_exec import ANALYST_CHART_LIMITS

        return await _run_regex_guarded(
            q_regex,
            service.field_change,
            primary,
            comparison,
            body.field,
            min(body.limit, ANALYST_CHART_LIMITS.change_top_n[1]),
            union_cap=ANALYST_CHART_LIMITS.change_union,
            derive=body.derive,
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


@router.post("/{case_id}/timelines/{timeline_id}/viz/lanes")
async def get_lanes(
    case_id: str,
    timeline_id: str,
    body: LanesRequest,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return interval lanes — one lane per value of ``field``, bars from start to end.

    See ``EventQueryService.field_lanes``. Reads only; the lane cap is clamped
    to the analyst ceiling and the row cap is the analyst's, both echoed.
    """
    from vestigo.agent.chart_exec import ANALYST_CHART_LIMITS

    if body.pairing == "next_end" and (body.start_filter is None or body.end_filter is None):
        raise HTTPException(
            status_code=422, detail="pairing='next_end' requires 'start_filter' and 'end_filter'"
        )
    primary = await _resolve_body_query(case_id, timeline_id, body.primary)
    start = end = None
    regex_flags = [primary.q_regex]
    modes = [primary.filter_modes, primary.exclusion_modes]
    if body.pairing == "next_end" and body.start_filter is not None and body.end_filter is not None:
        start = await _resolve_body_query(case_id, timeline_id, body.start_filter)
        end = await _resolve_body_query(case_id, timeline_id, body.end_filter)
        # Pinned to the primary's window, as a custom Compare layer is.
        start = replace(start, start=primary.start, end=primary.end)
        end = replace(end, start=primary.start, end=primary.end)
        regex_flags += [start.q_regex, end.q_regex]
        modes += [start.filter_modes, start.exclusion_modes, end.filter_modes, end.exclusion_modes]
    service = _get_query_service()
    q_regex = _uses_regex(any(regex_flags), *modes)
    return await _run_regex_guarded(
        q_regex,
        service.field_lanes,
        primary,
        body.field,
        pairing=body.pairing,
        start=start,
        end=end,
        limit_y=min(body.limit_y, ANALYST_CHART_LIMITS.lanes[1]),
        rows_cap=ANALYST_CHART_LIMITS.lanes_rows,
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
    - ``distinct`` — number of distinct non-empty values; ``null`` for a
      virtual field with an unbounded domain (a date, a year-month).
    - ``coverage`` — fraction of events with a non-empty value (0-1);
      ``null`` for a virtual field, which is not measured against the data.
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

    Their ``distinct``/``coverage`` are ``null`` rather than measured, and
    deliberately not faked as ``1.0``: a time part is undefined for an undated
    (sentinel-timestamp) event, so full coverage would be a claim about the
    data that this endpoint never checked. A picker showing "—" is honest; a
    fabricated number in a forensic tool is not.
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
            "distinct": len(spec.domain) if spec.domain else None,
            "coverage": None,
            "label": spec.label,
        }
        for token, spec in TIME_FIELD_SPECS.items()
    )
    return {"fields": fields}
