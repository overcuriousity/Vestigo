"""Tests for Vestigo streaming parsers."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vestigo.db._dt import NULL_TS_SENTINEL
from vestigo.ingestion.parser import (
    JsonlParser,
    TimesketchCsvParser,
    _normalise_tag_field,
    detect_format,
    get_parser,
)
from vestigo.models.event import Event, ParserConfig, content_hash


@pytest.fixture
def timesketch_csv(tmp_path: Path) -> Path:
    """Create a sample Timesketch-compatible CSV file."""
    path = tmp_path / "timeline.csv"
    path.write_text(
        "datetime,timestamp_desc,source,source_long,message,parser,display_name,tag\n"
        "2024-01-01T00:00:00+00:00,Creation Time,LOG,Syslog,User login,user,auth.log,login|success\n"
        "2024-01-01T00:01:00+00:00,Creation Time,LOG,Syslog,User logout,user,auth.log,logout\n"
    )
    return path


@pytest.fixture
def jsonl_file(tmp_path: Path) -> Path:
    """Create a sample JSONL file."""
    path = tmp_path / "timeline.jsonl"
    path.write_text(
        '{"timestamp":"2024-01-01T00:00:00+00:00","timestamp_desc":"created","message":"User login","source":"auth","tags":["login","success"],"extra_field":"value1"}\n'
        '{"timestamp":"2024-01-01T00:01:00+00:00","timestamp_desc":"created","message":"User logout","source":"auth","tags":"logout","extra_field":"value2"}\n'
    )
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        ("login", ["login"]),
        ("login|success", ["login", "success"]),
        ("login,success", ["login", "success"]),
        ('["API Error", "Sensitive File"]', ["API Error", "Sensitive File"]),
        ("['API Error', 'Sensitive File']", ["API Error", "Sensitive File"]),
        ("[]", []),
        ('["single"]', ["single"]),
        # Malformed list literal falls back to comma splitting.
        ('["broken", "list"', ['["broken"', '"list"']),
    ],
)
def test_normalise_tag_field(value: str, expected: list[str]) -> None:
    assert _normalise_tag_field(value) == expected


def test_detect_format(tmp_path: Path) -> None:
    assert detect_format(tmp_path / "test.csv") == "timesketch_csv"
    assert detect_format(tmp_path / "test.jsonl") == "jsonl"
    assert detect_format(tmp_path / "test.json") == "jsonl"
    with pytest.raises(ValueError):
        detect_format(tmp_path / "test.unknown")


def test_get_parser_unsupported() -> None:
    with pytest.raises(ValueError, match="Unsupported parser format"):
        get_parser("unknown", "case1", "source1", file_hash="h")


def test_csv_parser_maps_common_fields(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser(
        "case1", "source1", config, file_hash="hash1", source_name="source.csv"
    )
    events = list(parser.parse(timesketch_csv))

    assert len(events) == 2
    first, second = events

    assert first.case_id == "case1"
    assert first.source_id == "source1"
    assert first.parser_name == "timesketch_csv"
    assert first.parser_version == "0.1.0"
    assert first.timestamp == "2024-01-01T00:00:00+00:00"
    assert first.timestamp_desc == "Creation Time"
    assert first.artifact == "LOG"
    assert first.artifact_long == "Syslog"
    assert first.message == "User login"
    assert first.display_name == "auth.log"
    assert first.tags == ["login", "success"]
    assert first.line_number == 2
    assert first.byte_offset == len(timesketch_csv.read_text().splitlines(keepends=True)[0])

    assert second.message == "User logout"
    assert second.tags == ["logout"]


def test_csv_parser_content_hash_is_raw_line(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser(
        "case1", "source1", config, file_hash="hash1", source_name="source.csv"
    )
    events = list(parser.parse(timesketch_csv))

    raw_lines = timesketch_csv.read_text().splitlines(keepends=True)[1:]
    for event, raw_line in zip(events, raw_lines, strict=False):
        assert event.content_hash == content_hash(raw_line)
        assert event.raw_line == raw_line


def test_csv_parser_event_id_is_deterministic(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser(
        "case1", "source1", config, file_hash="hash1", source_name="source.csv"
    )
    first_run = [e.event_id for e in parser.parse(timesketch_csv)]
    second_run = [e.event_id for e in parser.parse(timesketch_csv)]
    assert first_run == second_run


def test_event_id_uses_file_hash_not_path(tmp_path: Path) -> None:
    """Identical content under different temp paths yields identical IDs when file_hash matches."""
    content = "datetime,message\n2024-01-01T00:00:00+00:00,Hello\n"
    path_a = tmp_path / "a.csv"
    path_b = tmp_path / "b.csv"
    path_a.write_text(content)
    path_b.write_text(content)

    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser_a = TimesketchCsvParser(
        "case1", "source1", config, file_hash="same_hash", source_name="source.csv"
    )
    parser_b = TimesketchCsvParser(
        "case1", "source1", config, file_hash="same_hash", source_name="source.csv"
    )
    ids_a = [e.event_id for e in parser_a.parse(path_a)]
    ids_b = [e.event_id for e in parser_b.parse(path_b)]
    assert ids_a == ids_b
    # Provenance should be the supplied source name, not the temp path.
    assert parser_a.parse(path_a).__next__().source_file == Path("source.csv")


def test_different_file_hash_produces_different_event_ids(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser_a = TimesketchCsvParser(
        "case1", "source1", config, file_hash="hash_a", source_name="a.csv"
    )
    parser_b = TimesketchCsvParser(
        "case1", "source1", config, file_hash="hash_b", source_name="b.csv"
    )
    ids_a = [e.event_id for e in parser_a.parse(timesketch_csv)]
    ids_b = [e.event_id for e in parser_b.parse(timesketch_csv)]
    assert ids_a != ids_b


def test_csv_parser_preserves_unknown_columns(timesketch_csv: Path, tmp_path: Path) -> None:
    path = tmp_path / "extra.csv"
    path.write_text("datetime,message,unknown_column\n2024-01-01T00:00:00+00:00,Hello,world\n")
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser(
        "case1", "source1", config, file_hash="hash1", source_name="source.csv"
    )
    events = list(parser.parse(path))
    assert len(events) == 1
    assert events[0].attributes == {"unknown_column": "world"}


def test_jsonl_parser_maps_common_fields(jsonl_file: Path) -> None:
    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "source1", config, file_hash="hash1", source_name="source.jsonl")
    events = list(parser.parse(jsonl_file))

    assert len(events) == 2
    first, second = events

    assert first.message == "User login"
    assert first.tags == ["login", "success"]
    assert first.timestamp == "2024-01-01T00:00:00+00:00"
    assert first.attributes == {"extra_field": "value1"}
    assert first.line_number == 1

    assert second.message == "User logout"
    assert second.tags == ["logout"]
    assert second.attributes == {"extra_field": "value2"}


def test_jsonl_parser_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"message":"good"}\nthis is not json\n{"message":"also good"}\n')
    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "source1", config, file_hash="hash1", source_name="source.jsonl")
    events = list(parser.parse(path))
    assert len(events) == 2
    assert events[0].message == "good"
    assert events[1].message == "also good"


def test_jsonl_parser_skips_non_object_json_lines(tmp_path: Path) -> None:
    """Well-formed non-object JSON (array/string/null/number) must be skipped,
    not crash the whole file's ingest with an AttributeError."""
    path = tmp_path / "mixed.jsonl"
    path.write_text('{"message":"good"}\n[1,2,3]\n"hello"\nnull\n42\n{"message":"also good"}\n')
    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "source1", config, file_hash="hash1", source_name="source.jsonl")
    events = list(parser.parse(path))
    assert [e.message for e in events] == ["good", "also good"]
    assert events[1].line_number == 6


