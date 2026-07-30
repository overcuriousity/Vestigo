"""Tests for the evtx2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib.

``tests/data/Security_short_selected.evtx`` is vendored from
https://github.com/omerbenamram/evtx (samples/, MIT, Copyright (c) 2019 Omer Ben-Amram).
It is the smallest possible real EVTX container: a 4096-byte file header plus exactly one
64 KiB chunk holding 7 Security records.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import struct
import zlib
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from vestigo.db._columns import TOP_LEVEL_EVENT_COLUMNS, resolve_column_token
from vestigo.ingestion import parquet_format

_SCRIPT = (
    Path(__file__).parent.parent / "src" / "vestigo" / "assets" / "converters" / "evtx2vestigo.py"
)
DATA = Path(__file__).parent / "data"
FIXTURE = DATA / "Security_short_selected.evtx"

pytest.importorskip("evtx", reason="the evtx wheel is a converter-only dependency")


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("evtx2vestigo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _convert(converter, input_path: Path, out: Path, workers: int = 1, **kwargs) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False, **kwargs)
    assert rc == 0
    return pq.ParquetFile(out)


def _rows(pf: pq.ParquetFile) -> list[dict]:
    return pf.read().to_pylist()


def _attrs(row: dict) -> dict[str, str]:
    return dict(row["attributes"])


def _two_chunk_file(dest: Path) -> Path:
    """Write a two-chunk .evtx by repeating the fixture's single chunk.

    The fixture is exactly one 4096-byte header plus one 64 KiB chunk, so appending a copy
    of that chunk produces a structurally valid multi-chunk container — and one whose two
    chunks carry the *same* record ids, which is what a re-chunked or partially overwritten
    log looks like.
    """
    raw = bytearray(FIXTURE.read_bytes())
    struct.pack_into("<Q", raw, 8, 0)  # first chunk number
    struct.pack_into("<Q", raw, 16, 1)  # last chunk number
    struct.pack_into("<H", raw, 42, 2)  # number_of_chunks
    struct.pack_into("<I", raw, 124, zlib.crc32(bytes(raw[:120])))
    dest.write_bytes(bytes(raw) + bytes(raw[4096 : 4096 + 65536]))
    return dest


class TestSpecParity:
    def test_embedded_spec_matches_server_module(self, converter):
        assert converter.FORMAT_VERSION == parquet_format.FORMAT_VERSION
        assert converter.META_FORMAT_VERSION == parquet_format.META_FORMAT_VERSION
        assert converter.META_CONVERTER_NAME == parquet_format.META_CONVERTER_NAME
        assert converter.META_CONVERTER_VERSION == parquet_format.META_CONVERTER_VERSION
        assert converter.META_ORIGINAL_FILES == parquet_format.META_ORIGINAL_FILES
        assert converter.META_CONVERTED_AT == parquet_format.META_CONVERTED_AT
        assert converter.META_ROW_COUNTS == parquet_format.META_ROW_COUNTS
        assert converter.META_TIMEZONE_ASSUMPTION == parquet_format.META_TIMEZONE_ASSUMPTION
        assert converter.META_PARSE_DECISIONS == parquet_format.META_PARSE_DECISIONS
        assert converter.PARQUET_EVENT_SCHEMA == parquet_format.PARQUET_EVENT_SCHEMA

    def test_output_validates_against_server_spec(self, converter, tmp_path):
        pf = _convert(converter, FIXTURE, tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "evtx2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(FIXTURE), str(tmp_path / "out.csv"), 1, False)


class TestMapsBlob:
    """The embedded EvtxECmd corpus is opaque in review, so assert its invariants."""

    def test_blob_decodes_with_expected_shape(self, converter):
        corpus = converter._maps()
        meta = corpus["_meta"]
        assert meta["commit"] == converter.MAPS_SOURCE_COMMIT
        assert len(converter.MAPS_SOURCE_COMMIT) == 40
        assert meta["map_count"] == len(corpus["maps"]) > 400
        assert meta["alias_count"] == len(corpus["aliases"]) > 400

    def test_every_key_is_normalized(self, converter):
        corpus = converter._maps()
        for key in corpus["maps"]:
            channel, provider, event_id = key.split("|")
            assert channel == channel.lower()
            assert provider == provider.lower()
            assert event_id.lstrip("-").isdigit()
        for key, target in corpus["aliases"].items():
            assert key.split("|")[1] == "*"
            assert target in corpus["maps"]

    def test_every_shipped_expression_is_supported(self, converter):
        """Nothing in the blob may hit an unsupported branch of _resolve_xpath."""
        supported = (
            converter._RE_DATA_NAMED,
            converter._RE_DATA_INDEX,
            converter._RE_SYSTEM_ATTR,
            converter._RE_SYSTEM_TEXT,
            converter._RE_USERDATA,
        )
        literals = {"/Event/EventData/Data", "/Event/EventData"}
        for mapping in converter._maps()["maps"].values():
            for entry in mapping["m"]:
                for value in entry["v"]:
                    expr = value["e"]
                    assert expr in literals or any(rx.match(expr) for rx in supported), expr

    def test_every_shipped_refine_compiles(self, converter):
        for mapping in converter._maps()["maps"].values():
            for entry in mapping["m"]:
                for value in entry["v"]:
                    if "r" in value:
                        re.compile(value["r"])

    def test_properties_are_known_columns(self, converter):
        known = set(converter._MAP_PROPERTY_ORDER)
        for mapping in converter._maps()["maps"].values():
            for entry in mapping["m"]:
                assert entry["p"] in known, entry["p"]


class TestParsing:
    def test_golden_records(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        assert len(rows) == 7

        by_event = {}
        for row in rows:
            by_event.setdefault(_attrs(row)["EventID"], row)

        # 4776 — NTLM credential validation, with an EvtxECmd Lookups translation.
        logon = by_event["4776"]
        attrs = _attrs(logon)
        assert attrs["Channel"] == "Security"
        assert attrs["Provider_Name"] == "Microsoft-Windows-Security-Auditing"
        assert attrs["Computer"] == "temporal"
        assert attrs["TargetUserName"] == "Administrator"
        assert logon["artifact"] == "winevtx:logon:credential_validation"
        assert logon["artifact_long"] == "windows:evtx:Security"
        assert logon["timestamp_desc"] == "Credential Validation Time"
        assert attrs["MapDescription"] == "NTLM authentication request"
        # 0xc000006a resolves through the map's Lookups table rather than staying raw.
        assert "password is wrong" in logon["message"]
        assert attrs["Status"] == "0xc000006a"

    def test_event_id_is_an_unpadded_string(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        for row in rows:
            event_id = _attrs(row)["EventID"]
            assert event_id.isdigit()
            assert event_id == str(int(event_id)), "zero-padding breaks Sigma's string compare"

    def test_timestamps_are_utc_and_millisecond_truncated(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        for row in rows:
            ts = row["timestamp"]
            assert ts is not None
            assert ts.tzinfo is not None
            assert ts.microsecond % 1000 == 0
            # The untruncated original survives verbatim.
            assert _attrs(row)["TimeCreated_SystemTime"].startswith(ts.strftime("%Y-%m-%dT%H:%M"))

    def test_message_is_never_empty(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        assert all(row["message"].strip() for row in rows)

    def test_lookup_resolves_percent_coded_values(self, converter, tmp_path):
        """A map whose Lookups key the raw %%NNNNN value must still resolve."""
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        wfp = next(r for r in rows if _attrs(r)["EventID"] == "5152")
        assert _attrs(wfp)["Direction"] == "%%14592", "the raw field stays verbatim"
        assert "Direction: Inbound" in wfp["message"]

    def test_footer_records_forensic_decisions(self, converter, tmp_path):
        pf = _convert(converter, FIXTURE, tmp_path / "out.parquet")
        meta = pf.metadata.metadata
        counts = json.loads(meta[converter.META_ROW_COUNTS.encode()])
        assert counts == {"parsed": 7, "skipped_malformed": 0, "skipped_by_time": 0}
        decisions = json.loads(meta[converter.META_PARSE_DECISIONS.encode()])
        assert decisions["maps"] == "evtxecmd"
        assert decisions["evtxecmd_maps_commit"] == converter.MAPS_SOURCE_COMMIT
        assert decisions["byte_offset_fallback_rows"] == 0
        assert decisions["chunk_errors"] == 0
        assert meta[converter.META_TIMEZONE_ASSUMPTION.encode()].decode().startswith("EVTX")


class TestSigmaFieldContract:
    """Community Windows Sigma rules must resolve against these attribute names."""

    def test_no_attribute_shadows_a_top_level_column(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        for row in rows:
            for key in _attrs(row):
                assert key not in TOP_LEVEL_EVENT_COLUMNS
                assert resolve_column_token(key)[0] is None, key

    def test_sigma_canonical_names_are_present(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        for row in rows:
            attrs = _attrs(row)
            assert "EventID" in attrs
            assert "Channel" in attrs
            assert "Provider_Name" in attrs

    def test_event_data_keeps_native_windows_names(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        attrs = _attrs(next(r for r in rows if _attrs(r)["EventID"] == "4776"))
        assert attrs["TargetUserName"] == "Administrator"
        assert attrs["Workstation"] == "TEMPORAL"

    def test_map_output_is_namespaced(self, converter, tmp_path):
        """Map-derived text must never masquerade as a raw Windows field."""
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        for row in rows:
            for key in _attrs(row):
                if key.startswith("Map"):
                    continue
                assert not key.startswith("PayloadData"), key

    def test_reserved_keys_are_prefixed(self, converter):
        assert converter._safe_attr_key("Message") == "evt_Message"
        assert converter._safe_attr_key("file_hash") == "evt_file_hash"
        assert converter._safe_attr_key("TargetUserName") == "TargetUserName"

    def test_reserved_keys_match_the_server_column_set(self, converter):
        """The converter is a standalone copy of the server's column list — pin it."""
        assert set(converter._RESERVED_ATTR_KEYS) == TOP_LEVEL_EVENT_COLUMNS

    def test_named_data_field_wins_over_a_positional_key(self, converter):
        """A record mixing a named `Data1` with unnamed positional Data must not
        lose the named value — that is the name a Sigma rule addresses."""
        import xml.etree.ElementTree as ET

        root = ET.fromstring(
            "<Event><EventData>"
            '<Data Name="Data1">named</Data>'
            "<Data>positional</Data>"
            "</EventData></Event>"
        )
        attrs = converter._extract_event_data(root)
        assert attrs["Data1"] == "named"
        assert attrs["Data1_pos"] == "positional"

    def test_named_data_field_wins_regardless_of_document_order(self, converter):
        """Same record with the positional element first. Which key each value
        lands under is a property of the record, not of the order the writer
        happened to emit them in — resolving it as we went would let the named
        value overwrite the positional one and lose it silently."""
        import xml.etree.ElementTree as ET

        root = ET.fromstring(
            "<Event><EventData>"
            "<Data>positional</Data>"
            '<Data Name="Data1">named</Data>'
            "</EventData></Event>"
        )
        attrs = converter._extract_event_data(root)
        assert attrs["Data1"] == "named"
        assert attrs["Data1_pos"] == "positional"

    def test_positional_keys_are_untouched_without_a_named_collision(self, converter):
        """No named DataN in the record means the positional keys keep the plain
        spelling — the disambiguation must not fire on every unnamed field."""
        import xml.etree.ElementTree as ET

        root = ET.fromstring(
            "<Event><EventData><Data>one</Data><Data>two</Data></EventData></Event>"
        )
        attrs = converter._extract_event_data(root)
        assert attrs == {"Data1": "one", "Data2": "two"}


