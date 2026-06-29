"""Tests for TraceVector streaming parsers."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracevector.ingestion.parser import JsonlParser, TimesketchCsvParser, detect_format, get_parser
from tracevector.models.event import Event, ParserConfig, content_hash


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


def test_detect_format(tmp_path: Path) -> None:
    assert detect_format(tmp_path / "test.csv") == "timesketch_csv"
    assert detect_format(tmp_path / "test.jsonl") == "jsonl"
    assert detect_format(tmp_path / "test.json") == "jsonl"
    with pytest.raises(ValueError):
        detect_format(tmp_path / "test.unknown")


def test_get_parser_unsupported() -> None:
    with pytest.raises(ValueError, match="Unsupported parser format"):
        get_parser("unknown", "case1", "timeline1")


def test_csv_parser_maps_common_fields(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser("case1", "timeline1", config)
    events = list(parser.parse(timesketch_csv))

    assert len(events) == 2
    first, second = events

    assert first.case_id == "case1"
    assert first.timeline_id == "timeline1"
    assert first.parser_name == "timesketch_csv"
    assert first.parser_version == "0.1.0"
    assert first.timestamp == "2024-01-01T00:00:00+00:00"
    assert first.timestamp_desc == "Creation Time"
    assert first.source == "LOG"
    assert first.source_long == "Syslog"
    assert first.message == "User login"
    assert first.display_name == "auth.log"
    assert first.tags == ["login", "success"]
    assert first.line_number == 2
    assert first.byte_offset == len(timesketch_csv.read_text().splitlines(keepends=True)[0])

    assert second.message == "User logout"
    assert second.tags == ["logout"]


def test_csv_parser_content_hash_is_raw_line(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser("case1", "timeline1", config)
    events = list(parser.parse(timesketch_csv))

    raw_lines = timesketch_csv.read_text().splitlines(keepends=True)[1:]
    for event, raw_line in zip(events, raw_lines, strict=False):
        assert event.content_hash == content_hash(raw_line)
        assert event.raw_line == raw_line


def test_csv_parser_event_id_is_deterministic(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser("case1", "timeline1", config)
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
        "case1", "timeline1", config, file_hash="same_hash", source_name="source.csv"
    )
    parser_b = TimesketchCsvParser(
        "case1", "timeline1", config, file_hash="same_hash", source_name="source.csv"
    )
    ids_a = [e.event_id for e in parser_a.parse(path_a)]
    ids_b = [e.event_id for e in parser_b.parse(path_b)]
    assert ids_a == ids_b
    # Provenance should be the supplied source name, not the temp path.
    assert parser_a.parse(path_a).__next__().source_file == Path("source.csv")


def test_different_file_hash_produces_different_event_ids(timesketch_csv: Path) -> None:
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser_a = TimesketchCsvParser(
        "case1", "timeline1", config, file_hash="hash_a", source_name="a.csv"
    )
    parser_b = TimesketchCsvParser(
        "case1", "timeline1", config, file_hash="hash_b", source_name="b.csv"
    )
    ids_a = [e.event_id for e in parser_a.parse(timesketch_csv)]
    ids_b = [e.event_id for e in parser_b.parse(timesketch_csv)]
    assert ids_a != ids_b


def test_csv_parser_preserves_unknown_columns(timesketch_csv: Path, tmp_path: Path) -> None:
    path = tmp_path / "extra.csv"
    path.write_text("datetime,message,unknown_column\n2024-01-01T00:00:00+00:00,Hello,world\n")
    config = ParserConfig(name="timesketch_csv", version="0.1.0")
    parser = TimesketchCsvParser("case1", "timeline1", config)
    events = list(parser.parse(path))
    assert len(events) == 1
    assert events[0].attributes == {"unknown_column": "world"}


def test_jsonl_parser_maps_common_fields(jsonl_file: Path) -> None:
    config = ParserConfig(name="jsonl", version="0.1.0")
    parser = JsonlParser("case1", "timeline1", config)
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
    parser = JsonlParser("case1", "timeline1", config)
    events = list(parser.parse(path))
    assert len(events) == 2
    assert events[0].message == "good"
    assert events[1].message == "also good"


def test_event_text_for_embedding() -> None:
    from tracevector.models.event import Event

    event = Event(
        case_id="c",
        timeline_id="t",
        source_file=Path("/tmp/test.log"),
        byte_offset=0,
        content_hash="abc",
        parser_name="p",
        parser_version="1",
        raw_line='{"message":"login"}',
        message="User login",
        timestamp="2024-01-01T00:00:00+00:00",
        timestamp_desc="created",
        source="auth",
        tags=["login"],
        attributes={"ip": "10.0.0.1"},
    )
    text = event.text_for_embedding()
    assert "User login" in text
    assert "source=auth" in text
    assert "tags=login" in text
    assert "ip=10.0.0.1" in text


def test_parse_timestamp_normalizes_common_formats() -> None:
    from datetime import datetime

    from tracevector.models.event import _parse_timestamp

    assert _parse_timestamp("2024-01-01T00:00:00+00:00") == datetime(
        2024, 1, 1, 0, 0, 0, tzinfo=UTC
    )
    assert _parse_timestamp("2024-01-01 00:00:00") == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert _parse_timestamp("1704067200") == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert _parse_timestamp("1704067200000") == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    assert _parse_timestamp("1764367341913908") == datetime(
        2025, 11, 28, 22, 2, 21, 913908, tzinfo=UTC
    )
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-date") is None


def test_event_to_clickhouse_row_parses_timestamp() -> None:
    event = Event(
        case_id="c",
        timeline_id="t",
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


def test_event_to_clickhouse_row_null_for_bad_timestamp() -> None:
    event = Event(
        case_id="c",
        timeline_id="t",
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
    assert row["timestamp"] is None
