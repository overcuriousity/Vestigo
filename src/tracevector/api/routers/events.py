"""API routes for querying events."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Generator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from tracevector.db.postgres import PostgresStore, generate_id
from tracevector.db.queries import EventQuery, EventQueryService
from tracevector.db.similarity import SimilarityService

_query_service: EventQueryService | None = None


def _get_query_service() -> EventQueryService:
    global _query_service  # noqa: PLW0603
    if _query_service is None:
        _query_service = EventQueryService()
    return _query_service

router = APIRouter(prefix="/api/cases", tags=["events"])

_store: PostgresStore | None = None


def get_store() -> PostgresStore:
    """Return a cached PostgresStore instance."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = PostgresStore()
    return _store


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


@router.get("/{case_id}/timelines/{timeline_id}/events")
async def list_events(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(default=None, description="Full-text search in message"),
    source: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    exclude_tag: str | None = Query(default=None),
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    filters: str | None = Query(
        default=None,
        description='JSON object of field equality filters, e.g. {"ip_address_city":"Falkenstein"}',
    ),
    exclusions: str | None = Query(
        default=None,
        description='JSON object of field exclusion filters, e.g. {"status_code":"200"}',
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="desc", description="Sort order: asc or desc"),
) -> dict[str, Any]:
    """List events for a timeline with optional filters."""
    if order not in ("asc", "desc"):
        order = "desc"

    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    service = _get_query_service()
    page = service.query(
        EventQuery(
            case_id=case_id,
            timeline_id=timeline_id,
            q=q,
            source=source,
            tag=tag,
            exclude_tag=exclude_tag,
            start=start,
            end=end,
            field_filters=_parse_json_object(filters),
            field_exclusions=_parse_json_object(exclusions),
            limit=limit,
            offset=offset,
            order=order,  # type: ignore[arg-type]
        )
    )
    return {
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "events": page.events,
    }


@router.get("/{case_id}/timelines/{timeline_id}/fields")
async def list_fields(
    case_id: str,
    timeline_id: str,
) -> dict[str, Any]:
    """Return the displayable field names for a timeline.

    ``top_level`` contains the fixed columns common to every event.
    ``attributes`` contains the dynamic keys aggregated from the ``attributes``
    Map across a sample of up to 50 000 events.  Useful for building a column
    picker in the UI.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    service = _get_query_service()
    return service.list_fields(case_id, timeline_id)


@router.get("/{case_id}/timelines/{timeline_id}/embedding-fields")
async def list_embedding_fields(
    case_id: str,
    timeline_id: str,
) -> dict[str, Any]:
    """Return per-source field information for the embedding wizard.

    For each distinct ``source`` in the timeline, returns the event count,
    the embeddable top-level fields, available attribute keys, and a
    recommended preselection.  Used by the frontend embedding wizard to
    let analysts choose which fields of which sources to embed.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    service = _get_query_service()
    return service.list_fields_by_source(case_id, timeline_id)


@router.get("/{case_id}/timelines/{timeline_id}/histogram")
async def get_histogram(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(default=None),
    source: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    exclude_tag: str | None = Query(default=None),
    start: datetime | None = Query(default=None),  # noqa: B008
    end: datetime | None = Query(default=None),  # noqa: B008
    filters: str | None = Query(default=None),
    exclusions: str | None = Query(default=None),
    buckets: int = Query(default=60, ge=10, le=200),
) -> dict[str, Any]:
    """Return a bucketed event-count histogram for a timeline.

    Honors the same filter params as the events list endpoint so the histogram
    always reflects the currently-filtered view.  ``buckets`` controls the
    target number of time buckets (10–200, default 60); the actual interval is
    ``max(1, duration / buckets)`` seconds.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    service = _get_query_service()
    return service.histogram(
        EventQuery(
            case_id=case_id,
            timeline_id=timeline_id,
            q=q,
            source=source,
            tag=tag,
            exclude_tag=exclude_tag,
            start=start,
            end=end,
            field_filters=_parse_json_object(filters),
            field_exclusions=_parse_json_object(exclusions),
        ),
        buckets=buckets,
    )


# ── Export models ─────────────────────────────────────────────────────────────


class ExportFilter(BaseModel):
    """Filter parameters for event export."""

    q: str | None = None
    source: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    # 'fields' / 'exclude' map to field_filters / field_exclusions in EventQuery.
    fields: dict[str, str] = Field(default_factory=dict)
    exclude: dict[str, str] = Field(default_factory=dict)


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
    "source",
    "source_long",
    "display_name",
    "message",
    "tags",
    "attributes",
]


def _stream_jsonl(query: EventQuery) -> Generator[str]:
    """Yield one JSONL line per matching event."""
    service = EventQueryService()
    for event in service.iter_events(query):
        yield json.dumps(event, default=str) + "\n"


def _stream_csv(query: EventQuery) -> Generator[str]:
    """Yield CSV rows for all matching events (header first)."""
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
) -> StreamingResponse:
    """Stream all events matching the given filters as CSV or JSONL."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    eq = EventQuery(
        case_id=case_id,
        timeline_id=timeline_id,
        q=body.filter.q,
        source=body.filter.source,
        tag=body.filter.tag,
        exclude_tag=body.filter.exclude_tag,
        start=body.filter.start,
        end=body.filter.end,
        field_filters=body.filter.fields,
        field_exclusions=body.filter.exclude,
    )

    if body.format == "jsonl":
        media_type = "application/x-ndjson"
        ext = "jsonl"
        content = _stream_jsonl(eq)
    else:
        media_type = "text/csv"
        ext = "csv"
        content = _stream_csv(eq)

    filename = f"{case_id}-{timeline_id}-events.{ext}"
    return StreamingResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Similarity / outlier endpoints ───────────────────────────────────────────

