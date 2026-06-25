"""Streaming parsers for Timesketch-compatible timeline formats.

Parsers are intentionally low-level: they read source files in a streaming
fashion, record byte offsets, compute per-record content hashes, and emit
:py:class:`~tracevector.models.event.Event` objects.  This preserves the
forensic invariant that every event can be traced back to an exact location
and hash of the immutable source file.
"""

from __future__ import annotations

import csv
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tracevector.models.event import Event, ParserConfig, content_hash


class _RecordTrackingIterator:
    """Track the start index and raw lines of each CSV record.

    ``csv.DictReader`` consumes one or more physical lines per logical record
    (e.g. for quoted fields containing newlines). This wrapper records which
    lines belong to each record so the caller can reconstruct the exact source
    bytes and byte offset.
    """

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.index = 0
        self.record_start_index = 0

    def __iter__(self) -> _RecordTrackingIterator:
        return self

    def __next__(self) -> str:
        if self.index >= len(self.lines):
            raise StopIteration
        line = self.lines[self.index]
        self.index += 1
        return line

    def finish_record(self) -> tuple[int, int]:
        """Return the line indices of the record just completed."""
        start = self.record_start_index
        self.record_start_index = self.index
        return start, self.index


def _normalise_tag_field(value: str) -> list[str]:
    """Split a Timesketch tag field into individual tags.

    Timesketch stores multiple tags separated by commas or pipes.
    """
    if not value:
        return []
    tags: list[str] = []
    for delimiter in (",", "|"):
        if delimiter in value:
            tags = [t.strip() for t in value.split(delimiter) if t.strip()]
            break
    if not tags:
        tags = [value.strip()]
    return tags


class Parser(ABC):
    """Abstract base class for TraceVector streaming parsers."""

    def __init__(self, case_id: str, timeline_id: str, config: ParserConfig) -> None:
        self.case_id = case_id
        self.timeline_id = timeline_id
        self.config = config

    @abstractmethod
    def parse(self, path: Path) -> Iterator[Event]:
        """Yield :py:class:`Event` records from ``path``."""
        raise NotImplementedError

    def _make_event(
        self,
        source_file: Path,
        byte_offset: int,
        line_number: int | None,
        raw_line: str,
        message: str,
        timestamp: str | None = None,
        timestamp_desc: str | None = None,
        source: str | None = None,
        source_long: str | None = None,
        display_name: str | None = None,
        tags: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        """Build an :py:class:`Event` with forensic metadata populated."""
        return Event(
            case_id=self.case_id,
            timeline_id=self.timeline_id,
            source_file=source_file,
            byte_offset=byte_offset,
            line_number=line_number,
            content_hash=content_hash(raw_line),
            parser_name=self.config.name,
            parser_version=self.config.version,
            raw_line=raw_line,
            message=message,
            timestamp=timestamp,
            timestamp_desc=timestamp_desc,
            source=source,
            source_long=source_long,
            display_name=display_name,
            tags=tags or [],
            attributes=attributes or {},
        )


class TimesketchCsvParser(Parser):
    """Streaming parser for Timesketch-compatible CSV files.

    Recognises common Timesketch column names and maps them to event fields.
    Any columns not in the known mapping are preserved in ``attributes``.

    Note:
        This parser reads one physical line at a time.  CSV records with
        embedded newlines inside quoted fields are not split correctly; for
        forensic timeline exports this is acceptable because such records are
        rare and the raw line is preserved verbatim for manual review.
    """

    KNOWN_COLUMNS: dict[str, str] = {
        "datetime": "timestamp",
        "timestamp_desc": "timestamp_desc",
        "timestamp": "timestamp",
        "message": "message",
        "source": "source",
        "source_long": "source_long",
        "parser": "parser",
        "display_name": "display_name",
        "tag": "tags",
        "tags": "tags",
    }

    def parse(self, path: Path) -> Iterator[Event]:
        """Yield events from a Timesketch-compatible CSV file."""
        source_file = path.resolve()
        with source_file.open("r", encoding="utf-8", newline="", errors="replace") as fh:
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
            header_reader = csv.reader([header_line], dialect=dialect)
            headers = next(header_reader, None) or []
            headers = [h.strip() if h else h for h in headers]
            header_bytes = len(header_line.encode("utf-8"))

            # Read all physical lines up front. csv.DictReader will correctly
            # group them into logical records (including quoted multi-line
            # fields), and we can compute byte offsets from the line list.
            lines = list(fh)
            line_byte_offsets = [0]
            for line in lines:
                line_byte_offsets.append(
                    line_byte_offsets[-1] + len(line.encode("utf-8"))
                )

            wrapper = _RecordTrackingIterator(lines)
            row_reader = csv.DictReader(
                wrapper,
                fieldnames=headers,
                dialect=dialect,
            )
            for row in row_reader:
                start_idx, end_idx = wrapper.finish_record()
                raw_line = "".join(lines[start_idx:end_idx])
                if not raw_line.strip():
                    continue
                byte_offset = header_bytes + line_byte_offsets[start_idx]
                line_number = 2 + start_idx  # header is line 1, first data row line 2
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
            source=mapped.get("source"),
            source_long=mapped.get("source_long"),
            display_name=mapped.get("display_name"),
            tags=tags,
            attributes=attributes,
        )


class JsonlParser(Parser):
    """Streaming parser for JSON Lines files.

    Each line must contain one JSON object.  Common keys are mapped to event
    fields; remaining keys are preserved in ``attributes``.
    """

    KNOWN_KEYS: dict[str, str] = {
        "datetime": "timestamp",
        "timestamp": "timestamp",
        "timestamp_desc": "timestamp_desc",
        "message": "message",
        "msg": "message",
        "source": "source",
        "source_long": "source_long",
        "parser": "parser",
        "display_name": "display_name",
        "tag": "tags",
        "tags": "tags",
    }

    def parse(self, path: Path) -> Iterator[Event]:
        """Yield events from a JSONL file."""
        source_file = path.resolve()
        with source_file.open("r", encoding="utf-8", errors="replace") as fh:
            byte_offset = 0
            line_number = 0
            for raw_line in fh:
                line_number += 1
                current_offset = byte_offset
                byte_offset += len(raw_line.encode("utf-8"))
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    # Forensic rigor: malformed lines are skipped but the raw line
                    # remains in the immutable source file for manual inspection.
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
            source=mapped.get("source"),
            source_long=mapped.get("source_long"),
            display_name=mapped.get("display_name"),
            tags=tags,
            attributes=attributes,
        )


def get_parser(
    format_name: str,
    case_id: str,
    timeline_id: str,
    options: dict[str, Any] | None = None,
) -> Parser:
    """Return a parser instance for ``format_name``.

    Supported formats:
      - ``timesketch_csv`` / ``csv``: Timesketch-compatible CSV.
      - ``jsonl`` / ``json``: JSON Lines.
    """
    config = ParserConfig(
        name=format_name,
        version="0.1.0",
        options=options or {},
    )
    name = format_name.lower()
    if name in {"timesketch_csv", "csv"}:
        return TimesketchCsvParser(case_id, timeline_id, config)
    if name in {"jsonl", "json"}:
        return JsonlParser(case_id, timeline_id, config)
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
    raise ValueError(f"Cannot detect parser format for: {path}")
