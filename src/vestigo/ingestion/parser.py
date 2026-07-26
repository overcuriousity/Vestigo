"""Streaming parsers for Timesketch-compatible timeline formats.

Parsers are intentionally low-level: they read source files in a streaming
fashion, record byte offsets, compute per-record content hashes, and emit
:py:class:`~vestigo.models.event.Event` objects.  This preserves the
forensic invariant that every event can be traced back to an exact location
and hash of the immutable source file.
"""

from __future__ import annotations

import ast
import csv
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from vestigo.models.event import Event, ParserConfig, content_hash


def _raw_bytes_and_text(line: str) -> tuple[int, str]:
    """Return ``(byte_length, sanitized_text)`` for a surrogate-escaped line.

    Source files are decoded with ``errors="surrogateescape"`` so undecodable
    bytes survive round-tripping: re-encoding recovers the *exact* original
    bytes, which is the only way ``byte_offset`` can keep pointing at the real
    location in the immutable source file. Measuring ``errors="replace"`` text
    instead over-counts by two bytes per bad byte (U+FFFD encodes to three),
    so every offset after the first non-UTF-8 byte would be wrong.

    The text handed on is re-decoded with ``errors="replace"``, so the event
    payload sees the same U+FFFD substitution as before — lone surrogates
    would blow up JSON encoding and the ClickHouse insert.
    """
    raw = line.encode("utf-8", "surrogateescape")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return len(raw), raw.decode("utf-8", "replace")
    return len(raw), line


class _RecordTrackingIterator:
    """Track the byte offset, line number, and raw lines of each CSV record.

    ``csv.DictReader`` consumes one or more physical lines per logical record
    (e.g. for quoted fields containing newlines). This wrapper reads lines
    lazily from the underlying file and buffers only the lines of the record
    currently being consumed, so the caller can reconstruct the exact source
    bytes and byte offset without loading the whole file into memory —
    multi-GB CSV timelines are the target workload.

    ``csv.reader`` consumes exactly the lines of one logical record before
    yielding it (no read-ahead), so at ``finish_record`` time the buffer holds
    precisely that record's physical lines. Blank lines that ``DictReader``
    skips internally end up prepended to the next record's buffer — identical
    to the previous list-index-based implementation's behaviour.
    """

    def __init__(self, fh: Iterator[str], start_offset: int, start_line: int) -> None:
        self._fh = fh
        self._buffer: list[str] = []
        self._next_offset = start_offset
        self._record_offset = start_offset
        self._next_line = start_line
        self._record_line = start_line

    def __iter__(self) -> _RecordTrackingIterator:
        return self

    def __next__(self) -> str:
        line = next(self._fh)
        byte_length, text = _raw_bytes_and_text(line)
        self._buffer.append(text)
        self._next_offset += byte_length
        self._next_line += 1
        return text

    def finish_record(self) -> tuple[int, int, str]:
        """Return ``(byte_offset, line_number, raw_text)`` of the record just completed."""
        offset = self._record_offset
        line_number = self._record_line
        raw = "".join(self._buffer)
        self._buffer.clear()
        self._record_offset = self._next_offset
        self._record_line = self._next_line
        return offset, line_number, raw


def _normalise_tag_field(value: str) -> list[str]:
    """Split a Timesketch tag field into individual tags.

    Timesketch stores multiple tags separated by commas or pipes. Some
    exporters instead serialise the tag list as a JSON or Python literal
    (``["a", "b"]`` / ``['a', 'b']``), so list-shaped values are decoded
    before falling back to delimiter splitting.
    """
    if not value:
        return []
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        parsed = None
        try:
            parsed = json.loads(stripped)
        except ValueError:
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                parsed = None
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()]
    tags: list[str] = []
    for delimiter in (",", "|"):
        if delimiter in value:
            tags = [t.strip() for t in value.split(delimiter) if t.strip()]
            break
    if not tags:
        tags = [value.strip()]
    return tags


