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

from tracevector.core.config import get_settings
from tracevector.db.anomaly_stats import FreqFinding, StatisticalAnomalyService, ValueFinding
from tracevector.db.postgres import PostgresStore, generate_id
from tracevector.db.queries import EventQuery, EventQueryService
from tracevector.db.similarity import SimilarityService

_query_service: EventQueryService | None = None


def _get_query_service() -> EventQueryService:
    global _query_service  # noqa: PLW0603
    if _query_service is None:
        _query_service = EventQueryService()
    return _query_service


_embedding_model: Any = None


def _get_field_encoder() -> Any:
    """Return the embedding ``encode`` callable for wizard field pairing.

    Cached across calls so the model loads at most once.  Returns ``None`` when
    the model cannot be loaded (e.g. airgapped without cached weights), which
    degrades the wizard gracefully to heuristic-only recommendations.
    """
    global _embedding_model  # noqa: PLW0603
    if _embedding_model is None:
        try:
            from tracevector.models.embeddings import EmbeddingModel

            _embedding_model = EmbeddingModel()
            _embedding_model.load()
        except Exception:  # noqa: BLE001
            return None
    return _embedding_model.encode


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


def _parse_exclusions_object(value: str | None) -> dict[str, list[str]]:
    """Parse a JSON string into a string-to-list[str] dict for exclusion filters.

    Accepts both ``{"key": "value"}`` (legacy single-value) and
    ``{"key": ["v1", "v2"]}`` (multi-value distillation).
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
    result: dict[str, list[str]] = {}
    for k, v in parsed.items():
        if isinstance(v, list):
            result[str(k)] = [str(item) for item in v]
        else:
            result[str(k)] = [str(v)]
    return result


async def _resolve_timeline_source_ids(case_id: str, timeline_id: str) -> list[str]:
    """Return the source IDs attached to a timeline."""
    store = get_store()
    timeline = await store.get_timeline(case_id, timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    sources = await store.list_timeline_sources(case_id, timeline_id)
    return [s.id for s in sources]


@router.get("/{case_id}/timelines/{timeline_id}/events")
async def list_events(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(default=None, description="Full-text search in message"),
    artifact: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
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

    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)

    service = _get_query_service()
    page = service.query(
        EventQuery(
            case_id=case_id,
            source_ids=source_ids,
            q=q,
            artifact=artifact,
            source_id=source_id,
            tag=tag,
            exclude_tag=exclude_tag,
            start=start,
            end=end,
            field_filters=_parse_json_object(filters),
            field_exclusions=_parse_exclusions_object(exclusions),
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


class BulkAnnotateByFilterRequest(BaseModel):
    annotation_type: str = Field(
        ..., description="Annotation type: 'tag', 'comment', or 'normal'."
    )
    content: str = Field(..., min_length=1, max_length=4096)
    q: str | None = None
    artifact: str | None = None
    source_id: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
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


@router.post("/{case_id}/timelines/{timeline_id}/events/annotations/bulk")
async def bulk_annotate_by_filter(
    case_id: str,
    timeline_id: str,
    body: BulkAnnotateByFilterRequest,
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

    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)

    service = _get_query_service()
    refs = service.query_event_refs(
        EventQuery(
            case_id=case_id,
            source_ids=source_ids,
            q=body.q,
            artifact=body.artifact,
            source_id=body.source_id,
            tag=body.tag,
            exclude_tag=body.exclude_tag,
            start=body.start,
            end=body.end,
            field_filters=_parse_json_object(body.filters),
            field_exclusions=_parse_exclusions_object(body.exclusions),
        )
    )

    if not refs:
        return {"tagged": 0}

    store = get_store()
    rows = [
        {
            "annotation_id": generate_id(f"{event_id}_{body.annotation_type}"),
            "case_id": case_id,
            "source_id": str(src_id),
            "event_id": str(event_id),  # ClickHouse may return UUID objects
            "annotation_type": body.annotation_type,
            "content": body.content.strip(),
            "origin": "user",
        }
        for event_id, src_id in refs
    ]
    tagged = await store.bulk_create_annotations(rows)
    return {"tagged": tagged}


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
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    service = _get_query_service()
    return service.list_fields(case_id, source_ids)


@router.get("/{case_id}/timelines/{timeline_id}/embedding-fields")
async def list_embedding_fields(
    case_id: str,
    timeline_id: str,
) -> dict[str, Any]:
    """Return per-artifact field information for the embedding wizard.

    For each distinct ``artifact`` across the timeline's sources, returns the
    event count, the embeddable top-level fields, available attribute keys, and
    a recommended preselection.  Used by the frontend embedding wizard to let
    analysts choose which fields of which artifacts to embed.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    service = _get_query_service()
    return service.list_fields_by_artifact(
        case_id, source_ids, encode=_get_field_encoder()
    )


