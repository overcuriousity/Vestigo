"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tracevector.db.clickhouse import ClickHouseStore


@dataclass
class EventQuery:
    """Query parameters for the event viewer."""

    case_id: str
    timeline_id: str | None = None
    q: str | None = None
    source: str | None = None
    tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    field_filters: dict[str, str] = field(default_factory=dict)
    field_exclusions: dict[str, str] = field(default_factory=dict)
    limit: int = 50
    offset: int = 0


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


def _format_clickhouse_datetime(value: datetime) -> str:
    """Format a datetime for ClickHouse SQL."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


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
        self.conditions.append(sql_fragment.replace(":name", f":{name}"))
        self.parameters[name] = value

    def add_field_filter(self, key: str, value: str) -> None:
        """Add an equality filter on a top-level column or attribute."""
        column = self._column_expr(key)
        self.add_param(f"{column} = :name", value)

    def add_field_exclusion(self, key: str, value: str) -> None:
        """Add a not-equals exclusion on a top-level column or attribute."""
        column = self._column_expr(key)
        self.add_param(f"{column} != :name", value)

    def _column_expr(self, key: str) -> str:
        normalized = key.strip().lower()
        if normalized in _TOP_LEVEL_FILTER_COLUMNS:
            return normalized
        # Map lookup; parameterize the key as well to stay defensive.
        key_param = self._param_name()
        self.parameters[key_param] = key
        return f"attributes[:{key_param}]"

    def where_clause(self) -> str:
        return " AND ".join(self.conditions)


class EventQueryService:
    """Query service for events stored in ClickHouse."""

    def __init__(self, store: ClickHouseStore | None = None) -> None:
        self.store = store or ClickHouseStore()

    def query(self, query: EventQuery) -> EventPage:
        """Execute an :py:class:`EventQuery` and return a paginated result."""
        self.store.init_schema()

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

        where = builder.where_clause()
        database = self.store.database

        count_result = self.store.client.query(
            f"SELECT count() FROM {database}.events WHERE {where}",
            parameters=builder.parameters,
        )
        total = count_result.result_rows[0][0] if count_result.result_rows else 0

        event_result = self.store.client.query(
            f"""
            SELECT
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
            FROM {database}.events
            WHERE {where}
            ORDER BY timestamp DESC, event_id
            LIMIT {query.limit}
            OFFSET {query.offset}
            """,
            parameters=builder.parameters,
        )

        columns = event_result.column_names
        events = [dict(zip(columns, row, strict=False)) for row in event_result.result_rows]

        return EventPage(
            total=total,
            offset=query.offset,
            limit=query.limit,
            events=events,
        )
