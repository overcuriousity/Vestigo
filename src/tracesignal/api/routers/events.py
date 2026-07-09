"""API routes for querying events."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import Generator
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, Literal

from clickhouse_connect.driver.exceptions import DatabaseError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from tracesignal.api.deps import (
    get_current_user,
    get_store,
    require_case_contribute,
    require_case_read,
    require_password_current,
)
from tracesignal.core.config import get_settings
from tracesignal.core.events_bus import publish_annotation_change
from tracesignal.db._dt import ensure_utc
from tracesignal.db.anomaly_stats import (
    AnalysisWindows,
    CharsetFinding,
    ComboFinding,
    EntropyFinding,
    FreqFinding,
    NoveltyFieldInfo,
    OrderFinding,
    RangeFinding,
    ShiftFinding,
    StatisticalAnomalyService,
    TimeWindow,
    ValueFinding,
    windows_from_split,
)
from tracesignal.db.field_stats import (
    ensure_source_field_stats,
    merged_inventory,
    merged_list_fields,
)
from tracesignal.db.postgres import (
    BaselineDefinition,
    Case,
    PostgresStore,
    User,
    allowlist_hash,
    generate_id,
)
from tracesignal.db.queries import EventQuery, EventQueryService, TagFilter
from tracesignal.db.similarity import EncoderUnavailableError, SimilarityService
from tracesignal.models.embeddings import embeddings_available

_EMBEDDINGS_UNAVAILABLE_DETAIL = (
    "Embedding support is not installed. Install the 'embeddings' extra "
    "(uv sync --extra embeddings) or configure TS_EMBEDDING_API_BASE_URL "
    "to use a remote embedding endpoint."
)

_query_service: EventQueryService | None = None


def _get_query_service() -> EventQueryService:
    global _query_service  # noqa: PLW0603
    if _query_service is None:
        _query_service = EventQueryService()
    return _query_service


# None = never tried, False = load failed (cached — a broken/missing local
# model must not re-attempt a multi-second load, or worse a network download,
# on every wizard open), EmbeddingModel = loaded.
_embedding_model: Any = None


def _get_field_encoder() -> Any:
    """Return the embedding ``encode`` callable for wizard field pairing.

    Cached across calls — success *and* failure — so the model loads at most
    once per process. Returns ``None`` when the model cannot be loaded (e.g.
    airgapped without cached weights), which degrades the wizard gracefully to
    heuristic-only recommendations.

    Loading a local sentence-transformer takes seconds (or attempts a network
    download when ``TS_ALLOW_ONLINE`` is set and weights are uncached) — this
    must only ever be called from a worker thread, never on the event loop.
    """
    global _embedding_model  # noqa: PLW0603
    if _embedding_model is None:
        try:
            from tracesignal.models.embeddings import EmbeddingModel

            model = EmbeddingModel()
            # encode() lazy-loads locally or routes to the remote endpoint on
            # its own; calling load() unconditionally here would raise in
            # remote mode (load() is local-model-only) and get swallowed by
            # the except below, silently disabling field pairing whenever
            # remote embeddings are configured.
            if not model.is_remote:
                model.load()
            _embedding_model = model
        except Exception:  # noqa: BLE001
            _embedding_model = False
    if _embedding_model is False:
        return None
    return _embedding_model.encode


router = APIRouter(prefix="/api/cases", tags=["events"])


def _validate_regex(q: str | None, q_regex: bool) -> None:
    """Reject an obviously invalid regex search pattern with a 400.

    ``re.compile`` is a cheap pre-check that catches plain syntax errors
    before a ClickHouse round trip. It is not authoritative — ClickHouse
    matches with RE2, which rejects some Python-valid constructs (e.g.
    lookbehind) — so callers running a regex query must also route the scan
    through :func:`_run_regex_guarded`.
    """
    if q_regex and q:
        try:
            re.compile(q)
        except re.error as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid regular expression: {exc}"
            ) from exc


async def _run_regex_guarded(q_regex: bool, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking query in the threadpool, mapping RE2 failures to 400.

    RE2 rejects some patterns Python's ``re`` accepts, so a pattern can pass
    :func:`_validate_regex` and still fail to compile inside ClickHouse —
    without this, that surfaces as a 500 instead of a client error.
    """
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except DatabaseError as exc:
        message = str(exc)
        if q_regex and re.search(r"re2|regex", message, re.IGNORECASE):
            raise HTTPException(
                status_code=400, detail="invalid regular expression (rejected by RE2)"
            ) from exc
        raise


def _parse_json_object(value: str | None) -> dict[str, str]:
    """Parse a JSON string into a string-to-string dict.

    Returns an empty dict for ``None`` or empty input.
    """
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON filter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Filter must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def _parse_cursor(value: str | None, *, param_name: str) -> tuple[datetime, str] | None:
    """Parse a `"<iso-ts>,<event_id>"` keyset cursor query param.

    Split from the right since ISO timestamps never contain a comma and
    event_id (UUID) doesn't either, so a single rsplit is unambiguous. An
    empty `event_id` is valid — it's a synthetic lower bound meaning "before
    every event at exactly this timestamp" (empty string sorts before any
    real event_id string), used when the caller only knows a target time and
    not a specific anchor event (e.g. a Frequency finding's window start).
    """
    if not value:
        return None
    ts_str, sep, event_id = value.rpartition(",")
    if not sep or not ts_str:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name} cursor — expected '<iso-timestamp>,<event_id>'",
        )
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {param_name} cursor timestamp: {exc}"
        ) from exc
    return ts, event_id


def _parse_multivalue_object(value: str | None) -> dict[str, list[str]]:
    """Parse a JSON string into a string-to-list[str] dict for field filters/exclusions.

    Accepts both ``{"key": "value"}`` (legacy single-value — pre-multivalue
    URLs and saved views) and ``{"key": ["v1", "v2"]}`` (multi-value
    distillation). Returns an empty dict for ``None`` or empty input.
    """
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON filter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Filter must be a JSON object")
    result: dict[str, list[str]] = {}
    for k, v in parsed.items():
        if isinstance(v, list):
            result[str(k)] = [str(item) for item in v]
        else:
            result[str(k)] = [str(v)]
    return result


_VALID_FILTER_MODES = {"exact", "wildcard", "regex"}


def _parse_modes_object(value: str | None) -> dict[str, str]:
    """Parse a ``{"field": "exact"|"wildcard"|"regex"}`` match-mode map.

    Absent keys mean exact everywhere downstream; explicit ``"exact"``
    entries are accepted and harmless. Unknown mode strings are a client
    error — silently coercing them to exact would make a filter match
    something other than what the analyst asked for.
    """
    parsed = _parse_json_object(value)
    for k, v in parsed.items():
        if v not in _VALID_FILTER_MODES:
            raise HTTPException(status_code=400, detail=f"invalid match mode {v!r} for field {k!r}")
    return parsed


def _validate_field_regexes(
    field_map: dict[str, str] | dict[str, list[str]], modes: dict[str, str]
) -> None:
    """Pre-check every regex-mode field pattern with ``re.compile`` → 400.

    Same non-authoritative cheap check as :func:`_validate_regex` — RE2
    inside ClickHouse is the final arbiter, guarded by
    :func:`_run_regex_guarded`.
    """
    for key, mode in modes.items():
        if mode != "regex":
            continue
        raw = field_map.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        for pattern in values:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid regular expression for field {key!r}: {exc}",
                ) from exc


def _uses_regex(q_regex: bool, *mode_maps: dict[str, str]) -> bool:
    """Whether any part of the query runs an RE2 pattern in ClickHouse."""
    return q_regex or any("regex" in m.values() for m in mode_maps)


async def _resolve_timeline_scope(
    case_id: str, timeline_id: str
) -> tuple[list[str], dict[str, list[str]] | None]:
    """Return a timeline's *ready* source IDs plus its field mappings (issue #10).

    Every endpoint whose parameters carry field tokens (filters, group-bys,
    detector fields, exports) must resolve through this so canonical mapped
    fields work uniformly; endpoints that never see a field token can keep
    using :func:`_resolve_timeline_source_ids`.

    Sources still being ingested (``status != "ready"``) are excluded here —
    this is the single choke point that keeps half-ingested files out of the
    explorer, histogram, export, detectors, and the field/embedding wizards,
    where partial data would silently produce wrong counts and wrong
    statistical baselines. The sources API still lists them (with their
    status) so the UI can show ingest progress.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case_id, timeline_id)
    return [s.id for s in sources if s.is_ready], timeline.field_mappings or None


async def _resolve_timeline_source_ids(case_id: str, timeline_id: str) -> list[str]:
    """Return the source IDs attached to a timeline."""
    source_ids, _ = await _resolve_timeline_scope(case_id, timeline_id)
    return source_ids


_FIELDS_NONE_TOKEN = "__none__"


def _parse_novelty_fields(fields: str | None) -> list[str] | None:
    """Parse the ``fields`` param for value_novelty scans.

    ``None`` (param omitted) → ``None``, meaning the backend auto-selects
    fields. The reserved ``"__none__"`` token means the analyst explicitly
    deselected every field in the picker, so no fields should be scanned —
    it maps to ``[]`` rather than falling back to auto-selection.
    """
    if fields is None:
        return None
    if fields == _FIELDS_NONE_TOKEN:
        return []
    return [f.strip() for f in fields.split(",") if f.strip()]


async def _resolve_tags_filter(
    case_id: str, source_ids: list[str], tag_values: list[str] | None
) -> TagFilter | None:
    """Resolve a set of unified tag values to a :class:`TagFilter` (OR across values).

    Merges two independent tagging systems that share a UI: user annotation
    tags (Postgres) and parser-derived ``Event.tags`` (ClickHouse) — an event
    matches if *either* system has *any* of these exact values, so the analyst
    doesn't need to know or care which system a given tag value came from.
    Shared by both the include and exclude resolvers below.

    Only the Postgres half is resolved here (it can't be expressed inside a
    ClickHouse WHERE clause); the parser-tag half is matched natively via
    ``hasAny(tags, ...)`` inside the ClickHouse query itself
    (:meth:`~tracesignal.db.queries.EventQueryService._build_where`), so this
    no longer does a second ClickHouse round trip just to union event_ids in
    Python.
    """
    if not tag_values:
        return None
    store = get_store()
    ann_ids = await store.list_event_ids_by_annotation_type(
        case_id, source_ids, "tag", origin="user", content_in=tag_values
    )
    return TagFilter(tag_values=tag_values, postgres_event_ids=ann_ids)


def _intersect_optional(*id_lists: list[str] | None) -> list[str] | None:
    """Intersect any number of optional event_id restriction lists.

    ``None`` means "no restriction" and is ignored; if every list is ``None``
    the result is ``None`` (no restriction). Otherwise returns the
    intersection of all non-``None`` lists — each represents an independent
    filter that must all be satisfied simultaneously.
    """
    present = [set(x) for x in id_lists if x is not None]
    if not present:
        return None
    result = present[0]
    for s in present[1:]:
        result &= s
    return list(result)


def _parse_str_list(value: str | None) -> list[str] | None:
    """Parse a comma-separated list query param (e.g. ``artifacts``, ``ids``)."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


