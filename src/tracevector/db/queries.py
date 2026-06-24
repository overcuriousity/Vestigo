"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

from dataclasses import dataclass
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
    limit: int = 50
    offset: int = 0


@dataclass
class EventPage:
    """Paginated event query result."""

    total: int
    offset: int
    limit: int
    events: list[dict[str, Any]]


def _format_clickhouse_datetime(value: datetime) -> str:
    """Format a datetime for ClickHouse SQL."""
    return value.strftime("%Y-%m-%d %H:%%M:%S")


class EventQueryService:
    """Query service for events stored in ClickHouse."""

    def __init__(self, store: ClickHouseStore | None = None) -> None:
        self.store = store or ClickHouseStore()

    def query(self, query: EventQuery) -> EventPage:
        """Execute an :py:class:`EventQuery` and return a paginated result."""
        self.store.init_schema()
        where_clauses: list[str] = [f"case_id = {query.case_id!r}"]
        if query.timeline_id is not None:
            where_clauses.append(f"timeline_id = {query.timeline_id!r}")
        if query.q:
            # ClickHouse tokenbf_v1 index supports hasToken and multiSearchAny.
            # We use lower() + LIKE for substring search as a simple baseline.
            escaped = query.q.replace("\\", "\\\\").replace("'", "\\'")
            where_clauses.append(f"message ILIKE '%{escaped}%'")
        if query.source:
            where_clauses.append(f"source = {query.source!r}")
        if query.tag:
            where_clauses.append(f"has(tags, {query.tag!r})")
        if query.start is not None:
            where_clauses.append(f"timestamp >= '{query.start.strftime('%Y-%m-%d %H:%M:%S')}'")
        if query.end is not None:
            where_clauses.append(f"timestamp <= '{query.end.strftime('%Y-%m-%d %H:%M:%S')}'")

        where = " AND ".join(where_clauses)

        count_result = self.store.client.query(
            f"SELECT count() FROM {self.store.database}.events WHERE {where}"
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
            FROM {self.store.database}.events
            WHERE {where}
            ORDER BY timestamp DESC, event_id
            LIMIT {query.limit}
            OFFSET {query.offset}
            """
        )

        columns = event_result.column_names
        events = [dict(zip(columns, row, strict=False)) for row in event_result.result_rows]

        return EventPage(
            total=total,
            offset=query.offset,
            limit=query.limit,
            events=events,
        )
