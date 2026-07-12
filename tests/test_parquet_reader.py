"""Tests for the server-side Vestigo interchange Parquet reader."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.test_pipeline import FakeClickHouseStore
from vestigo.db._arrow_schema import EVENT_ARROW_SCHEMA
from vestigo.ingestion import parquet_format
from vestigo.ingestion.parquet_reader import ParquetEventsParser
from vestigo.ingestion.parser import detect_format, get_parser
from vestigo.ingestion.pipeline import IngestionPipeline
from vestigo.models.event import Event, derive_event_id

_SCRIPT = (
    Path(__file__).parent.parent / "src" / "vestigo" / "assets" / "converters" / "nginx2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter_output(tmp_path_factory) -> Path:
    """A real converter-produced parquet file from the nginx fixture."""
    spec = importlib.util.spec_from_file_location("nginx2vestigo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path_factory.mktemp("parquet") / "access.parquet"
    assert module.convert(str(DATA / "nginx_access.log"), str(out), workers=1, verbose=False) == 0
    return out


def _parser(**overrides) -> ParquetEventsParser:
    kwargs: dict = {
        "format_name": "vestigo_parquet",
        "case_id": "case-p",
        "source_id": "src-p",
        "file_hash": "d" * 64,
        "source_name": "access.parquet",
    }
    kwargs.update(overrides)
    return get_parser(**kwargs)


class TestRegistry:
    def test_detect_format_parquet(self, tmp_path):
        assert detect_format(tmp_path / "x.parquet") == "vestigo_parquet"

    def test_get_parser_returns_reader(self):
        assert isinstance(_parser(), ParquetEventsParser)
        assert isinstance(_parser(format_name="parquet"), ParquetEventsParser)


class TestArrowBatches:
    def test_batches_conform_to_event_schema(self, converter_output):
        batches = list(_parser().parse_arrow_batches(converter_output))
        assert batches
        total = 0
        for batch in batches:
            assert batch.schema == EVENT_ARROW_SCHEMA
            total += batch.num_rows
        assert total == 3

    def test_stamped_columns(self, converter_output):
        batch = next(iter(_parser().parse_arrow_batches(converter_output)))
        rows = batch.to_pylist()
        raw_hash = None
        for row in rows:
            assert row["case_id"] == "case-p"
            assert row["source_id"] == "src-p"
            assert row["parser_name"] == "nginx2vestigo"
            assert row["parser_version"]
            assert row["line_number"] == 0
            assert row["embedding_model"] == ""
            assert row["ingest_time"] is not None
            raw_hash = row["file_hash"]
        # file_hash is the raw evidence file's sha256, not the parquet's.
        import hashlib

        assert raw_hash == hashlib.sha256((DATA / "nginx_access.log").read_bytes()).hexdigest()

    def test_event_id_matches_event_derivation(self, converter_output):
        batch = next(iter(_parser().parse_arrow_batches(converter_output)))
        for row in batch.to_pylist():
            expected = derive_event_id(
                case_id="case-p",
                source_id="src-p",
                source_identity=row["file_hash"],
                byte_offset=row["byte_offset"],
                content_hash=row["content_hash"],
                parser_name=row["parser_name"],
                parser_version=row["parser_version"],
            )
            assert row["event_id"] == str(expected)
            # And the Event dataclass agrees for the same inputs.
            event = Event(
                case_id="case-p",
                source_id="src-p",
                source_file=Path(row["source_file"]),
                byte_offset=row["byte_offset"],
                content_hash=row["content_hash"],
                file_hash=row["file_hash"],
                parser_name=row["parser_name"],
                parser_version=row["parser_version"],
                raw_line=row["message"],
                message=row["message"],
            )
            assert str(event.event_id) == row["event_id"]

    def test_progress_reported(self, converter_output):
        calls: list[int] = []
        list(_parser().parse_arrow_batches(converter_output, on_progress=calls.append))
        assert calls
        assert calls == sorted(calls)
        assert calls[-1] == converter_output.stat().st_size

    def test_parse_yields_matching_events(self, converter_output):
        parser = _parser()
        events = list(parser.parse(converter_output))
        batch_ids = [
            row["event_id"]
            for batch in parser.parse_arrow_batches(converter_output)
            for row in batch.to_pylist()
        ]
        assert [str(e.event_id) for e in events] == batch_ids
        assert all(e.timestamp is not None for e in events)


class TestValidation:
    def _write(self, path: Path, schema: pa.Schema, metadata: dict[str, str]) -> Path:
        table = pa.Table.from_pydict(
            {name: [] for name in schema.names}, schema=schema.with_metadata(metadata)
        )
        pq.write_table(table, path)
        return path

    def _good_meta(self) -> dict[str, str]:
        return {
            parquet_format.META_FORMAT_VERSION: parquet_format.FORMAT_VERSION,
            parquet_format.META_CONVERTER_NAME: "x2vestigo",
            parquet_format.META_CONVERTER_VERSION: "1.0.0",
            parquet_format.META_ORIGINAL_FILES: json.dumps(
                [{"name": "x.log", "sha256": "a" * 64, "size_bytes": 1}]
            ),
        }

    def test_rejects_plain_parquet(self, tmp_path):
        path = self._write(tmp_path / "x.parquet", parquet_format.PARQUET_EVENT_SCHEMA, {})
        with pytest.raises(ValueError, match="Not a Vestigo interchange"):
            list(_parser().parse_arrow_batches(path))

    def test_rejects_wrong_version(self, tmp_path):
        meta = self._good_meta()
        meta[parquet_format.META_FORMAT_VERSION] = "999"
        path = self._write(tmp_path / "x.parquet", parquet_format.PARQUET_EVENT_SCHEMA, meta)
        with pytest.raises(ValueError, match="Unsupported Vestigo Parquet format version"):
            list(_parser().parse_arrow_batches(path))

    def test_rejects_missing_columns(self, tmp_path):
        schema = pa.schema([pa.field("message", pa.string())])
        path = self._write(tmp_path / "x.parquet", schema, self._good_meta())
        with pytest.raises(ValueError, match="missing required columns"):
            list(_parser().parse_arrow_batches(path))

    def test_rejects_missing_provenance(self, tmp_path):
        meta = self._good_meta()
        meta[parquet_format.META_ORIGINAL_FILES] = "[]"
        path = self._write(tmp_path / "x.parquet", parquet_format.PARQUET_EVENT_SCHEMA, meta)
        with pytest.raises(ValueError, match="no original evidence files"):
            list(_parser().parse_arrow_batches(path))

    def test_accepts_legacy_tracesignal_keys(self, tmp_path):
        """Files from pre-rename (*2tracesignal.py) converters still validate."""
        meta = {
            key.replace("vestigo.", "tracesignal.", 1): value
            for key, value in self._good_meta().items()
        }
        path = self._write(tmp_path / "x.parquet", parquet_format.PARQUET_EVENT_SCHEMA, meta)
        parsed = parquet_format.validate_parquet_source(
            pq.ParquetFile(path).schema_arrow, pq.ParquetFile(path).schema_arrow.metadata
        )
        assert parsed.converter_name == "x2vestigo"
        assert parsed.original_files[0].name == "x.log"

    def test_rejects_wrong_column_type(self, tmp_path):
        fields = [
            pa.field(f.name, pa.string() if f.name == "byte_offset" else f.type)
            for f in parquet_format.PARQUET_EVENT_SCHEMA
        ]
        path = self._write(tmp_path / "x.parquet", pa.schema(fields), self._good_meta())
        with pytest.raises(ValueError, match="byte_offset"):
            list(_parser().parse_arrow_batches(path))


class TestNullHandling:
    def test_null_timestamp_becomes_sentinel(self, tmp_path):
        schema = parquet_format.PARQUET_EVENT_SCHEMA
        row = {
            "source_file": "x.log",
            "file_hash": "a" * 64,
            "byte_offset": 0,
            "content_hash": "b" * 64,
            "message": "undated line",
            "timestamp": None,
            "timestamp_desc": None,
            "artifact": "x",
            "artifact_long": "x:y",
            "display_name": None,
            "tags": [],
            "attributes": {},
        }
        meta = {
            parquet_format.META_FORMAT_VERSION: parquet_format.FORMAT_VERSION,
            parquet_format.META_CONVERTER_NAME: "x2vestigo",
            parquet_format.META_CONVERTER_VERSION: "1.0.0",
            parquet_format.META_ORIGINAL_FILES: json.dumps(
                [{"name": "x.log", "sha256": "a" * 64, "size_bytes": 1}]
            ),
        }
        table = pa.Table.from_pylist([row], schema=schema.with_metadata(meta))
        path = tmp_path / "x.parquet"
        pq.write_table(table, path)

        batch = next(iter(_parser().parse_arrow_batches(path)))
        out = batch.to_pylist()[0]
        assert out["timestamp"] == datetime(2299, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)
        assert out["timestamp_desc"] == ""
        assert out["display_name"] == ""

    @pytest.mark.parametrize("column", ["file_hash", "byte_offset", "content_hash", "source_file"])
    def test_null_provenance_rejected(self, tmp_path, column):
        # A null in a provenance column would let the stored value and the
        # event_id derived from it silently diverge — must be rejected, not
        # coerced to "" / 0.
        row = {
            "source_file": "x.log",
            "file_hash": "a" * 64,
            "byte_offset": 0,
            "content_hash": "b" * 64,
            "message": "line",
            "timestamp": None,
            "timestamp_desc": None,
            "artifact": "x",
            "artifact_long": "x:y",
            "display_name": None,
            "tags": [],
            "attributes": {},
        }
        row[column] = None
        meta = {
            parquet_format.META_FORMAT_VERSION: parquet_format.FORMAT_VERSION,
            parquet_format.META_CONVERTER_NAME: "x2vestigo",
            parquet_format.META_CONVERTER_VERSION: "1.0.0",
            parquet_format.META_ORIGINAL_FILES: json.dumps(
                [{"name": "x.log", "sha256": "a" * 64, "size_bytes": 1}]
            ),
        }
        table = pa.Table.from_pylist(
            [row], schema=parquet_format.PARQUET_EVENT_SCHEMA.with_metadata(meta)
        )
        path = tmp_path / "x.parquet"
        pq.write_table(table, path)

        with pytest.raises(ValueError, match=f"null values in provenance column '{column}'"):
            list(_parser().parse_arrow_batches(path))


class TestPipelineIntegration:
    def test_end_to_end_bulk_ingest(self, converter_output):
        clickhouse = FakeClickHouseStore()
        pipeline = IngestionPipeline(
            case_id="case-p",
            source_id="src-p",
            clickhouse=clickhouse,
            source_name="access.parquet",
            file_hash="d" * 64,
        )
        result = pipeline.run(converter_output)
        assert result.events_parsed == 3
        assert result.events_inserted == 3
        assert sum(b.num_rows for b in clickhouse.arrow_batches) == 3
        assert clickhouse.events == []  # bulk path, no Event objects