async def _resolve_run_event_ids(case_id: str, run_id: str | None) -> list[str]:
    """Resolve a persisted detector ``run_id`` to its finding event_ids.

    Replaces the old ``live_event_ids`` approach (a comma-separated ID list
    re-uploaded on every request) — the client now references a persisted
    :class:`~tracesignal.db.postgres.DetectorRun` by a single short ID
    instead. 404s on an unknown/foreign-case run_id rather than silently
    matching nothing, since a stale run_id is a client bug worth surfacing.
    """
    if not run_id:
        return []
    store = get_store()
    run = await store.get_detector_run(case_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown detector run: {run_id}")
    return [r["event_id"] for r in run.result.get("results", []) if r.get("event_id")]


async def _resolve_annotated_event_ids(
    case_id: str,
    source_ids: list[str],
    annotated: str | None,
    tag_value: str | None,
    run_id: str | None = None,
) -> list[str] | None:
    """Resolve the ``annotated``/``annotation_tag_value`` filter to an event_id list.

    ``annotated`` is a comma-separated subset of ``{"tag", "anomaly"}``. Matching
    event_ids across the requested types are unioned (OR semantics). Returns
    ``None`` when no filter is requested (no restriction).

    ``run_id`` references a persisted detector run (see
    ``_resolve_run_event_ids``). Live findings from that run don't otherwise
    reach the database as annotations, so the "anomaly" branch can't see them
    from annotations alone — when "anomaly" is requested, the run's finding
    event_ids are unioned in too, so the filter matches detected-but-
    unconfirmed findings as well as tagged/persisted ones.
    """
    if not annotated:
        return None
    store = get_store()
    types = {t.strip() for t in annotated.split(",") if t.strip()}
    event_ids: set[str] = set()
    if "tag" in types:
        ids = await store.list_event_ids_by_annotation_type(
            case_id, source_ids, "tag", origin="user", content=tag_value or None
        )
        event_ids.update(ids)
    if "anomaly" in types:
        ids = await store.list_event_ids_by_annotation_type(
            case_id, source_ids, "anomaly", origin="system"
        )
        event_ids.update(ids)
        event_ids.update(await _resolve_run_event_ids(case_id, run_id))
    return list(event_ids)


async def _resolve_event_id_filters(
    case_id: str,
    source_ids: list[str],
    *,
    annotated: str | None,
    annotation_tag_value: str | None,
    run_id: str | None,
    tags_include: str | None,
    tags_exclude: str | None,
    ids: str | None,
) -> tuple[list[str] | None, TagFilter | None, TagFilter | None]:
    """Resolve the annotated/tags_include/tags_exclude/ids filter combo shared
    by list_events, bulk_annotate_by_filter, get_histogram, and export_events.

    Returns ``(event_ids, tags_include, tags_exclude)`` ready to pass straight
    into :class:`EventQuery`. ``tags_include``/``tags_exclude`` are kept
    separate from ``event_ids`` rather than intersected into it, since they
    carry OR-between-two-systems semantics that ``EventQuery`` applies as its
    own ANDed predicate (see :class:`~tracesignal.db.queries.TagFilter`).
    Each of the four endpoints previously re-implemented this same
    resolve-and-intersect sequence with ~10 identical query params; a filter
    added to one and not the others silently made the grid, histogram,
    bulk-tag, and export disagree on which events match.
    """
    annotated_ids = await _resolve_annotated_event_ids(
        case_id, source_ids, annotated, annotation_tag_value, run_id
    )
    tags_include_filter = await _resolve_tags_filter(
        case_id, source_ids, _parse_str_list(tags_include)
    )
    tags_exclude_filter = await _resolve_tags_filter(
        case_id, source_ids, _parse_str_list(tags_exclude)
    )
    event_ids = _intersect_optional(annotated_ids, _parse_str_list(ids))
    return event_ids, tags_include_filter, tags_exclude_filter


@router.get("/{case_id}/timelines/{timeline_id}/events")
async def list_events(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(
        default=None, description="Free-text search, broadened across all fields"
    ),
    q_regex: bool = Query(
        default=False,
        description=(
            "Treat q as an RE2 regular expression (case-sensitive; "
            "prefix (?i) for case-insensitive)."
        ),
    ),
    artifact: str | None = Query(default=None),
    artifacts: str | None = Query(
        default=None, description="Comma-separated artifact values (OR'd)"
    ),
    source_id: str | None = Query(default=None),
    tag: str | None = Query(
        default=None, description="Deprecated single-value form — prefer tags_include."
    ),
    exclude_tag: str | None = Query(
        default=None, description="Deprecated single-value form — prefer tags_exclude."
    ),
    tags_include: str | None = Query(
        default=None,
        description=(
            "Comma-separated unified tag filter (OR'd) — matches either a user "
            "annotation tag or a parser-derived Event.tags value with this "
            "exact content."
        ),
    ),
    tags_exclude: str | None = Query(
        default=None,
        description="Comma-separated unified tag values to exclude (event dropped if it has any).",
    ),
    ids: str | None = Query(
        default=None,
        description="Comma-separated event_id allowlist (e.g. semantic search results).",
    ),
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    event_id: str | None = Query(
        default=None,
        description="Fetch a single event by its exact event_id, ignoring all other filters.",
    ),
    filters: str | None = Query(
        default=None,
        description='JSON object of field equality filters, e.g. {"ip_address_city":"Falkenstein"}',
    ),
    exclusions: str | None = Query(
        default=None,
        description='JSON object of field exclusion filters, e.g. {"status_code":"200"}',
    ),
    filter_modes: str | None = Query(
        default=None,
        description=(
            "JSON object mapping a `filters` field to its match mode "
            '("exact"|"wildcard"|"regex"), e.g. {"src_ip":"wildcard"}. '
            "Absent fields match exact."
        ),
    ),
    exclusion_modes: str | None = Query(
        default=None,
        description=(
            "JSON object mapping an `exclusions` field to its match mode — "
            "applies to every excluded value under that field."
        ),
    ),
    annotated: str | None = Query(
        default=None,
        description='Comma-separated annotation types to filter to, e.g. "tag,anomaly".',
    ),
    annotation_tag_value: str | None = Query(
        default=None,
        description="Narrow the 'tag' annotation type to a specific tag value.",
    ),
    run_id: str | None = Query(
        default=None,
        description=(
            "ID of a persisted detector run (from GET .../anomalies) — its "
            "finding event IDs are unioned into the 'anomaly' branch of "
            "`annotated` so it also matches not-yet-tagged findings."
        ),
    ),
    after: str | None = Query(
        default=None,
        description=(
            "Keyset cursor '<iso-timestamp>,<event_id>' — fetch the next page "
            "of events further in the requested `order` direction. Mutually "
            "exclusive with `before`."
        ),
    ),
    before: str | None = Query(
        default=None,
        description=(
            "Keyset cursor '<iso-timestamp>,<event_id>' — fetch the page of "
            "events immediately preceding this point (opposite direction of "
            "`after`). Mutually exclusive with `after`."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="desc", description="Sort order: asc or desc"),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List events for a timeline with optional filters."""
    if order not in ("asc", "desc"):
        order = "desc"
    _validate_regex(q, q_regex)
    parsed_filters = _parse_multivalue_object(filters)
    parsed_exclusions = _parse_multivalue_object(exclusions)
    parsed_filter_modes = _parse_modes_object(filter_modes)
    parsed_exclusion_modes = _parse_modes_object(exclusion_modes)
    _validate_field_regexes(parsed_filters, parsed_filter_modes)
    _validate_field_regexes(parsed_exclusions, parsed_exclusion_modes)

    after_cursor = _parse_cursor(after, param_name="after")
    before_cursor = _parse_cursor(before, param_name="before")
    if after_cursor is not None and before_cursor is not None:
        raise HTTPException(status_code=400, detail="Cannot set both 'after' and 'before' cursors")

    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    if event_id:
        # A specific event_id short-circuits the annotated/tags_include/ids
        # resolution entirely — those filters are irrelevant once the caller
        # already knows the exact event, and skipping them avoids wasted
        # annotation/tag lookups on this hot single-event-lookup path.
        event_ids: list[str] | None = [event_id]
        tags_include_filter = None
        tags_exclude_filter = await _resolve_tags_filter(
            case_id, source_ids, _parse_str_list(tags_exclude)
        )
    else:
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

    service = _get_query_service()
    # EventQueryService is synchronous (blocking ClickHouse scans) — run it in
    # the threadpool so a slow scan doesn't stall the event loop for every
    # other request (the ClickHouse client is built for concurrent threadpool
    # use, see ClickHouseStore's autogenerate_session_id note).
    page = await _run_regex_guarded(
        _uses_regex(q_regex, parsed_filter_modes, parsed_exclusion_modes),
        service.query,
        EventQuery(
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
            limit=limit,
            offset=offset,
            order=order,  # type: ignore[arg-type]
            after=after_cursor,
            before=before_cursor,
            field_mappings=field_mappings,
        ),
    )
    return {
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "events": page.events,
        "has_more_after": page.has_more_after,
        "has_more_before": page.has_more_before,
        "next_cursor": page.next_cursor,
        "prev_cursor": page.prev_cursor,
    }


class BulkAnnotateByFilterRequest(BaseModel):
    annotation_type: str = Field(..., description="Annotation type: 'tag', 'comment', or 'normal'.")
    content: str = Field(..., min_length=1, max_length=4096)
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
    filters: str | None = Field(
        default=None,
        description='JSON field-equality filters, e.g. {"ip_address_city":"Berlin"}',
    )
    exclusions: str | None = Field(
        default=None,
        description='JSON field-exclusion filters, e.g. {"status_code":["200"]}',
    )
    filter_modes: str | None = Field(
        default=None,
        description='JSON match-mode map for `filters`, e.g. {"src_ip":"wildcard"}.',
    )
    exclusion_modes: str | None = Field(
        default=None,
        description="JSON match-mode map for `exclusions` (mode applies to all values per field).",
    )
    annotated: str | None = Field(
        default=None,
        description='Comma-separated annotation types to restrict to, e.g. "tag,anomaly".',
    )
    annotation_tag_value: str | None = Field(
        default=None,
        description="Narrow the 'tag' annotation type to a specific tag value.",
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "ID of a persisted detector run (from GET .../anomalies) — its "
            "finding event IDs are unioned into the 'anomaly' branch of "
            "`annotated` so bulk actions can apply to not-yet-tagged findings."
        ),
    )


@router.post("/{case_id}/timelines/{timeline_id}/events/annotations/bulk")
async def bulk_annotate_by_filter(
    case_id: str,
    timeline_id: str,
    body: BulkAnnotateByFilterRequest,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Create an annotation on every event matching the given filter.

    The filter parameters mirror those accepted by ``list_events`` and are
    resolved server-side so that events beyond the first loaded page are
    also tagged.  At most 100 000 events are written per call.
    """
    allowed_types = {"tag", "comment", "normal"}
    if body.annotation_type not in allowed_types:
        raise HTTPException(
            status_code=422,
            detail=f"annotation_type must be one of {sorted(allowed_types)}",
        )
    _validate_regex(body.q, body.q_regex)
    parsed_filters = _parse_multivalue_object(body.filters)
    parsed_exclusions = _parse_multivalue_object(body.exclusions)
    parsed_filter_modes = _parse_modes_object(body.filter_modes)
    parsed_exclusion_modes = _parse_modes_object(body.exclusion_modes)
    _validate_field_regexes(parsed_filters, parsed_filter_modes)
    _validate_field_regexes(parsed_exclusions, parsed_exclusion_modes)

    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    event_ids, tags_include_filter, tags_exclude_filter = await _resolve_event_id_filters(
        case_id,
        source_ids,
        annotated=body.annotated,
        annotation_tag_value=body.annotation_tag_value,
        run_id=body.run_id,
        tags_include=body.tags_include,
        tags_exclude=body.tags_exclude,
        ids=body.ids,
    )

    service = _get_query_service()
    # Blocking ClickHouse scan — threadpool, same as list_events.
    refs = await _run_regex_guarded(
        _uses_regex(body.q_regex, parsed_filter_modes, parsed_exclusion_modes),
        service.query_event_refs,
        EventQuery(
            case_id=case_id,
            source_ids=source_ids,
            q=body.q,
            q_regex=body.q_regex,
            artifact=body.artifact,
            artifacts=_parse_str_list(body.artifacts),
            source_id=body.source_id,
            tag=body.tag,
            exclude_tag=body.exclude_tag,
            start=body.start,
            end=body.end,
            field_filters=parsed_filters,
            field_exclusions=parsed_exclusions,
            filter_modes=parsed_filter_modes,
            exclusion_modes=parsed_exclusion_modes,
            event_ids=event_ids,
            tags_include=tags_include_filter,
            tags_exclude=tags_exclude_filter,
            field_mappings=field_mappings,
        ),
    )

    store = get_store()
    tagged = 0
    if refs:
        rows = [
            {
                "annotation_id": generate_id(f"{event_id}_{body.annotation_type}"),
                "case_id": case_id,
                "source_id": str(src_id),
                "event_id": str(event_id),  # ClickHouse may return UUID objects
                "annotation_type": body.annotation_type,
                "content": body.content.strip(),
                "origin": "user",
                "created_by": user.id,
            }
            for event_id, src_id in refs
        ]
        tagged = await store.bulk_create_annotations(rows)
        if tagged:
            publish_annotation_change(case_id, timeline_id, None, user)
    await store.record_audit(
        action="events.bulk_annotate",
        actor=user,
        case_id=case_id,
        target_type="timeline",
        target_id=timeline_id,
        detail={
            "annotation_type": body.annotation_type,
            "content": body.content.strip(),
            "matched": len(refs),
            "tagged": tagged,
            "filter": body.model_dump(
                exclude={"annotation_type", "content"}, exclude_none=True, exclude_defaults=True
            ),
        },
    )
    return {"tagged": tagged}


@router.get("/{case_id}/timelines/{timeline_id}/fields")
async def list_fields(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return the displayable field names for a timeline.

    ``top_level`` contains the fixed columns common to every event.
    ``attributes`` contains the dynamic keys aggregated from the per-source
    field-stats cache (see ``db/field_stats.py``). ``derived_suffixes`` lists
    the registered enrichers' output-field names — the UI uses it to tell a
    real ``<attr_key>:<output_field>`` enrichment-derived key apart from a
    raw vendor key that happens to contain a colon, instead of guessing from
    the key name alone. Useful for building a column picker in the UI.
    """
    from tracesignal.enrichers.registry import all_enrichers

    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    stats = await ensure_source_field_stats(
        get_store(), _get_query_service().store, case_id, source_ids
    )
    result = merged_list_fields(stats, field_mappings)
    result["derived_suffixes"] = sorted(
        {field for enricher in all_enrichers() for field in enricher.output_fields}
    )
    return result


@router.get("/{case_id}/timelines/{timeline_id}/artifacts")
async def list_artifacts(
    case_id: str, timeline_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """Return distinct ``artifact`` values present in the timeline.

    Powers the artifact filter's autocomplete/multi-select in the UI.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    service = _get_query_service()
    artifacts = await run_in_threadpool(service.list_distinct_artifacts, case_id, source_ids)
    return {"artifacts": artifacts}


@router.get("/{case_id}/timelines/{timeline_id}/tags/merged")
async def list_merged_tags(
    case_id: str, timeline_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """Return the union of distinct user annotation tags and parser-derived tags.

    Powers the unified "Tags" filter panel, which matches a value against
    either tagging system (see ``_resolve_tags_filter``).
    Distinct from ``GET /timelines/{timeline_id}/tags`` (annotation tags
    only), which is what the "add tag" annotation UI uses — you can only
    create annotation tags, not parser tags, so that list must stay pure.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    store = get_store()
    service = _get_query_service()
    ann_tags = await store.list_distinct_tag_contents(case_id, source_ids)
    parser_tags = await run_in_threadpool(service.list_distinct_parser_tags, case_id, source_ids)
    return {"tags": sorted(set(ann_tags) | set(parser_tags))}


@router.get("/{case_id}/timelines/{timeline_id}/embedding-fields")
async def list_embedding_fields(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return per-artifact field information for the embedding wizard.

    For each distinct ``artifact`` across the timeline's sources, returns the
    event count, the embeddable top-level fields, available attribute keys, and
    a recommended preselection.  Used by the frontend embedding wizard to let
    analysts choose which fields of which artifacts to embed.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    service = _get_query_service()
    # _get_field_encoder() may load the local embedding model (seconds, or a
    # network fetch on a misconfigured install) — resolve it inside the worker
    # thread, not on the event loop as an argument expression would.
    return await run_in_threadpool(
        lambda: service.list_fields_by_artifact(case_id, source_ids, encode=_get_field_encoder())
    )


@router.get("/{case_id}/sources/{source_id}/embedding-fields")
async def list_source_embedding_fields(
    case_id: str,
    source_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Per-artifact field recommendations for a single source's embedding wizard.

    Same payload as the timeline-scoped endpoint but scoped to one source, which
    is the unit the embed job operates on.  Runs the hybrid heuristic→pairs
    recommender; field pairing degrades to heuristic-only if the model can't load.
    """
    service = _get_query_service()
    # Same event-loop rule as the timeline-scoped endpoint above: the encoder
    # is resolved inside the worker thread.
    return await run_in_threadpool(
        lambda: service.list_fields_by_artifact(case_id, [source_id], encode=_get_field_encoder())
    )


@router.get("/{case_id}/timelines/{timeline_id}/histogram")
async def get_histogram(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(default=None),
    q_regex: bool = Query(default=False, description="Treat q as an RE2 regular expression."),
    artifact: str | None = Query(default=None),
    artifacts: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    tag: str | None = Query(
        default=None, description="Deprecated single-value form — prefer tags_include."
    ),
    exclude_tag: str | None = Query(
        default=None, description="Deprecated single-value form — prefer tags_exclude."
    ),
    tags_include: str | None = Query(default=None),
    tags_exclude: str | None = Query(default=None),
    ids: str | None = Query(default=None),
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    filters: str | None = Query(default=None),
    exclusions: str | None = Query(default=None),
    filter_modes: str | None = Query(default=None),
    exclusion_modes: str | None = Query(default=None),
    annotated: str | None = Query(default=None),
    annotation_tag_value: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    buckets: int = Query(default=60, ge=10, le=200),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return a bucketed event-count histogram for a timeline.

    Honors the same filter params as the events list endpoint so the histogram
    always reflects the currently-filtered view.  ``buckets`` controls the
    target number of time buckets (10–200, default 60); the actual interval is
    ``max(1, duration / buckets)`` seconds.
    """
    _validate_regex(q, q_regex)
    parsed_filters = _parse_multivalue_object(filters)
    parsed_exclusions = _parse_multivalue_object(exclusions)
    parsed_filter_modes = _parse_modes_object(filter_modes)
    parsed_exclusion_modes = _parse_modes_object(exclusion_modes)
    _validate_field_regexes(parsed_filters, parsed_filter_modes)
    _validate_field_regexes(parsed_exclusions, parsed_exclusion_modes)
    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
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
    service = _get_query_service()
    # Blocking ClickHouse scan — threadpool, same as list_events.
    return await _run_regex_guarded(
        _uses_regex(q_regex, parsed_filter_modes, parsed_exclusion_modes),
        service.histogram,
        EventQuery(
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
        ),
        buckets=buckets,
    )


# ── Export models ─────────────────────────────────────────────────────────────


class ExportFilter(BaseModel):
    """Filter parameters for event export."""

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
    # 'fields' / 'exclude' map to field_filters / field_exclusions in EventQuery.
    # Both accept legacy scalar values ({"k": "v"}) via the validator below.
    fields: dict[str, list[str]] = Field(default_factory=dict)
    exclude: dict[str, list[str]] = Field(default_factory=dict)
    # Match-mode maps for fields/exclude ("exact" when absent) — structured
    # dicts like their siblings, unlike the JSON-string query params.
    field_modes: dict[str, str] = Field(default_factory=dict)
    exclude_modes: dict[str, str] = Field(default_factory=dict)
    annotated: str | None = None
    annotation_tag_value: str | None = None
    run_id: str | None = None

    @field_validator("fields", "exclude", mode="before")
    @classmethod
    def _coerce_scalar_values(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: val if isinstance(val, list) else [val] for k, val in v.items()}
        return v


class ExportRequest(BaseModel):
    """Request body for the export endpoint."""

    format: Literal["csv", "jsonl"]
    filter: ExportFilter = Field(default_factory=ExportFilter)


# ── Export streaming helpers ──────────────────────────────────────────────────

# Core scalar columns included in CSV exports (attributes flattened to JSON).
_CSV_COLUMNS = [
    "event_id",
    "timestamp",
    "timestamp_desc",
    "source_id",
    "artifact",
    "artifact_long",
    "display_name",
    "message",
    "tags",
    "attributes",
    "content_hash",
    "file_hash",
    "user_tags",
    "comments",
    "anomaly_findings",
]


def _index_annotations_by_event(
    annotations: list[Any],
) -> dict[str, list[Any]]:
    """Group annotation ORM rows by event_id for O(1) lookup while streaming."""
    by_event: dict[str, list[Any]] = {}
    for a in annotations:
        by_event.setdefault(a.event_id, []).append(a)
    return by_event


def _stream_jsonl(query: EventQuery, annotations_by_event: dict[str, list[Any]]) -> Generator[str]:
    """Yield one JSONL line per matching event, with its annotations attached."""
    service = EventQueryService()
    for event in service.iter_events(query):
        row = dict(event)
        row["annotations"] = [a.to_dict() for a in annotations_by_event.get(row["event_id"], [])]
        yield json.dumps(row, default=str) + "\n"


def _stream_csv(query: EventQuery, annotations_by_event: dict[str, list[Any]]) -> Generator[str]:
    """Yield CSV rows for all matching events (header first), annotations flattened."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=_CSV_COLUMNS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    # Header row
    writer.writeheader()
    yield buf.getvalue()

    service = EventQueryService()
    for event in service.iter_events(query):
        # Normalise list/dict fields that don't serialize well in CSV.
        row = dict(event)
        tags = row.get("tags")
        if isinstance(tags, list):
            row["tags"] = ";".join(str(t) for t in tags)
        attrs = row.get("attributes")
        if isinstance(attrs, dict):
            row["attributes"] = json.dumps(attrs)

        anns = annotations_by_event.get(row["event_id"], [])
        row["user_tags"] = ";".join(
            a.content for a in anns if a.annotation_type == "tag" and a.origin == "user"
        )
        row["comments"] = " | ".join(
            a.content for a in anns if a.annotation_type == "comment" and a.origin == "user"
        )
        row["anomaly_findings"] = " | ".join(
            a.content for a in anns if a.annotation_type == "anomaly" and a.origin == "system"
        )

        buf.seek(0)
        buf.truncate()
        writer.writerow(row)
        yield buf.getvalue()


# ── Export endpoint ───────────────────────────────────────────────────────────


@router.post("/{case_id}/timelines/{timeline_id}/export")
async def export_events(
    case_id: str,
    timeline_id: str,
    body: ExportRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream all events matching the given filters as CSV or JSONL.

    Each row/line carries its annotations (user tags, comments, and any
    persisted anomaly findings) so the export is a self-contained record —
    tagging a finding is what makes it show up here.
    """
    _validate_regex(body.filter.q, body.filter.q_regex)
    for modes in (body.filter.field_modes, body.filter.exclude_modes):
        for k, v in modes.items():
            if v not in _VALID_FILTER_MODES:
                raise HTTPException(
                    status_code=400, detail=f"invalid match mode {v!r} for field {k!r}"
                )
    _validate_field_regexes(body.filter.fields, body.filter.field_modes)
    _validate_field_regexes(body.filter.exclude, body.filter.exclude_modes)
    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    event_ids, tags_include_filter, tags_exclude_filter = await _resolve_event_id_filters(
        case_id,
        source_ids,
        annotated=body.filter.annotated,
        annotation_tag_value=body.filter.annotation_tag_value,
        run_id=body.filter.run_id,
        tags_include=body.filter.tags_include,
        tags_exclude=body.filter.tags_exclude,
        ids=body.filter.ids,
    )

    store = get_store()
    annotations_by_event = _index_annotations_by_event(
        await store.list_source_annotations(case_id, source_ids)
    )

    eq = EventQuery(
        case_id=case_id,
        source_ids=source_ids,
        q=body.filter.q,
        q_regex=body.filter.q_regex,
        artifact=body.filter.artifact,
        artifacts=_parse_str_list(body.filter.artifacts),
        source_id=body.filter.source_id,
        tag=body.filter.tag,
        exclude_tag=body.filter.exclude_tag,
        start=body.filter.start,
        end=body.filter.end,
        field_filters=body.filter.fields,
        field_exclusions=body.filter.exclude,
        filter_modes=body.filter.field_modes,
        exclusion_modes=body.filter.exclude_modes,
        event_ids=event_ids,
        tags_include=tags_include_filter,
        tags_exclude=tags_exclude_filter,
        field_mappings=field_mappings,
    )

    if _uses_regex(bool(eq.q_regex and eq.q), eq.filter_modes, eq.exclusion_modes):
        # Force RE2 compilation on a cheap 1-row scan before streaming starts,
        # so a pattern that passes _validate_regex's re.compile pre-check but
        # is rejected by ClickHouse's RE2 driver still surfaces as a clean 400
        # instead of breaking the response mid-stream.
        await _run_regex_guarded(True, _get_query_service().query, replace(eq, limit=1, offset=0))

    if body.format == "jsonl":
        media_type = "application/x-ndjson"
        ext = "jsonl"
        content = _stream_jsonl(eq, annotations_by_event)
    else:
        media_type = "text/csv"
        ext = "csv"
        content = _stream_csv(eq, annotations_by_event)

    # Audited before streaming starts: an export that fails mid-stream still
    # extracted data up to the failure point, so the attempt itself is the
    # custody-relevant fact.
    await store.record_audit(
        action="events.export",
        actor=user,
        case_id=case_id,
        target_type="timeline",
        target_id=timeline_id,
        detail={
            "format": body.format,
            "filter": body.filter.model_dump(exclude_none=True, exclude_defaults=True),
        },
    )

    filename = f"{case_id}-{timeline_id}-events.{ext}"
    return StreamingResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Similarity / anomaly endpoints ───────────────────────────────────────────

_similarity_service: SimilarityService | None = None


def _get_similarity_service() -> SimilarityService:
    global _similarity_service  # noqa: PLW0603
    if _similarity_service is None:
        _similarity_service = SimilarityService()
    return _similarity_service


_stat_anomaly_service: StatisticalAnomalyService | None = None


def _get_stat_anomaly_service() -> StatisticalAnomalyService:
    global _stat_anomaly_service  # noqa: PLW0603
    if _stat_anomaly_service is None:
        _stat_anomaly_service = StatisticalAnomalyService()
    return _stat_anomaly_service


async def _resolve_field_inventory(
    svc: StatisticalAnomalyService,
    store: PostgresStore,
    case_id: str,
    source_ids: list[str],
    field_mappings: dict[str, list[str]] | None,
) -> tuple[list[tuple[str, int, int]], int]:
    """Candidate field inventory from the per-source stats cache.

    Only the exact canonical-mapping aggregates stay a live query (see
    ``db/field_stats.py``). Shared by ``_run_stat_detector``'s auto-field
    novelty path and ``list_anomaly_fields`` so both endpoints agree on which
    fields are candidates.
    """
    stats = await ensure_source_field_stats(store, svc.ch, case_id, source_ids)
    inventory, total = merged_inventory(stats, field_mappings)
    if field_mappings and total:
        inventory = inventory + await run_in_threadpool(
            svc.canonical_inventory, case_id, source_ids, field_mappings
        )
    return inventory, total


def _windows_from_definition(definition: BaselineDefinition) -> AnalysisWindows:
    """Build detector ``AnalysisWindows`` from a persisted baseline definition."""
    suspects = tuple(
        TimeWindow(
            label=w.get("label", f"window-{i}"),
            start=ensure_utc(datetime.fromisoformat(w["start"])),
            end=ensure_utc(datetime.fromisoformat(w["end"])),
        )
        for i, w in enumerate(definition.suspect_windows or [])
    )
    return AnalysisWindows(
        baseline=TimeWindow(
            "baseline",
            ensure_utc(definition.baseline_start),
            ensure_utc(definition.baseline_end),
        ),
        suspects=suspects,
    )


async def _resolve_analysis_windows(
    store: PostgresStore,
    svc: StatisticalAnomalyService,
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    baseline_id: str | None,
    baseline_end: datetime | None,
    temporal: bool,
) -> AnalysisWindows | None:
    """Resolve the temporal windows for a detector run.

    Precedence: an explicit ``baseline_id`` (a saved definition) wins; else an
    explicit legacy ``baseline_end`` split; else ``temporal=True`` falls back
    to the timeline midpoint. The legacy split forms are converted to a single
    baseline+suspect window pair via ``windows_from_split`` so the detectors
    have one temporal code path. Returns None for self-baseline runs.
    """
    if baseline_id is not None:
        definition = await store.get_baseline_definition(case_id, timeline_id, baseline_id)
        if definition is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown baseline definition: {baseline_id}"
            )
        return _windows_from_definition(definition)

    if baseline_end is None and not temporal:
        return None

    min_ts, max_ts = await run_in_threadpool(svc.get_timeline_range, case_id, source_ids)
    if min_ts is None or max_ts is None:
        return None
    split = ensure_utc(baseline_end) if baseline_end is not None else min_ts + (max_ts - min_ts) / 2
    return windows_from_split(split, min_ts, max_ts)


async def _run_stat_detector(
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    *,
    detector: str,
    fields: str | None,
    series_field: str,
    z_threshold: float | None,
    baseline_end: datetime | None,
    temporal: bool,
    baseline_id: str | None = None,
    limit: int,
    min_skew_seconds: float | None = None,
    fdr_q: float | None = None,
    min_ratio: float | None = None,
    field_mappings: dict[str, list[str]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve analysis windows + value allowlist, then dispatch to the detector.

    Returns ``(result, resolution)`` where *resolution* carries the forensic
    snapshot (baseline id/name, windows payload + hash, allowlist hash + count)
    for :func:`_persist_detector_run`. Shared by ``list_anomalies`` (preview)
    and ``tag_anomalies`` (persist) so "Tag N anomalies" persists exactly the
    finding set the preview showed.
    """
    cfg = get_settings()
    store = get_store()
    svc = _get_stat_anomaly_service()

    # Value-level allowlist (analyst-declared "normal"), and the legacy per-event
    # `normal` annotations still honored as an event-level exclusion. Both are
    # independent of window resolution — fetch concurrently. The allowlist covers
    # entries declared for this detector *plus* detector-agnostic ones (the
    # `"*"` wildcard, written from a field-value row where no detector context
    # exists — see docs/ANOMALY_DETECTION.md), so a value marked normal anywhere
    # suppresses it here too.
    allow_task = store.list_allowlist_entries(case_id, timeline_id)
    normal_task = store.list_event_ids_by_annotation_type(case_id, source_ids, "normal")
    windows_task = _resolve_analysis_windows(
        store, svc, case_id, timeline_id, source_ids, baseline_id, baseline_end, temporal
    )
    all_entries, normal_ids, windows = await asyncio.gather(allow_task, normal_task, windows_task)
    allow_entries = [e for e in all_entries if e.detector in (detector, "*")]
    allowlist: set[tuple[str, str]] | None = {(e.field, e.value) for e in allow_entries} or None
    exclude_ids: set[str] | None = set(normal_ids) if normal_ids else None

    resolution: dict[str, Any] = {
        "baseline_id": baseline_id,
        "windows": windows.payload() if windows is not None else None,
        "windows_hash": windows.config_hash() if windows is not None else None,
        "allowlist_hash": allowlist_hash(allow_entries),
        "allowlist_count": len(allow_entries),
    }

    if detector == "timestamp_order":
        # Mode-less: no windows, and per-event (not value-level) so it keeps the
        # legacy event-level `normal` suppression only.
        result = await run_in_threadpool(
            svc.find_order_violations,
            case_id=case_id,
            source_ids=source_ids,
            min_skew_seconds=(
                min_skew_seconds if min_skew_seconds is not None else cfg.stat_order_min_skew
            ),
            limit=limit,
            exclude_event_ids=exclude_ids,
        )
        return result, resolution

    if detector == "frequency":
        result = await run_in_threadpool(
            svc.find_frequency_anomalies,
            case_id=case_id,
            source_ids=source_ids,
            series_field=series_field,
            limit=limit,
            bucket_count=cfg.stat_frequency_buckets,
            z_threshold=z_threshold if z_threshold is not None else cfg.stat_z_threshold,
            windows=windows,
            exclude_event_ids=exclude_ids,
            allowlist=allowlist,
            field_mappings=field_mappings,
        )
        return result, resolution

    parsed_fields = _parse_novelty_fields(fields)

    if detector == "numeric_range":
        result = await run_in_threadpool(
            svc.find_range_violations,
            case_id=case_id,
            source_ids=source_ids,
            fields=parsed_fields,
            limit=limit,
            per_field_limit=cfg.stat_per_field_limit,
            windows=windows,
            exclude_event_ids=exclude_ids,
            allowlist=allowlist,
            field_mappings=field_mappings,
        )
        return result, resolution

    if detector == "value_combo":
        if parsed_fields is not None and len(parsed_fields) < 2:
            raise HTTPException(
                status_code=422,
                detail="value_combo requires at least two fields.",
            )
        if parsed_fields is not None and len(parsed_fields) > 4:
            raise HTTPException(
                status_code=422,
                detail="value_combo supports at most four fields.",
            )
        try:
            result = await run_in_threadpool(
                svc.find_value_combos,
                case_id=case_id,
                source_ids=source_ids,
                fields=parsed_fields,
                limit=limit,
                rarity_floor=cfg.stat_rarity_floor,
                windows=windows,
                exclude_event_ids=exclude_ids,
                allowlist=allowlist,
                field_mappings=field_mappings,
            )
            return result, resolution
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Auto-field selection (no explicit fields): resolve the candidate
    # inventory from the per-source field-stats cache here — the detectors
    # run sync in a worker thread and can't await the cache themselves. This
    # keeps find_value_novelty/find_charset_novelty off the live map-scanning
    # field_inventory query, which is expensive on wide sources.
    inventory: list[tuple[str, int, int]] | None = None
    inventory_total: int | None = None
    if parsed_fields is None:
        inventory, inventory_total = await _resolve_field_inventory(
            svc, store, case_id, source_ids, field_mappings
        )

    if detector == "charset":
        result = await run_in_threadpool(
            svc.find_charset_novelty,
            case_id=case_id,
            source_ids=source_ids,
            fields=parsed_fields,
            limit=limit,
            per_field_limit=cfg.stat_per_field_limit,
            rarity_floor=cfg.stat_charset_rarity_floor,
            windows=windows,
            exclude_event_ids=exclude_ids,
            allowlist=allowlist,
            field_mappings=field_mappings,
            inventory=inventory,
            inventory_total=inventory_total,
        )
        return result, resolution

    if detector == "entropy":
        result = await run_in_threadpool(
            svc.find_entropy_outliers,
            case_id=case_id,
            source_ids=source_ids,
            fields=parsed_fields,
            limit=limit,
            per_field_limit=cfg.stat_per_field_limit,
            windows=windows,
            exclude_event_ids=exclude_ids,
            allowlist=allowlist,
            field_mappings=field_mappings,
            inventory=inventory,
            inventory_total=inventory_total,
        )
        return result, resolution

    if detector == "proportion_shift":
        # Snapshot the *effective* thresholds (request override or server
        # default) so the persisted run stays self-describing.
        resolution["shift_fdr_q"] = fdr_q if fdr_q is not None else cfg.stat_shift_fdr_q
        resolution["shift_min_ratio"] = (
            min_ratio if min_ratio is not None else cfg.stat_shift_min_ratio
        )
        result = await run_in_threadpool(
            svc.find_proportion_shifts,
            case_id=case_id,
            source_ids=source_ids,
            fields=parsed_fields,
            limit=limit,
            windows=windows,
            fdr_q=resolution["shift_fdr_q"],
            min_ratio=resolution["shift_min_ratio"],
            max_candidates_per_field=cfg.stat_shift_max_candidates_per_field,
            exclude_event_ids=exclude_ids,
            allowlist=allowlist,
            field_mappings=field_mappings,
            inventory=inventory,
            inventory_total=inventory_total,
        )
        return result, resolution

    result = await run_in_threadpool(
        svc.find_value_novelty,
        case_id=case_id,
        source_ids=source_ids,
        fields=parsed_fields,
        limit=limit,
        rarity_floor=cfg.stat_rarity_floor,
        windows=windows,
        per_field_limit=cfg.stat_per_field_limit,
        exclude_event_ids=exclude_ids,
        allowlist=allowlist,
        field_mappings=field_mappings,
        inventory=inventory,
        inventory_total=inventory_total,
    )
    return result, resolution


async def _resolve_similarity_source_ids(case_id: str, timeline_id: str | None) -> list[str]:
    """Return the source IDs to search: a timeline's sources, or the whole case's.

    Similarity search is not timeline-specific at the storage layer (Qdrant
    collections are per-case, points tagged by ``source_id``), so scoping to
    a timeline is an optional narrowing, not a requirement.
    """
    if timeline_id is not None:
        return await _resolve_timeline_source_ids(case_id, timeline_id)
    store = get_store()
    sources = await store.list_sources(case_id)
    # Same readiness rule as _resolve_timeline_scope: never search
    # half-ingested sources.
    return [s.id for s in sources if s.is_ready]


@router.get("/{case_id}/events/{event_id}/similar")
async def find_similar_events(
    case_id: str,
    event_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    timeline_id: str | None = Query(default=None),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return events semantically similar to ``event_id`` using vector search.

    Searches across the whole case by default; pass ``timeline_id`` to narrow
    to one timeline's sources. Returns ``status="not_embedded"`` when no
    vectors exist, ``status="vector_not_found"`` when the specific event has
    no vector.
    """
    source_ids = await _resolve_similarity_source_ids(case_id, timeline_id)
    svc = _get_similarity_service()
    result = await run_in_threadpool(svc.find_similar, case_id, source_ids, event_id, limit=limit)
    return {
        "status": result.status,
        "results": [
            {"event_id": r.event_id, "score": r.score, "event": r.event} for r in result.results
        ],
    }


@router.get("/{case_id}/events/semantic-search")
async def semantic_search_events(
    case_id: str,
    q: str = Query(..., min_length=1),
    limit: int = Query(default=10, ge=1, le=100),
    timeline_id: str | None = Query(default=None),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return events semantically similar to a free-text query.

    Searches across the whole case by default; pass ``timeline_id`` to narrow
    to one timeline's sources. Returns ``status="not_embedded"`` when no
    vectors exist for the searched sources.
    """
    if not embeddings_available():
        # find_similar_by_text encodes the query text at request time, which
        # needs the local model (or a remote endpoint) — fail clearly instead
        # of surfacing an ImportError from the worker thread.
        raise HTTPException(status_code=503, detail=_EMBEDDINGS_UNAVAILABLE_DETAIL)
    source_ids = await _resolve_similarity_source_ids(case_id, timeline_id)
    svc = _get_similarity_service()
    try:
        result = await run_in_threadpool(
            svc.find_similar_by_text, case_id, source_ids, q, limit=limit
        )
    except EncoderUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": result.status,
        "results": [
            {"event_id": r.event_id, "score": r.score, "event": r.event} for r in result.results
        ],
    }


def _serialize_finding(
    r: ValueFinding
    | FreqFinding
    | OrderFinding
    | ComboFinding
    | RangeFinding
    | CharsetFinding
    | EntropyFinding
    | ShiftFinding,
) -> dict[str, Any]:
    """Serialise a Value/Freq/Order/Combo/Range/Charset/Entropy/Shift finding to a JSON-safe dict."""
    # Charset/Entropy/Shift finding dataclass fields are exactly the wire keys,
    # so asdict() avoids a hand-maintained field-by-field transcription that
    # would silently drop any newly added field.
    if isinstance(r, ShiftFinding):
        return {"type": "proportion_shift", **asdict(r)}
    if isinstance(r, EntropyFinding):
        return {"type": "entropy", **asdict(r)}
    if isinstance(r, CharsetFinding):
        return {"type": "charset", **asdict(r)}
    if isinstance(r, ValueFinding):
        return {
            "type": "value_novelty",
            "field": r.field,
            "value": r.value,
            "count": r.count,
            "score": r.score,
            "first_seen": r.first_seen,
            "event_id": r.event_id,
            "event": r.event,
            "details": r.details,
        }
    if isinstance(r, RangeFinding):
        return {
            "type": "numeric_range",
            "field": r.field,
            "value": r.value,
            "count": r.count,
            "score": r.score,
            "direction": r.direction,
            "lower": r.lower,
            "upper": r.upper,
            "first_seen": r.first_seen,
            "event_id": r.event_id,
            "event": r.event,
            "details": r.details,
        }
    if isinstance(r, ComboFinding):
        return {
            "type": "value_combo",
            "fields": r.fields,
            "values": r.values,
            "count": r.count,
            "score": r.score,
            "first_seen": r.first_seen,
            "event_id": r.event_id,
            "event": r.event,
            "details": r.details,
        }
    if isinstance(r, OrderFinding):
        return {
            "type": "timestamp_order",
            "source_id": r.source_id,
            "event_id": r.event_id,
            "timestamp": r.timestamp,
            "prev_timestamp": r.prev_timestamp,
            "skew_seconds": r.skew_seconds,
            "byte_offset": r.byte_offset,
            "line_number": r.line_number,
            "score": r.score,
            "event": r.event,
            "details": r.details,
        }
    # FreqFinding
    return {
        "type": "frequency",
        "series_field": r.series_field,
        "series_value": r.series_value,
        "window_start": r.window_start,
        "window_end": r.window_end,
        "observed": r.observed,
        "expected": r.expected,
        "z_score": r.z_score,
        "score": r.score,
        "event_id": r.event_id,
        "event": r.event,
        "details": r.details,
    }


@router.get("/{case_id}/timelines/{timeline_id}/anomalies/fields")
async def list_anomaly_fields(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return candidate fields for anomaly detection, annotated with cardinality.

    Each entry carries:
    - ``token``       — field token to pass to the ``fields`` / ``series_field`` params.
    - ``distinct``    — number of distinct non-empty values.
    - ``coverage``    — fraction of events with a non-empty value (0–1).
    - ``kind``        — ``"categorical"`` | ``"constant"`` | ``"identifier"`` | ``"sparse"``.
    - ``recommended`` — ``true`` when the field is suitable for novelty detection.
    """
    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    svc = _get_stat_anomaly_service()
    inventory, total = await _resolve_field_inventory(
        svc, get_store(), case_id, source_ids, field_mappings
    )
    fields: list[NoveltyFieldInfo] = await run_in_threadpool(
        svc.recommend_novelty_fields, case_id, source_ids, total, field_mappings, inventory
    )
    return {
        "fields": [
            {
                "token": f.token,
                "distinct": f.distinct,
                "coverage": f.coverage,
                "kind": f.kind,
                "recommended": f.recommended,
            }
            for f in fields
        ]
    }


@router.get("/{case_id}/timelines/{timeline_id}/anomalies/numeric-fields")
async def list_numeric_anomaly_fields(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return numeric-parseable candidate fields for the numeric-range detector.

    Same shape as ``/anomalies/fields`` plus ``numeric_ratio`` (fraction of a
    field's non-empty values that parse as a number). Candidate inventory comes
    from the per-source stats cache; the numeric probe is a single live query.
    """
    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    svc = _get_stat_anomaly_service()
    stats = await ensure_source_field_stats(get_store(), svc.ch, case_id, source_ids)
    inventory, total = merged_inventory(stats, field_mappings)
    if field_mappings and total:
        inventory = inventory + await run_in_threadpool(
            svc.canonical_inventory, case_id, source_ids, field_mappings
        )
    fields = await run_in_threadpool(
        svc.recommend_numeric_fields, case_id, source_ids, total, field_mappings, inventory
    )
    return {
        "fields": [
            {
                "token": f.token,
                "distinct": f.distinct,
                "coverage": f.coverage,
                "numeric_ratio": f.numeric_ratio,
                "recommended": f.recommended,
            }
            for f in fields
        ]
    }


def _short_ts(value: Any) -> str:
    """Trim an ISO timestamp to minute precision for human-readable reasons."""
    if not value:
        return "?"
    return str(value).replace("T", " ")[:16]


def _window_phrase(details: dict[str, Any], *, prefix: str = "suspect window") -> str:
    """A short 'suspect window 'label' 〔start – end〕' phrase, or '' if absent.

    Reads the window attribution the temporal detectors stamp into each
    finding's ``details`` (``window_*`` for value-shaped detectors,
    ``suspect_window_*`` for frequency), so the tag reason names exactly the
    window the finding was attributed to.
    """
    label = details.get("window_label") or details.get("suspect_window_label")
    if not label:
        return ""
    start = details.get("window_start") or details.get("suspect_window_start")
    end = details.get("window_end") or details.get("suspect_window_end")
    span = f" 〔{_short_ts(start)} – {_short_ts(end)}〕" if start and end else ""
    return f"{prefix} {label!r}{span}"


def _serialize_stat_result(result: Any) -> dict[str, Any]:
    """Serialize a StatAnomalyResult to the shape shared by list_anomalies/tag_anomalies."""
    return {
        "status": result.status,
        "detector": result.detector,
        "method": result.method,
        "baseline_size": result.baseline_size,
        "results": [_serialize_finding(r) for r in result.results],
        "z_threshold": result.z_threshold,
        "warnings": list(getattr(result, "warnings", []) or []),
        "windows": getattr(result, "windows", None),
    }


async def _persist_detector_run(
    case_id: str,
    timeline_id: str,
    *,
    detector: str,
    fields: str | None,
    series_field: str,
    z_threshold: float | None,
    baseline_end: datetime | None,
    temporal: bool,
    limit: int,
    payload: dict[str, Any],
    resolution: dict[str, Any],
    min_skew_seconds: float | None = None,
) -> str:
    """Persist a detector scan's request params + serialized result, return the run_id.

    *resolution* carries the forensic snapshot from :func:`_run_stat_detector`
    (resolved baseline id, window ranges + hash, allowlist hash + count) so a
    persisted run stays fully self-describing even after the baseline
    definition or allowlist is later edited or deleted.
    """
    store = get_store()
    run = await store.create_detector_run(
        case_id,
        timeline_id,
        detector,
        params={
            "fields": fields,
            "series_field": series_field,
            "z_threshold": z_threshold,
            "baseline_end": baseline_end.isoformat() if baseline_end else None,
            "temporal": temporal,
            "limit": limit,
            "min_skew_seconds": min_skew_seconds,
            # proportion_shift: effective (request-or-default) thresholds; None
            # for every other detector.
            "fdr_q": resolution.get("shift_fdr_q"),
            "min_ratio": resolution.get("shift_min_ratio"),
            "baseline_id": resolution.get("baseline_id"),
            "windows": resolution.get("windows"),
            "windows_hash": resolution.get("windows_hash"),
            "allowlist_hash": resolution.get("allowlist_hash"),
            "allowlist_count": resolution.get("allowlist_count"),
        },
        result=payload,
    )
    return run.id


@router.get("/{case_id}/detector-runs/{run_id}")
async def get_detector_run(
    case_id: str, run_id: str, case: Case = Depends(require_case_read)
) -> dict[str, Any]:
    """Return a persisted detector run's params and findings.

    Lets the client (or an analyst debugging a filter) inspect what a
    ``run_id`` — as referenced by ``list_events``/``histogram``/bulk-annotate/
    export's ``run_id`` filter param — actually contains, without re-running
    the detector.
    """
    store = get_store()
    run = await store.get_detector_run(case_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown detector run: {run_id}")
    return run.to_dict()


@router.get("/{case_id}/timelines/{timeline_id}/anomalies")
async def list_anomalies(
    case_id: str,
    timeline_id: str,
    detector: str = Query(
        default="value_novelty",
        description="Detector to run: 'value_novelty', 'value_combo', 'frequency', 'timestamp_order', 'numeric_range', 'charset', 'entropy', or 'proportion_shift'.",
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated field tokens for value_novelty "
            "(e.g. 'artifact,display_name,attr:user_agent'). "
            "When omitted the cardinality-based recommender selects fields automatically."
        ),
    ),
    series_field: str = Query(
        default="artifact",
        description="Field to group frequency series by.",
    ),
    z_threshold: float | None = Query(
        default=None,
        gt=0,
        description="|z| cutoff for the frequency detector. Omit to use the server default.",
    ),
    min_skew_seconds: float | None = Query(
        default=None,
        ge=0,
        description=(
            "Minimum backwards jump (seconds) for the timestamp_order detector. "
            "Omit to use the server default."
        ),
    ),
    fdr_q: float | None = Query(
        default=None,
        gt=0,
        le=1,
        description=(
            "Benjamini-Hochberg false-discovery-rate ceiling for the "
            "proportion_shift detector. Omit to use the server default."
        ),
    ),
    min_ratio: float | None = Query(
        default=None,
        gt=1,
        description=(
            "Effect-size floor (rate ratio, either direction) for the "
            "proportion_shift detector. Omit to use the server default."
        ),
    ),
    baseline_end: datetime | None = Query(  # noqa: B008
        default=None,
        description="Explicit temporal baseline end timestamp (detect window = after this).",
    ),
    temporal: bool = Query(
        default=False,
        description=(
            "Enable legacy temporal mode.  When no baseline_end is given the "
            "timeline midpoint is used as the baseline/detect split.  Prefer "
            "baseline_id for an explicit baseline + suspect windows."
        ),
    ),
    baseline_id: str | None = Query(
        default=None,
        description=(
            "ID of a saved baseline definition (baseline range + suspect windows) "
            "to run temporal detection against. Takes precedence over "
            "baseline_end/temporal."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=500),
    persist: bool = Query(
        default=True,
        description=(
            "Persist this scan as a DetectorRun and return its run_id, so the "
            "client can reference the finding set by ID (e.g. to filter the "
            "grid to 'anomaly') instead of re-uploading event IDs."
        ),
    ),
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Run a statistical anomaly detector on the timeline and return findings.

    No embeddings required — operates on already-ingested ClickHouse data.

    **value_novelty**: flags rare or first-seen field values, ranked by surprise
    score (-log frequency).  Works immediately after ingestion.

    **value_combo**: the multi-field extension of value_novelty — flags rare or
    first-seen *combinations* of two or more fields (requires ≥ 2 `fields`, or
    auto-picks the top two recommended).

    **frequency**: flags time windows with anomalous event-count z-scores per
    field-value series.

    **timestamp_order**: flags events whose timestamp runs backwards relative
    to record order within a source (log-tampering / clock-manipulation).

    **numeric_range**: for numeric-parseable fields, learns a baseline band
    (IQR fence self-baseline, or min/max temporal) and flags values outside it.

    **charset**: per field, learns a reference character set and flags values
    containing characters outside it (rare-character self-baseline, or
    never-seen-in-baseline temporal).

    **entropy**: per field, flags values whose Shannon character entropy falls
    outside a Tukey fence over the field's baseline entropy distribution
    (random-looking or degenerate strings).

    **proportion_shift**: per (field, value), flags values whose *share* of
    events differs significantly between the baseline window and a suspect
    window (2×2 G-test, Benjamini-Hochberg FDR across the run, rate-ratio
    effect floor). Temporal-only — requires baseline_id/baseline_end/temporal;
    first-seen values are excluded (value_novelty owns those).
    """
    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    result, resolution = await _run_stat_detector(
        case_id,
        timeline_id,
        source_ids,
        detector=detector,
        fields=fields,
        series_field=series_field,
        z_threshold=z_threshold,
        baseline_end=baseline_end,
        temporal=temporal,
        baseline_id=baseline_id,
        limit=limit,
        min_skew_seconds=min_skew_seconds,
        fdr_q=fdr_q,
        min_ratio=min_ratio,
        field_mappings=field_mappings,
    )

    payload = _serialize_stat_result(result)
    run_id = None
    if persist and result.status == "ok":
        run_id = await _persist_detector_run(
            case_id,
            timeline_id,
            detector=detector,
            fields=fields,
            series_field=series_field,
            z_threshold=z_threshold,
            baseline_end=baseline_end,
            temporal=temporal,
            limit=limit,
            min_skew_seconds=min_skew_seconds,
            payload=payload,
            resolution=resolution,
        )
    # GETs are skipped by the generic audit middleware, so detector-run
    # launches would otherwise leave no trace at all. Audited regardless of
    # `persist` — unpersisted preview scans still read case data and should
    # remain visible in the custody trail, just without a run_id to anchor to.
    await get_store().record_audit(
        action="anomaly.run",
        actor=user,
        case_id=case_id,
        target_type="detector_run",
        target_id=run_id,
        detail={
            "detector": detector,
            "timeline_id": timeline_id,
            "fields": fields,
            "series_field": series_field,
            "temporal": temporal,
            "baseline_end": baseline_end.isoformat() if baseline_end else None,
            "baseline_id": resolution.get("baseline_id"),
            "windows_hash": resolution.get("windows_hash"),
            "persist": persist,
        },
    )
    payload["run_id"] = run_id
    return payload


class TagAnomaliesRequest(BaseModel):
    """Request body for the tag-anomalies endpoint."""

    detector: str = Field(
        default="value_novelty",
        description="Detector to run: 'value_novelty', 'value_combo', 'frequency', 'timestamp_order', 'numeric_range', 'charset', 'entropy', or 'proportion_shift'.",
    )
    fields: str | None = Field(
        default=None,
        description="Comma-separated field tokens for value_novelty.",
    )
    series_field: str = Field(
        default="artifact",
        description="Field to group frequency series by.",
    )
    z_threshold: float | None = Field(
        default=None,
        gt=0,
        description="|z| cutoff for the frequency detector. Omit to use the server default.",
    )
    min_skew_seconds: float | None = Field(
        default=None,
        ge=0,
        description="Minimum backwards jump (seconds) for the timestamp_order detector.",
    )
    fdr_q: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="BH false-discovery-rate ceiling for the proportion_shift detector.",
    )
    min_ratio: float | None = Field(
        default=None,
        gt=1,
        description="Effect-size floor (rate ratio) for the proportion_shift detector.",
    )
    baseline_end: datetime | None = Field(
        default=None,
        description="Explicit temporal baseline end timestamp.",
    )
    temporal: bool = Field(
        default=False,
        description=(
            "Enable legacy temporal mode.  When no baseline_end is given the "
            "timeline midpoint is used as the baseline/detect split.  Prefer "
            "baseline_id."
        ),
    )
    baseline_id: str | None = Field(
        default=None,
        description=(
            "ID of a saved baseline definition to run temporal detection against. "
            "Takes precedence over baseline_end/temporal."
        ),
    )
    limit: int = Field(default=50, ge=1, le=500)


@router.post("/{case_id}/timelines/{timeline_id}/anomalies/tag")
async def tag_anomalies(
    case_id: str,
    timeline_id: str,
    body: TagAnomaliesRequest,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Re-run the statistical anomaly detector and persist findings as annotations.

    Clears prior system ``anomaly`` annotations for the timeline's sources before
    writing new ones, so repeated calls replace rather than accumulate results.
    Each finding receives:

    - ``annotation_type="anomaly"`` / ``origin="system"``
    - A human-readable ``content`` describing the finding.
    - A structured ``details`` JSON for the Analysis panel.

    Returns the same shape as ``GET /anomalies`` plus a ``tagged`` count.
    """
    store = get_store()
    source_ids, field_mappings = await _resolve_timeline_scope(case_id, timeline_id)
    result, resolution = await _run_stat_detector(
        case_id,
        timeline_id,
        source_ids,
        detector=body.detector,
        fields=body.fields,
        series_field=body.series_field,
        z_threshold=body.z_threshold,
        baseline_end=body.baseline_end,
        temporal=body.temporal,
        baseline_id=body.baseline_id,
        limit=body.limit,
        min_skew_seconds=body.min_skew_seconds,
        fdr_q=body.fdr_q,
        min_ratio=body.min_ratio,
        field_mappings=field_mappings,
    )

    if result.status != "ok":
        return {
            "status": result.status,
            "detector": result.detector,
            "method": result.method,
            "tagged": 0,
            "skipped_unresolved": 0,
            "baseline_size": result.baseline_size,
            "results": [],
            "z_threshold": result.z_threshold,
            "run_id": None,
        }

    # Clear prior (non-pinned) system anomaly annotations for this timeline's
    # sources. Pinned rows — created via the per-event "Persist" action — are
    # left alone so a manually-confirmed finding survives even if this
    # re-scan no longer surfaces it.
    await store.delete_system_annotations(case_id, source_ids, "anomaly", detector=body.detector)
    pinned_event_ids = set(
        await store.list_pinned_event_ids(case_id, source_ids, "anomaly", detector=body.detector)
    )

    # Write one system annotation per finding, skipping events that already
    # have a pinned annotation to avoid a duplicate row for the same event.
    annotation_rows = []
    skipped_unresolved = 0
    for r in result.results:
        if isinstance(r, ValueFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            if result.method == "temporal":
                # Every temporal-mode finding is, by construction, absent from
                # the baseline window (backend filters on baseline_cnt = 0) —
                # a materially stronger, more specific claim than "rare", so
                # say exactly that rather than reusing the self-baseline text.
                where = _window_phrase(r.details) or "the detect window"
                wtot = r.details.get("window_total_events", result.baseline_size)
                content = (
                    f"New value — {r.field}={r.value!r}: absent from the "
                    f"{result.baseline_size:,}-event baseline; first appears in "
                    f"{where} at {r.first_seen} ({r.count} of {wtot:,} window "
                    f"events; surprise {r.score:.2f})"
                )
            else:
                content = (
                    f"Rare value — {r.field}={r.value!r}: appears {r.count} "
                    f"time(s) of {result.baseline_size:,} events in the "
                    f"corpus (surprise {r.score:.2f})"
                )
        elif isinstance(r, RangeFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            band_desc = "baseline min/max" if result.method == "temporal-range" else "IQR fence"
            where = _window_phrase(r.details)
            in_window = f" in {where}" if where else ""
            content = (
                f"Out-of-range value — {r.field}={r.value:g}: {r.direction} the "
                f"learned band [{r.lower:g}, {r.upper:g}] ({band_desc}){in_window}"
            )
        elif isinstance(r, EntropyFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            band_desc = (
                "baseline-window entropy IQR fence"
                if result.method == "temporal-iqr"
                else "corpus entropy IQR fence"
            )
            look = "random-looking" if r.direction == "above" else "degenerate/repetitive"
            where = _window_phrase(r.details)
            in_window = f" in {where}" if where else ""
            content = (
                f"Entropy outlier — {r.field}={r.value!r}: character entropy "
                f"{r.entropy:.2f} bits is {r.direction} the learned band "
                f"[{r.lower:.2f}, {r.upper:.2f}] ({band_desc}; {look}){in_window}"
            )
        elif isinstance(r, CharsetFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            chars_desc = ", ".join(
                f"{c!r} (U+{ord(c):04X})" if len(c) == 1 else repr(c) for c in r.novel_chars
            )
            if result.method == "temporal-charset":
                where = _window_phrase(r.details)
                in_window = f" in {where}" if where else ""
                content = (
                    f"Charset novelty — {r.field}={r.value!r}: contains "
                    f"character(s) {chars_desc} never seen in this field's "
                    f"baseline-window values{in_window} (surprise {r.score:.2f})"
                )
            else:
                content = (
                    f"Charset novelty — {r.field}={r.value!r}: contains rare "
                    f"character(s) {chars_desc} appearing in almost no other "
                    f"value of this field (surprise {r.score:.2f})"
                )
        elif isinstance(r, ComboFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            combo = ", ".join(f"{f}={v!r}" for f, v in zip(r.fields, r.values, strict=False))
            if result.method == "temporal":
                where = _window_phrase(r.details) or "the detect window"
                wtot = r.details.get("window_total_events", result.baseline_size)
                content = (
                    f"New combination — ({combo}): absent from the "
                    f"{result.baseline_size:,}-event baseline; first appears in "
                    f"{where} at {r.first_seen} ({r.count} of {wtot:,} window "
                    f"events; surprise {r.score:.2f})"
                )
            else:
                content = (
                    f"Rare combination — ({combo}): appears {r.count} time(s) "
                    f"of {result.baseline_size:,} events in the corpus "
                    f"(surprise {r.score:.2f})"
                )
        elif isinstance(r, ShiftFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            where = _window_phrase(r.details) or "the suspect window"
            bl_pct = f"{r.baseline_rate * 100:.2g}%"
            if r.count == 0:
                content = (
                    f"Proportion shift — {r.field}={r.value!r}: present "
                    f"{r.baseline_count}× in the {result.baseline_size:,}-event "
                    f"baseline ({bl_pct}) but absent from {where} "
                    f"(G={r.g_statistic:.1f}, q={r.q_value:.3g})"
                )
            else:
                content = (
                    f"Proportion shift — {r.field}={r.value!r}: share of events "
                    f"went {bl_pct} → {r.window_rate * 100:.2g}% "
                    f"({r.rate_ratio:.1f}×, {r.direction}) in {where} "
                    f"(G={r.g_statistic:.1f}, q={r.q_value:.3g})"
                )
        elif isinstance(r, OrderFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            content = (
                f"Out-of-order timestamp — {r.source_id}: event at "
                f"{r.timestamp} occurs after a record dated {r.prev_timestamp} "
                f"({r.skew_seconds:.1f}s backwards; record order = byte offset "
                f"{r.byte_offset})"
            )
        else:
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            if result.method == "temporal-z-score":
                where = _window_phrase(r.details)
                in_window = f" in {where}" if where else ""
                content = (
                    f"Frequency spike — {r.series_field}={r.series_value!r} "
                    f"at {r.window_start}: {r.observed} events observed vs "
                    f"{r.expected:.1f} expected from the baseline-window "
                    f"event-count distribution (z={r.z_score:.2f}){in_window}"
                )
            else:
                content = (
                    f"Frequency spike — {r.series_field}={r.series_value!r} "
                    f"at {r.window_start}: {r.observed} events observed vs "
                    f"{r.expected:.1f} expected from this series' own overall "
                    f"event-count distribution, which includes this window "
                    f"(z={r.z_score:.2f})"
                )
        if not event_id:
            # Representative event couldn't be resolved (e.g. deleted between
            # detection and tagging) — nothing to attach the annotation to.
            skipped_unresolved += 1
            continue
        if event_id in pinned_event_ids:
            continue
        annotation_rows.append(
            {
                "annotation_id": generate_id(f"{event_id}_anomaly_{result.detector}"),
                "case_id": case_id,
                "source_id": src_id,
                "event_id": event_id,
                "annotation_type": "anomaly",
                "content": content,
                "origin": "system",
                "details": r.details,
                "detector": result.detector,
            }
        )

    tagged = await store.bulk_create_annotations(annotation_rows) if annotation_rows else 0
    if tagged:
        publish_annotation_change(case_id, timeline_id, None, user)

    payload = _serialize_stat_result(result)
    run_id = await _persist_detector_run(
        case_id,
        timeline_id,
        detector=body.detector,
        fields=body.fields,
        series_field=body.series_field,
        z_threshold=body.z_threshold,
        baseline_end=body.baseline_end,
        temporal=body.temporal,
        limit=body.limit,
        min_skew_seconds=body.min_skew_seconds,
        payload=payload,
        resolution=resolution,
    )

    await store.record_audit(
        action="anomaly.tag",
        actor=user,
        case_id=case_id,
        target_type="detector_run",
        target_id=run_id,
        detail={
            "detector": body.detector,
            "timeline_id": timeline_id,
            "tagged": tagged,
            "temporal": body.temporal,
            "baseline_end": body.baseline_end.isoformat() if body.baseline_end else None,
        },
    )

    return {
        "status": "ok",
        "detector": result.detector,
        "method": result.method,
        "tagged": tagged,
        "skipped_unresolved": skipped_unresolved,
        "baseline_size": result.baseline_size,
        "results": payload["results"],
        "z_threshold": result.z_threshold,
        "run_id": run_id,
    }


class PersistAnomalyFindingRequest(BaseModel):
    """Body for persisting one live (not-yet-tagged) anomaly finding."""

    detector: str = Field(
        ...,
        description=(
            "Detector id ('value_novelty', 'charset', 'proportion_shift', …) "
            "that produced this finding."
        ),
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Human-readable finding description, as shown in the Analysis panel.",
    )
    details: dict[str, Any] = Field(default_factory=dict)


@router.post("/{case_id}/sources/{source_id}/events/{event_id}/anomalies/persist")
async def persist_anomaly_finding(
    case_id: str,
    source_id: str,
    event_id: str,
    body: PersistAnomalyFindingRequest,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(require_password_current),
) -> dict[str, Any]:
    """Persist a single live anomaly finding as a system annotation.

    Unlike ``/anomalies/tag`` (which re-runs a detector and replaces every
    system annotation for the timeline's sources), this writes exactly one
    row for this event and leaves every other tagged finding untouched — it's
    the action behind the event detail panel's per-finding "Persist" button,
    for confirming one finding without re-tagging everything else.

    Written with ``pinned=True`` so a later bulk "Tag N as anomaly" re-run
    (which clears and rewrites non-pinned system annotations) doesn't delete
    this manually-confirmed finding, even if the re-scan no longer surfaces it.
    """
    store = get_store()
    annotation_id = generate_id(f"{event_id}_anomaly_{body.detector}")
    annotation = await store.create_annotation(
        case_id=case_id,
        source_id=source_id,
        event_id=event_id,
        annotation_id=annotation_id,
        annotation_type="anomaly",
        content=body.content,
        origin="system",
        details=body.details,
        pinned=True,
        detector=body.detector,
    )
    publish_annotation_change(case_id, None, event_id, user)
    await store.record_audit(
        action="anomaly.persist_finding",
        actor=user,
        case_id=case_id,
        target_type="event",
        target_id=event_id,
        detail={"detector": body.detector, "source_id": source_id},
    )
    return {"annotation": annotation.to_dict()}