@router.get("/{case_id}/sources/{source_id}/embedding-fields")
async def list_source_embedding_fields(
    case_id: str,
    source_id: str,
) -> dict[str, Any]:
    """Per-artifact field recommendations for a single source's embedding wizard.

    Same payload as the timeline-scoped endpoint but scoped to one source, which
    is the unit the embed job operates on.  Runs the hybrid heuristic→pairs
    recommender; field pairing degrades to heuristic-only if the model can't load.
    """
    service = _get_query_service()
    return service.list_fields_by_artifact(
        case_id, [source_id], encode=_get_field_encoder()
    )


@router.get("/{case_id}/timelines/{timeline_id}/histogram")
async def get_histogram(
    case_id: str,
    timeline_id: str,
    q: str | None = Query(default=None),
    artifact: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
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
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    service = _get_query_service()
    return service.histogram(
        EventQuery(
            case_id=case_id,
            source_ids=source_ids,
            q=q,
            artifact=artifact,
            source_id=source_id,
            tag=tag,
            exclude_tag=exclude_tag,
            start=start,
            end=end,
            field_filters=_parse_json_object(filters),
            field_exclusions=_parse_exclusions_object(exclusions),
        ),
        buckets=buckets,
    )


# ── Export models ─────────────────────────────────────────────────────────────


class ExportFilter(BaseModel):
    """Filter parameters for event export."""

    q: str | None = None
    artifact: str | None = None
    source_id: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    # 'fields' / 'exclude' map to field_filters / field_exclusions in EventQuery.
    fields: dict[str, str] = Field(default_factory=dict)
    exclude: dict[str, list[str]] = Field(default_factory=dict)


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
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)

    eq = EventQuery(
        case_id=case_id,
        source_ids=source_ids,
        q=body.filter.q,
        artifact=body.filter.artifact,
        source_id=body.filter.source_id,
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


@router.get("/{case_id}/timelines/{timeline_id}/events/{event_id}/similar")
async def find_similar_events(
    case_id: str,
    timeline_id: str,
    event_id: str,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Return events semantically similar to ``event_id`` using vector search.

    Requires embeddings to have been generated for the timeline's sources.
    Returns ``status="not_embedded"`` when no vectors exist,
    ``status="vector_not_found"`` when the specific event has no vector.
    """
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    svc = _get_similarity_service()
    result = svc.find_similar(case_id, source_ids, event_id, limit=limit)
    return {
        "status": result.status,
        "results": [
            {"event_id": r.event_id, "score": r.score, "event": r.event}
            for r in result.results
        ],
    }


def _serialize_finding(r: ValueFinding | FreqFinding) -> dict[str, Any]:
    """Serialise a ValueFinding or FreqFinding to a JSON-safe dict."""
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


@router.get("/{case_id}/timelines/{timeline_id}/anomalies")
async def list_anomalies(
    case_id: str,
    timeline_id: str,
    detector: str = Query(
        default="value_novelty",
        description="Detector to run: 'value_novelty' or 'frequency'.",
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated field tokens for value_novelty "
            "(e.g. 'artifact,display_name,attr:user_agent'). "
            "Defaults to artifact, timestamp_desc, display_name."
        ),
    ),
    series_field: str = Query(
        default="artifact",
        description="Field to group frequency series by.",
    ),
    baseline_start: datetime | None = Query(  # noqa: B008
        default=None,
        description="Temporal baseline end timestamp (detect window = after this).",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Run a statistical anomaly detector on the timeline and return findings.

    No embeddings required — operates on already-ingested ClickHouse data.

    **value_novelty**: flags rare or first-seen field values, ranked by surprise
    score (-log frequency).  Works immediately after ingestion.

    **frequency**: flags time windows with anomalous event-count z-scores per
    field-value series.
    """
    cfg = get_settings()
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    svc = _get_stat_anomaly_service()

    if detector == "frequency":
        result = svc.find_frequency_anomalies(
            case_id=case_id,
            source_ids=source_ids,
            series_field=series_field,
            limit=limit,
            bucket_count=cfg.stat_frequency_buckets,
            z_threshold=cfg.stat_z_threshold,
            baseline_end=baseline_start,
        )
    else:
        parsed_fields = (
            [f.strip() for f in fields.split(",") if f.strip()]
            if fields
            else None
        )
        result = svc.find_value_novelty(
            case_id=case_id,
            source_ids=source_ids,
            fields=parsed_fields,
            limit=limit,
            rarity_floor=cfg.stat_rarity_floor,
            baseline_end=baseline_start,
            per_field_limit=cfg.stat_per_field_limit,
        )

    return {
        "status": result.status,
        "detector": result.detector,
        "method": result.method,
        "baseline_size": result.baseline_size,
        "results": [_serialize_finding(r) for r in result.results],
    }


class TagAnomaliesRequest(BaseModel):
    """Request body for the tag-anomalies endpoint."""

    detector: str = Field(
        default="value_novelty",
        description="Detector to run: 'value_novelty' or 'frequency'.",
    )
    fields: str | None = Field(
        default=None,
        description="Comma-separated field tokens for value_novelty.",
    )
    series_field: str = Field(
        default="artifact",
        description="Field to group frequency series by.",
    )
    baseline_start: datetime | None = Field(
        default=None,
        description="Temporal baseline end timestamp.",
    )
    limit: int = Field(default=50, ge=1, le=500)


@router.post("/{case_id}/timelines/{timeline_id}/anomalies/tag")
async def tag_anomalies(
    case_id: str,
    timeline_id: str,
    body: TagAnomaliesRequest,
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
    cfg = get_settings()
    store = get_store()
    source_ids = await _resolve_timeline_source_ids(case_id, timeline_id)
    svc = _get_stat_anomaly_service()

    if body.detector == "frequency":
        result = svc.find_frequency_anomalies(
            case_id=case_id,
            source_ids=source_ids,
            series_field=body.series_field,
            limit=body.limit,
            bucket_count=cfg.stat_frequency_buckets,
            z_threshold=cfg.stat_z_threshold,
            baseline_end=body.baseline_start,
        )
    else:
        parsed_fields = (
            [f.strip() for f in body.fields.split(",") if f.strip()]
            if body.fields
            else None
        )
        result = svc.find_value_novelty(
            case_id=case_id,
            source_ids=source_ids,
            fields=parsed_fields,
            limit=body.limit,
            rarity_floor=cfg.stat_rarity_floor,
            baseline_end=body.baseline_start,
            per_field_limit=cfg.stat_per_field_limit,
        )

    if result.status != "ok":
        return {
            "status": result.status,
            "detector": result.detector,
            "method": result.method,
            "tagged": 0,
            "baseline_size": result.baseline_size,
            "results": [],
        }

    # Clear prior system anomaly annotations for this timeline's sources.
    await store.delete_system_annotations(case_id, source_ids, "anomaly")

    # Write one system annotation per finding.
    annotation_rows = []
    for r in result.results:
        if isinstance(r, ValueFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            content = (
                f"Rare value — {r.field}={r.value!r} "
                f"(count {r.count}, surprise {r.score:.2f})"
            )
        else:
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            content = (
                f"Frequency spike — {r.series_field}={r.series_value!r} "
                f"at {r.window_start}: {r.observed} events "
                f"(expected {r.expected:.1f}, z={r.z_score:.2f})"
            )
        if not event_id:
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
            }
        )

    tagged = await store.bulk_create_annotations(annotation_rows) if annotation_rows else 0

    return {
        "status": "ok",
        "detector": result.detector,
        "method": result.method,
        "tagged": tagged,
        "baseline_size": result.baseline_size,
        "results": [_serialize_finding(r) for r in result.results],
    }
