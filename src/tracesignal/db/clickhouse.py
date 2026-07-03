"""ClickHouse connection and event storage.

The event table schema is optimised for forensic timeline analysis:
* MergeTree ordered by (case_id, source_id, timestamp)
* Projections or token bloom filters for full-text search
* Immutable forensic provenance columns

Events are scoped by ``source_id`` (one ingested file) so that a Source can be
shared across multiple Timelines without duplication. Timeline queries resolve
member source IDs and use ``source_id IN (...)`` filtering.
"""

from __future__ import annotations

import contextlib
from datetime import UTC
from typing import Any

import clickhouse_connect

from tracesignal.core.config import get_settings
from tracesignal.models.event import Event


def _normalize_event_datetimes(row: dict[str, Any]) -> dict[str, Any]:
    """Attach an explicit UTC offset to an event row's timestamp columns.

    The `events` table's `timestamp`/`ingest_time` columns have no explicit
    timezone component, so clickhouse-connect returns naive `datetime`
    objects for them. Left naive, a downstream `.isoformat()` call omits the
    timezone offset — and a bare "YYYY-MM-DDTHH:MM:SS" string is ambiguous to
    JS's `Date` parser (browsers treat it as local time), silently shifting
    the displayed/compared timestamp by the browser's UTC offset.
    """
    for key in ("timestamp", "ingest_time"):
        value = row.get(key)
        if value is None or isinstance(value, str):
            continue
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=UTC)
        row[key] = value.isoformat()
    return row


