"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from tracevector.db.clickhouse import ClickHouseStore
from tracevector.db.field_recommend import (
    recommend_fields,
    recommend_fields_across_sources,
    timeline_cohesion_summary,
    timeline_universal_cohesion,
)


@dataclass
class EventQuery:
    """Query parameters for the event viewer."""

    case_id: str
    source_ids: list[str] | None = None
    q: str | None = None
    artifact: str | None = None
    source_id: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    field_filters: dict[str, str] = field(default_factory=dict)
    field_exclusions: dict[str, list[str]] = field(default_factory=dict)
    # Optional event_id allowlist (e.g. resolved from an annotation filter).
    # None means "no restriction"; an empty list matches zero events.
    event_ids: list[str] | None = None
    limit: int = 50
    offset: int = 0
    order: Literal["asc", "desc"] = "desc"


def _iter_attr_items(attrs: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(key, value)`` from a ClickHouse Map column.

    clickhouse-connect returns Map columns as a ``dict``, but tolerate a list of
    pairs as well so the caller never has to care about the driver shape.
    """
    if isinstance(attrs, dict):
        yield from attrs.items()
    elif isinstance(attrs, (list, tuple)):
        for item in attrs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                yield item[0], item[1]


@dataclass
class EventPage:
    """Paginated event query result."""

    total: int
    offset: int
    limit: int
    events: list[dict[str, Any]]


# Columns that exist directly on the events table. Any other field key is
# treated as a key in the ``attributes`` Map column.
_TOP_LEVEL_FILTER_COLUMNS = frozenset(
    {
        "message",
        "timestamp",
        "timestamp_desc",
        "artifact",
        "artifact_long",
        "display_name",
        "parser_name",
        "parser_version",
        "source_file",
        "source_id",
        "content_hash",
        "file_hash",
    }
)

# Top-level columns surfaced as choosable display columns in the UI.
# Separate from _TOP_LEVEL_FILTER_COLUMNS (which is for filter routing).
TOP_LEVEL_DISPLAY_COLUMNS = [
    "timestamp",
    "source_id",
    "artifact",
    "artifact_long",
    "display_name",
    "message",
    "timestamp_desc",
    "tags",
    "_annotations",
]


def _format_clickhouse_datetime(value: datetime) -> str:
    """Format a datetime for ClickHouse SQL."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_event_datetimes(row: dict[str, Any]) -> dict[str, Any]:
    """Attach an explicit UTC offset to an event row's timestamp columns.

    The `events` table's `timestamp`/`ingest_time` columns have no explicit
    timezone component, so clickhouse-connect returns naive `datetime`
    objects for them. Left as-is, FastAPI's JSON encoder calls `.isoformat()`
    on the naive value, which omits the timezone offset — and a bare
    "YYYY-MM-DDTHH:MM:SS" string is ambiguous to JS's `Date` parser (browsers
    treat it as local time), silently shifting every event's displayed and
    compared timestamp by the browser's UTC offset.
    """
    for key in ("timestamp", "ingest_time"):
        value = row.get(key)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            row[key] = value.isoformat()
    return row


# Columns selected in every event query (shared between paginated query and export).
_EVENT_SELECT_COLUMNS = """
    event_id,
    case_id,
    source_id,
    source_file,
    byte_offset,
    line_number,
    content_hash,
    file_hash,
    parser_name,
    parser_version,
    ingest_time,
    message,
    timestamp,
    timestamp_desc,
    artifact,
    artifact_long,
    display_name,
    tags,
    attributes,
    embedding_model,
    embedding_config_hash,
    vector_id
"""


class _ParameterizedQueryBuilder:
    """Build a ClickHouse WHERE clause using named parameters."""

    def __init__(self) -> None:
        self.conditions: list[str] = []
        self.parameters: dict[str, Any] = {}
        self._counter = 0

    def _param_name(self) -> str:
        name = f"p{self._counter}"
        self._counter += 1
        return name

    def add(self, condition: str) -> None:
        """Add a raw condition that does not need parameterization."""
        self.conditions.append(condition)

    def add_param(self, sql_fragment: str, value: Any) -> None:
        """Add a condition containing exactly one ':name' placeholder."""
        name = self._param_name()
        self.conditions.append(sql_fragment.replace(":name", f"{{{name}:String}}"))
        self.parameters[name] = value

    def add_in_list(self, column: str, values: list[str]) -> None:
        """Add a membership condition for a list of string values.

        Uses ``has({arr:Array(String)}, toString(column))`` rather than
        ``column IN ({p0}, {p1}, ...)`` because ClickHouse 24.x requires the
        second argument of ``IN`` to be a constant or table expression — a list
        of individual parameterized strings does not qualify.

        The column is wrapped in ``toString()`` because this is also used for
        ``event_id``, a native ``UUID`` column — ``has()`` requires a common
        type between the array and the column, and there is no implicit
        common type between ``Array(String)`` and ``UUID`` (this fails with
        ClickHouse error 386 NO_COMMON_TYPE), even when the array is empty.
        """
        name = self._param_name()
        self.conditions.append(f"has({{{name}:Array(String)}}, toString({column}))")
        self.parameters[name] = values

    def add_field_filter(self, key: str, value: str) -> None:
        """Add an equality filter on a top-level column or attribute."""
        column = self._column_expr(key)
        self.add_param(f"{column} = :name", value)

    def add_field_exclusion(self, key: str, values: list[str]) -> None:
        """Add a NOT IN exclusion on a top-level column or attribute."""
        column = self._column_expr(key)
        if len(values) == 1:
            self.add_param(f"{column} != :name", values[0])
        else:
            name = self._param_name()
            self.conditions.append(f"{column} NOT IN {{{name}:Array(String)}}")
            self.parameters[name] = values

    def add_tag_exclusion(self, value: str) -> None:
        """Exclude events that have *value* in their tags array."""
        self.add_param("NOT has(tags, :name)", value)

    def _column_expr(self, key: str) -> str:
        normalized = key.strip().lower()
        if normalized in _TOP_LEVEL_FILTER_COLUMNS:
            return normalized
        # Map lookup; parameterize the key as well to stay defensive.
        key_param = self._param_name()
        self.parameters[key_param] = key
        return f"attributes[{{{key_param}:String}}]"

    def where_clause(self) -> str:
        return " AND ".join(self.conditions)


class EventQueryService:
    """Query service for events stored in ClickHouse."""

    def __init__(self, store: ClickHouseStore | None = None) -> None:
        self.store = store or ClickHouseStore()

    def _build_where(self, query: EventQuery) -> tuple[str, dict[str, Any]]:
        """Build the parameterized WHERE clause for *query*.

        Returns the clause string and the bound parameters dict.
        Both are consumed by :py:meth:`query` (paginated) and
        :py:meth:`iter_events` (streaming export).
        """
        builder = _ParameterizedQueryBuilder()
        builder.add_param("case_id = :name", query.case_id)

        if query.source_ids is not None:
            builder.add_in_list("source_id", query.source_ids)

        if query.source_id is not None:
            builder.add_param("source_id = :name", query.source_id)

        if query.q:
            # ClickHouse tokenbf_v1 index supports hasToken and multiSearchAny.
            # We use ILIKE for substring search as a simple baseline.
            builder.add_param("message ILIKE :name", f"%{query.q}%")

        if query.artifact:
            builder.add_param("artifact = :name", query.artifact)

        if query.tag:
            builder.add_param("has(tags, :name)", query.tag)

        if query.exclude_tag:
            builder.add_tag_exclusion(query.exclude_tag)

        if query.start is not None:
            builder.add_param(
                "timestamp >= :name",
                _format_clickhouse_datetime(query.start),
            )

        if query.end is not None:
            builder.add_param(
                "timestamp <= :name",
                _format_clickhouse_datetime(query.end),
            )

        if query.event_ids is not None:
            builder.add_in_list("event_id", query.event_ids)

        for key, value in (query.field_filters or {}).items():
            builder.add_field_filter(key, value)

        for key, values in (query.field_exclusions or {}).items():
            builder.add_field_exclusion(key, values)

        return builder.where_clause(), builder.parameters

    def query(self, query: EventQuery) -> EventPage:
        """Execute an :py:class:`EventQuery` and return a paginated result."""
        self.store.init_schema()

        where, parameters = self._build_where(query)
        database = self.store.database

        count_result = self.store.client.query(
            f"SELECT count() FROM {database}.events WHERE {where}",
            parameters=parameters,
        )
        total = count_result.result_rows[0][0] if count_result.result_rows else 0

        sort_dir = query.order.upper()
        event_result = self.store.client.query(
            f"""
            SELECT {_EVENT_SELECT_COLUMNS}
            FROM {database}.events
            WHERE {where}
            ORDER BY timestamp {sort_dir}, event_id
            LIMIT {query.limit}
            OFFSET {query.offset}
            """,
            parameters=parameters,
        )

        columns = event_result.column_names
        events = [
            _normalize_event_datetimes(dict(zip(columns, row, strict=False)))
            for row in event_result.result_rows
        ]

        return EventPage(
            total=total,
            offset=query.offset,
            limit=query.limit,
            events=events,
        )

    def iter_events(
        self, query: EventQuery, batch_size: int = 1000
    ) -> Iterator[dict[str, Any]]:
        """Yield every event matching *query*, paging through ClickHouse in batches.

        This is used for streaming export where the full result set should not
        be materialised in memory.  The ``limit`` and ``offset`` fields of
        *query* are ignored — all matching rows are yielded.
        """
        self.store.init_schema()

        where, parameters = self._build_where(query)
        database = self.store.database
        sort_dir = query.order.upper()
        offset = 0

        while True:
            result = self.store.client.query(
                f"""
                SELECT {_EVENT_SELECT_COLUMNS}
                FROM {database}.events
                WHERE {where}
                ORDER BY timestamp {sort_dir}, event_id
                LIMIT {batch_size}
                OFFSET {offset}
                """,
                parameters=parameters,
            )
            columns = result.column_names
            rows = result.result_rows
            for row in rows:
                yield _normalize_event_datetimes(dict(zip(columns, row, strict=False)))
            if len(rows) < batch_size:
                break
            offset += batch_size

    def query_event_refs(
        self, query: EventQuery, cap: int = 100_000
    ) -> list[tuple[str, str]]:
        """Return (event_id, source_id) pairs for all events matching *query*.

        Like :py:meth:`query` but only fetches the two identifier columns,
        making it suitable for server-side bulk annotation.  ``limit`` and
        ``offset`` on *query* are ignored — the full matching set is returned
        up to *cap* rows to bound runaway writes.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database

        result = self.store.client.query(
            f"SELECT event_id, source_id FROM {database}.events WHERE {where} LIMIT {cap}",
            parameters=parameters,
        )
        return [(row[0], row[1]) for row in result.result_rows]

    def list_fields(
        self, case_id: str, source_ids: list[str]
    ) -> dict[str, list[str]]:
        """Return the displayable field names for a timeline.

        ``top_level`` contains the fixed columns common to every event.
        ``attributes`` contains the dynamic keys aggregated from the ``attributes``
        Map across a sample of up to 50 000 events.  Useful for building a column
        picker in the UI.
        """
        self.store.init_schema()
        database = self.store.database

        params: dict[str, Any] = {"p0": case_id, "src": source_ids}

        result = self.store.client.query(
            f"""
            SELECT groupUniqArrayArray(mapKeys(attributes)) AS keys
            FROM {database}.events
            WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
            """,
            parameters=params,
        )
        raw_keys: list[str] = result.result_rows[0][0] if result.result_rows else []
        return {
            "top_level": TOP_LEVEL_DISPLAY_COLUMNS,
            "attributes": sorted(raw_keys),
        }

    # Top-level fields meaningful for embedding (not IDs/provenance).
    _EMBEDDABLE_TOP_LEVEL = [
        "message",
        "timestamp_desc",
        "artifact_long",
        "display_name",
        "tags",
    ]

    def list_fields_by_artifact(
        self,
        case_id: str,
        source_ids: list[str],
        *,
        encode: Callable[[list[str]], list[list[float]]] | None = None,
        sample_per_artifact: int = 400,
    ) -> dict[str, Any]:
        """Return per-artifact field information for the embedding wizard.

        For each distinct ``artifact`` across the sources, returns the event
        count, the available top-level embedding fields, the dynamic attribute
        keys, and a *content-aware* recommendation produced by the hybrid
        heuristic→pairs strategy (see :mod:`tracevector.db.field_recommend`).

        When multiple sources are passed the recommendation uses
        :func:`~tracevector.db.field_recommend.recommend_fields_across_sources`
        which applies cross-source cohesion scoring so that the wizard
        default-selects only fields that carry **comparable content across all
        sources** (avoiding the batch-effect where embedding space separates
        events by source format rather than behaviour).

        The top-level ``cohesion`` key summarises the timeline's embedding
        substrate quality: ``"strong"`` / ``"moderate"`` / ``"weak"`` /
        ``"unavailable"``.

        Per-field verdicts now include ``present_in_sources`` and ``cohesion``
        when the multi-source path is used.

        ``encode`` is the embedding callable; pass ``None`` for heuristic-only.
        """
        self.store.init_schema()
        database = self.store.database

        params: dict[str, Any] = {"p0": case_id, "src": source_ids, "per": sample_per_artifact}

        # 1. Full attribute-key inventory + event count per artifact.
        inv = self.store.client.query(
            f"""
            SELECT
                artifact,
                count() AS n,
                groupUniqArrayArray(mapKeys(attributes)) AS attr_keys
            FROM {database}.events
            WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
            GROUP BY artifact
            ORDER BY n DESC
            """,
            parameters=params,
        )
        inventory = {
            (row[0] or ""): (row[1], sorted(row[2]) if row[2] else [])
            for row in inv.result_rows
        }

        # 2. Randomised value sample per artifact **and source** so that
        #    cross-source cohesion can be computed per field.
        cols = ["message", "timestamp_desc", "artifact_long", "display_name", "tags"]
        sample = self.store.client.query(
            f"""
            SELECT source_id, artifact, {", ".join(cols)}, attributes
            FROM (
                SELECT source_id, artifact, {", ".join(cols)}, attributes,
                       row_number() OVER (
                           PARTITION BY artifact, source_id ORDER BY rand()
                       ) AS _rn
                FROM (
                    SELECT source_id, artifact, {", ".join(cols)}, attributes
                    FROM {database}.events
                    WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
                    LIMIT 200000
                )
            )
            WHERE _rn <= {{per:UInt32}}
            """,
            parameters=params,
        )

        is_multi_source = len(source_ids) > 1

        # artifact -> source_id -> token -> list of sampled values  (multi-source)
        # artifact -> token -> list of sampled values               (single-source)
        samples_by_src: dict[str, dict[str, dict[str, list[Any]]]] = {}
        samples_flat: dict[str, dict[str, list[Any]]] = {}
        for row in sample.result_rows:
            src_id = row[0]
            artifact_name = row[1] or ""
            src_bucket = samples_by_src.setdefault(artifact_name, {}).setdefault(src_id, {})
            flat_bucket = samples_flat.setdefault(artifact_name, {})
            for i, col in enumerate(cols, start=2):
                value = row[i]
                if col == "tags" and isinstance(value, (list, tuple)):
                    value = " ".join(str(t) for t in value)
                src_bucket.setdefault(col, []).append(value)
                flat_bucket.setdefault(col, []).append(value)
            attrs = row[len(cols) + 2]
            for key, value in _iter_attr_items(attrs):
                src_bucket.setdefault(f"attr:{key}", []).append(value)
                flat_bucket.setdefault(f"attr:{key}", []).append(value)

        artifacts = []
        all_verdicts_for_cohesion = []

        for artifact_name, (count, attr_keys) in inventory.items():
            if is_multi_source:
                # Build field_samples_by_source: source_id → token → values.
                # Seed every candidate token for every source so absent fields
                # still get verdicts with present_in_sources=0.
                all_tokens = list(self._EMBEDDABLE_TOP_LEVEL) + [
                    f"attr:{k}" for k in attr_keys
                ]
                src_samples: dict[str, dict[str, list[Any]]] = {
                    src_id: {
                        token: samples_by_src.get(artifact_name, {})
                        .get(src_id, {})
                        .get(token, [])
                        for token in all_tokens
                    }
                    for src_id in source_ids
                }
                rec = recommend_fields_across_sources(
                    src_samples,
                    source_count=len(source_ids),
                    encode=encode,
                )
                all_verdicts_for_cohesion.extend(rec.verdicts)
                field_analysis = [
                    {
                        "token": v.token,
                        "recommended": v.recommended,
                        "kind": v.kind,
                        "reason": v.reason,
                        "present_in_sources": v.present_in_sources,
                        "cohesion": v.cohesion,
                    }
                    for v in rec.verdicts
                ]
            else:
                # Single source — use the original per-artifact recommender.
                flat_bucket = samples_flat.get(artifact_name, {})
                field_samples: dict[str, list[Any]] = {
                    t: flat_bucket.get(t, []) for t in self._EMBEDDABLE_TOP_LEVEL
                }
                for key in attr_keys:
                    token = f"attr:{key}"
                    field_samples[token] = flat_bucket.get(token, [])
                rec_single = recommend_fields(field_samples, encode=encode)
                field_analysis = [
                    {
                        "token": v.token,
                        "recommended": v.recommended,
                        "kind": v.kind,
                        "reason": v.reason,
                        "present_in_sources": 1,
                        "cohesion": None,
                    }
                    for v in rec_single.verdicts
                ]
                rec = rec_single  # for recommended / related_groups below

            artifacts.append(
                {
                    "artifact": artifact_name,
                    "count": count,
                    "top_level": self._EMBEDDABLE_TOP_LEVEL,
                    "attributes": attr_keys,
                    "recommended": rec.recommended,
                    "field_analysis": field_analysis,
                    "related_groups": rec.related_groups,
                }
            )

        # Aggregate cross-source cohesion summary for the whole timeline.
        #
        # Per-artifact cohesion (all_verdicts_for_cohesion) only sees a field
        # as "shared" when the *same* artifact type appears in ≥2 sources.
        # For timelines with disjoint artifact sets this always yields zero
        # shared fields, producing a spurious "weak" verdict.
        #
        # Instead we use timeline_universal_cohesion: pool each source's
        # values across ALL its artifacts for the canonical top-level fields
        # (message, display_name, tags, timestamp_desc) and compute cohesion
        # there.  These fields exist in every Timesketch source regardless of
        # artifact type, so they provide an honest cross-source signal.
        if is_multi_source:
            # Build source_id -> token -> [values] pooled across all artifacts.
            pooled_by_source: dict[str, dict[str, list[Any]]] = {}
            for artifact_name, src_map in samples_by_src.items():
                for src_id, token_map in src_map.items():
                    dest = pooled_by_source.setdefault(src_id, {})
                    for token, vals in token_map.items():
                        dest.setdefault(token, []).extend(vals)
            universal_verdicts = timeline_universal_cohesion(
                pooled_by_source,
                encode=encode,
            )
            cohesion_summary = timeline_cohesion_summary(
                universal_verdicts,
                source_count=len(source_ids),
                encode_available=encode is not None,
            )
        else:
            cohesion_summary = timeline_cohesion_summary(
                [],
                source_count=len(source_ids),
                encode_available=encode is not None,
            )

        return {
            "artifacts": artifacts,
            "cohesion": {
                "level": cohesion_summary.level,
                "mean_cohesion": cohesion_summary.mean_cohesion,
                "shared_field_count": cohesion_summary.shared_field_count,
                "source_count": cohesion_summary.source_count,
                "message": cohesion_summary.message,
            },
        }

    def histogram(
        self, query: EventQuery, buckets: int = 60
    ) -> dict[str, Any]:
        """Return a bucketed event-count histogram honoring all query filters.

        If the query has no explicit time range the min/max timestamps are
        derived from the filtered event set first.  Returns an empty bucket
        list when there are no matching events.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database

        # Resolve time range.
        if query.start is not None and query.end is not None:
            min_ts: datetime | None = query.start
            max_ts: datetime | None = query.end
        else:
            range_result = self.store.client.query(
                f"SELECT min(timestamp), max(timestamp) FROM {database}.events WHERE {where}",
                parameters=parameters,
            )
            row = range_result.result_rows[0] if range_result.result_rows else (None, None)
            min_ts, max_ts = row[0], row[1]

        if min_ts is None or max_ts is None:
            return {"interval_seconds": 0, "min": None, "max": None, "buckets": []}

        # Ensure timezone-aware for arithmetic.
        if hasattr(min_ts, "tzinfo") and min_ts.tzinfo is None:
            min_ts = min_ts.replace(tzinfo=UTC)
        if hasattr(max_ts, "tzinfo") and max_ts.tzinfo is None:
            max_ts = max_ts.replace(tzinfo=UTC)

        duration = (max_ts - min_ts).total_seconds()
        interval = max(1, int(duration / buckets))

        bucket_result = self.store.client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {interval} second) AS bucket,
                   count() AS c
            FROM {database}.events
            WHERE {where} AND timestamp IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
            """,
            parameters=parameters,
        )

        def _to_utc_iso(dt: Any) -> str:
            if hasattr(dt, "isoformat"):
                if hasattr(dt, "tzinfo") and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.isoformat()
            return str(dt)

        bucket_list = [
            {"start": _to_utc_iso(row[0]), "count": row[1]}
            for row in bucket_result.result_rows
        ]
        return {
            "interval_seconds": interval,
            "min": min_ts.isoformat(),
            "max": max_ts.isoformat(),
            "buckets": bucket_list,
        }