class TestByteOffsets:
    def test_offsets_resolve_to_records_in_the_original_file(self, converter, tmp_path):
        """dd out the span at byte_offset and its sha256 must equal content_hash."""
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        raw = FIXTURE.read_bytes()
        for row in rows:
            attrs = _attrs(row)
            offset = row["byte_offset"]
            size = int(attrs["record_size"])
            span = raw[offset : offset + size]
            assert span[:4] == b"\x2a\x2a\x00\x00"
            assert len(span) == size
            # The join key is the record *header* id, which an extracted log can
            # renumber independently of the <EventRecordID> element in the payload.
            assert struct.unpack_from("<Q", span, 8)[0] == int(attrs["evtx_record_id"])
            assert hashlib.sha256(span).hexdigest() == row["content_hash"]

    def test_offsets_are_unique(self, converter, tmp_path):
        rows = _rows(_convert(converter, FIXTURE, tmp_path / "out.parquet"))
        offsets = [row["byte_offset"] for row in rows]
        assert len(set(offsets)) == len(offsets)

    def test_file_provenance(self, converter, tmp_path):
        pf = _convert(converter, FIXTURE, tmp_path / "out.parquet")
        originals = json.loads(pf.schema_arrow.metadata[converter.META_ORIGINAL_FILES.encode()])
        assert len(originals) == 1
        entry = originals[0]
        assert entry["name"] == FIXTURE.name
        assert entry["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        assert entry["size_bytes"] == FIXTURE.stat().st_size
        assert entry["path"] == str(FIXTURE.resolve())
        assert entry["mtime"]
        for row in _rows(pf):
            assert row["file_hash"] == entry["sha256"]
            assert row["source_file"] == FIXTURE.name


class TestChunkScanner:
    def test_scanner_finds_every_record(self, converter):
        data = FIXTURE.read_bytes()
        offsets, note = converter._scan_record_offsets(data)
        assert len(offsets) == 7
        assert "chunks_ok=1" in note
        assert "chunks_skipped=0" in note

    def test_truncated_file_degrades_without_raising(self, converter):
        offsets, note = converter._scan_record_offsets(FIXTURE.read_bytes()[:5000])
        assert isinstance(offsets, dict)
        assert isinstance(note, str)

    def test_bad_chunk_magic_is_skipped_not_fatal(self, converter):
        data = bytearray(FIXTURE.read_bytes())
        data[4096:4104] = b"XXXXXXXX"
        offsets, note = converter._scan_record_offsets(bytes(data))
        assert offsets == {}
        assert "chunks_skipped=1" in note

    def test_non_evtx_input_reports_rather_than_raises(self, converter):
        offsets, note = converter._scan_record_offsets(b"not an evtx file at all")
        assert offsets == {}
        assert note == "not an evtx file header"


class TestChunkImages:
    """The per-chunk images handed to the parser, and the multi-chunk path in general."""

    def test_every_chunk_image_is_a_valid_evtx_document(self, converter, tmp_path):
        """Including the header checksum — a stricter parser must not reject the image."""
        data = _two_chunk_file(tmp_path / "two.evtx").read_bytes()
        stats = converter._ChunkScanStats()
        images = [image for image, _offsets in converter._iter_chunks(data, stats)]
        assert len(images) == 2
        for image in images:
            assert image[:8] == converter._EVTX_MAGIC
            assert struct.unpack_from("<H", image, 42)[0] == 1, "the image holds one chunk"
            assert struct.unpack_from("<I", image, 124)[0] == zlib.crc32(image[:120])
        assert stats.note() == (
            "chunks_ok=2 chunks_skipped=0 duplicate_record_ids=7 truncated_chunks=0"
        )

    def test_multi_chunk_file_parses_every_record(self, converter, tmp_path):
        source = _two_chunk_file(tmp_path / "two.evtx")
        rows = _rows(_convert(converter, source, tmp_path / "out.parquet"))
        assert len(rows) == 14

    def test_duplicate_record_ids_keep_distinct_offsets(self, converter, tmp_path):
        """Two records sharing a record id must not share a forensic identity.

        The server derives ``event_id`` from ``(file_hash, byte_offset, content_hash)``, so
        a per-file offset join would collapse the second chunk's records onto the first's.
        """
        source = _two_chunk_file(tmp_path / "two.evtx")
        rows = _rows(_convert(converter, source, tmp_path / "out.parquet"))
        raw = source.read_bytes()
        # The two chunks are byte-identical copies, so equal content_hashes are correct;
        # the offsets are what must differ, and event identity is derived from both.
        assert len({row["byte_offset"] for row in rows}) == 14
        assert len({(row["byte_offset"], row["content_hash"]) for row in rows}) == 14
        # Every offset still addresses the record it claims to.
        for row in rows:
            size = int(_attrs(row)["record_size"])
            span = raw[row["byte_offset"] : row["byte_offset"] + size]
            assert span[:4] == b"\x2a\x2a\x00\x00"
            assert hashlib.sha256(span).hexdigest() == row["content_hash"]
        # Both copies of record id 1 are present, at different offsets.
        first = sorted(r["byte_offset"] for r in rows if _attrs(r)["evtx_record_id"] == "1")
        assert len(first) == 2
        assert first[1] - first[0] == 65536

    def test_a_failing_chunk_scan_costs_only_that_chunk(self, converter, monkeypatch, tmp_path):
        """A read error on damaged media must not end the walk (mmap raises OSError)."""
        data = _two_chunk_file(tmp_path / "two.evtx").read_bytes()
        real = converter._scan_chunk
        calls = []

        def _flaky(buf, chunk_start):
            calls.append(chunk_start)
            if len(calls) == 1:
                raise OSError("simulated read error")
            return real(buf, chunk_start)

        monkeypatch.setattr(converter, "_scan_chunk", _flaky)
        stats = converter._ChunkScanStats()
        images = [image for image, _offsets in converter._iter_chunks(data, stats)]
        assert len(calls) == 2, "the second chunk was still attempted"
        assert len(images) == 1
        assert stats.chunks_ok == 1
        assert stats.chunks_bad == 1
        assert "scan_error=OSError" in stats.note()

    def test_footer_reports_the_duplicate_ids(self, converter, tmp_path):
        source = _two_chunk_file(tmp_path / "two.evtx")
        pf = _convert(converter, source, tmp_path / "out.parquet")
        decisions = json.loads(pf.metadata.metadata[converter.META_PARSE_DECISIONS.encode()])
        assert decisions["chunk_scan"]["two.evtx"] == (
            "chunks_ok=2 chunks_skipped=0 duplicate_record_ids=7 truncated_chunks=0"
        )
        assert decisions["byte_offset_fallback_rows"] == 0


class _CapturingBuffer:
    """Stands in for _BatchBuffer: keeps what the converter appended, writes nothing."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str, dict]] = []

    def append(self, source_file, file_hash, byte_offset, content_hash, row) -> None:
        self.rows.append((byte_offset, content_hash, row))


def _stub_parser(converter, monkeypatch, records: list[dict]):
    """Replace PyEvtxParser so a hand-built record can be pushed through _convert_evtx.

    Byte-patching the fixture is not an option: the parser validates chunk checksums, so a
    record carrying an XML-illegal byte cannot be produced from real chunk bytes.
    """

    class _Stub:
        def __init__(self, _blob) -> None:
            pass

        def records(self):
            return iter(records)

    monkeypatch.setattr(converter, "PyEvtxParser", _Stub)


_STUB_XML = (
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
    '<Provider Name="Microsoft-Windows-Security-Auditing"/><EventID>4739</EventID>'
    '<TimeCreated SystemTime="2016-06-29T15:24:34.346Z"/><Channel>Security</Channel>'
    "<Computer>temporal</Computer></System><EventData>"
    '<Data Name="DomainPolicyChanged">policy{}value</Data></EventData></Event>'
)


class TestDegradedRecords:
    def test_illegal_xml_control_char_is_sanitized_not_dropped(self, converter, monkeypatch):
        data = FIXTURE.read_bytes()
        known_id = next(iter(converter._scan_record_offsets(data)[0]))
        _stub_parser(
            converter,
            monkeypatch,
            [{"data": _STUB_XML.format("\x03"), "event_record_id": known_id, "timestamp": ""}],
        )
        buffer = _CapturingBuffer()
        counts = converter._convert_evtx(data, "stub.evtx", "deadbeef", buffer)
        assert counts.parsed == 1
        assert counts.sanitized == 1
        assert counts.skipped == 0
        _offset, _content_hash, row = buffer.rows[0]
        attrs = row["attributes"]
        assert attrs["xml_sanitized"] == "1"
        assert attrs["DomainPolicyChanged"] == "policy�value"
        # A sanitized record still resolves to its raw span, so content_hash stays
        # raw and byte_offset stays a real offset — neither basis flag is set.
        assert "content_hash_basis" not in attrs
        assert "byte_offset_basis" not in attrs

    def test_unlocatable_record_falls_back_to_the_record_id(self, converter, monkeypatch):
        data = FIXTURE.read_bytes()
        _stub_parser(
            converter,
            monkeypatch,
            [{"data": _STUB_XML.format("-"), "event_record_id": 987654321, "timestamp": ""}],
        )
        buffer = _CapturingBuffer()
        counts = converter._convert_evtx(data, "stub.evtx", "deadbeef", buffer)
        assert counts.parsed == 1
        assert counts.offset_fallback == 1
        offset, content_hash, row = buffer.rows[0]
        assert offset == 987654321
        # A record id is indistinguishable from a real byte offset by
        # inspection, so the row says outright that this one is not an offset —
        # `dd bs=1 skip=<byte_offset>` against it would reproduce nothing.
        assert row["attributes"]["byte_offset_basis"] == "record_id"
        assert row["attributes"]["content_hash_basis"] == "rendered_xml"
        assert "record_size" not in row["attributes"]
        assert content_hash == hashlib.sha256(_STUB_XML.format("-").encode()).hexdigest()

    def test_unparseable_record_is_counted_not_written(self, converter, monkeypatch):
        data = FIXTURE.read_bytes()
        _stub_parser(converter, monkeypatch, [{"data": "<Event><unclosed>", "event_record_id": 1}])
        buffer = _CapturingBuffer()
        counts = converter._convert_evtx(data, "stub.evtx", "deadbeef", buffer)
        assert counts.parsed == 0
        assert counts.skipped == 1
        assert buffer.rows == []


class TestRefineInputCap:
    def test_a_pathological_value_is_truncated_before_matching(self, converter):
        assert converter._REFINE_INPUT_LIMIT == 8192
        value = "a" * (converter._REFINE_INPUT_LIMIT + 50) + "TAIL"
        # The tail is past the cap, so it cannot be matched — that is the point.
        assert converter._apply_refine(value, "TAIL") == ""
        assert converter._apply_refine("prefix TAIL", "TAIL") == "TAIL"


class TestNoMaps:
    def test_no_maps_drops_map_attributes_but_keeps_rows(self, converter, tmp_path):
        with_maps = _rows(_convert(converter, FIXTURE, tmp_path / "a.parquet"))
        without = _rows(_convert(converter, FIXTURE, tmp_path / "b.parquet", no_maps=True))
        assert len(with_maps) == len(without)
        assert any(k.startswith("Map") for k in _attrs(with_maps[0]))
        for row in without:
            assert not [k for k in _attrs(row) if k.startswith("Map")]
            assert row["message"].strip()

    def test_no_maps_is_recorded_in_the_footer(self, converter, tmp_path):
        pf = _convert(converter, FIXTURE, tmp_path / "out.parquet", no_maps=True)
        decisions = json.loads(pf.metadata.metadata[converter.META_PARSE_DECISIONS.encode()])
        assert decisions["maps"] == "disabled"
        assert decisions["evtxecmd_maps_commit"] == ""


class TestInputHandling:
    def test_directory_input(self, converter, tmp_path):
        source = tmp_path / "logs"
        source.mkdir()
        (source / "one.evtx").write_bytes(FIXTURE.read_bytes())
        (source / "two.evtx").write_bytes(FIXTURE.read_bytes())
        rows = _rows(_convert(converter, source, tmp_path / "out.parquet"))
        assert len(rows) == 14
        assert {row["source_file"] for row in rows} == {"one.evtx", "two.evtx"}

    def test_text_export_is_rejected_with_a_pointer(self, converter, tmp_path):
        with pytest.raises(SystemExit, match="evtx2timesketch"):
            converter.convert(
                str(DATA / "evtx_text_export.xml"), str(tmp_path / "o.parquet"), 1, False
            )

    def test_non_evtx_binary_is_rejected(self, converter, tmp_path):
        bogus = tmp_path / "bogus.evtx"
        bogus.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07rest")
        with pytest.raises(SystemExit, match="bad magic"):
            converter.convert(str(bogus), str(tmp_path / "o.parquet"), 1, False)

    def test_missing_input(self, converter, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            converter.convert(str(tmp_path / "nope.evtx"), str(tmp_path / "o.parquet"), 1, False)

    def test_directory_without_evtx_files(self, converter, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SystemExit, match="no .evtx files"):
            converter.convert(str(empty), str(tmp_path / "o.parquet"), 1, False)

    def test_junk_file_in_a_directory_is_warned_about_and_skipped(
        self, converter, tmp_path, capsys
    ):
        """One stray file must not cost the rest of a triage collection."""
        source = tmp_path / "logs"
        source.mkdir()
        (source / "real.evtx").write_bytes(FIXTURE.read_bytes())
        (source / "export.evtx").write_bytes((DATA / "evtx_text_export.xml").read_bytes())
        (source / "empty.evtx").write_bytes(b"")
        rows = _rows(_convert(converter, source, tmp_path / "out.parquet"))
        assert {row["source_file"] for row in rows} == {"real.evtx"}
        err = capsys.readouterr().err
        assert "skipping" in err and "export.evtx" in err
        assert "evtx2timesketch.py" in err
        assert "empty.evtx" in err

    def test_directory_of_only_junk_fails(self, converter, tmp_path):
        source = tmp_path / "logs"
        source.mkdir()
        (source / "export.evtx").write_bytes((DATA / "evtx_text_export.xml").read_bytes())
        with pytest.raises(SystemExit, match="every candidate was skipped"):
            converter.convert(str(source), str(tmp_path / "o.parquet"), 1, False)


class TestParallel:
    def test_parallel_matches_sequential(self, converter, tmp_path):
        # The parallel run goes through subprocess (same pattern as the nginx and
        # cloudtrail converters' TestParallel): spawned workers re-import the script
        # as __main__, which an importlib-loaded module cannot satisfy.
        import subprocess
        import sys

        source = tmp_path / "logs"
        source.mkdir()
        for name in ("a.evtx", "b.evtx", "c.evtx"):
            (source / name).write_bytes(FIXTURE.read_bytes())

        sequential = _rows(_convert(converter, source, tmp_path / "seq.parquet", workers=1))
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "-i",
                str(source),
                "-o",
                str(tmp_path / "par.parquet"),
                "-w",
                "3",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        parallel = _rows(pq.ParquetFile(tmp_path / "par.parquet"))
        assert sequential == parallel


class TestDeterminism:
    def test_repeated_runs_are_identical(self, converter, tmp_path):
        first = _rows(_convert(converter, FIXTURE, tmp_path / "one.parquet"))
        second = _rows(_convert(converter, FIXTURE, tmp_path / "two.parquet"))
        assert first == second


class TestSplit:
    def test_split_into_parts(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(FIXTURE), str(out), 1, False, split="3")
        assert rc == 0
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 3
        total = 0
        for part in parts:
            pf = pq.ParquetFile(part)
            parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            total += pf.metadata.num_rows
        assert total == 7
        assert not out.exists()

    def test_size_mode_rotates(self, converter, tmp_path):
        source = tmp_path / "logs"
        source.mkdir()
        for i in range(20):
            (source / f"log{i:02d}.evtx").write_bytes(FIXTURE.read_bytes())
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(source), str(out), 1, False, split="4K")
        assert rc == 0
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) >= 2
        rows = [r for p in parts for r in pq.ParquetFile(p).read().to_pylist()]
        assert len(rows) == 140
        ref = _convert(converter, source, tmp_path / "ref.parquet")
        assert rows == ref.read().to_pylist()

    def test_invalid_split_fails_before_writing(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        with pytest.raises(SystemExit, match="invalid --split"):
            converter.convert(str(FIXTURE), str(out), 1, False, split="banana")
        assert not out.exists()


def test_time_window_filter(converter, tmp_path):
    pf = _convert(
        converter,
        FIXTURE,
        tmp_path / "out.parquet",
        since="2016-06-29T15:24:36Z",
    )
    rows = _rows(pf)
    assert 0 < len(rows) < 7
    counts = json.loads(pf.metadata.metadata[converter.META_ROW_COUNTS.encode()])
    assert counts["skipped_by_time"] == 7 - len(rows)


def test_time_window_until_filter(converter, tmp_path):
    pf = _convert(
        converter,
        FIXTURE,
        tmp_path / "out.parquet",
        until="2016-06-29T15:24:35Z",
    )
    rows = _rows(pf)
    assert 0 < len(rows) < 7
    counts = json.loads(pf.metadata.metadata[converter.META_ROW_COUNTS.encode()])
    assert counts["skipped_by_time"] == 7 - len(rows)
    assert all(row["timestamp"].strftime("%H:%M:%S") <= "15:24:35" for row in rows)


def test_time_window_excluding_everything_returns_nonzero(converter, tmp_path):
    rc = converter.convert(
        str(FIXTURE), str(tmp_path / "out.parquet"), 1, False, since="2099-01-01T00:00:00Z"
    )
    assert rc == 1
