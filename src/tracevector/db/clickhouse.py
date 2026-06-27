"""ClickHouse connection and event storage.

The event table schema is optimised for forensic timeline analysis:
* MergeTree ordered by (case_id, timeline_id, timestamp)
* Projections or token bloom filters for full-text search
* Immutable forensic provenance columns
"""

from __future__ import annotations

from typing import Any

import clickhouse_connect

from tracevector.core.config import get_settings
from tracevector.models.event import Event

_EVENT_COLUMNS = [
    "event_id",
    "case_id",
    "timeline_id",
    "source_file",
    "byte_offset",
    "line_number",
    "content_hash",
    "parser_name",
    "parser_version",
    "ingest_time",
    "message",
    "timestamp",
    "timestamp_desc",
    "source",
    "source_long",
    "display_name",
    "tags",
    "attributes",
    "embedding_model",
    "embedding_config_hash",
    "vector_id",
]

_EVENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {database}.events (
    event_id UUID,
    case_id LowCardinality(String),
    timeline_id LowCardinality(String),
    source_file String,
    byte_offset UInt64,
    line_number UInt64,
    content_hash FixedString(64),
    parser_name LowCardinality(String),
    parser_version LowCardinality(String),
    ingest_time DateTime64(3),
    message String,
    timestamp DateTime64(3),
    timestamp_desc LowCardinality(String),
    source LowCardinality(String),
    source_long LowCardinality(String),
    display_name LowCardinality(String),
    tags Array(String),
    attributes Map(String, String),
    embedding_model LowCardinality(String),
    embedding_config_hash FixedString(64),
    vector_id String,
    INDEX message_idx message TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 1,
    INDEX content_hash_idx content_hash TYPE bloom_filter GRANULARITY 4
)
ENGINE = MergeTree()
ORDER BY (case_id, timeline_id, timestamp, event_id)
PARTITION BY (case_id, timeline_id)
SETTINGS index_granularity = 8192
""".strip()


class ClickHouseStore:
    """Sync ClickHouse client for event data."""

    def __init__(self) -> None:
        settings = get_settings()
        self.database = settings.clickhouse_database
        self.client = clickhouse_connect.get_client(
            host=self._host(settings.clickhouse_url),
            port=self._port(settings.clickhouse_url),
            username=settings.clickhouse_username,
            password=settings.clickhouse_password,
            database="default",
        )

    @staticmethod
    def _host(url: str) -> str:
        return url.split("://")[-1].split(":")[0]

    @staticmethod
    def _port(url: str) -> int:
        parts = url.split("://")[-1].split(":")
        return int(parts[1]) if len(parts) > 1 else 8123

    def init_schema(self) -> None:
        """Create the target database and events table if they do not exist."""
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        self.client.command(_EVENTS_TABLE_DDL.format(database=self.database))

    def insert_events(self, events: list[Event]) -> int:
        """Insert a batch of events into ClickHouse.

        Args:
            events: List of :py:class:`~tracevector.models.event.Event` objects.

        Returns:
            Number of rows inserted.
        """
        if not events:
            return 0
        rows = [event.to_clickhouse_row() for event in events]
        data = [[row[column] for column in _EVENT_COLUMNS] for row in rows]
        response = self.client.insert(
            table=f"{self.database}.events",
            data=data,
            column_names=_EVENT_COLUMNS,
            database=self.database,
        )
        return response.written_rows

    def count_events(self, case_id: str | None = None, timeline_id: str | None = None) -> int:
        """Return the number of events, optionally filtered by case/timeline."""
        query = f"SELECT count() FROM {self.database}.events"
        conditions: list[str] = []
        if case_id is not None:
            conditions.append(f"case_id = {case_id!r}")
        if timeline_id is not None:
            conditions.append(f"timeline_id = {timeline_id!r}")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        result = self.client.query(query)
        return result.result_rows[0][0] if result.result_rows else 0

    def list_events(
        self,
        case_id: str,
        timeline_id: str,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return a batch of raw event rows for a timeline ordered by event_id.

        This is used by the embedding pipeline to read events that were
        previously ingested without vectors.
        """
        result = self.client.query(
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
            FROM {self.database}.events
            WHERE case_id = {{case_id:String}} AND timeline_id = {{timeline_id:String}}
            ORDER BY event_id
            LIMIT {limit}
            OFFSET {offset}
            """,
            parameters={"case_id": case_id, "timeline_id": timeline_id},
        )
        columns = result.column_names
        return [dict(zip(columns, row, strict=False)) for row in result.result_rows]

    def get_events_by_ids(
        self, case_id: str, timeline_id: str, event_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return a mapping of event_id → event dict for a list of IDs.

        Only returns rows that exist in ClickHouse.  Unknown IDs are silently
        absent from the result (callers should fall back to the Qdrant payload).
        """
        if not event_ids:
            return {}
        self.init_schema()
        # Build parameterized IN clause: {p0:String},{p1:String},...
        param_names = [f"p{i}" for i in range(len(event_ids))]
        in_clause = ", ".join(f"{{{name}:String}}" for name in param_names)
        parameters: dict[str, str] = dict(zip(param_names, event_ids, strict=False))
        parameters["case_id"] = case_id
        parameters["timeline_id"] = timeline_id
        result = self.client.query(
            f"""
            SELECT
                event_id, case_id, timeline_id, source_file, byte_offset, line_number,
                content_hash, parser_name, parser_version, ingest_time, message,
                timestamp, timestamp_desc, source, source_long, display_name,
                tags, attributes, embedding_model, embedding_config_hash, vector_id
            FROM {self.database}.events
            WHERE case_id = {{case_id:String}}
              AND timeline_id = {{timeline_id:String}}
              AND toString(event_id) IN ({in_clause})
            """,
            parameters=parameters,
        )
        columns = result.column_names
        rows = [dict(zip(columns, row, strict=False)) for row in result.result_rows]
        return {str(row["event_id"]): row for row in rows}

    def delete_timeline_events(self, case_id: str, timeline_id: str) -> None:
        """Remove all events for a timeline by dropping its ClickHouse partition.

        The ``events`` table is partitioned by ``(case_id, timeline_id)`` so
        ``DROP PARTITION`` is instant and does not require a full-table scan.
        If the partition does not exist (timeline was never uploaded) the call
        is a silent no-op.
        """
        try:
            # clickhouse-connect does not support parameterised DDL.  The IDs
            # produced by generate_id() contain only [a-zA-Z0-9_-] so
            # direct interpolation is safe here.
            partition_expr = f"tuple('{case_id}', '{timeline_id}')"
            self.client.command(
                f"ALTER TABLE {self.database}.events DROP PARTITION {partition_expr}"
            )
        except Exception:  # noqa: BLE001
            # Partition may not exist (timeline never ingested any events); that
            # is fine — treat as a no-op.
            pass

    def health(self) -> dict[str, Any]:
        """Return a simple health status for the ClickHouse connection."""
        try:
            result = self.client.query("SELECT 1")
            return {"status": "ok", "ping": result.result_rows[0][0] == 1}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}
