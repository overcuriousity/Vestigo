"""Tests for the pcap2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib. Fixtures are hand-built
classic pcap / pcapng byte streams (see tests/data/gen_pcap_fixtures.py):
``sample.pcap``/``sample.pcapng`` hold one TCP SYN, one UDP and one ARP request
frame each; ``sample_http.pcap`` holds one keep-alive HTTP/1.1 connection with
an out-of-order request, a retransmitted segment and a chunked response. The
reassembly edge cases build their own captures from the same builders.
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


def _convert(converter, input_path: Path, out: Path, workers: int = 1, **kwargs) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False, **kwargs)
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


@pytest.fixture(scope="module")
def gen():
    """The fixture generator, imported for its frame builders."""
    spec = importlib.util.spec_from_file_location(
        "gen_pcap_fixtures", DATA / "gen_pcap_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stream_capture(gen, path: Path, exchanges) -> Path:
    """Write a one-connection capture from ``(from_client, payload)`` pairs.

    Handshake and sequence numbers are handled here; each payload is split into
    in-order MTU-sized segments. For out-of-order/retransmit cases build frames
    by hand.
    """
    mtu = 1400
    frames = [gen._c2s(0, gen.SYN), gen._s2c(0, gen.SYN | gen.ACK), gen._c2s(1, gen.ACK)]
    client_offset = server_offset = 1
    for from_client, payload in exchanges:
        for start in range(0, max(len(payload), 1), mtu):
            segment = payload[start : start + mtu]
            if from_client:
                frames.append(gen._c2s(client_offset, gen.PSH | gen.ACK, segment))
                client_offset += len(segment)
            else:
                frames.append(gen._s2c(server_offset, gen.PSH | gen.ACK, segment))
                server_offset += len(segment)
    gen.write_classic_pcap(path, frames)
    return path


def _transactions(pf: pq.ParquetFile) -> list[dict]:
    return [
        {**row, "attributes": dict(row["attributes"])}
        for row in pf.read().to_pylist()
        if row["artifact"] == "network:http:transaction"
    ]


class TestHttpReassembly:
    """--reassemble http: derived transaction rows over the packet floor."""

    def test_golden_transactions(self, converter, tmp_path):
        pf = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "out.parquet", reassemble="http"
        )
        transactions = _transactions(pf)
        assert len(transactions) == 2

        first = transactions[0]
        attrs = first["attributes"]
        assert first["timestamp_desc"] == "HTTP Request Time"
        assert first["artifact_long"] == "web:access:request"
        # Field names shared with nginx2vestigo, so saved Views port across sources.
        assert attrs["http_method"] == "GET"
        assert attrs["http_uri"] == "/index.html"
        assert attrs["http_protocol"] == "HTTP/1.1"
        assert attrs["http_request_full"] == "GET /index.html HTTP/1.1"
        assert attrs["status_code"] == "200"
        assert attrs["http_status_reason"] == "OK"
        assert attrs["src_ip"] == "10.0.0.1"
        assert attrs["src_port"] == "40001"
        assert attrs["dst_ip"] == "10.0.0.2"
        assert attrs["dst_port"] == "80"
        assert attrs["http_transaction_index"] == "1"
        # Chunked body: 5 + 6 bytes of payload, framing bytes excluded.
        assert attrs["http_response_body_bytes"] == "11"
        assert "http_incomplete" not in attrs
        assert "reassembly_gap" not in attrs
        assert 'GET /index.html HTTP/1.1" 200' in first["message"]

        second = transactions[1]["attributes"]
        assert second["http_request_full"] == "POST /submit HTTP/1.1"
        assert second["status_code"] == "404"
        assert second["http_request_body_bytes"] == "9"
        assert second["http_transaction_index"] == "2"

    def test_packet_rows_are_untouched(self, converter, tmp_path):
        """Transaction rows are added, never substituted for the packet floor."""
        with_reasm = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "a.parquet", reassemble="http"
        ).read()
        without = (
            _convert(converter, DATA / "sample_http.pcap", tmp_path / "b.parquet")
            .read()
            .to_pylist()
        )
        packets = [
            row for row in with_reasm.to_pylist() if row["artifact"] != "network:http:transaction"
        ]
        assert packets == without

    def test_retransmit_and_reorder_do_not_corrupt_the_stream(self, converter, tmp_path):
        """The fixture's duplicate segment contributes nothing; the reorder resolves."""
        pf = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "out.parquet", reassemble="http"
        )
        attrs = _transactions(pf)[0]["attributes"]
        # Two request segments + three response segments; the retransmit of the
        # first body segment carried no new bytes, so it is not a contributor.
        assert attrs["packet_count"] == "5"
        assert len(attrs["packet_offsets"].split(",")) == 5

    def test_provenance_is_rederivable_from_the_listed_offsets(self, converter, tmp_path):
        """content_hash = sha256 over the tag + contributing records in capture order."""
        raw = (DATA / "sample_http.pcap").read_bytes()
        pf = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "out.parquet", reassemble="http"
        )
        for transaction in _transactions(pf):
            attrs = transaction["attributes"]
            assert attrs["reassembled"] == "true"
            assert attrs["byte_offset_basis"] == "request_line_record"
            assert attrs["content_hash_basis"] == "reassembled_records"
            offsets = [int(value) for value in attrs["packet_offsets"].split(",")]
            assert offsets == sorted(offsets)
            digest = hashlib.sha256()
            digest.update(
                b"vestigo:http-transaction:%s\n" % attrs["http_transaction_index"].encode()
            )
            for offset in offsets:
                incl_len = struct.unpack("<I", raw[offset + 8 : offset + 12])[0]
                digest.update(raw[offset : offset + 16 + incl_len])
            assert digest.hexdigest() == transaction["content_hash"]

    def test_byte_offset_anchors_the_request_line_record(self, converter, tmp_path):
        """Not the lowest offset — the fixture sends the request's tail first."""
        raw = (DATA / "sample_http.pcap").read_bytes()
        pf = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "out.parquet", reassemble="http"
        )
        attrs = _transactions(pf)[0]["attributes"]
        anchor = _transactions(pf)[0]["byte_offset"]
        offsets = [int(value) for value in attrs["packet_offsets"].split(",")]
        assert anchor != min(offsets)
        incl_len = struct.unpack("<I", raw[anchor + 8 : anchor + 12])[0]
        assert b"GET /index.html" in raw[anchor : anchor + 16 + incl_len]

    def test_transaction_row_never_collides_with_a_packet_row(self, converter, gen, tmp_path):
        """A one-packet transaction must not share a packet row's event identity.

        The server derives ``event_id`` from ``(…, byte_offset, content_hash, …)``,
        so a transaction that reused both would land on the same id as the packet
        it was reassembled from.
        """
        capture = _stream_capture(
            gen, tmp_path / "single.pcap", [(True, b"GET /gone HTTP/1.1\r\nHost: h\r\n\r\n")]
        )
        rows = _convert(converter, capture, tmp_path / "out.parquet", reassemble="http").read()
        identities = [(r["byte_offset"], r["content_hash"]) for r in rows.to_pylist()]
        assert len(identities) == len(set(identities))

    def test_timestamp_is_the_first_captured_byte_of_the_request(self, converter, tmp_path):
        """Not the packet that completed the header block, which can be later.

        The fixture sends the request's tail first, so the packet that completes
        the framing is not the one that carried the request's earliest byte.
        """
        rows = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "out.parquet", reassemble="http"
        ).read()
        packets = {
            row["byte_offset"]: row
            for row in rows.to_pylist()
            if row["artifact"] != "network:http:transaction"
        }
        transaction = _transactions(
            _convert(
                converter, DATA / "sample_http.pcap", tmp_path / "out2.parquet", reassemble="http"
            )
        )[0]
        contributing = [int(v) for v in transaction["attributes"]["packet_offsets"].split(",")]
        request_side = [
            packets[offset]
            for offset in contributing
            if dict(packets[offset]["attributes"])["src_port"] == "40001"
        ]
        assert len(request_side) == 2  # the reordered halves of the request
        assert transaction["timestamp"] == min(row["timestamp"] for row in request_side)

    def test_pipelined_responses_are_framed_against_their_own_request(
        self, converter, gen, tmp_path
    ):
        """Both requests in one packet, both responses in the next.

        The HEAD response is bodyless whatever its Content-Length says; the GET
        response that follows it in the same packet must still be framed with a
        body, or its two body bytes desync everything after it.
        """
        frames = [
            gen._c2s(0, gen.SYN),
            gen._s2c(0, gen.SYN | gen.ACK),
            gen._c2s(1, gen.ACK),
            gen._c2s(
                1,
                gen.PSH | gen.ACK,
                b"HEAD /a HTTP/1.1\r\nHost: h\r\n\r\nGET /b HTTP/1.1\r\nHost: h\r\n\r\n",
            ),
            gen._s2c(
                1,
                gen.PSH | gen.ACK,
                b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi",
            ),
        ]
        gen.write_classic_pcap(tmp_path / "pipeline.pcap", frames)
        transactions = _transactions(
            _convert(
                converter, tmp_path / "pipeline.pcap", tmp_path / "out.parquet", reassemble="http"
            )
        )
        assert [t["attributes"]["http_method"] for t in transactions] == ["HEAD", "GET"]
        assert transactions[0]["attributes"]["http_response_body_bytes"] == "0"
        assert transactions[1]["attributes"]["http_response_body_bytes"] == "2"
        assert "http_incomplete" not in transactions[1]["attributes"]

    def test_ipv6_fragment_tail_is_not_decoded_as_l4(self, converter, gen, tmp_path):
        """A non-first fragment carries body bytes, not a TCP header.

        Decoding one would invent ports and a sequence number, and hand the
        reassembler a phantom flow built from payload bytes.
        """
        payload = b"GET /notreally HTTP/1.1\r\nHost: h\r\n\r\n" + b"A" * 24
        fragment_header = struct.pack(">BBHI", 6, 0, (185 << 3) | 1, 0xCAFE)  # offset 185, more=1
        body = fragment_header + payload
        src6 = bytes.fromhex("20010db8" + "00" * 11 + "01")
        dst6 = bytes.fromhex("20010db8" + "00" * 11 + "02")
        ipv6 = struct.pack(">IHBB", 0x60000000, len(body), 44, 64) + src6 + dst6 + body
        frame = gen.eth(gen.MAC2, gen.MAC1, 0x86DD, ipv6)
        gen.write_classic_pcap(tmp_path / "frag6.pcap", [frame])
        pf = _convert(
            converter, tmp_path / "frag6.pcap", tmp_path / "out.parquet", reassemble="http"
        )
        rows = pf.read().to_pylist()
        assert len(rows) == 1
        attrs = dict(rows[0]["attributes"])
        assert attrs["ip_version"] == "6"
        assert attrs["fragment_offset"] == "185"
        assert "src_port" not in attrs
        assert "tcp_sequence" not in attrs
        assert _transactions(pf) == []

    def test_parse_decisions_record_the_flag(self, converter, tmp_path):
        pf = _convert(
            converter, DATA / "sample_http.pcap", tmp_path / "out.parquet", reassemble="http"
        )
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        assert json.loads(meta[parquet_format.META_PARSE_DECISIONS])["reassemble"] == "http"
        counts = json.loads(
            {
                k.decode(): v.decode()
                for k, v in pf.metadata.metadata.items()
                if k != b"ARROW:schema"
            }["vestigo.row_counts"]
        )
        assert counts["http_transactions"] == 2
        assert counts["parsed"] == counts["packets"] + counts["http_transactions"]

    def test_off_by_default(self, converter, tmp_path):
        pf = _convert(converter, DATA / "sample_http.pcap", tmp_path / "out.parquet")
        assert _transactions(pf) == []
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        assert json.loads(meta[parquet_format.META_PARSE_DECISIONS])["reassemble"] is None

    def test_rejects_unknown_protocol(self, converter, tmp_path):
        with pytest.raises(SystemExit, match="reassemble"):
            converter.convert(
                str(DATA / "sample_http.pcap"),
                str(tmp_path / "o.parquet"),
                1,
                False,
                reassemble="ftp",
            )

    def test_content_encoding_reports_decoded_size(self, converter, gen, tmp_path):
        import gzip

        payload = b"compressible " * 64
        body = gzip.compress(payload)
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        capture = _stream_capture(
            gen,
            tmp_path / "gz.pcap",
            [(True, b"GET /big HTTP/1.1\r\nHost: h\r\n\r\n"), (False, response)],
        )
        attrs = _transactions(
            _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        )[0]["attributes"]
        assert attrs["http_content_encoding"] == "gzip"
        assert attrs["http_response_body_bytes"] == str(len(body))
        assert attrs["http_response_body_decoded_bytes"] == str(len(payload))

    def test_head_response_has_no_body(self, converter, gen, tmp_path):
        """Content-Length on a HEAD response describes a body that is not sent."""
        capture = _stream_capture(
            gen,
            tmp_path / "head.pcap",
            [
                (True, b"HEAD /page HTTP/1.1\r\nHost: h\r\n\r\n"),
                (False, b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\n\r\n"),
                (True, b"GET /next HTTP/1.1\r\nHost: h\r\n\r\n"),
                (False, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"),
            ],
        )
        transactions = _transactions(
            _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        )
        assert [t["attributes"]["http_method"] for t in transactions] == ["HEAD", "GET"]
        assert transactions[0]["attributes"]["http_response_body_bytes"] == "0"
        assert "http_incomplete" not in transactions[0]["attributes"]

    def test_interim_100_continue_is_not_a_transaction(self, converter, gen, tmp_path):
        capture = _stream_capture(
            gen,
            tmp_path / "continue.pcap",
            [
                (
                    True,
                    b"POST /up HTTP/1.1\r\nHost: h\r\nExpect: 100-continue\r\nContent-Length: 4\r\n\r\n",
                ),
                (False, b"HTTP/1.1 100 Continue\r\n\r\n"),
                (True, b"data"),
                (False, b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n"),
            ],
        )
        transactions = _transactions(
            _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        )
        assert len(transactions) == 1
        assert transactions[0]["attributes"]["status_code"] == "201"
        assert transactions[0]["attributes"]["http_request_body_bytes"] == "4"

    def test_request_without_response_is_emitted_incomplete(self, converter, gen, tmp_path):
        capture = _stream_capture(
            gen, tmp_path / "orphan.pcap", [(True, b"GET /gone HTTP/1.1\r\nHost: h\r\n\r\n")]
        )
        transactions = _transactions(
            _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        )
        assert len(transactions) == 1
        attrs = transactions[0]["attributes"]
        assert attrs["http_incomplete"] == "true"
        assert attrs["http_response_missing"] == "true"
        assert attrs["http_uri"] == "/gone"

    def test_non_http_traffic_yields_no_transactions(self, converter, gen, tmp_path):
        capture = _stream_capture(
            gen, tmp_path / "ssh.pcap", [(True, b"SSH-2.0-OpenSSH_9.6\r\n"), (False, b"\x00" * 64)]
        )
        pf = _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        assert _transactions(pf) == []
        assert pf.metadata.num_rows > 0  # packet rows still there

    def test_gap_is_flagged_not_hidden(self, converter, gen, tmp_path):
        """A missing middle segment must never silently splice two halves."""
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n"
        frames = [
            gen._c2s(0, gen.SYN),
            gen._s2c(0, gen.SYN | gen.ACK),
            gen._c2s(1, gen.ACK),
            gen._c2s(1, gen.PSH | gen.ACK, b"GET /x HTTP/1.1\r\nHost: h\r\n\r\n"),
            gen._s2c(1, gen.ACK, head),
            # First 10 body bytes never captured; the last 10 arrive, then FIN.
            gen._s2c(1 + len(head) + 10, gen.PSH | gen.ACK, b"BBBBBBBBBB"),
            gen._s2c(1 + len(head) + 20, gen.FIN | gen.ACK),
            gen._c2s(1 + 28, gen.FIN | gen.ACK),
        ]
        gen.write_classic_pcap(tmp_path / "gap.pcap", frames)
        transactions = _transactions(
            _convert(converter, tmp_path / "gap.pcap", tmp_path / "out.parquet", reassemble="http")
        )
        assert len(transactions) == 1
        assert transactions[0]["attributes"]["http_incomplete"] == "true"

    def test_parallel_matches_sequential(self, converter, tmp_path):
        """Streams never cross files, so per-file workers change nothing."""
        import subprocess
        import sys

        caps = tmp_path / "captures"
        caps.mkdir()
        for i in range(4):
            (caps / f"cap_{i}.pcap").write_bytes((DATA / "sample_http.pcap").read_bytes())
        pf_seq = _convert(converter, caps, tmp_path / "seq.parquet", reassemble="http")
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
                "--reassemble",
                "http",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr

        def key_set(pf):
            return {
                (r["source_file"], r["byte_offset"], r["content_hash"], r["artifact"])
                for r in pf.read().to_pylist()
            }

        assert key_set(pq.ParquetFile(tmp_path / "par.parquet")) == key_set(pf_seq)
        assert len(_transactions(pf_seq)) == 8

    def test_help_states_the_limits(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"], capture_output=True, text=True
        )
        text = " ".join(proc.stdout.split())
        assert "--reassemble" in text
        assert "HTTPS is not decrypted" in text
        assert "HTTP/2 and HTTP/3 are not parsed" in text
        assert "snaplen" in text


class TestHttpReassemblyHostileInput:
    """Evidence is attacker-authored; a crafted stream costs one flow, not the run."""

    def test_endless_headers_do_not_grow_without_bound(self, converter, gen, tmp_path):
        chunk = b"X-Pad: " + b"A" * 1000 + b"\r\n"
        repeats = (converter._REASM_MAX_HEADER_BYTES // len(chunk)) + 4
        capture = _stream_capture(
            gen,
            tmp_path / "headers.pcap",
            [(True, b"GET / HTTP/1.1\r\n" + chunk * repeats)],
        )
        pf = _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        assert _transactions(pf) == []
        assert pf.metadata.num_rows > 0

    def test_absurd_content_length_does_not_allocate(self, converter, gen, tmp_path):
        capture = _stream_capture(
            gen,
            tmp_path / "cl.pcap",
            [
                (True, b"GET /x HTTP/1.1\r\nHost: h\r\n\r\n"),
                (False, b"HTTP/1.1 200 OK\r\nContent-Length: 999999999999\r\n\r\nshort"),
            ],
        )
        pf = _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        # The response is refused; the unanswered request still surfaces.
        transactions = _transactions(pf)
        assert len(transactions) == 1
        assert transactions[0]["attributes"]["http_incomplete"] == "true"
        assert transactions[0]["attributes"]["http_response_missing"] == "true"

    def test_decompression_bomb_is_reported_not_expanded(self, converter, gen, tmp_path):
        import zlib

        bomb = zlib.compress(b"\x00" * (converter._REASM_MAX_DECOMPRESSED_BYTES + 1024), 9)
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Encoding: deflate\r\nContent-Length: "
            + str(len(bomb)).encode()
            + b"\r\n\r\n"
            + bomb
        )
        capture = _stream_capture(
            gen,
            tmp_path / "bomb.pcap",
            [(True, b"GET /b HTTP/1.1\r\nHost: h\r\n\r\n"), (False, response)],
        )
        attrs = _transactions(
            _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        )[0]["attributes"]
        assert attrs["http_response_body_decode_failed"] == "true"
        assert "http_response_body_decoded_bytes" not in attrs

    def test_connection_close_body_is_bounded(self, converter, gen, tmp_path, monkeypatch):
        """Nothing on the wire ends an ``until_close`` body — only the cap does."""
        monkeypatch.setattr(converter, "_REASM_MAX_BODY_BYTES", 2048)
        capture = _stream_capture(
            gen,
            tmp_path / "close.pcap",
            [
                (True, b"GET /big HTTP/1.0\r\nHost: h\r\n\r\n"),
                (False, b"HTTP/1.0 200 OK\r\n\r\n" + b"A" * 8192),
            ],
        )
        pf = _convert(converter, capture, tmp_path / "out.parquet", reassemble="http")
        # The response is refused; the unanswered request still surfaces.
        transactions = _transactions(pf)
        assert len(transactions) == 1
        assert transactions[0]["attributes"]["http_response_missing"] == "true"
        assert pf.metadata.num_rows > 1  # packet rows survive

    def test_unanswered_request_queue_is_bounded(self, converter):
        """A single-direction capture must not queue requests for the whole run."""
        reassembler = converter._HttpReassembler()
        request = b"GET /x HTTP/1.1\r\nHost: h\r\n\r\n"
        rows = []
        for index in range(converter._REASM_MAX_PENDING_REQUESTS + 20):
            attrs = {
                "src_ip": "10.0.0.1",
                "src_port": 1024,
                "dst_ip": "10.0.0.2",
                "dst_port": 80,
                "tcp_flags": "PSHACK",
                "captured_length": 60,
                "packet_length": 60,
                "tcp_sequence": 1 + index * len(request),
            }
            rows.extend(
                reassembler.handle(attrs, request, index * 100, b"record", 1_000_000 + index)
            )
        flow = next(iter(reassembler.flows.values()))
        assert len(flow.requests) <= converter._REASM_MAX_PENDING_REQUESTS
        assert len(rows) == 20  # the overflow is emitted, not dropped
        assert all(row[2]["attributes"]["http_response_missing"] == "true" for row in rows)

    def test_records_before_a_skipped_gap_stay_in_the_provenance(self, converter):
        """Forwarding over a hole must not silently drop what it consumed."""
        direction = converter._HttpDirection(False)
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"
        direction.add(1000, True, b"", 0, b"", 1)
        direction.add(1001, False, head, 10, b"head-record", 2)
        list(direction.messages(2, converter._NO_PEER_METHOD, False))
        body_start = 1001 + len(head)
        # One contiguous body segment, then holes past it until the pending cap
        # forces the direction to give up and jump forward.
        direction.add(body_start, False, b"B" * 10, 20, b"body-record", 3)
        for index in range(converter._REASM_MAX_PENDING_SEGMENTS + 1):
            direction.add(body_start + 5000 + index * 10, False, b"C" * 10, 1000 + index, b"r", 4)
        assert direction.gap is True
        assert 20 in direction.msg.records  # consumed before the jump, still credited

    def test_flow_table_is_bounded(self, converter, gen, tmp_path):
        """Beyond the LRU cap the oldest flow is evicted, not accumulated."""
        reassembler = converter._HttpReassembler()
        for index in range(converter._REASM_MAX_FLOWS + 50):
            attrs = {
                "src_ip": "10.0.0.1",
                "src_port": 1024 + index,
                "dst_ip": "10.0.0.2",
                "dst_port": 80,
                "tcp_flags": "SYN",
                "captured_length": 60,
                "packet_length": 60,
                "tcp_sequence": 1000,
            }
            reassembler.handle(attrs, b"", index * 100, b"record", 1_000_000 + index)
        assert len(reassembler.flows) <= converter._REASM_MAX_FLOWS
        assert reassembler.evicted >= 50


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
