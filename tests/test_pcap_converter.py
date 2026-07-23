"""Tests for the pcap2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib. Fixtures are hand-built
classic pcap / pcapng byte streams (see tests/data/gen_pcap_fixtures.py),
each holding one TCP SYN, one UDP, and one ARP request frame.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from vestigo.ingestion import parquet_format

_SCRIPT = (
    Path(__file__).parent.parent / "src" / "vestigo" / "assets" / "converters" / "pcap2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("pcap2vestigo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _convert(converter, input_path: Path, out: Path, workers: int = 1) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False)
    assert rc == 0
    return pq.ParquetFile(out)


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
        pf = _convert(converter, DATA / "sample.pcap", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "pcap2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(DATA / "sample.pcap"), str(tmp_path / "out.csv"), 1, False)


class TestClassicPcap:
    def test_golden_packets(self, converter, tmp_path):
        pf = _convert(converter, DATA / "sample.pcap", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3

        tcp = rows[0]
        assert tcp["artifact"] == "network:packet:tcp"
        assert tcp["artifact_long"] == "network:packet:capture"
        assert tcp["timestamp_desc"] == "Packet Capture Time"
        attrs = dict(tcp["attributes"])
        assert attrs["src_ip"] == "10.0.0.1"
        assert attrs["dst_ip"] == "10.0.0.2"
        assert attrs["src_port"] == "12345"
        assert attrs["dst_port"] == "80"
        assert attrs["tcp_flags"] == "SYN"
        assert "SYN" in tcp["message"]

        udp = rows[1]
        attrs = dict(udp["attributes"])
        assert attrs["protocol"] == "udp"
        assert attrs["src_port"] == "53000"
        assert attrs["dst_port"] == "53"

        arp = rows[2]
        attrs = dict(arp["attributes"])
        assert attrs["protocol"] == "arp"
        assert attrs["arp_sender_ip"] == "10.0.0.1"
        assert attrs["arp_target_ip"] == "10.0.0.2"
        assert "who-has" in arp["message"]

    def test_byte_offsets_resolve_to_record_headers(self, converter, tmp_path):
        raw = (DATA / "sample.pcap").read_bytes()
        pf = _convert(converter, DATA / "sample.pcap", tmp_path / "out.parquet")
        for row in pf.read().to_pylist():
            offset = row["byte_offset"]
            _ts_sec, _ts_frac, incl_len, _orig_len = struct.unpack(
                "<IIII", raw[offset : offset + 16]
            )
            record_bytes = raw[offset : offset + 16 + incl_len]
            assert hashlib.sha256(record_bytes).hexdigest() == row["content_hash"]

    def test_file_provenance(self, converter, tmp_path):
        src = DATA / "sample.pcap"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert len(originals) == 1
        entry = originals[0]
        assert entry["name"] == "sample.pcap"
        assert entry["sha256"] == expected
        assert entry["size_bytes"] == src.stat().st_size
        assert entry["path"] == str(src.resolve())
        assert entry["mtime"]  # ISO-8601 file mtime present (converter >= 1.3.0)
        for row in pf.read().to_pylist():
            assert row["file_hash"] == expected
            assert row["source_file"] == "sample.pcap"


class TestPcapNg:
    def test_golden_packets(self, converter, tmp_path):
        pf = _convert(converter, DATA / "sample.pcapng", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3
        assert [dict(r["attributes"])["protocol"] for r in rows] == ["tcp", "udp", "arp"]
        for row in rows:
            assert dict(row["attributes"])["link_type"] == "ethernet"


class TestOversizedLengthGuard:
    """A corrupt/crafted length field must not force a multi-GB allocation."""

    def test_classic_record_length_capped(self, converter):
        import io

        huge = converter._MAX_RECORD_BYTES + 1
        # 16-byte record header: ts_sec, ts_frac, incl_len, orig_len.
        hdr = struct.pack("<IIII", 0, 0, huge, huge)
        with pytest.raises(converter.PcapParseError, match="exceeds"):
            list(converter._iter_pcap_classic(io.BytesIO(hdr), "<", False, "ethernet"))

    def test_pcapng_block_length_capped(self, converter):
        import io

        huge = converter._MAX_RECORD_BYTES + 1
        # Section Header Block: magic, total_length, byte-order magic.
        shb = converter._PCAPNG_MAGIC + struct.pack("<I", huge) + b"\x4d\x3c\x2b\x1a"
        with pytest.raises(converter.PcapParseError, match="exceeds"):
            list(converter._iter_pcap_ng(io.BytesIO(shb)))


class TestDirectoryInput:
    def test_directory_of_captures(self, converter, tmp_path):
        caps = tmp_path / "captures"
        caps.mkdir()
        (caps / "a.pcap").write_bytes((DATA / "sample.pcap").read_bytes())
        (caps / "b.pcapng").write_bytes((DATA / "sample.pcapng").read_bytes())
        pf = _convert(converter, caps, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 6
        by_file = {row["source_file"] for row in rows}
        assert by_file == {"a.pcap", "b.pcapng"}


class TestParallel:
    def test_cross_file_parallel_equals_sequential(self, converter, tmp_path):
        # Parallel mode spawns worker processes that re-import the script as
        # __main__ — only possible when it runs as a real CLI process, so the
        # parallel run goes through subprocess.
        import subprocess
        import sys

        caps = tmp_path / "captures"
        caps.mkdir()
        for i in range(6):
            (caps / f"cap_{i}.pcap").write_bytes((DATA / "sample.pcap").read_bytes())

        pf_seq = _convert(converter, caps, tmp_path / "seq.parquet", workers=1)
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "-i",
                str(caps),
                "-o",
                str(tmp_path / "par.parquet"),
                "-w",
                "4",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        pf_par = pq.ParquetFile(tmp_path / "par.parquet")

        def row_set(pf):
            return {
                (r["source_file"], r["byte_offset"], r["content_hash"])
                for r in pf.read().to_pylist()
            }

        seq_rows = row_set(pf_seq)
        assert len(seq_rows) == 18
        assert row_set(pf_par) == seq_rows


class TestDeterminism:
    def test_two_runs_identical_rows(self, converter, tmp_path):
        pf1 = _convert(converter, DATA / "sample.pcap", tmp_path / "a.parquet")
        pf2 = _convert(converter, DATA / "sample.pcap", tmp_path / "b.parquet")
        assert pf1.read().to_pylist() == pf2.read().to_pylist()


class TestSplit:
    def test_parts_mode_smoke(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(DATA / "sample.pcap"), str(out), 1, False, split="2")
        assert rc == 0
        assert not out.exists()
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 2
        rows = [r for p in parts for r in pq.ParquetFile(p).read().to_pylist()]
        ref = _convert(converter, DATA / "sample.pcap", tmp_path / "ref.parquet")
        assert rows == ref.read().to_pylist()
        for p in parts:
            pf = pq.ParquetFile(p)
            meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            assert meta.converter_name == "pcap2vestigo"


def test_time_window_filter(converter, tmp_path):
    """--since/--until drop out-of-window rows and record honest counts."""
    src = DATA / "sample.pcap"

    # A far-future --since drops every timestamped row by time.
    dropped = tmp_path / "dropped.parquet"
    converter.convert(str(src), str(dropped), 1, False, since="2099-01-01T00:00:00Z")
    pf = pq.ParquetFile(dropped)
    footer = {
        k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
    }
    counts = json.loads(footer["vestigo.row_counts"])
    assert counts["skipped_by_time"] > 0
    assert counts["parsed"] == pf.metadata.num_rows
    assert footer["vestigo.converted_at"]
    assert footer["vestigo.timezone_assumption"]

    # A wide-open window keeps exactly what the unfiltered run keeps.
    ref = _convert(converter, src, tmp_path / "all.parquet")
    wide = tmp_path / "wide.parquet"
    converter.convert(
        str(src),
        str(wide),
        1,
        False,
        since="1970-01-01T00:00:00Z",
        until="2099-01-01T00:00:00Z",
    )
    assert pq.ParquetFile(wide).metadata.num_rows == ref.metadata.num_rows