class Parser(ABC):
    """Abstract base class for Vestigo streaming parsers."""

    def __init__(
        self,
        case_id: str,
        source_id: str,
        config: ParserConfig,
        file_hash: str | None = None,
        source_name: str | None = None,
    ) -> None:
        self.case_id = case_id
        self.source_id = source_id
        self.config = config
        self.file_hash = file_hash
        self.source_name = source_name

    @abstractmethod
    def parse(self, path: Path) -> Iterator[Event]:
        """Yield :py:class:`Event` records from ``path``."""
        raise NotImplementedError

    def parse_arrow_batches(
        self,
        path: Path,
        on_progress: Callable[[int], None] | None = None,
    ) -> Iterator[Any] | None:
        """Optional bulk path: yield ``EVENT_ARROW_SCHEMA`` record batches.

        Parsers that can produce columnar batches directly (the Parquet
        reader) override this; the pipeline then bulk-inserts each batch via
        ``ClickHouseStore.insert_events_arrow`` instead of building ``Event``
        objects. ``on_progress`` receives bytes consumed within ``path``.
        Returning ``None`` (the default) selects the ``parse()`` event loop.
        """
        return None

    def _make_event(
        self,
        source_file: Path,
        byte_offset: int,
        line_number: int | None,
        raw_line: str,
        message: str,
        timestamp: str | None = None,
        timestamp_desc: str | None = None,
        artifact: str | None = None,
        artifact_long: str | None = None,
        display_name: str | None = None,
        tags: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        """Build an :py:class:`Event` with forensic metadata populated."""
        if not self.file_hash:
            raise ValueError(
                "A real file hash is required for forensic integrity. "
                "Rejecting ingestion without a file-level hash."
            )
        # Fall back to the read path when no explicit source name is supplied
        # (e.g. CLI one-off ingestion).
        provenance_file = Path(self.source_name) if self.source_name else source_file
        return Event(
            case_id=self.case_id,
            source_id=self.source_id,
            source_file=provenance_file,
            byte_offset=byte_offset,
            line_number=line_number,
            content_hash=content_hash(raw_line),
            file_hash=self.file_hash,
            parser_name=self.config.name,
            parser_version=self.config.version,
            raw_line=raw_line,
            message=message,
            timestamp=timestamp,
            timestamp_desc=timestamp_desc,
            artifact=artifact,
            artifact_long=artifact_long,
            display_name=display_name,
            tags=tags or [],
            attributes=attributes or {},
        )


class TimesketchCsvParser(Parser):
    """Streaming parser for Timesketch-compatible CSV files.

    Recognises common Timesketch column names and maps them to event fields.
    Any columns not in the known mapping are preserved in ``attributes``.

    Note:
        Quoted fields containing embedded newlines are handled: lines are
        streamed through :class:`_RecordTrackingIterator`, which groups the
        physical lines of each logical CSV record and tracks its exact byte
        offset, line number, and raw text.
    """

    KNOWN_COLUMNS: dict[str, str] = {
        "datetime": "timestamp",
        "timestamp_desc": "timestamp_desc",
        "timestamp": "timestamp",
        "message": "message",
        "source": "artifact",
        "source_long": "artifact_long",
        "parser": "parser",
        "display_name": "display_name",
        "tag": "tags",
        "tags": "tags",
    }

    def parse(self, path: Path) -> Iterator[Event]:
        """Yield events from a Timesketch-compatible CSV file."""
        source_file = path.resolve()
        with source_file.open("r", encoding="utf-8", newline="", errors="surrogateescape") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            # Timesketch CSV exports use "" to escape quotes inside quoted
            # fields (e.g. log messages containing quotes). Force this even
            # when the sniffer guesses differently.
            dialect.doublequote = True

            # Read header line manually so we know the exact byte boundary.
            header_line = fh.readline()
            if not header_line:
                return
            header_bytes, header_text = _raw_bytes_and_text(header_line)
            header_reader = csv.reader([header_text], dialect=dialect)
            headers = next(header_reader, None) or []
            headers = [h.strip() if h else h for h in headers]

            # Stream the remaining lines through the tracking wrapper —
            # csv.DictReader groups them into logical records (including
            # quoted multi-line fields) while the wrapper tracks byte
            # offsets and line numbers incrementally.
            wrapper = _RecordTrackingIterator(fh, start_offset=header_bytes, start_line=2)
            row_reader = csv.DictReader(
                wrapper,
                fieldnames=headers,
                dialect=dialect,
            )
            for row in row_reader:
                byte_offset, line_number, raw_line = wrapper.finish_record()
                if not raw_line.strip():
                    continue
                yield self._event_from_row(
                    source_file,
                    byte_offset,
                    line_number,
                    raw_line,
                    row,
                )

    def _event_from_row(
        self,
        path: Path,
        byte_offset: int,
        line_number: int,
        raw_line: str,
        row: dict[str, str],
    ) -> Event:
        """Map a CSV row to an :py:class:`Event`."""
        mapped: dict[str, Any] = {}
        attributes: dict[str, Any] = {}
        for key, value in row.items():
            if key is None:
                continue
            normalised_key = key.strip().lower()
            mapped_key = self.KNOWN_COLUMNS.get(normalised_key)
            if mapped_key == "tags":
                mapped["tags"] = _normalise_tag_field(value)
            elif mapped_key:
                mapped[mapped_key] = value
            else:
                attributes[key] = value

        message = mapped.get("message") or raw_line.strip()
        tags: list[str] = mapped.get("tags", [])
        if not tags and "tag" in attributes:
            tags = _normalise_tag_field(attributes.pop("tag"))

        return self._make_event(
            source_file=path,
            byte_offset=byte_offset,
            line_number=line_number,
            raw_line=raw_line,
            message=message,
            timestamp=mapped.get("timestamp"),
            timestamp_desc=mapped.get("timestamp_desc"),
            artifact=mapped.get("artifact"),
            artifact_long=mapped.get("artifact_long"),
            display_name=mapped.get("display_name"),
            tags=tags,
            attributes=attributes,
        )


class JsonlParser(Parser):
    """Streaming parser for JSON Lines files.

    Each line must contain one JSON object.  Common keys are mapped to event
    fields; remaining keys are preserved in ``attributes``.  Lines that are
    malformed JSON or well-formed non-object JSON (arrays, strings, numbers,
    ``null``) are skipped; the raw lines remain in the immutable source file.
    """

    KNOWN_KEYS: dict[str, str] = {
        "datetime": "timestamp",
        "timestamp": "timestamp",
        "timestamp_desc": "timestamp_desc",
        "message": "message",
        "msg": "message",
        "source": "artifact",
        "source_long": "artifact_long",
        "parser": "parser",
        "display_name": "display_name",
        "tag": "tags",
        "tags": "tags",
    }

    def parse(self, path: Path) -> Iterator[Event]:
        """Yield events from a JSONL file."""
        source_file = path.resolve()
        with source_file.open("r", encoding="utf-8", errors="surrogateescape") as fh:
            byte_offset = 0
            line_number = 0
            for line in fh:
                line_number += 1
                byte_length, raw_line = _raw_bytes_and_text(line)
                current_offset = byte_offset
                byte_offset += byte_length
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    # Forensic rigor: malformed lines are skipped but the raw line
                    # remains in the immutable source file for manual inspection.
                    continue
                if not isinstance(obj, dict):
                    # Well-formed but non-object JSON (array, string, number,
                    # null) has no key/value fields to map — skipped like a
                    # malformed line; the raw line stays in the immutable
                    # source file for manual inspection.
                    continue
                yield self._event_from_object(
                    source_file, current_offset, line_number, raw_line, obj
                )

    def _event_from_object(
        self,
        path: Path,
        byte_offset: int,
        line_number: int,
        raw_line: str,
        obj: dict[str, Any],
    ) -> Event:
        """Map a JSON object to an :py:class:`Event`."""
        mapped: dict[str, Any] = {}
        attributes: dict[str, Any] = {}
        for key, value in obj.items():
            mapped_key = self.KNOWN_KEYS.get(key.lower())
            if mapped_key == "tags":
                if isinstance(value, list):
                    mapped["tags"] = [str(v) for v in value]
                elif isinstance(value, str):
                    mapped["tags"] = _normalise_tag_field(value)
                else:
                    mapped["tags"] = [str(value)]
            elif mapped_key:
                mapped[mapped_key] = str(value) if value is not None else None
            else:
                attributes[key] = value

        message = mapped.get("message") or raw_line.strip()
        tags: list[str] = mapped.get("tags", [])

        return self._make_event(
            source_file=path,
            byte_offset=byte_offset,
            line_number=line_number,
            raw_line=raw_line,
            message=message,
            timestamp=mapped.get("timestamp"),
            timestamp_desc=mapped.get("timestamp_desc"),
            artifact=mapped.get("artifact"),
            artifact_long=mapped.get("artifact_long"),
            display_name=mapped.get("display_name"),
            tags=tags,
            attributes=attributes,
        )


def get_parser(
    format_name: str,
    case_id: str,
    source_id: str,
    options: dict[str, Any] | None = None,
    file_hash: str | None = None,
    source_name: str | None = None,
) -> Parser:
    """Return a parser instance for ``format_name``.

    Supported formats:
      - ``timesketch_csv`` / ``csv``: Timesketch-compatible CSV.
      - ``jsonl`` / ``json``: JSON Lines.
      - ``vestigo_parquet`` / ``parquet``: Vestigo interchange Parquet
        produced by a converter script; the effective parser identity
        (name/version) comes from the file's footer metadata, not from this
        config.

    Args:
        format_name: Parser format identifier.
        case_id: Investigation case identifier.
        source_id: Source identifier within the case.
        options: Optional parser-specific options.
        file_hash: SHA-256 hex digest of the whole source file. Required for
            forensic integrity; ingestion is rejected when not supplied.
        source_name: Optional provenance name (e.g. original filename) to store
            as ``source_file`` instead of the transient read path.
    """
    config = ParserConfig(
        name=format_name,
        version="0.1.0",
        options=options or {},
    )
    name = format_name.lower()
    if name in {"timesketch_csv", "csv"}:
        return TimesketchCsvParser(
            case_id, source_id, config, file_hash=file_hash, source_name=source_name
        )
    if name in {"jsonl", "json"}:
        return JsonlParser(case_id, source_id, config, file_hash=file_hash, source_name=source_name)
    if name in {"vestigo_parquet", "parquet"}:
        # Imported lazily: parquet_reader pulls in pyarrow.parquet, which the
        # common CSV/JSONL paths never need.
        from vestigo.ingestion.parquet_reader import ParquetEventsParser

        return ParquetEventsParser(
            case_id, source_id, config, file_hash=file_hash, source_name=source_name
        )
    raise ValueError(f"Unsupported parser format: {format_name}")


def detect_format(path: Path) -> str:
    """Infer parser format from file extension.

    Falls back to ``jsonl`` for ``.json`` and ``timesketch_csv`` for ``.csv``.
    """
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        return "timesketch_csv"
    if suffix in {".jsonl", ".json", ".ndjson"}:
        return "jsonl"
    if suffix == ".parquet":
        return "vestigo_parquet"
    raise ValueError(f"Cannot detect parser format for: {path}")
