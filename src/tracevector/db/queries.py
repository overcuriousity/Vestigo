"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from tracevector.db.clickhouse import ClickHouseStore
from tracevector.db.field_recommend import recommend_fields


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
    field_exclusions: dict[str, str] = field(default_factory=dict)
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
        """Add an ``IN (...)`` condition for a list of string values."""
        names = [self._param_name() for _ in values]
        in_clause = ", ".join(f"{{{name}:String}}" for name in names)
        self.conditions.append(f"{column} IN ({in_clause})")
        for name, value in zip(names, values, strict=False):
            self.parameters[name] = value

    def add_field_filter(self, key: str, value: str) -> None:
        """Add an equality filter on a top-level column or attribute."""
        column = self._column_expr(key)
        self.add_param(f"{column} = :name", value)

    def add_field_exclusion(self, key: str, value: str) -> None:
        """Add a not-equals exclusion on a top-level column or attribute."""
        column = self._column_expr(key)
        self.add_param(f"{column} != :name", value)

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

        for key, value in (query.field_filters or {}).items():
            builder.add_field_filter(key, value)

        for key, value in (query.field_exclusions or {}).items():
            builder.add_field_exclusion(key, value)

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
        events = [dict(zip(columns, row, strict=False)) for row in event_result.result_rows]

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
                yield dict(zip(columns, row, strict=False))
            if len(rows) < batch_size:
                break
            offset += batch_size

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

        params: dict[str, str] = {"p0": case_id}
        source_params = [f"s{i}" for i in range(len(source_ids))]
        source_in = ", ".join(f"{{{name}:String}}" for name in source_params)
        for name, value in zip(source_params, source_ids, strict=False):
            params[name] = value

        result = self.store.client.query(
            f"""
            SELECT groupUniqArrayArray(mapKeys(attributes)) AS keys
            FROM (
                SELECT attributes
                FROM {database}.events
                WHERE case_id = {{p0:String}} AND source_id IN ({source_in})
                LIMIT 50000
            )
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
    ) -> dict[str, list[dict[str, Any]]]:
        """Return per-artifact field information for the embedding wizard.

        For each distinct ``artifact`` across the sources, returns the event
        count, the available top-level embedding fields, the dynamic attribute
        keys, and a *content-aware* recommendation produced by the hybrid
        heuristic→pairs strategy (see :mod:`tracevector.db.field_recommend`):

        - ``recommended`` — tokens whose sampled values look semantically rich.
        - ``field_analysis`` — per-field verdict (recommended / kind / reason)
          so the wizard can explain why each field was kept or dropped.
        - ``related_groups`` — groups of fields whose values embed close together
          (only populated when an ``encode`` callable is supplied).

        ``encode`` is the embedding callable used for stage 2; pass ``None`` for
        a fast heuristic-only result.  Field values are sampled per artifact
        (``sample_per_artifact`` rows, randomised).
        """
        self.store.init_schema()
        database = self.store.database

        params: dict[str, Any] = {"p0": case_id, "per": sample_per_artifact}
        source_params = [f"s{i}" for i in range(len(source_ids))]
        source_in = ", ".join(f"{{{name}:String}}" for name in source_params)
        for name, value in zip(source_params, source_ids, strict=False):
            params[name] = value

        # 1. Full attribute-key inventory + event count per artifact.
        inv = self.store.client.query(
            f"""
            SELECT
                artifact,
                count() AS n,
                groupUniqArrayArray(mapKeys(attributes)) AS attr_keys
            FROM (
                SELECT artifact, attributes
                FROM {database}.events
                WHERE case_id = {{p0:String}} AND source_id IN ({source_in})
                LIMIT 50000
            )
            GROUP BY artifact
            ORDER BY n DESC
            """,
            parameters=params,
        )
        inventory = {
            (row[0] or ""): (row[1], sorted(row[2]) if row[2] else [])
            for row in inv.result_rows
        }

        # 2. Randomised value sample per artifact for content classification.
        cols = ["message", "timestamp_desc", "artifact_long", "display_name", "tags"]
        sample = self.store.client.query(
            f"""
            SELECT artifact, {", ".join(cols)}, attributes
            FROM (
                SELECT artifact, {", ".join(cols)}, attributes,
                       row_number() OVER (PARTITION BY artifact ORDER BY rand()) AS _rn
                FROM (
                    SELECT artifact, {", ".join(cols)}, attributes
                    FROM {database}.events
                    WHERE case_id = {{p0:String}} AND source_id IN ({source_in})
                    LIMIT 200000
                )
            )
            WHERE _rn <= {{per:UInt32}}
            """,
            parameters=params,
        )

        # artifact -> token -> list of sampled values
        samples: dict[str, dict[str, list[Any]]] = {}
        for row in sample.result_rows:
            artifact_name = row[0] or ""
            bucket = samples.setdefault(artifact_name, {})
            for i, col in enumerate(cols, start=1):
                value = row[i]
                if col == "tags" and isinstance(value, (list, tuple)):
                    value = " ".join(str(t) for t in value)
                bucket.setdefault(col, []).append(value)
            attrs = row[len(cols) + 1]
            for key, value in _iter_attr_items(attrs):
                bucket.setdefault(f"attr:{key}", []).append(value)

        artifacts = []
        for artifact_name, (count, attr_keys) in inventory.items():
            bucket = samples.get(artifact_name, {})
            # Seed every candidate token so each gets a verdict, even if unsampled.
            field_samples: dict[str, list[Any]] = {
                t: bucket.get(t, []) for t in self._EMBEDDABLE_TOP_LEVEL
            }
            for key in attr_keys:
                token = f"attr:{key}"
                field_samples[token] = bucket.get(token, [])

            rec = recommend_fields(field_samples, encode=encode)

            artifacts.append(
                {
                    "artifact": artifact_name,
                    "count": count,
                    "top_level": self._EMBEDDABLE_TOP_LEVEL,
                    "attributes": attr_keys,
                    "recommended": rec.recommended,
                    "field_analysis": [
                        {
                            "token": v.token,
                            "recommended": v.recommended,
                            "kind": v.kind,
                            "reason": v.reason,
                        }
                        for v in rec.verdicts
                    ],
                    "related_groups": rec.related_groups,
                }
            )

        return {"artifacts": artifacts}

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