def test_jsonl_byte_offsets_survive_invalid_utf8(tmp_path: Path) -> None:
    """A non-UTF-8 byte must not shift every later byte_offset.

    Text decoded with ``errors="replace"`` turns each bad byte into U+FFFD,
    which re-encodes to three bytes — measuring the decoded line therefore
    over-counts and breaks the event-to-source-byte invariant for the whole
    rest of the file.
    """
    path = tmp_path / "latin1.jsonl"
    line1 = b'{"message":"caf\xe9 broken"}\n'
    line2 = b'{"message":"second"}\n'
    path.write_bytes(line1 + line2)

    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "source1", config, file_hash="hash1", source_name="s.jsonl")
    events = list(parser.parse(path))

    assert [e.byte_offset for e in events] == [0, len(line1)]
    raw = path.read_bytes()
    for event, expected in zip(events, (line1, line2), strict=True):
        assert raw[event.byte_offset : event.byte_offset + len(expected)] == expected


def test_csv_byte_offsets_survive_invalid_utf8(tmp_path: Path) -> None:
    """Same invariant for the CSV record tracker, including the header."""
    path = tmp_path / "latin1.csv"
    header = b"datetime,message\n"
    row1 = b"2024-01-01T00:00:00+00:00,caf\xe9 broken\n"
    row2 = b"2024-01-01T00:01:00+00:00,second\n"
    path.write_bytes(header + row1 + row2)

    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser("case1", "source1", config, file_hash="hash1", source_name="s.csv")
    events = list(parser.parse(path))

    assert [e.byte_offset for e in events] == [len(header), len(header) + len(row1)]
    raw = path.read_bytes()
    for event, expected in zip(events, (row1, row2), strict=True):
        assert raw[event.byte_offset : event.byte_offset + len(expected)] == expected