_similarity_service: SimilarityService | None = None


def _get_similarity_service() -> SimilarityService:
    global _similarity_service  # noqa: PLW0603
    if _similarity_service is None:
        _similarity_service = SimilarityService()
    return _similarity_service


@router.get("/{case_id}/timelines/{timeline_id}/events/{event_id}/similar")
async def find_similar_events(
    case_id: str,
    timeline_id: str,
    event_id: str,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Return events semantically similar to ``event_id`` using vector search.

    Requires embeddings to have been generated for the timeline.  Returns
    ``status="not_embedded"`` when no vectors exist, ``status="vector_not_found"``
    when the specific event has no vector.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    svc = _get_similarity_service()
    result = svc.find_similar(case_id, timeline_id, event_id, limit=limit)
    return {
        "status": result.status,
        "results": [
            {"event_id": r.event_id, "score": r.score, "event": r.event}
            for r in result.results
        ],
    }


@router.get("/{case_id}/timelines/{timeline_id}/anomalies")
async def list_anomalies(
    case_id: str,
    timeline_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    sample_size: int = Query(default=5000, ge=10, le=100000),
) -> dict[str, Any]:
    """Return the most unusual events in a timeline (read-only preview).

    Uses distance-to-centroid scoring over the timeline's vector embeddings.
    Results are statistical outliers — *rare* lines, not necessarily malicious.
    Requires embeddings to have been generated first.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    svc = _get_similarity_service()
    # Resolve normal IDs here (in async context) before calling sync find_anomalies.
    normal_ids = await store.list_event_ids_by_annotation_type(
        case_id, timeline_id, "normal"
    )
    result = svc.find_anomalies(
        case_id, timeline_id, limit=limit, sample_size=sample_size,
        normal_ids=normal_ids,
    )
    return {
        "status": result.status,
        "method": result.method,
        "sample_size": result.sample_size,
        "baseline_size": result.baseline_size,
        "embedding_config_hash": result.embedding_config_hash,
        "results": [
            {
                "event_id": r.event_id,
                "score": r.score,
                "event": r.event,
                "details": r.details,
            }
            for r in result.results
        ],
    }


class TagAnomaliesRequest(BaseModel):
    """Request body for the tag-outliers endpoint."""

    limit: int = Field(default=50, ge=1, le=500)
    sample_size: int = Field(default=5000, ge=10, le=100000)


@router.post("/{case_id}/timelines/{timeline_id}/anomalies/tag")
async def tag_anomalies(
    case_id: str,
    timeline_id: str,
    body: TagAnomaliesRequest,
) -> dict[str, Any]:
    """Re-compute outliers and persist them as system annotations.

    Clears any existing system outlier annotations for the timeline first,
    so repeated calls replace rather than accumulate results.  Each outlier
    receives:
    - ``annotation_type="outlier"`` / ``origin="system"``
    - A human-readable ``content`` summarising the score.
    - A structured ``details`` JSON with the raw math for the Analysis panel.

    Returns the same result shape as ``GET /anomalies`` plus a ``tagged`` count.
    """
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    svc = _get_similarity_service()
    normal_ids = await store.list_event_ids_by_annotation_type(
        case_id, timeline_id, "normal"
    )
    result = svc.find_anomalies(
        case_id, timeline_id, limit=body.limit, sample_size=body.sample_size,
        normal_ids=normal_ids,
    )

    if result.status != "ok":
        return {
            "status": result.status,
            "method": result.method,
            "tagged": 0,
            "sample_size": result.sample_size,
            "baseline_size": result.baseline_size,
            "embedding_config_hash": result.embedding_config_hash,
            "results": [],
        }

    # Clear prior system outlier annotations for this timeline.
    await store.delete_system_annotations(case_id, timeline_id, "outlier")

    # Write one system annotation per outlier.
    rows = []
    for r in result.results:
        d = r.details
        method = d.get("method", "centroid-distance")
        if method == "normal-baseline":
            content = (
                f"Outlier — distance {d['distance']:.4f} from analyst-defined normal baseline "
                f"({d['baseline_size']} normal events, rank {d['rank']}/{d['of']})"
            )
        else:
            content = (
                f"Outlier — cosine distance {d['distance']:.4f} from timeline centroid "
                f"(rank {d['rank']}/{d['of']}, sample {d.get('sample_size', '?')})"
            )
        rows.append(
            {
                "annotation_id": generate_id(f"{r.event_id}_outlier"),
                "case_id": case_id,
                "timeline_id": timeline_id,
                "event_id": r.event_id,
                "annotation_type": "outlier",
                "content": content,
                "origin": "system",
                "details": d,
            }
        )

    tagged = await store.bulk_create_annotations(rows)

    return {
        "status": "ok",
        "method": result.method,
        "tagged": tagged,
        "sample_size": result.sample_size,
        "baseline_size": result.baseline_size,
        "embedding_config_hash": result.embedding_config_hash,
        "results": [
            {
                "event_id": r.event_id,
                "score": r.score,
                "event": r.event,
                "details": r.details,
            }
            for r in result.results
        ],
    }
