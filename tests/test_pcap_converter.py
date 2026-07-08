"""Tests for the pcap2tracesignal Parquet converter script.

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

from tracesignal.ingestion import parquet_format

_SCRIPT = (
    Path(__file__).parent.parent
    / "src"
    / "tracesignal"
    / "assets"
    / "converters"
    / "pcap2tracesignal.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("pcap2tracesignal", _SCRIPT)
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
        assert converter.PARQUET_EVENT_SCHEMA == parquet_format.PARQUET_EVENT_SCHEMA

    def test_output_validates_against_server_spec(self, converter, tmp_path):
        pf = _convert(converter, DATA / "sample.pcap", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "pcap2tracesignal"
        assert meta.converter_version == converter.CONVERTER_VERSION


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
        assert originals == [
            {"name": "sample.pcap", "sha256": expected, "size_bytes": src.stat().st_size}
        ]
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
