"""ClickHouse connection and event storage.

The event table schema is optimised for forensic timeline analysis:
* MergeTree ordered by (case_id, source_id, timestamp)
* Projections or token bloom filters for full-text search
* Forensic provenance columns (``content_hash``/``file_hash``) computed from
  the raw record/file bytes at ingest and never recomputed afterwards.

Events are scoped by ``source_id`` (one ingested file) so that a Source can be
shared across multiple Timelines without duplication. Timeline queries resolve
member source IDs and use ``source_id IN (...)`` filtering.

Immutability contract: the *original evidence files* are hashed and immutable;
this table is a normalized derivative of them. Enrichers amend the
``attributes`` map after ingest via an atomic per-source partition rewrite
(``stage_enrichment_rows`` / ``finalize_enrichment_apply``) — the provenance
columns are never touched, so hash verification against the original file is
unaffected.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator
from datetime import UTC
from typing import Any

import clickhouse_connect

from tracesignal.core.config import get_settings
from tracesignal.models.event import Event

logger = logging.getLogger(__name__)


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


def _validate_partition_id(value: str, label: str) -> str:
    """Fail closed on any ID that can't be safely interpolated into a partition expression.

    ``ALTER TABLE ... DROP/REPLACE PARTITION`` expressions cannot be
    query-parameterized, so IDs are string-interpolated there. All case and
    source IDs are server-generated via ``postgres.generate_id``, which only
    emits alphanumeric characters (Unicode-aware, matching ``str.isalnum``)
    plus ``-``/``_`` — the same predicate is enforced here, so quotes,
    whitespace, and control characters can never reach the DDL string.
    Anything else is a bug or tampering.
    """
    if not value or not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError(f"unsafe {label} for partition expression: {value!r}")
    return value


def _partition_expr(case_id: str, source_id: str) -> str:
    """Build a validated ``(case_id, source_id)`` partition tuple expression."""
    return (
        f"tuple('{_validate_partition_id(case_id, 'case_id')}', "
        f"'{_validate_partition_id(source_id, 'source_id')}')"
    )


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

# Prefix for the transient tables the enrichment-apply stage/finalize step
# works through; stale ones (crash mid-apply) are swept at startup by
# drop_stale_enrichment_scratch_tables.
_ENRICH_SCRATCH_PREFIX = "tmp_enrich_"


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

    @staticmethod
    def _string_in_clause(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
        """Build a parameterized IN-list body and its bindings for String values.

        Returns ``("{p0:String}, {p1:String}, ...", {"p0": v0, ...})`` — the
        caller wraps the body in ``... IN (<body>)`` and merges the bindings
        into its parameters dict. Prefixes must be distinct across clauses in
        the same query.
        """
        names = [f"{prefix}{i}" for i in range(len(values))]
        body = ", ".join(f"{{{name}:String}}" for name in names)
        return body, dict(zip(names, values, strict=False))

    def init_schema(self) -> None:
        """Create the target database and events table if they do not exist."""
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        self.client.command(_EVENTS_TABLE_DDL.format(database=self.database))
        # Enrichment output moved into events.attributes (stage_enrichment_rows / finalize_enrichment_apply);
        # the former side table is dead. Destructive, but pre-release
        # databases are documented as deprecated and the data is derived —
        # re-running the enricher regenerates it.
        self.client.command(f"DROP TABLE IF EXISTS {self.database}.event_enrichments")

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

    def attribute_keys_present(
        self, case_id: str, source_ids: list[str], keys: list[str]
    ) -> set[str]:
        """Return which of *keys* actually occur as attribute map keys.

        Targeted existence check (bounded by ``len(keys)`` map lookups per
        row, no key-enumeration ``ARRAY JOIN``) for raw keys that field-mapping
        validation didn't find in the per-source field-stats cache — that
        cache caps attribute keys per source (``_MAX_ATTR_KEYS_PER_SOURCE`` in
        ``field_stats.py``) to bound its payload, so a real but low-coverage
        key can rank outside the cap and needs this live fallback instead of
        being rejected as nonexistent.
        """
        if not keys or not source_ids:
            return set()
        self.init_schema()
        db = self.database
        params: dict[str, Any] = {"cid": case_id, "sids": source_ids}
        select_parts = []
        for i, key in enumerate(keys):
            pname = f"k{i}"
            params[pname] = key
            select_parts.append(f"countIf(mapContains(attributes, {{{pname}:String}})) AS c{i}")
        result = self.client.query(
            f"SELECT {', '.join(select_parts)} FROM {db}.events "
            "WHERE case_id = {cid:String} AND source_id IN {sids:Array(String)}",
            parameters=params,
        )
        if not result.result_rows:
            return set()
        row = result.result_rows[0]
        return {key for i, key in enumerate(keys) if int(row[i]) > 0}

    def _enrichment_scratch_tables(self, scratch_suffix: str) -> tuple[str, str]:
        # Suffix comes from the in-process job id (uuid4 hex); defensive
        # strip anyway since it lands in DDL identifiers.
        suffix = re.sub(r"[^a-zA-Z0-9_]", "", scratch_suffix)
        return (
            f"{self.database}.{_ENRICH_SCRATCH_PREFIX}rows_{suffix}",
            f"{self.database}.{_ENRICH_SCRATCH_PREFIX}events_{suffix}",
        )

    def create_enrichment_scratch(self, scratch_suffix: str) -> None:
        """(Re)create the scratch rows table one source's apply stages into.

        Call once before any ``stage_enrichment_rows`` calls for this
        ``scratch_suffix`` (typically the job id).
        """
        rows_table, _ = self._enrichment_scratch_tables(scratch_suffix)
        self.client.command(f"DROP TABLE IF EXISTS {rows_table}")
        self.client.command(
            f"CREATE TABLE {rows_table} "
            "(event_id UUID, field_key String, value String) "
            "ENGINE = MergeTree ORDER BY event_id"
        )

    def stage_enrichment_rows(self, scratch_suffix: str, chunk: list[tuple[str, str, str]]) -> int:
        """Insert one page of ``(event_id, field_key, value)`` triples into the scratch table.

        Callers can stream pages in one at a time (each discarded from Python
        memory after this returns) instead of materializing the whole
        source's triples before staging any of them — ``finalize_enrichment_apply``
        does the actual (expensive, one-shot) partition rewrite once every
        page has been staged.
        """
        if not chunk:
            return 0
        rows_table, _ = self._enrichment_scratch_tables(scratch_suffix)
        self.client.insert(
            table=rows_table,
            data=chunk,
            column_names=["event_id", "field_key", "value"],
        )
        return len(chunk)

    def finalize_enrichment_apply(
        self,
        case_id: str,
        source_id: str,
        scratch_suffix: str,
        owned_suffixes: list[str] | None = None,
    ) -> None:
        """Atomically merge the staged scratch rows into one source's ``events.attributes``.

        An enriched copy of the source's partition is built into a scratch
        events table via ``mapUpdate`` over a LEFT JOIN against the rows
        staged by ``stage_enrichment_rows``, and the live partition is
        swapped in one ``ALTER TABLE ... REPLACE PARTITION``. Idempotent —
        re-applying the same rows overwrites the same map keys with the same
        values — so a crashed apply can simply be re-run from the Postgres
        staging rows.

        ``owned_suffixes`` is the enricher's ``output_fields`` (the ``<field>``
        half of each derived ``<attr_key>:<field>`` key). Before merging, every
        existing attribute key ending in one of these suffixes is stripped, so
        a re-run whose output *shrank* — e.g. an updated GeoLite2 database that
        no longer resolves an IP — does not leave a stale derived value behind
        (mapUpdate alone only overwrites keys that are re-emitted). Left None
        for callers that only add keys; then nothing is stripped. Note: if two
        enrichers ever shared an output-field suffix, one's apply would strip
        the other's keys — today GeoIP is the only enricher, so suffixes are
        unique.

        Transiently doubles the partition's disk footprint (scratch copy).
        Caller must serialize applies per ``(case_id, source_id)`` — two
        concurrent REPLACEs would silently discard one side's keys.
        """
        rows_table, events_table = self._enrichment_scratch_tables(scratch_suffix)
        partition_expr = _partition_expr(case_id, source_id)
        # Strip this enricher's previously-derived keys (last ':'-segment in
        # owned_suffixes) before merging the fresh output, so stale values are
        # removed rather than lingering. With no suffixes the mapFilter is a
        # no-op (has() over an empty array is always false → nothing dropped).
        attr_source = (
            "mapFilter((k, v) -> NOT has({owned_suffixes:Array(String)}, "
            "splitByChar(':', k)[-1]), e.attributes)"
        )
        select_columns = ",\n            ".join(
            f"mapUpdate({attr_source}, m.enr) AS attributes"
            if column == "attributes"
            else f"e.{column}"
            for column in _EVENT_COLUMNS
        )
        self.client.command(f"DROP TABLE IF EXISTS {events_table}")
        # AS clones the full DDL (engine, ORDER BY, PARTITION BY, skip
        # indexes, settings) — required for REPLACE PARTITION.
        self.client.command(f"CREATE TABLE {events_table} AS {self.database}.events")
        self.client.query(
            f"""
            INSERT INTO {events_table} ({", ".join(_EVENT_COLUMNS)})
            SELECT
                {select_columns}
            FROM {self.database}.events AS e
            LEFT JOIN (
                SELECT
                    event_id,
                    CAST(
                        (groupArray(field_key), groupArray(value)),
                        'Map(String, String)'
                    ) AS enr
                FROM {rows_table}
                GROUP BY event_id
            ) AS m ON e.event_id = m.event_id
            WHERE e.case_id = {{case_id:String}} AND e.source_id = {{source_id:String}}
            SETTINGS join_use_nulls = 0
            """,
            parameters={
                "case_id": case_id,
                "source_id": source_id,
                "owned_suffixes": owned_suffixes or [],
            },
        )
        self.client.command(
            f"ALTER TABLE {self.database}.events "
            f"REPLACE PARTITION {partition_expr} FROM {events_table}"
        )

    def drop_enrichment_scratch(self, scratch_suffix: str) -> None:
        """Drop both scratch tables for ``scratch_suffix``, ignoring errors."""
        rows_table, events_table = self._enrichment_scratch_tables(scratch_suffix)
        with contextlib.suppress(Exception):
            self.client.command(f"DROP TABLE IF EXISTS {events_table}")
        with contextlib.suppress(Exception):
            self.client.command(f"DROP TABLE IF EXISTS {rows_table}")

    def drop_stale_enrichment_scratch_tables(self) -> int:
        """Drop scratch tables orphaned by a crash mid-apply. Returns how many were dropped."""
        result = self.client.query(
            "SELECT name FROM system.tables WHERE database = {db:String} AND name LIKE {p:String}",
            parameters={"db": self.database, "p": f"{_ENRICH_SCRATCH_PREFIX}%"},
        )
        names = [row[0] for row in result.result_rows]
        for name in names:
            with contextlib.suppress(Exception):
                self.client.command(f"DROP TABLE IF EXISTS {self.database}.{name}")
        return len(names)

    def count_events(
        self,
        case_id: str | None = None,
        source_id: str | None = None,
        source_ids: list[str] | None = None,
    ) -> int:
        """Return the number of events, optionally filtered by case/source."""
        query = f"SELECT count() FROM {self.database}.events"
        conditions: list[str] = []
        parameters: dict[str, str] = {}
        if case_id is not None:
            conditions.append("case_id = {case_id:String}")
            parameters["case_id"] = case_id
        if source_id is not None:
            conditions.append("source_id = {source_id:String}")
            parameters["source_id"] = source_id
        if source_ids is not None:
            if not source_ids:
                return 0
            source_in, source_binds = self._string_in_clause("s", source_ids)
            conditions.append(f"source_id IN ({source_in})")
            parameters.update(source_binds)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        result = self.client.query(query, parameters=parameters)
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

    def iter_source_events(
        self,
        case_id: str,
        source_id: str,
        batch_size: int,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield successive ``list_events`` batches for one source.

        Shared batching primitive for whole-source jobs (embedding pipeline,
        enrichers). Stops on an empty batch; a short batch also ends the
        iteration (the table can't grow mid-job — sources are ingest-once).
        Each ``next()`` issues one blocking query, so async callers should
        drive the iterator from a worker thread.
        """
        offset = 0
        while True:
            batch = self.list_events(
                case_id=case_id, source_id=source_id, limit=batch_size, offset=offset
            )
            if not batch:
                return
            yield batch
            if len(batch) < batch_size:
                return
            offset += len(batch)

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
        # Build parameterized IN clauses (distinct prefixes so the bindings
        # don't collide in the merged parameters dict).
        event_in, event_binds = self._string_in_clause("e", event_ids)
        source_in, source_binds = self._string_in_clause("s", source_ids)
        parameters: dict[str, str] = {**event_binds, **source_binds, "case_id": case_id}
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
        A missing partition is a server-side no-op; a missing ``events``
        table (fresh install, schema never initialized) is treated as a
        benign no-op too. Any other failure is logged and re-raised — a
        silently failed evidence delete would leave orphan events behind a
        "successful" delete, which is a forensic-integrity violation.
        """
        partition_expr = _partition_expr(case_id, source_id)
        try:
            self.client.command(
                f"ALTER TABLE {self.database}.events DROP PARTITION {partition_expr}"
            )
        except Exception as exc:
            if "UNKNOWN_TABLE" in str(exc):
                logger.debug(
                    "events table missing while deleting source %s (case %s); nothing to drop",
                    source_id,
                    case_id,
                )
                return
            logger.exception(
                "Failed to drop events partition for source %s (case %s)", source_id, case_id
            )
            raise