def test_jsonl_byte_offsets_are_bytes_not_characters(tmp_path: Path) -> None:
    """Valid multi-byte UTF-8 must count bytes, not characters.

    The ASCII fast path in ``_raw_bytes_and_text`` returns ``len(line)``, which
    is only equal to the byte length while the line is ASCII — a line that is
    valid UTF-8 but not ASCII has to fall through to the encode.
    """
    path = tmp_path / "utf8.jsonl"
    line1 = '{"message":"café ☕"}\n'.encode()
    line2 = b'{"message":"ascii"}\n'
    path.write_bytes(line1 + line2)
    assert len(line1) > len(line1.decode())  # multi-byte, so the counts differ

    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "source1", config, file_hash="hash1", source_name="s.jsonl")
    events = list(parser.parse(path))

    assert [e.byte_offset for e in events] == [0, len(line1)]
    assert events[0].message == "café ☕"


def test_invalid_utf8_still_yields_clean_text(tmp_path: Path) -> None:
    """Undecodable bytes stay replaced by U+FFFD in the event payload — the
    stored message must never carry lone surrogates into JSON/ClickHouse."""
    path = tmp_path / "latin1.jsonl"
    path.write_bytes(b'{"message":"caf\xe9"}\n')

    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "source1", config, file_hash="hash1", source_name="s.jsonl")
    (event,) = list(parser.parse(path))

    assert event.message == "caf�"
    event.message.encode("utf-8")  # would raise on a surrogate


def test_missing_file_hash_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_text("datetime,message\n2024-01-01T00:00:00+00:00,Hello\n")
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser("case1", "source1", config)
    with pytest.raises(ValueError, match="real file hash"):
        next(parser.parse(path))


def test_event_text_for_embedding() -> None:
    event = Event(
        case_id="c",
        source_id="s",
        source_file=Path("/tmp/test.log"),
        byte_offset=0,
        content_hash="abc",
        parser_name="p",
        parser_version="1",
        raw_line='{"message":"login"}',
        message="User login",
        timestamp="2024-01-01T00:00:00+00:00",
        timestamp_desc="created",
        artifact="auth",
        tags=["login"],
        attributes={"ip": "10.0.0.1"},
    )
    text = event.text_for_embedding()
    assert "User login" in text
    assert "artifact=auth" in text
    assert "tags=login" in text
    assert "ip=10.0.0.1" in text


def test_parse_timestamp_normalizes_common_formats() -> None:
    from vestigo.models.event import _parse_timestamp

    assert _parse_timestamp("2024-01-01T00:00:00+00:00") == datetime(
        2024, 1, 1, 0, 0, 0, tzinfo=UTC
    )
    with pytest.warns(UserWarning, match="Naive timestamp"):
        assert _parse_timestamp("2024-01-01 00:00:00") == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert _parse_timestamp("1704067200") == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert _parse_timestamp("1704067200000") == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert _parse_timestamp("1764367341913908") == datetime(
        2025, 11, 28, 22, 2, 21, 913908, tzinfo=UTC
    )
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-date") is None


def test_event_to_clickhouse_row_parses_timestamp() -> None:
    event = Event(
        case_id="c",
        source_id="s",
        source_file=Path("/tmp/test.log"),
        byte_offset=0,
        content_hash="abc",
        parser_name="p",
        parser_version="1",
        raw_line="line",
        message="msg",
        timestamp="2024-01-01T00:00:00+00:00",
    )
    row = event.to_clickhouse_row()
    assert row["timestamp"] == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_event_to_clickhouse_row_sentinel_for_bad_timestamp() -> None:
    # Unparsable timestamps store the year-2299 sentinel (the column is a
    # non-Nullable MergeTree sort key); serialization presents it as null.
    event = Event(
        case_id="c",
        source_id="s",
        source_file=Path("/tmp/test.log"),
        byte_offset=0,
        content_hash="abc",
        parser_name="p",
        parser_version="1",
        raw_line="line",
        message="msg",
        timestamp="not-a-date",
    )
    row = event.to_clickhouse_row()
    assert row["timestamp"] == NULL_TS_SENTINEL