_EVENT_COLUMNS = [
    "event_id",
    "case_id",
    "source_id",
    "source_file",
    "byte_offset",
    "line_number",
    "content_hash",
    "file_hash",
    "parser_name",
    "parser_version",
    "ingest_time",
    "message",
    "timestamp",
    "timestamp_desc",
    "artifact",
    "artifact_long",
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
    source_id LowCardinality(String),
    source_file String,
    byte_offset UInt64,
    line_number UInt64,
    content_hash FixedString(64),
    file_hash FixedString(64),
    parser_name LowCardinality(String),
    parser_version LowCardinality(String),
    ingest_time DateTime64(3),
    message String,
    timestamp Nullable(DateTime64(3)),
    timestamp_desc LowCardinality(String),
    artifact LowCardinality(String),
    artifact_long LowCardinality(String),
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
ORDER BY (case_id, source_id, timestamp, event_id)
PARTITION BY (case_id, source_id)
SETTINGS index_granularity = 8192, allow_nullable_key = 1
""".strip()

_ENRICHMENT_COLUMNS = [
    "event_id",
    "case_id",
    "source_id",
    "enricher_key",
    "field_key",
    "value",
    "computed_at",
    "enricher_config_hash",
]

_EVENT_ENRICHMENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {database}.event_enrichments (
    event_id UUID,
    case_id LowCardinality(String),
    source_id LowCardinality(String),
    enricher_key LowCardinality(String),
    field_key LowCardinality(String),
    value String,
    computed_at DateTime64(3),
    enricher_config_hash String DEFAULT ''
)
ENGINE = MergeTree()
ORDER BY (case_id, source_id, event_id, enricher_key, field_key)
PARTITION BY (case_id, source_id)
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
            # This client is a process-wide singleton shared across FastAPI's
            # threadpool workers. clickhouse-connect auto-generates a
            # session_id by default, and the server rejects concurrent
            # queries within the same session_id — so two overlapping
            # requests (e.g. the analysis tabs firing several queries at
            # once) would 500 with "Attempt to execute concurrent queries
            # within the same session." Nothing here relies on session-scoped
            # state (temp tables, session settings), so disable it.
            autogenerate_session_id=False,
        )

    @staticmethod
    def _host(url: str) -> str:
        return url.split("://")[-1].split(":")[0]

    @staticmethod
    def _port(url: str) -> int:
        parts = url.split("://")[-1].split(":")
        return int(parts[1]) if len(parts) > 1 else 8123

    def init_schema(self) -> None:
        """Create the target database and events/event_enrichments tables if they do not exist."""
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        self.client.command(_EVENTS_TABLE_DDL.format(database=self.database))
        self.client.command(_EVENT_ENRICHMENTS_TABLE_DDL.format(database=self.database))

    def insert_events(self, events: list[Event]) -> int:
        """Insert a batch of events into ClickHouse.

        Args:
            events: List of :py:class:`~tracesignal.models.event.Event` objects.

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

    def bulk_insert_enrichments(self, rows: list[dict[str, Any]]) -> int:
        """Bulk-insert enrichment result rows into the append-only ``event_enrichments`` table.

        Each row is one ``(event_id, enricher_key, field_key)`` -> value pair.
        Never mutates the ``events`` table itself — enrichment output is
        joined in at query time (see ``db/queries.py``).
        """
        if not rows:
            return 0
        rows = [
            {**row, "enricher_config_hash": row.get("enricher_config_hash", "")} for row in rows
        ]
        data = [[row[column] for column in _ENRICHMENT_COLUMNS] for row in rows]
        response = self.client.insert(
            table=f"{self.database}.event_enrichments",
            data=data,
            column_names=_ENRICHMENT_COLUMNS,
            database=self.database,
        )
        return response.written_rows

    def count_events(
        self,
        case_id: str | None = None,
        source_id: str | None = None,
        source_ids: list[str] | None = None,
    ) -> int:
        """Return the number of events, optionally filtered by case/source."""
        query = f"SELECT count() FROM {self.database}.events"
        conditions: list[str] = []
        if case_id is not None:
            conditions.append(f"case_id = {case_id!r}")
        if source_id is not None:
            conditions.append(f"source_id = {source_id!r}")
        if source_ids is not None:
            ids = ", ".join(f"{s!r}" for s in source_ids)
            conditions.append(f"source_id IN ({ids})")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        result = self.client.query(query)
        return result.result_rows[0][0] if result.result_rows else 0

    def list_events(
        self,
        case_id: str,
        source_id: str,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return a batch of raw event rows for a source ordered by event_id.

        This is used by the embedding pipeline to read events that were
        previously ingested without vectors.
        """
        result = self.client.query(
            f"""
            SELECT
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
            FROM {self.database}.events
            WHERE case_id = {{case_id:String}} AND source_id = {{source_id:String}}
            ORDER BY event_id
            LIMIT {limit}
            OFFSET {offset}
            """,
            parameters={"case_id": case_id, "source_id": source_id},
        )
        columns = result.column_names
        return [
            _normalize_event_datetimes(dict(zip(columns, row, strict=False)))
            for row in result.result_rows
        ]

    def get_events_by_ids(
        self,
        case_id: str,
        source_ids: list[str],
        event_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return a mapping of event_id → event dict for a list of IDs.

        Only returns rows that exist in ClickHouse. Unknown IDs are silently
        absent from the result (callers should fall back to the Qdrant payload).
        """
        if not event_ids:
            return {}
        self.init_schema()
        # Build parameterized IN clauses.
        event_params = [f"e{i}" for i in range(len(event_ids))]
        event_in = ", ".join(f"{{{name}:String}}" for name in event_params)
        source_params = [f"s{i}" for i in range(len(source_ids))]
        source_in = ", ".join(f"{{{name}:String}}" for name in source_params)
        parameters: dict[str, str] = dict(zip(event_params, event_ids, strict=False))
        parameters.update(zip(source_params, source_ids, strict=False))
        parameters["case_id"] = case_id
        result = self.client.query(
            f"""
            SELECT
                event_id, case_id, source_id, source_file, byte_offset, line_number,
                content_hash, file_hash, parser_name, parser_version, ingest_time,
                message, timestamp, timestamp_desc, artifact, artifact_long, display_name,
                tags, attributes, embedding_model, embedding_config_hash, vector_id
            FROM {self.database}.events
            WHERE case_id = {{case_id:String}}
              AND source_id IN ({source_in})
              AND toString(event_id) IN ({event_in})
            """,
            parameters=parameters,
        )
        columns = result.column_names
        rows = [
            _normalize_event_datetimes(dict(zip(columns, row, strict=False)))
            for row in result.result_rows
        ]
        return {str(row["event_id"]): row for row in rows}

    def delete_source_events(self, case_id: str, source_id: str) -> None:
        """Remove all events for a source by dropping its ClickHouse partition.

        The ``events`` table is partitioned by ``(case_id, source_id)`` so
        ``DROP PARTITION`` is instant and does not require a full-table scan.
        If the partition does not exist the call is a silent no-op.
        """
        partition_expr = f"tuple('{case_id}', '{source_id}')"
        with contextlib.suppress(Exception):
            self.client.command(
                f"ALTER TABLE {self.database}.events DROP PARTITION {partition_expr}"
            )
        with contextlib.suppress(Exception):
            self.client.command(
                f"ALTER TABLE {self.database}.event_enrichments DROP PARTITION {partition_expr}"
            )

    def delete_timeline_events(self, case_id: str, source_ids: list[str]) -> None:
        """Remove all events for a timeline by dropping partitions for its sources."""
        for source_id in source_ids:
            self.delete_source_events(case_id, source_id)

    def health(self) -> dict[str, Any]:
        """Return a simple health status for the ClickHouse connection."""
        try:
            result = self.client.query("SELECT 1")
            return {"status": "ok", "ping": result.result_rows[0][0] == 1}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}
