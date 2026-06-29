"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from tracevector.db.clickhouse import ClickHouseStore


@dataclass
class EventQuery:
    """Query parameters for the event viewer."""

    case_id: str
    timeline_id: str | None = None
    q: str | None = None
    source: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    field_filters: dict[str, str] = field(default_factory=dict)
    field_exclusions: dict[str, str] = field(default_factory=dict)
    limit: int = 50
    offset: int = 0
    order: Literal["asc", "desc"] = "desc"


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
        "source",
        "source_long",
        "display_name",
        "parser_name",
        "parser_version",
        "source_file",
    }
)

# Top-level columns surfaced as choosable display columns in the UI.
# Separate from _TOP_LEVEL_FILTER_COLUMNS (which is for filter routing).
TOP_LEVEL_DISPLAY_COLUMNS = [
    "timestamp",
    "source",
    "source_long",
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
    timeline_id,
    source_file,
    byte_offset,
    line_number,
    content_hash,
    parser_name,
    parser_version,
    ingest_time,
    message,
    timestamp,
    timestamp_desc,
    source,
    source_long,
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

        if query.timeline_id is not None:
            builder.add_param("timeline_id = :name", query.timeline_id)

        if query.q:
            # ClickHouse tokenbf_v1 index supports hasToken and multiSearchAny.
            # We use ILIKE for substring search as a simple baseline.
            builder.add_param("message ILIKE :name", f"%{query.q}%")

        if query.source:
            builder.add_param("source = :name", query.source)

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

    def list_fields(self, case_id: str, timeline_id: str) -> dict[str, list[str]]:
        """Return the top-level display columns and dynamic attribute keys for a timeline.

        Attribute keys are aggregated across a sample of up to 50 000 events so the
        call stays fast even on large timelines.
        """
        self.store.init_schema()
        database = self.store.database

        result = self.store.client.query(
            f"""
            SELECT groupUniqArrayArray(mapKeys(attributes)) AS keys
            FROM (
                SELECT attributes
                FROM {database}.events
                WHERE case_id = {{p0:String}} AND timeline_id = {{p1:String}}
                LIMIT 50000
            )
            """,
            parameters={"p0": case_id, "p1": timeline_id},
        )
        raw_keys: list[str] = result.result_rows[0][0] if result.result_rows else []
        return {
            "top_level": TOP_LEVEL_DISPLAY_COLUMNS,
            "attributes": sorted(raw_keys),
        }

    def list_fields_by_source(
        self, case_id: str, timeline_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Return per-source field information for the embedding wizard.

        For each distinct ``source`` value in the timeline, returns the event
        count, the available top-level embedding fields, and the dynamic
        attribute keys found in that source's events.  A ``recommended`` list
        preselects ``message`` (always) plus ``display_name`` and ``source_long``
        where present, and all attribute keys (they are source-specific by
        definition).  The analyst trims in the wizard.

        Results are based on a sample of up to 50 000 events per source.
        """
        self.store.init_schema()
        database = self.store.database

        # Top-level fields that are meaningful for embedding (not IDs/provenance).
        EMBEDDABLE_TOP_LEVEL = [
            "message",
            "timestamp_desc",
            "source_long",
            "display_name",
            "tags",
        ]

        result = self.store.client.query(
            f"""
            SELECT
                source,
                count() AS n,
                groupUniqArrayArray(mapKeys(attributes)) AS attr_keys
            FROM (
                SELECT source, attributes
                FROM {database}.events
                WHERE case_id = {{p0:String}} AND timeline_id = {{p1:String}}
                LIMIT 50000
            )
            GROUP BY source
            ORDER BY n DESC
            """,
            parameters={"p0": case_id, "p1": timeline_id},
        )

        sources = []
        for row in result.result_rows:
            source_name = row[0] or ""
            count = row[1]
            attr_keys = sorted(row[2]) if row[2] else []

            # Recommended = always message, plus optional top-level, plus all attrs.
            recommended: list[str] = ["message"]
            for f in ("display_name", "source_long", "timestamp_desc", "tags"):
                if f not in recommended:
                    recommended.append(f)
            recommended.extend(f"attr:{k}" for k in attr_keys)

            sources.append(
                {
                    "source": source_name,
                    "count": count,
                    "top_level": EMBEDDABLE_TOP_LEVEL,
                    "attributes": attr_keys,
                    "recommended": recommended,
                }
            )

        return {"sources": sources}

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
            min_ts = min_ts.replace(tzinfo=timezone.utc)
        if hasattr(max_ts, "tzinfo") and max_ts.tzinfo is None:
            max_ts = max_ts.replace(tzinfo=timezone.utc)

        duration = (max_ts - min_ts).total_seconds()
        interval = max(1, int(duration / buckets))

        bucket_result = self.store.client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {interval} second) AS bucket,
                   count() AS c
            FROM {database}.events
            WHERE {where}
            GROUP BY bucket
            ORDER BY bucket
            """,
            parameters=parameters,
        )
        def _to_utc_iso(dt: Any) -> str:
            if hasattr(dt, "isoformat"):
                if hasattr(dt, "tzinfo") and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
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
