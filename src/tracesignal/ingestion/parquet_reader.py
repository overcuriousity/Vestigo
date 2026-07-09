"""Server-side reader for TraceSignal interchange Parquet files.

Converter scripts (``assets/converters/*2tracesignal.py``) parse raw evidence
logs client-side and upload one ``.parquet`` file; this parser maps its rows
onto the ClickHouse ``events`` schema without re-parsing — the hot path is
``parse_arrow_batches()``, which stamps the server-side columns (``event_id``,
``case_id``/``source_id``, parser identity from the footer, ``ingest_time``)
onto each record batch and hands it to ``ClickHouseStore.insert_events_arrow``.

Forensic identity: ``Event.file_hash``/``byte_offset``/``content_hash`` come
from the converter's per-row columns and refer to the **original raw evidence
file**, not the uploaded parquet; ``parser_name``/``parser_version`` are the
converter's embedded name/version. ``event_id`` therefore matches what
:class:`~tracesignal.models.event.Event` would derive for the same inputs
(see ``derive_event_id``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tracesignal.db._arrow_schema import EVENT_ARROW_SCHEMA
from tracesignal.db._dt import NULL_TS_SENTINEL, is_null_ts_sentinel
from tracesignal.ingestion.parquet_format import (
    ParquetSourceMeta,
    validate_parquet_source,
)
from tracesignal.ingestion.parser import Parser
from tracesignal.models.event import Event, derive_event_id


class ParquetEventsParser(Parser):
    """Parser for TraceSignal interchange Parquet uploads."""

    def read_source_meta(self, path: Path) -> ParquetSourceMeta:
        """Validate ``path`` and return its converter provenance metadata."""
        pf = pq.ParquetFile(path)
        try:
            return validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        finally:
            pf.close()

    def parse_arrow_batches(
        self,
        path: Path,
        on_progress: Callable[[int], None] | None = None,
    ) -> Iterator[pa.RecordBatch] | None:
        """Yield ``EVENT_ARROW_SCHEMA`` batches for the pipeline's bulk path."""
        return self._batches(path, on_progress)

    def _batches(
        self,
        path: Path,
        on_progress: Callable[[int], None] | None = None,
    ) -> Iterator[pa.RecordBatch]:
        from tracesignal.core.config import get_settings

        batch_size = get_settings().ingest_batch_size
        with pq.ParquetFile(path) as pf:
            meta = validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            total_rows = pf.metadata.num_rows
            file_bytes = path.stat().st_size
            # One shared ingest stamp per upload, like the Event default would
            # produce per object — but constant across the file, which is the
            # honest representation of a single bulk ingest.
            ingest_time = datetime.now(UTC)
            rows_done = 0
            for batch in pf.iter_batches(batch_size=batch_size):
                yield self._stamp_batch(batch, meta, ingest_time)
                rows_done += batch.num_rows
                if on_progress is not None and total_rows:
                    on_progress(int(file_bytes * rows_done / total_rows))

    def _stamp_batch(
        self,
        batch: pa.RecordBatch,
        meta: ParquetSourceMeta,
        ingest_time: datetime,
    ) -> pa.RecordBatch:
        """Map an interchange batch onto the full ClickHouse event schema."""
        n = batch.num_rows

        # Provenance columns anchor event identity (event_id is derived from
        # them). A null here would let the stored column and the id that
        # certifies it diverge — content_hash/file_hash fill to "" below while
        # the id was derived from None, and a null byte_offset would collide
        # with a legitimate offset-0 row. Reject upfront rather than silently
        # corrupt forensic provenance.
        for name in ("file_hash", "byte_offset", "content_hash", "source_file"):
            if batch.column(name).null_count:
                raise ValueError(
                    f"TraceSignal Parquet file has null values in provenance "
                    f"column {name!r}; every row must carry its raw-evidence "
                    "provenance. Re-create the file with a current converter."
                )

        byte_offsets = batch.column("byte_offset").to_pylist()
        content_hashes = batch.column("content_hash").to_pylist()
        file_hashes = batch.column("file_hash").to_pylist()
        event_ids = [
            str(
                derive_event_id(
                    case_id=self.case_id,
                    source_id=self.source_id,
                    source_identity=file_hash,
                    byte_offset=byte_offset,
                    content_hash=content_hash,
                    parser_name=meta.converter_name,
                    parser_version=meta.converter_version,
                )
            )
            for file_hash, byte_offset, content_hash in zip(
                file_hashes, byte_offsets, content_hashes, strict=True
            )
        ]

        def _const(value: str) -> pa.Array:
            return pa.array([value] * n, type=pa.string())

        def _text(name: str) -> pa.Array:
            return pc.fill_null(batch.column(name), "")

        ts_type = EVENT_ARROW_SCHEMA.field("timestamp").type
        timestamp = pc.fill_null(
            batch.column("timestamp").cast(ts_type), pa.scalar(NULL_TS_SENTINEL, type=ts_type)
        )

        arrays = [
            pa.array(event_ids, type=pa.string()),
            _const(self.case_id),
            _const(self.source_id),
            _text("source_file"),
            batch.column("byte_offset"),
            pa.array([0] * n, type=pa.uint64()),  # line_number: not populated
            _text("content_hash"),
            _text("file_hash"),
            _const(meta.converter_name),
            _const(meta.converter_version),
            pa.array([ingest_time] * n, type=EVENT_ARROW_SCHEMA.field("ingest_time").type),
            _text("message"),
            timestamp,
            _text("timestamp_desc"),
            _text("artifact"),
            _text("artifact_long"),
            _text("display_name"),
            batch.column("tags"),
            batch.column("attributes"),
            _const(""),  # embedding_model
            _const(""),  # embedding_config_hash
        ]
        return pa.RecordBatch.from_arrays(arrays, schema=EVENT_ARROW_SCHEMA)

    def parse(self, path: Path) -> Iterator[Event]:
        """Sequential ``Event`` generator — compatibility path, not the hot one.

        Events are constructed directly (not via ``_make_event``) because the
        provenance columns come from the converter: ``content_hash`` is the
        hash of the original raw line (which this reader never sees in raw
        form) and ``file_hash`` is per-row.
        """
        for batch in self._batches(path):
            for row in batch.to_pylist():
                event = Event(
                    case_id=self.case_id,
                    source_id=self.source_id,
                    source_file=Path(row["source_file"]),
                    byte_offset=row["byte_offset"],
                    content_hash=row["content_hash"],
                    file_hash=row["file_hash"],
                    parser_name=row["parser_name"],
                    parser_version=row["parser_version"],
                    raw_line=row["message"],
                    message=row["message"],
                    timestamp=(
                        None
                        if row["timestamp"] is None or is_null_ts_sentinel(row["timestamp"])
                        else row["timestamp"].isoformat()
                    ),
                    timestamp_desc=row["timestamp_desc"] or None,
                    artifact=row["artifact"] or None,
                    artifact_long=row["artifact_long"] or None,
                    display_name=row["display_name"] or None,
                    tags=row["tags"] or [],
                    attributes=dict(row["attributes"] or {}),
                )
                # The batch path derives event_id without Event's
                # content-hash recomputation; identities must agree.
                assert str(event.event_id) == row["event_id"]
                yield event
