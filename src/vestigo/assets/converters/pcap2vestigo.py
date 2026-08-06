#!/usr/bin/env python3
"""Convert packet captures (pcap/pcapng) to a Vestigo Parquet file.

Parses raw network captures produced by Wireshark/tcpdump — both the classic
libpcap format and the newer block-based pcapng format — locally and writes
one ``.parquet`` file in the Vestigo interchange format (version 1).
Upload the result to the Vestigo web interface or ingest it with
``vestigo ingest`` — no CSV/JSONL intermediate, no server re-parse.

One row is emitted per packet, decoded down to the Ethernet/Linux-SLL/raw-IP,
IPv4/IPv6, and TCP/UDP/ICMP/ARP headers. By default no TCP stream reassembly
and no multi-packet application-layer decoding is performed.

``--reassemble http`` adds one derived ``network:http:transaction`` row per
HTTP/1.x request/response pair, reassembled from the TCP streams. Those rows
are emitted *in addition to* the packet rows, never instead of them: packet
rows stay the forensic floor, and the same capture converted with and without
the flag yields byte-identical packet rows. Limits: cleartext HTTP/1.0 and 1.1
only — no HTTPS (not decrypted), no HTTP/2 or HTTP/3 (binary framing +
HPACK/QPACK), and nothing useful from a snaplen-truncated or single-direction
capture. See ``--help`` and ``docs/INPUT_FORMATS.md``.

Row order: packets are written in file order, but a transaction row is written
when its response completes, so with ``--reassemble`` the output is no longer
strictly ordered by ``byte_offset``. This is harmless — the server sorts on
query — but a reader walking the file expecting monotone offsets must not.

Unlike the vendored ``pcap2timesketch.py`` this converter does not merge
multiple input files into one globally time-sorted stream (that k-way merge
existed only to produce a sorted CSV/JSONL timeline) — the server sorts on
query, so each file's packets are written in file order. Multi-file input
still gets one worker process per file when ``-w`` allows it; there is no
cross-file merge step.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the packet record within it (``byte_offset``), and the sha256
    of the raw record bytes (``content_hash``). The exact byte span the
    ``content_hash`` covers depends on the capture format, so an examiner
    re-verifying by hand must hash the matching span: classic pcap = the
    16-byte record header plus captured data; pcapng = the whole block from
    its type field through the trailing block-total-length (options included).
    In all cases it is the contiguous ``byte_offset``-anchored span on disk,
  * the converter name and version, which become the server-side parser
    identity.

Reassembled transaction rows use a **different, explicitly non-contiguous**
convention, because a transaction spans N packet records that are not adjacent
on disk (and may interleave with other connections' records):

  * ``byte_offset`` = the offset of the record carrying the request line — a
    real offset into the capture, but the start of one packet, not of the
    transaction,
  * ``content_hash`` = sha256 over the concatenation of every contributing
    record's raw span, taken in capture order (ascending ``byte_offset``).
    Deterministic and re-derivable by hand, but **not** a single ``dd`` span,
  * ``packet_offsets`` lists those record offsets (``packet_count`` holds the
    true count; the list itself is capped, and ``packet_offsets_truncated``
    says so when it was), so an examiner can reconstruct the exact input.

Every such row carries ``reassembled=true``,
``byte_offset_basis=request_line_record`` and
``content_hash_basis=reassembled_records``, so the two conventions are never
confused by inspection. ``vestigo.parse_decisions.reassemble`` records the flag
in the footer: it changes which rows exist.

No gzip support: raw captures only (matches the vendored converter).

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow pcap2vestigo.py ...

Usage:

    python pcap2vestigo.py -i capture.pcap -o capture.parquet
    python pcap2vestigo.py -i /var/captures/ -o captures.parquet -w 8
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import hashlib
import io
import ipaddress
import multiprocessing
import os
import re
import struct
import sys
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "error: pyarrow is required to write Vestigo Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow pcap2vestigo.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "pcap2vestigo"
CONVERTER_VERSION = "1.4.0"

# ---------------------------------------------------------------------------
# Vestigo Parquet interchange format v1 — embedded copy of the spec in
# src/vestigo/ingestion/parquet_format.py (this script is a standalone
# download and cannot import it; the repo test suite asserts both stay equal).
# ---------------------------------------------------------------------------

FORMAT_VERSION = "1"
META_FORMAT_VERSION = "vestigo.format_version"
META_CONVERTER_NAME = "vestigo.converter_name"
META_CONVERTER_VERSION = "vestigo.converter_version"
META_ORIGINAL_FILES = "vestigo.original_files"
# Additive forensic footer metadata (Tier 1). Ignored by older readers.
META_CONVERTED_AT = "vestigo.converted_at"
META_ROW_COUNTS = "vestigo.row_counts"
META_TIMEZONE_ASSUMPTION = "vestigo.timezone_assumption"
META_PARSE_DECISIONS = "vestigo.parse_decisions"

PARQUET_EVENT_SCHEMA = pa.schema(
    [
        pa.field("source_file", pa.string()),
        pa.field("file_hash", pa.string()),
        pa.field("byte_offset", pa.uint64()),
        pa.field("content_hash", pa.string()),
        pa.field("message", pa.string()),
        pa.field("timestamp", pa.timestamp("ms", tz="UTC")),
        pa.field("timestamp_desc", pa.string()),
        pa.field("artifact", pa.string()),
        pa.field("artifact_long", pa.string()),
        pa.field("display_name", pa.string()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("attributes", pa.map_(pa.string(), pa.string())),
    ]
)

# ---------------------------------------------------------------------------
# pcap/pcapng parsing (ported from pcap2timesketch.py, converter parity)
# ---------------------------------------------------------------------------

# Upper bound on a single packet record / pcapng block. The length fields
# these come from are attacker-controlled (up to ~4 GiB) and are read into
# memory in one shot; a crafted or corrupt capture could otherwise force a
# multi-GB allocation (memory-exhaustion DoS). 256 MiB is far above any real
# packet or block yet bounds the damage — over it we treat the file as corrupt.
_MAX_RECORD_BYTES = 256 * 1024 * 1024

_PCAP_EXTENSIONS = {".pcap", ".pcapng", ".cap", ".dmp"}

_MAGIC_US_BE = b"\xa1\xb2\xc3\xd4"  # classic pcap, big-endian, microsecond ts
_MAGIC_US_LE = b"\xd4\xc3\xb2\xa1"  # classic pcap, little-endian, microsecond ts
_MAGIC_NS_BE = b"\xa1\xb2\x3c\x4d"  # classic pcap, big-endian, nanosecond ts
_MAGIC_NS_LE = b"\x4d\x3c\xb2\xa1"  # classic pcap, little-endian, nanosecond ts
_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"  # pcapng Section Header Block type (palindrome)

_ALL_MAGICS = {_MAGIC_US_BE, _MAGIC_US_LE, _MAGIC_NS_BE, _MAGIC_NS_LE, _PCAPNG_MAGIC}

# Link types we decode. Anything else is skipped per-packet with a warning.
_LINK_TYPE_NAMES = {1: "ethernet", 101: "raw_ip", 113: "linux_sll"}

_IP_PROTO_NAMES = {
    1: "icmp",
    2: "igmp",
    6: "tcp",
    17: "udp",
    47: "gre",
    50: "esp",
    51: "ah",
    58: "icmpv6",
    132: "sctp",
}

# IPv6 extension header types walked to reach the real transport header.
_IPV6_EXT_HEADERS = {0, 43, 44, 60, 51}

_TCP_FLAG_BITS = [
    (0x01, "FIN"),
    (0x02, "SYN"),
    (0x04, "RST"),
    (0x08, "PSH"),
    (0x10, "ACK"),
    (0x20, "URG"),
    (0x40, "ECE"),
    (0x80, "CWR"),
]


class PcapParseError(Exception):
    """Raised for a file/block-level capture corruption (caught per-file)."""


class _MalformedPacket(Exception):
    """Internal: a single packet's L2/L3 header could not be decoded."""


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _mac_str(data: bytes) -> str:
    return ":".join(f"{b:02x}" for b in data)


def _protocol_name(protocol_id: int) -> str:
    return _IP_PROTO_NAMES.get(protocol_id, "other")


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def _looks_like_capture(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in _ALL_MAGICS
    except OSError:
        return False


def find_pcap_files(input_path: str) -> list[Path]:
    """Resolve the input into a sorted list of pcap/pcapng files."""
    path = Path(input_path)
    if path.is_file():
        return [path]
    if path.is_dir():
        files: set[Path] = set()
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            if (
                candidate.suffix.lower() in _PCAP_EXTENSIONS
                or not candidate.suffix
                and _looks_like_capture(candidate)
            ):
                files.add(candidate)
        if not files:
            raise SystemExit(f"error: no pcap/pcapng files found in {input_path}")
        return sorted(files)
    raise SystemExit(f"error: input path not found: {input_path}")


def hash_file(path: Path) -> tuple[str, int]:
    """Return the streaming sha256 hex digest and size of ``path``."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# Classic pcap parsing
# ---------------------------------------------------------------------------

# Yields (ts_us, link_type_name, interface, captured_len, packet_len, data,
#         record_offset, record_bytes)
_PacketTuple = tuple[int, "str | None", str, int, int, bytes, int, bytes]


def _iter_pcap_classic(
    fh: BinaryIO, byte_order: str, nanosecond: bool, link_type_name: str | None
) -> Iterator[_PacketTuple]:
    while True:
        record_offset = fh.tell()
        hdr = fh.read(16)
        if len(hdr) == 0:
            return
        if len(hdr) < 16:
            raise PcapParseError("truncated pcap packet record header")

        ts_sec, ts_frac, incl_len, orig_len = struct.unpack(byte_order + "IIII", hdr)
        if incl_len > _MAX_RECORD_BYTES:
            raise PcapParseError(
                f"pcap packet record length {incl_len} exceeds the {_MAX_RECORD_BYTES}-byte cap"
            )
        data = fh.read(incl_len)
        if len(data) < incl_len:
            raise PcapParseError("truncated pcap packet data")

        ts_us = ts_sec * 1_000_000 + (ts_frac // 1000 if nanosecond else ts_frac)
        yield ts_us, link_type_name, "", incl_len, orig_len, data, record_offset, hdr + data


# ---------------------------------------------------------------------------
# pcapng parsing
# ---------------------------------------------------------------------------


def _parse_pcapng_options(data: bytes, byte_order: str) -> dict[int, bytes]:
    """Parse a pcapng options TLV list into ``{option_code: raw_value}``."""
    opts: dict[int, bytes] = {}
    offset = 0
    while offset + 4 <= len(data):
        code, length = struct.unpack(byte_order + "HH", data[offset : offset + 4])
        offset += 4
        if code == 0:  # opt_endofopt
            break
        value = data[offset : offset + length]
        opts.setdefault(code, value)
        offset += (length + 3) & ~3  # pad to 4-byte boundary
    return opts


def _tsresol_seconds(value: bytes | None) -> float:
    """Convert an ``if_tsresol`` option value to seconds-per-tick. Default: 1us."""
    if not value:
        return 1e-6
    b = value[0]
    if b & 0x80:
        return 2.0 ** (-(b & 0x7F))
    return 10.0 ** (-b)


def _iter_pcap_ng(fh: BinaryIO) -> Iterator[_PacketTuple]:
    """Yield packet tuples for pcapng blocks.

    Interfaces (link type + timestamp resolution + name) are tracked per
    section, reset at each new Section Header Block, and referenced by
    Enhanced Packet Blocks via ``interface_id``.
    """
    byte_order: str | None = None
    interfaces: list[dict[str, Any]] = []

    while True:
        record_offset = fh.tell()
        block_type_raw = fh.read(4)
        if len(block_type_raw) == 0:
            return
        if len(block_type_raw) < 4:
            raise PcapParseError("truncated pcapng block type")

        if block_type_raw == _PCAPNG_MAGIC:
            rest = fh.read(8)
            if len(rest) < 8:
                raise PcapParseError("truncated pcapng section header block")
            block_total_length_raw, bom_raw = rest[0:4], rest[4:8]
            if bom_raw == b"\x1a\x2b\x3c\x4d":
                byte_order = ">"
            elif bom_raw == b"\x4d\x3c\x2b\x1a":
                byte_order = "<"
            else:
                raise PcapParseError("bad pcapng byte-order magic")

            block_total_length = struct.unpack(byte_order + "I", block_total_length_raw)[0]
            if block_total_length < 16:
                raise PcapParseError("bad pcapng section header block length")
            if block_total_length > _MAX_RECORD_BYTES:
                raise PcapParseError(
                    f"pcapng section header block length {block_total_length} exceeds the "
                    f"{_MAX_RECORD_BYTES}-byte cap"
                )
            remaining = block_total_length - 16
            if len(fh.read(remaining)) < remaining:
                raise PcapParseError("truncated pcapng section header block body")
            if len(fh.read(4)) < 4:
                raise PcapParseError("truncated pcapng section header block trailer")

            interfaces = []
            continue

        if byte_order is None:
            raise PcapParseError("pcapng block encountered before a section header")

        block_total_length_raw = fh.read(4)
        if len(block_total_length_raw) < 4:
            raise PcapParseError("truncated pcapng block length")
        block_total_length = struct.unpack(byte_order + "I", block_total_length_raw)[0]
        if block_total_length < 12:
            raise PcapParseError("bad pcapng block length")
        if block_total_length > _MAX_RECORD_BYTES:
            raise PcapParseError(
                f"pcapng block length {block_total_length} exceeds the {_MAX_RECORD_BYTES}-byte cap"
            )

        body_len = block_total_length - 12
        body = fh.read(body_len)
        if len(body) < body_len:
            raise PcapParseError("truncated pcapng block body")
        trailer_raw = fh.read(4)
        if len(trailer_raw) < 4:
            raise PcapParseError("truncated pcapng block trailer")

        block_type = struct.unpack(byte_order + "I", block_type_raw)[0]

        if block_type == 1:  # Interface Description Block
            if len(body) < 8:
                raise PcapParseError("truncated pcapng interface description block")
            linktype_num, _reserved, _snaplen = struct.unpack(byte_order + "HHI", body[0:8])
            opts = _parse_pcapng_options(body[8:], byte_order)
            interfaces.append(
                {
                    "link_type": _LINK_TYPE_NAMES.get(linktype_num),
                    "tsresol_seconds": _tsresol_seconds(opts.get(9)),
                    "name": opts.get(2, b"").decode("utf-8", errors="replace"),
                }
            )

        elif block_type == 6:  # Enhanced Packet Block
            if len(body) < 20:
                raise PcapParseError("truncated pcapng enhanced packet block")
            interface_id, ts_high, ts_low, captured_len, packet_len = struct.unpack(
                byte_order + "IIIII", body[0:20]
            )
            packet_data = body[20 : 20 + captured_len]
            if interface_id < len(interfaces):
                iface = interfaces[interface_id]
            else:
                iface = {"link_type": None, "tsresol_seconds": 1e-6, "name": ""}
            ticks = (ts_high << 32) | ts_low
            ts_us = round(ticks * iface["tsresol_seconds"] * 1_000_000)
            record_bytes = block_type_raw + block_total_length_raw + body + trailer_raw
            yield (
                ts_us,
                iface["link_type"],
                iface["name"],
                captured_len,
                packet_len,
                packet_data,
                record_offset,
                record_bytes,
            )

        elif block_type == 3:  # Simple Packet Block (no interface ref, no timestamp)
            if len(body) < 4:
                raise PcapParseError("truncated pcapng simple packet block")
            packet_len = struct.unpack(byte_order + "I", body[0:4])[0]
            packet_data = body[4 : 4 + packet_len]
            iface = interfaces[0] if interfaces else {"link_type": None, "name": ""}
            record_bytes = block_type_raw + block_total_length_raw + body + trailer_raw
            yield (
                0,
                iface["link_type"],
                iface.get("name", ""),
                len(packet_data),
                packet_len,
                packet_data,
                record_offset,
                record_bytes,
            )

        # Any other block type (obsolete Packet Block, Name Resolution Block,
        # Interface Statistics Block, ...) is already consumed above and
        # simply skipped — it carries no packet to emit.


# ---------------------------------------------------------------------------
# L2 decoders
# ---------------------------------------------------------------------------


def _decode_ethernet(data: bytes) -> tuple[int, bytes, str, str] | None:
    """Return ``(ethertype, payload, src_mac, dst_mac)``, walking VLAN tags."""
    if len(data) < 14:
        return None
    dst_mac, src_mac = data[0:6], data[6:12]
    ethertype = struct.unpack(">H", data[12:14])[0]
    offset = 14
    while ethertype in (0x8100, 0x88A8) and len(data) >= offset + 4:
        ethertype = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        offset += 4
    return ethertype, data[offset:], _mac_str(src_mac), _mac_str(dst_mac)


def _decode_linux_sll(data: bytes) -> tuple[int, bytes, str, str] | None:
    """Return ``(ethertype, payload, src_mac, dst_mac)`` for Linux cooked capture."""
    if len(data) < 16:
        return None
    addr_len = struct.unpack(">H", data[4:6])[0]
    addr = data[6 : 6 + min(addr_len, 8)]
    ethertype = struct.unpack(">H", data[14:16])[0]
    src_mac = _mac_str(addr) if addr_len == 6 else ""
    return ethertype, data[16:], src_mac, ""


def _decode_raw_ip(data: bytes) -> tuple[int, bytes, str, str] | None:
    """Return ``(ethertype, payload, src_mac, dst_mac)`` inferred from IP version."""
    if not data:
        return None
    version = data[0] >> 4
    if version == 4:
        ethertype = 0x0800
    elif version == 6:
        ethertype = 0x86DD
    else:
        return None
    return ethertype, data, "", ""


# ---------------------------------------------------------------------------
# L3 decoders
# ---------------------------------------------------------------------------


def _decode_ipv4(data: bytes) -> dict[str, Any] | None:
    if len(data) < 20:
        return None
    ihl = (data[0] & 0x0F) * 4
    if ihl < 20 or len(data) < ihl:
        return None

    total_length = struct.unpack(">H", data[2:4])[0]
    ip_id = struct.unpack(">H", data[4:6])[0]
    flags_frag = struct.unpack(">H", data[6:8])[0]
    fragment_offset = (flags_frag & 0x1FFF) * 8
    ttl = data[8]
    protocol_id = data[9]
    src_ip = str(ipaddress.IPv4Address(data[12:16]))
    dst_ip = str(ipaddress.IPv4Address(data[16:20]))

    payload_end = min(total_length, len(data)) if total_length >= ihl else len(data)
    payload = data[ihl:payload_end]

    return {
        "ttl": ttl,
        "ip_id": ip_id,
        "fragment_offset": fragment_offset,
        "protocol_id": protocol_id,
        "protocol_name": _protocol_name(protocol_id),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "payload": payload,
    }


def _decode_ipv6(data: bytes) -> dict[str, Any] | None:
    if len(data) < 40:
        return None
    payload_length = struct.unpack(">H", data[4:6])[0]
    next_header = data[6]
    hop_limit = data[7]
    src_ip = str(ipaddress.IPv6Address(data[8:24]))
    dst_ip = str(ipaddress.IPv6Address(data[24:40]))

    remaining = data[40 : 40 + payload_length] if payload_length else data[40:]

    for _ in range(8):
        if next_header not in _IPV6_EXT_HEADERS or len(remaining) < 2:
            break
        if next_header == 44:  # Fragment header: fixed 8 bytes
            hdr_len_bytes = 8
        else:
            hdr_ext_len = remaining[1]
            hdr_len_bytes = (hdr_ext_len + 1) * 8
        if len(remaining) < hdr_len_bytes:
            break
        next_header = remaining[0]
        remaining = remaining[hdr_len_bytes:]

    return {
        "hop_limit": hop_limit,
        "protocol_id": next_header,
        "protocol_name": _protocol_name(next_header),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "payload": remaining,
    }


def _decode_arp(data: bytes) -> dict[str, Any] | None:
    if len(data) < 8:
        return None
    _htype, ptype = struct.unpack(">HH", data[0:4])
    hlen, plen = data[4], data[5]
    opcode = struct.unpack(">H", data[6:8])[0]

    result: dict[str, Any] = {"arp_opcode": opcode}
    offset = 8
    if ptype == 0x0800 and plen == 4 and len(data) >= offset + 2 * hlen + 2 * plen:
        sha = data[offset : offset + hlen]
        offset += hlen
        spa = data[offset : offset + plen]
        offset += plen
        tha = data[offset : offset + hlen]
        offset += hlen
        tpa = data[offset : offset + plen]
        result["arp_sender_ip"] = str(ipaddress.IPv4Address(spa))
        result["arp_target_ip"] = str(ipaddress.IPv4Address(tpa))
        if hlen == 6:
            result["src_mac"] = _mac_str(sha)
            result["dst_mac"] = _mac_str(tha)
    return result


# ---------------------------------------------------------------------------
# L4 decoders
# ---------------------------------------------------------------------------


def _decode_tcp(data: bytes) -> dict[str, Any] | None:
    if len(data) < 20:
        return None
    src_port, dst_port = struct.unpack(">HH", data[0:4])
    seq = struct.unpack(">I", data[4:8])[0]
    ack = struct.unpack(">I", data[8:12])[0]
    flags_byte = data[13]
    window = struct.unpack(">H", data[14:16])[0]
    flags = "".join(name for bit, name in _TCP_FLAG_BITS if flags_byte & bit)
    return {
        "src_port": src_port,
        "dst_port": dst_port,
        "tcp_sequence": seq,
        "tcp_ack": ack,
        "tcp_window": window,
        "tcp_flags": flags,
    }


def _decode_udp(data: bytes) -> dict[str, Any] | None:
    if len(data) < 8:
        return None
    src_port, dst_port, length = struct.unpack(">HHH", data[0:6])
    return {"src_port": src_port, "dst_port": dst_port, "udp_length": length}


def _decode_icmp(data: bytes) -> dict[str, Any] | None:
    if len(data) < 4:
        return None
    return {"icmp_type": data[0], "icmp_code": data[1]}


def _decode_l4(protocol_name: str, payload: bytes) -> dict[str, Any] | None:
    if protocol_name == "tcp":
        return _decode_tcp(payload)
    if protocol_name == "udp":
        return _decode_udp(payload)
    if protocol_name in ("icmp", "icmpv6"):
        return _decode_icmp(payload)
    return None


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


def _artifact_for(protocol: str) -> str:
    if protocol in ("tcp", "udp", "icmp", "icmpv6", "arp"):
        return f"network:packet:{protocol}"
    return "network:packet:other"


def _addr(ip: str, port: Any) -> str:
    return f"{ip}:{port}" if port not in (None, "") else ip


def _build_message(attrs: dict[str, Any]) -> str:
    protocol = attrs.get("protocol", "other")
    src_ip, dst_ip = attrs.get("src_ip", ""), attrs.get("dst_ip", "")
    length = attrs.get("packet_length") or attrs.get("captured_length") or 0

    if protocol == "tcp":
        flags = attrs.get("tcp_flags", "")
        return (
            f"TCP {_addr(src_ip, attrs.get('src_port'))} -> "
            f"{_addr(dst_ip, attrs.get('dst_port'))} [{flags}] len={length}"
        )
    if protocol == "udp":
        return (
            f"UDP {_addr(src_ip, attrs.get('src_port'))} -> "
            f"{_addr(dst_ip, attrs.get('dst_port'))} len={length}"
        )
    if protocol in ("icmp", "icmpv6"):
        label = "ICMPv6" if protocol == "icmpv6" else "ICMPv4"
        return (
            f"{label} {src_ip} -> {dst_ip} type={attrs.get('icmp_type', '')} "
            f"code={attrs.get('icmp_code', '')} len={length}"
        )
    if protocol == "arp":
        opcode = attrs.get("arp_opcode")
        if opcode == 1:
            return f"ARP who-has {attrs.get('arp_target_ip', '')} tell {attrs.get('arp_sender_ip', '')}"
        if opcode == 2:
            return f"ARP {attrs.get('arp_sender_ip', '')} is-at {attrs.get('src_mac', '')}"
        return f"ARP opcode={opcode}"
    if src_ip or dst_ip:
        return f"IP proto={attrs.get('protocol_id', '')} {src_ip} -> {dst_ip} len={length}"
    ethertype = attrs.get("ethertype", "")
    return f"Non-IP frame ethertype={ethertype} len={length}"


def _ts_to_dt(ts_us: int) -> datetime.datetime | None:
    if not ts_us:
        return None
    return datetime.datetime.fromtimestamp(ts_us / 1_000_000, tz=datetime.UTC)


def _tcp_payload(segment: bytes) -> bytes:
    """Return the TCP payload of an L4 segment (empty if headers are short/bogus)."""
    if len(segment) < 20:
        return b""
    data_offset = (segment[12] >> 4) * 4
    if data_offset < 20 or data_offset > len(segment):
        return b""
    return segment[data_offset:]


def build_row(
    ts_us: int,
    link_type: str,
    interface: str,
    captured_length: int,
    packet_length: int,
    raw_bytes: bytes,
    want_tcp_payload: bool = False,
) -> dict[str, Any]:
    """Decode one packet into an event row dict.

    Raises ``_MalformedPacket`` when the L2/L3 headers cannot be decoded.

    With ``want_tcp_payload`` the returned dict carries an extra ``tcp_payload``
    key (bytes) for TCP packets — the reassembler's input. It is deliberately
    *not* an attribute: raw payload bytes never reach an event row.
    """
    if link_type == "ethernet":
        decoded = _decode_ethernet(raw_bytes)
    elif link_type == "linux_sll":
        decoded = _decode_linux_sll(raw_bytes)
    elif link_type == "raw_ip":
        decoded = _decode_raw_ip(raw_bytes)
    else:
        raise _MalformedPacket(f"unsupported link type: {link_type}")

    if decoded is None:
        raise _MalformedPacket("short/truncated link-layer frame")
    ethertype, payload, src_mac, dst_mac = decoded

    attrs: dict[str, Any] = {
        "link_type": link_type,
        "interface": interface,
        "src_mac": src_mac,
        "dst_mac": dst_mac,
        "captured_length": captured_length,
        "packet_length": packet_length,
    }

    protocol = "other"
    src_ip = dst_ip = ""
    l4_segment = b""

    if ethertype == 0x0806:
        arp = _decode_arp(payload)
        if arp is not None:
            attrs.update(arp)
        protocol = "arp"
    elif ethertype == 0x0800:
        ip = _decode_ipv4(payload)
        if ip is None:
            raise _MalformedPacket("bad IPv4 header")
        attrs["ip_version"] = 4
        attrs["ttl"] = ip["ttl"]
        attrs["ip_id"] = ip["ip_id"]
        attrs["fragment_offset"] = ip["fragment_offset"]
        attrs["protocol_id"] = ip["protocol_id"]
        src_ip, dst_ip = ip["src_ip"], ip["dst_ip"]
        protocol = ip["protocol_name"]
        if ip["fragment_offset"] == 0:
            l4_segment = ip["payload"]
            l4 = _decode_l4(protocol, l4_segment)
            if l4:
                attrs.update(l4)
    elif ethertype == 0x86DD:
        ip = _decode_ipv6(payload)
        if ip is None:
            raise _MalformedPacket("bad IPv6 header")
        attrs["ip_version"] = 6
        attrs["hop_limit"] = ip["hop_limit"]
        attrs["protocol_id"] = ip["protocol_id"]
        src_ip, dst_ip = ip["src_ip"], ip["dst_ip"]
        protocol = ip["protocol_name"]
        l4_segment = ip["payload"]
        l4 = _decode_l4(protocol, l4_segment)
        if l4:
            attrs.update(l4)
    else:
        attrs["ethertype"] = f"0x{ethertype:04x}"

    attrs["protocol"] = protocol
    attrs["src_ip"] = normalize_ip(src_ip)
    attrs["dst_ip"] = normalize_ip(dst_ip)

    row = {
        "message": _build_message(attrs),
        "timestamp": _ts_to_dt(ts_us),
        "timestamp_desc": "Packet Capture Time",
        "artifact": _artifact_for(protocol),
        "artifact_long": "network:packet:capture",
        "attributes": attrs,
    }
    if want_tcp_payload and protocol == "tcp":
        row["tcp_payload"] = _tcp_payload(l4_segment)
    return row


# ---------------------------------------------------------------------------
# TCP reassembly + HTTP/1.x framing  (--reassemble http)
#
# Emits one derived ``network:http:transaction`` row per request/response *in
# addition to* the per-packet rows, which stay the forensic floor: nothing here
# removes or replaces a packet row, and a capture converted without
# ``--reassemble`` is a byte-identical subset of one converted with it.
#
# Every limit below exists because the input is incident evidence and therefore
# routinely hostile: sequence numbers, lengths, chunk sizes and compression
# ratios are all attacker-controlled. Exceeding any limit kills the one flow
# that did it (its packet rows survive), never the run.
# ---------------------------------------------------------------------------

# Per direction of a connection: buffered payload plus the raw record bytes
# retained for provenance. A request's records must be held until its response
# completes, so this is the dominant reassembly cost.
_REASM_MAX_STREAM_BYTES = 8 * 1024 * 1024
# Concurrently tracked connections; the least recently active is evicted first
# (its in-flight request is emitted as incomplete rather than dropped silently).
_REASM_MAX_FLOWS = 4096
# A flow with no packet for this much *capture* time is finalized and dropped.
_REASM_IDLE_SECONDS = 300
# Header block, before the terminating blank line, and its field count.
_REASM_MAX_HEADER_BYTES = 64 * 1024
_REASM_MAX_HEADERS = 256
# Body ceilings: framed (on the wire) and decompressed (gzip/deflate bomb).
_REASM_MAX_BODY_BYTES = 64 * 1024 * 1024
_REASM_MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024
# Out-of-order segments held while waiting for the gap ahead of them to fill.
_REASM_MAX_PENDING_SEGMENTS = 1024
# Contributing record offsets listed on a transaction row before truncation.
_REASM_MAX_LISTED_OFFSETS = 256
# Idle sweep cadence, in packets fed to the reassembler.
_REASM_SWEEP_EVERY = 5000

_REASM_HTTP_PORTS = frozenset({80, 81, 591, 3128, 8000, 8008, 8080, 8081, 8088, 8888})

_RE_REQUEST_LINE = re.compile(rb"^([A-Z][A-Z_-]{1,19}) ([^ \r\n]{1,8192}) (HTTP/\d\.\d)\r?\n")
_RE_STATUS_LINE = re.compile(rb"^(HTTP/\d\.\d) (\d{3})(?:[ \t]+([^\r\n]{0,256}))?\r?\n")


class _HttpMessage:
    """One framed HTTP/1.x message plus the capture records that carried it."""

    __slots__ = (
        "is_request",
        "method",
        "uri",
        "protocol",
        "status",
        "reason",
        "headers",
        "wire_bytes",
        "body_bytes",
        "decoded_body_bytes",
        "content_encoding",
        "records",
        "anchor_offset",
        "start_ts",
        "end_ts",
        "complete",
        "gap",
        "truncated",
    )

    def __init__(self, is_request: bool, start_ts: int) -> None:
        self.is_request = is_request
        self.method = ""
        self.uri = ""
        self.protocol = ""
        self.status = 0
        self.reason = ""
        self.headers: dict[str, str] = {}
        self.wire_bytes = 0
        self.body_bytes = 0
        self.decoded_body_bytes = -1
        self.content_encoding = ""
        self.records: dict[int, bytes] = {}
        self.anchor_offset = -1
        self.start_ts = start_ts
        self.end_ts = start_ts
        self.complete = False
        self.gap = False
        self.truncated = False


def _parse_headers(block: bytes) -> dict[str, str] | None:
    """Parse a header block (no start line, no terminating blank line).

    Returns lowercased field names mapped to values; repeated fields are joined
    with ``", "`` as RFC 9110 allows. ``None`` when the block is malformed or
    exceeds ``_REASM_MAX_HEADERS``.
    """
    headers: dict[str, str] = {}
    if not block:
        return headers
    lines = block.split(b"\n")
    if len(lines) > _REASM_MAX_HEADERS:
        return None
    name = ""
    for raw in lines:
        line = raw.rstrip(b"\r")
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):  # obs-fold continuation
            if not name:
                return None
            headers[name] += " " + line.strip().decode("latin-1")
            continue
        sep = line.find(b":")
        if sep <= 0:
            return None
        name = line[:sep].strip().lower().decode("latin-1")
        value = line[sep + 1 :].strip().decode("latin-1")
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return headers


def _decoded_length(body: bytes, encoding: str) -> int:
    """Decompressed length of ``body``, or -1 when it cannot be decoded.

    Capped at ``_REASM_MAX_DECOMPRESSED_BYTES`` — a compression bomb stops the
    walk and reports -1 rather than materializing the expansion.
    """
    token = encoding.split(",")[0].strip().lower()
    if token in ("gzip", "x-gzip", "deflate"):
        wbits = zlib.MAX_WBITS | 32 if token != "deflate" else zlib.MAX_WBITS
    else:
        return -1
    for attempt_wbits in (wbits, -zlib.MAX_WBITS):
        obj = zlib.decompressobj(attempt_wbits)
        total = 0
        try:
            for start in range(0, len(body), 65536):
                out = obj.decompress(body[start : start + 65536], _REASM_MAX_DECOMPRESSED_BYTES)
                total += len(out)
                if total >= _REASM_MAX_DECOMPRESSED_BYTES or obj.unconsumed_tail:
                    return -1
            total += len(obj.flush())
        except zlib.error:
            continue
        return total
    return -1


class _HttpDirection:
    """One direction of a TCP connection: byte reassembly, then HTTP framing.

    Sequence-ordered buffering with ISN tracking. A retransmit of already
    delivered bytes is dropped, an overlapping segment is trimmed to the part
    that is new (first writer wins, matching what the receiver saw), and a
    segment landing past the contiguous end is held until the gap fills.
    """

    def __init__(self, is_request_side: bool) -> None:
        self.is_request_side = is_request_side
        self.started = False
        self.isn = 0
        self.base = 0
        self.data = bytearray()
        self.pending: dict[int, bytes] = {}
        # (start, end, record_offset, record_bytes) in stream-offset space.
        self.records: list[tuple[int, int, int, bytes]] = []
        self.buffered = 0
        self.gap = False
        self.truncated = False
        self.closed = False
        self.dead = False
        self.last_ts = 0
        # Framing state.
        self.phase = "start"  # start | body | chunk_size | chunk_data | chunk_crlf | trailer
        self.msg: _HttpMessage | None = None
        self.body_mode = "none"  # none | length | chunked | until_close
        self.need = 0
        self.body = bytearray()
        self.chunk_left = 0

    # -- byte layer --------------------------------------------------------

    def add(
        self, seq: int, syn: bool, payload: bytes, record_offset: int, record_bytes: bytes, ts: int
    ) -> None:
        if self.dead:
            return
        self.last_ts = ts
        if not self.started:
            # Without a SYN the capture started mid-stream: anchor on the first
            # sequence number seen, which makes offsets relative but consistent.
            self.isn = (seq + 1) & 0xFFFFFFFF if syn else seq
            self.started = True
        if not payload:
            return

        # Lift the 32-bit sequence into the window around the current base, so
        # a stream crossing the wrap point stays monotone.
        delta = (seq - self.isn - self.base) & 0xFFFFFFFF
        if delta >= 0x80000000:
            delta -= 0x100000000
        off = self.base + delta
        end = off + len(payload)

        contiguous_end = self.base + len(self.data)
        if end <= contiguous_end:
            return  # pure retransmit of bytes already delivered
        if off < contiguous_end:  # overlap: keep what the receiver already had
            payload = payload[contiguous_end - off :]
            off = contiguous_end

        self.records.append((off, off + len(payload), record_offset, record_bytes))
        self.buffered += len(record_bytes) + len(payload)

        if off == contiguous_end:
            self.data += payload
            self._drain()
        else:
            if len(self.pending) >= _REASM_MAX_PENDING_SEGMENTS:
                self._forward_over_gap()
            existing = self.pending.get(off)
            if existing is None or len(existing) < len(payload):
                self.buffered -= len(existing) if existing else 0
                self.pending[off] = payload
            else:
                self.buffered -= len(payload)

        if self.buffered > _REASM_MAX_STREAM_BYTES:
            self.dead = True
            self._release()

    def _drain(self) -> None:
        while self.pending:
            contiguous_end = self.base + len(self.data)
            segment = self.pending.pop(contiguous_end, None)
            if segment is None:
                # Drop anything the contiguous run has since overtaken.
                stale = [off for off in self.pending if off < contiguous_end]
                if not stale:
                    break
                for off in stale:
                    held = self.pending.pop(off)
                    self.buffered -= len(held)
                    tail = held[contiguous_end - off :]
                    if tail and len(tail) > len(self.pending.get(contiguous_end, b"")):
                        self.buffered -= len(self.pending.get(contiguous_end, b""))
                        self.pending[contiguous_end] = tail
                        self.buffered += len(tail)
                continue
            # Pending bytes become contiguous bytes: no net change in ``buffered``.
            self.data += segment

    def _forward_over_gap(self) -> None:
        """Give up on a hole: jump to the lowest held segment, flagging the gap."""
        if not self.pending:
            return
        target = min(self.pending)
        self.gap = True
        if self.msg is not None:
            self.msg.gap = True
        # Records covering the skipped span still contributed to this message's
        # provenance, so they stay in ``records`` and are attributed on consume.
        self.buffered -= len(self.data)
        self.data.clear()
        self.base = target
        self._drain()

    def _release(self) -> None:
        self.data.clear()
        self.pending.clear()
        self.records.clear()
        self.body.clear()
        self.buffered = 0

    def _take(self, count: int) -> bytes:
        """Consume ``count`` bytes, attributing the records that carried them."""
        chunk = bytes(self.data[:count])
        start, end = self.base, self.base + count
        keep: list[tuple[int, int, int, bytes]] = []
        for record in self.records:
            if record[1] > start and record[0] < end and self.msg is not None:
                self.msg.records.setdefault(record[2], record[3])
            if record[1] > end:
                keep.append(record)
            else:
                self.buffered -= len(record[3])
        self.records = keep
        del self.data[:count]
        self.base = end
        self.buffered -= count
        if self.msg is not None:
            self.msg.wire_bytes += count
        return chunk

    # -- HTTP framing ------------------------------------------------------

    def messages(self, ts: int, peer_method: str, at_close: bool) -> Iterator[_HttpMessage]:
        """Yield every message the buffered bytes now complete.

        ``peer_method`` is the method of the request this response answers (a
        response to HEAD has no body whatever its ``Content-Length`` says);
        empty on the request side.
        """
        while not self.dead:
            if self.phase == "start":
                header_end = self._find_header_end()
                if header_end is None:
                    if len(self.data) > _REASM_MAX_HEADER_BYTES:
                        self.dead = True
                        self._release()
                    return
                if not self._begin_message(header_end, ts, peer_method):
                    return
                continue
            message = self._advance_body(at_close)
            if message is None:
                return
            yield message

    def _find_header_end(self) -> int | None:
        idx = self.data.find(b"\r\n\r\n")
        if idx >= 0:
            return idx + 4
        idx = self.data.find(b"\n\n")  # tolerate LF-only framing
        return idx + 2 if idx >= 0 else None

    def _begin_message(self, header_end: int, ts: int, peer_method: str) -> bool:
        """Parse one start line + header block. False kills or stalls the direction."""
        head = bytes(self.data[:header_end])
        if self.is_request_side:
            match = _RE_REQUEST_LINE.match(head)
        else:
            match = _RE_STATUS_LINE.match(head)
        if match is None:
            self.dead = True  # not HTTP/1.x on this connection
            self._release()
            return False
        headers = _parse_headers(head[match.end() : header_end].rstrip(b"\r\n"))
        if headers is None:
            self.dead = True
            self._release()
            return False

        message = _HttpMessage(self.is_request_side, ts)
        message.gap = self.gap
        message.truncated = self.truncated
        message.headers = headers
        self.msg = message
        if self.is_request_side:
            message.method = match.group(1).decode("latin-1")
            message.uri = match.group(2).decode("latin-1")
            message.protocol = match.group(3).decode("latin-1")
        else:
            message.protocol = match.group(1).decode("latin-1")
            message.status = int(match.group(2))
            message.reason = (match.group(3) or b"").decode("latin-1").strip()
        message.content_encoding = headers.get("content-encoding", "")
        # The record carrying the start line — the row's byte_offset. Not
        # necessarily the lowest contributing offset: an out-of-order segment
        # from later in the message can sit earlier in the file.
        for record in self.records:
            if record[0] <= self.base < record[1]:
                message.anchor_offset = record[2]
                break

        self._take(header_end)
        self._start_body(message, headers, peer_method)
        return True

    def _start_body(self, message: _HttpMessage, headers: dict[str, str], peer_method: str) -> None:
        self.body = bytearray()
        transfer_encoding = headers.get("transfer-encoding", "").lower()
        bodyless = not self.is_request_side and (
            message.status < 200 or message.status in (204, 304) or peer_method.upper() == "HEAD"
        )
        if bodyless:
            self.body_mode = "none"
            self.phase = "body"
            self.need = 0
        elif "chunked" in transfer_encoding:
            self.body_mode = "chunked"
            self.phase = "chunk_size"
        elif "content-length" in headers:
            try:
                length = int(headers["content-length"].split(",")[0].strip())
            except ValueError:
                length = -1
            if length < 0 or length > _REASM_MAX_BODY_BYTES:
                self.dead = True
                self._release()
                return
            self.body_mode = "length"
            self.need = length
            self.phase = "body"
        elif self.is_request_side:
            self.body_mode = "none"  # a request with neither framing header has no body
            self.need = 0
            self.phase = "body"
        else:
            self.body_mode = "until_close"
            self.phase = "body"

    def _advance_body(self, at_close: bool) -> _HttpMessage | None:
        message = self.msg
        if message is None:
            self.phase = "start"
            return None

        if self.body_mode in ("none", "length"):
            take = min(self.need, len(self.data))
            if take:
                self.body += self._take(take)
                self.need -= take
            if self.need and not at_close:
                return None
            if self.need and at_close:
                message.complete = False
                return self._finish_message(partial=True)
            return self._finish_message(partial=False)

        if self.body_mode == "until_close":
            if self.data:
                self.body += self._take(len(self.data))
            if not at_close:
                return None
            return self._finish_message(partial=False)

        return self._advance_chunked(at_close)

    def _advance_chunked(self, at_close: bool) -> _HttpMessage | None:
        while True:
            if self.phase == "chunk_size":
                idx = self.data.find(b"\n")
                if idx < 0:
                    if len(self.data) > _REASM_MAX_HEADER_BYTES:
                        self.dead = True
                        self._release()
                    break
                line = bytes(self._take(idx + 1)).strip()
                size_token = line.split(b";", 1)[0].strip()
                try:
                    self.chunk_left = int(size_token, 16)
                except ValueError:
                    self.dead = True
                    self._release()
                    break
                if self.chunk_left < 0 or len(self.body) + self.chunk_left > _REASM_MAX_BODY_BYTES:
                    self.dead = True
                    self._release()
                    break
                self.phase = "trailer" if self.chunk_left == 0 else "chunk_data"
            elif self.phase == "chunk_data":
                take = min(self.chunk_left, len(self.data))
                if take:
                    self.body += self._take(take)
                    self.chunk_left -= take
                if self.chunk_left:
                    break
                self.phase = "chunk_crlf"
            elif self.phase == "chunk_crlf":
                if len(self.data) < 2:
                    if len(self.data) == 1 and self.data[:1] == b"\n":
                        self._take(1)
                        self.phase = "chunk_size"
                        continue
                    break
                if self.data[:2] == b"\r\n":
                    self._take(2)
                elif self.data[:1] == b"\n":
                    self._take(1)
                self.phase = "chunk_size"
            else:  # trailer: header lines, then a blank line
                end = self._find_header_end()
                if end is None:
                    if self.data[:2] == b"\r\n":
                        self._take(2)
                        return self._finish_message(partial=False)
                    if self.data[:1] == b"\n":
                        self._take(1)
                        return self._finish_message(partial=False)
                    if len(self.data) > _REASM_MAX_HEADER_BYTES:
                        self.dead = True
                        self._release()
                    break
                self._take(end)
                return self._finish_message(partial=False)
        if at_close and self.msg is not None:
            return self._finish_message(partial=True)
        return None

    def _finish_message(self, partial: bool) -> _HttpMessage | None:
        message = self.msg
        if message is None:
            return None
        message.body_bytes = len(self.body)
        message.complete = not partial
        message.end_ts = max(self.last_ts, message.start_ts)
        message.gap = message.gap or self.gap
        message.truncated = message.truncated or self.truncated
        if message.content_encoding and self.body:
            message.decoded_body_bytes = _decoded_length(bytes(self.body), message.content_encoding)
        self.msg = None
        self.body = bytearray()
        self.phase = "start"
        self.body_mode = "none"
        self.need = 0
        return message


class _HttpFlow:
    """Both directions of one TCP connection, plus the request/response pairing."""

    __slots__ = (
        "client_endpoint",
        "server_endpoint",
        "client",
        "server",
        "requests",
        "last_ts",
        "index",
        "dead",
    )

    def __init__(self) -> None:
        self.client_endpoint: tuple[str, int] | None = None
        self.server_endpoint: tuple[str, int] | None = None
        self.client = _HttpDirection(True)
        self.server = _HttpDirection(False)
        self.requests: collections.deque[_HttpMessage] = collections.deque()
        self.last_ts = 0
        self.index = 0
        self.dead = False


class _HttpReassembler:
    """Feeds TCP packets in, yields ``network:http:transaction`` rows out.

    Rows are produced when a transaction *completes*, so they interleave with
    packet rows rather than following file order — harmless, the server sorts
    on query.
    """

    def __init__(self) -> None:
        self.flows: collections.OrderedDict[tuple, _HttpFlow] = collections.OrderedDict()
        self.emitted = 0
        self.incomplete = 0
        self.evicted = 0
        self.dropped_flows = 0
        self._seen = 0

    # -- packet intake -----------------------------------------------------

    def handle(
        self,
        attrs: dict[str, Any],
        payload: bytes,
        record_offset: int,
        record_bytes: bytes,
        ts_us: int,
    ) -> list[tuple[int, bytes, dict[str, Any]]]:
        src = (attrs.get("src_ip", ""), int(attrs.get("src_port") or 0))
        dst = (attrs.get("dst_ip", ""), int(attrs.get("dst_port") or 0))
        if not src[1] or not dst[1]:
            return []
        key = (src, dst) if src <= dst else (dst, src)

        self._seen += 1
        rows: list[tuple[int, bytes, dict[str, Any]]] = []
        if self._seen % _REASM_SWEEP_EVERY == 0:
            rows.extend(self._sweep_idle(ts_us))

        flow = self.flows.get(key)
        if flow is None:
            flow = _HttpFlow()
            self.flows[key] = flow
            while len(self.flows) > _REASM_MAX_FLOWS:
                _, victim = self.flows.popitem(last=False)
                self.evicted += 1
                rows.extend(self._finalize(victim))
        else:
            self.flows.move_to_end(key)
        flow.last_ts = ts_us

        if flow.dead:
            return rows

        flags = attrs.get("tcp_flags", "")
        direction = self._direction_for(flow, src, dst, payload, flags)
        if direction is not None:
            if attrs.get("captured_length", 0) < attrs.get("packet_length", 0):
                direction.truncated = True
            sequence = int(attrs.get("tcp_sequence") or 0)
            direction.add(sequence, "SYN" in flags, payload, record_offset, record_bytes, ts_us)
            rows.extend(self._pump(flow, ts_us, at_close=False))

        if "RST" in flags:
            rows.extend(self._finalize(flow))
            self.flows.pop(key, None)
        elif "FIN" in flags:
            if direction is not None:
                direction.closed = True
            if flow.client.closed and flow.server.closed:
                rows.extend(self._finalize(flow))
                self.flows.pop(key, None)
        if flow.client.dead and flow.server.dead and key in self.flows:
            rows.extend(self._finalize(flow))
            self.flows.pop(key, None)
        return rows

    def _direction_for(
        self,
        flow: _HttpFlow,
        src: tuple[str, int],
        dst: tuple[str, int],
        payload: bytes,
        flags: str,
    ) -> _HttpDirection | None:
        if flow.client_endpoint is None:
            # Assign roles from the first evidence available, in decreasing
            # order of reliability: handshake, wire content, then well-known port.
            # ``flags`` is the concatenated flag names ("PSHACK"), so match whole
            # names — "S" alone also hits PSH and RST.
            syn, ack = "SYN" in flags, "ACK" in flags
            src_is_client = True  # last resort: whoever spoke first
            for verdict, evidence in (
                (True, syn and not ack),
                (False, syn and ack),
                (False, payload[:5] == b"HTTP/"),
                (True, _RE_REQUEST_LINE.match(payload[:1024]) is not None),
                (True, dst[1] in _REASM_HTTP_PORTS),
                (False, src[1] in _REASM_HTTP_PORTS),
            ):
                if evidence:
                    src_is_client = verdict
                    break
            client = src if src_is_client else dst
            flow.client_endpoint = client
            flow.server_endpoint = dst if client == src else src
        if src == flow.client_endpoint:
            return flow.client
        if src == flow.server_endpoint:
            return flow.server
        return None

    def _sweep_idle(self, now_us: int) -> list[tuple[int, bytes, dict[str, Any]]]:
        rows: list[tuple[int, bytes, dict[str, Any]]] = []
        cutoff = now_us - _REASM_IDLE_SECONDS * 1_000_000
        while self.flows:
            key, flow = next(iter(self.flows.items()))
            if flow.last_ts >= cutoff:
                break
            self.flows.pop(key)
            self.evicted += 1
            rows.extend(self._finalize(flow))
        return rows

    # -- pairing / emission ------------------------------------------------

    def _pump(
        self, flow: _HttpFlow, ts: int, at_close: bool
    ) -> list[tuple[int, bytes, dict[str, Any]]]:
        rows: list[tuple[int, bytes, dict[str, Any]]] = []
        for request in flow.client.messages(ts, "", at_close):
            flow.requests.append(request)
        peer_method = flow.requests[0].method if flow.requests else ""
        for response in flow.server.messages(ts, peer_method, at_close):
            if 100 <= response.status < 200 and response.status != 101:
                continue  # interim (100-continue): the real response still follows
            request = flow.requests.popleft() if flow.requests else None
            rows.append(self._transaction(flow, request, response))
            if response.status == 101 or (
                request is not None
                and request.method.upper() == "CONNECT"
                and response.status < 300
            ):
                # Upgraded/tunneled: the rest of the connection is not HTTP/1.x.
                flow.client.dead = flow.server.dead = True
                flow.client._release()
                flow.server._release()
                break
            peer_method = flow.requests[0].method if flow.requests else ""
        return rows

    def _finalize(self, flow: _HttpFlow) -> list[tuple[int, bytes, dict[str, Any]]]:
        if flow.dead:
            return []
        flow.dead = True
        rows = self._pump(flow, flow.last_ts, at_close=True)
        pending_request = flow.client.msg
        if pending_request is not None:
            flow.client._finish_message(partial=True)
            flow.requests.append(pending_request)
        for request in flow.requests:
            rows.append(self._transaction(flow, request, None))
        flow.requests.clear()
        flow.client._release()
        flow.server._release()
        return rows

    def _transaction(
        self, flow: _HttpFlow, request: _HttpMessage | None, response: _HttpMessage | None
    ) -> tuple[int, bytes, dict[str, Any]]:
        flow.index += 1
        client_ip, client_port = flow.client_endpoint or ("", 0)
        server_ip, server_port = flow.server_endpoint or ("", 0)

        attrs: dict[str, Any] = {
            "protocol": "tcp",
            "src_ip": client_ip,
            "src_port": client_port,
            "dst_ip": server_ip,
            "dst_port": server_port,
            "http_transaction_index": flow.index,
        }

        records: dict[int, bytes] = {}
        if request is not None:
            records.update(request.records)
            attrs["http_method"] = request.method
            attrs["http_uri"] = request.uri
            attrs["http_protocol"] = request.protocol
            attrs["http_request_full"] = f"{request.method} {request.uri} {request.protocol}"
            attrs["http_request_bytes"] = request.wire_bytes
            if request.body_bytes:
                attrs["http_request_body_bytes"] = request.body_bytes
        else:
            attrs["http_request_missing"] = "true"

        if response is not None:
            records.update(response.records)
            attrs["status_code"] = response.status
            if response.reason:
                attrs["http_status_reason"] = response.reason
            if not request:
                attrs["http_protocol"] = response.protocol
            attrs["http_response_bytes"] = response.wire_bytes
            attrs["http_response_body_bytes"] = response.body_bytes
            if response.content_encoding:
                attrs["http_content_encoding"] = response.content_encoding
                if response.decoded_body_bytes >= 0:
                    attrs["http_response_body_decoded_bytes"] = response.decoded_body_bytes
                else:
                    attrs["http_response_body_decode_failed"] = "true"
        else:
            attrs["http_response_missing"] = "true"

        source = request or response
        if source is None:  # never called with neither; keeps the row well-formed if it is
            source = _HttpMessage(True, flow.last_ts)
        incomplete = (
            request is None or response is None or not request.complete or not response.complete
        )
        if incomplete:
            attrs["http_incomplete"] = "true"
            self.incomplete += 1
        if (request and request.gap) or (response and response.gap):
            attrs["reassembly_gap"] = "true"
        if (request and request.truncated) or (response and response.truncated):
            attrs["reassembly_truncated_capture"] = "true"

        offsets = sorted(records)
        content_bytes = b"".join(records[offset] for offset in offsets)
        anchor = source.anchor_offset
        if anchor < 0:
            anchor = offsets[0] if offsets else 0
        byte_offset = anchor
        attrs["reassembled"] = "true"
        attrs["byte_offset_basis"] = "request_line_record"
        attrs["content_hash_basis"] = "reassembled_records"
        attrs["packet_count"] = len(offsets)
        listed = offsets[:_REASM_MAX_LISTED_OFFSETS]
        attrs["packet_offsets"] = ",".join(str(offset) for offset in listed)
        if len(offsets) > len(listed):
            attrs["packet_offsets_truncated"] = "true"

        end_ts = (response or source).end_ts
        if end_ts > source.start_ts:
            attrs["duration_ms"] = (end_ts - source.start_ts) // 1000

        self.emitted += 1
        row = {
            "message": _build_transaction_message(attrs),
            "timestamp": _ts_to_dt(source.start_ts),
            "timestamp_desc": "HTTP Request Time",
            "artifact": "network:http:transaction",
            "artifact_long": "web:access:request",
            "attributes": attrs,
        }
        return byte_offset, content_bytes, row

    def flush(self) -> list[tuple[int, bytes, dict[str, Any]]]:
        rows: list[tuple[int, bytes, dict[str, Any]]] = []
        for flow in list(self.flows.values()):
            rows.extend(self._finalize(flow))
        self.flows.clear()
        return rows


def _build_transaction_message(attrs: dict[str, Any]) -> str:
    request = attrs.get("http_request_full") or "(request not captured)"
    status = attrs.get("status_code")
    outcome = str(status) if status else "no response"
    return (
        f"HTTP {_addr(attrs.get('src_ip', ''), attrs.get('src_port'))} -> "
        f"{_addr(attrs.get('dst_ip', ''), attrs.get('dst_port'))} "
        f'"{request}" {outcome}'
    )


# ---------------------------------------------------------------------------
# Row batching / Parquet writing
# ---------------------------------------------------------------------------

BATCH_ROWS = 50_000
# Default cap on parallel workers; each worker buffers one whole capture's
# decoded rows as Arrow IPC, so high core counts multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("PCAP2TS_DEFAULT_WORKERS", 4))


class _BatchBuffer:
    """Columnar row buffer flushed to a ParquetWriter as record batches."""

    def __init__(self, writer: pq.ParquetWriter) -> None:
        self._writer = writer
        self._columns: dict[str, list[Any]] = {name: [] for name in PARQUET_EVENT_SCHEMA.names}
        self.rows_written = 0

    def append(
        self,
        source_file: str,
        file_hash: str,
        byte_offset: int,
        content_bytes: bytes,
        row: dict[str, Any],
    ) -> None:
        cols = self._columns
        cols["source_file"].append(source_file)
        cols["file_hash"].append(file_hash)
        cols["byte_offset"].append(byte_offset)
        cols["content_hash"].append(hashlib.sha256(content_bytes).hexdigest())
        cols["message"].append(row["message"])
        cols["timestamp"].append(row["timestamp"])
        cols["timestamp_desc"].append(row["timestamp_desc"])
        cols["artifact"].append(row["artifact"])
        cols["artifact_long"].append(row["artifact_long"])
        cols["display_name"].append("")
        cols["tags"].append([])
        cols["attributes"].append(
            {k: str(v) for k, v in row["attributes"].items() if v is not None and str(v) != ""}
        )
        if len(cols["source_file"]) >= BATCH_ROWS:
            self.flush()

    def write_batch(self, batch: pa.RecordBatch) -> None:
        self._writer.write_batch(batch)
        self.rows_written += batch.num_rows

    def flush(self) -> None:
        if not self._columns["source_file"]:
            return
        batch = pa.RecordBatch.from_pydict(self._columns, schema=PARQUET_EVENT_SCHEMA)
        self.write_batch(batch)
        self._columns = {name: [] for name in PARQUET_EVENT_SCHEMA.names}


def _parse_since_until(value: str | None) -> datetime.datetime | None:
    """Parse an ISO 8601 ``--since``/``--until`` value to a UTC-aware datetime."""
    if not value:
        return None
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _convert_file(
    path: Path,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
    reassemble: str | None = None,
) -> tuple[int, int, int, int]:
    """Parse one capture file into the buffer.

    Returns ``(parsed, skipped, skipped_by_time, transactions)``. Never raises
    for corrupt/truncated data at the file level — it is reported to stderr
    instead, so one bad file doesn't abort the run.
    """
    parsed = 0
    skipped = 0
    skipped_by_time = 0
    transactions = 0
    reassembler = _HttpReassembler() if reassemble == "http" else None

    def emit(rows: list[tuple[int, bytes, dict[str, Any]]]) -> int:
        for offset, content_bytes, row in rows:
            buffer.append(source_file, file_hash, offset, content_bytes, row)
        return len(rows)

    try:
        fh = open(path, "rb")  # noqa: SIM115 — closed in the finally below; only the open is OSError-guarded
    except OSError as exc:
        sys.stderr.write(f"warning: cannot open {path}: {exc}\n")
        return parsed, skipped, skipped_by_time

    try:
        magic = fh.read(4)
        if magic in (_MAGIC_US_BE, _MAGIC_US_LE, _MAGIC_NS_BE, _MAGIC_NS_LE):
            byte_order = ">" if magic in (_MAGIC_US_BE, _MAGIC_NS_BE) else "<"
            nanosecond = magic in (_MAGIC_NS_BE, _MAGIC_NS_LE)
            header_rest = fh.read(20)
            if len(header_rest) < 20:
                sys.stderr.write(f"warning: truncated pcap global header, skipping: {path}\n")
                return parsed, skipped, skipped_by_time
            _, _, _, _, _, network = struct.unpack(byte_order + "HHiIII", header_rest)
            link_type = _LINK_TYPE_NAMES.get(network)
            packet_source = _iter_pcap_classic(fh, byte_order, nanosecond, link_type)
        elif magic == _PCAPNG_MAGIC:
            fh.seek(0)
            packet_source = _iter_pcap_ng(fh)
        else:
            sys.stderr.write(f"warning: unrecognized capture format, skipping: {path}\n")
            return parsed, skipped, skipped_by_time

        for (
            ts_us,
            link_type_name,
            interface,
            captured_length,
            packet_length,
            raw_data,
            record_offset,
            record_bytes,
        ) in packet_source:
            if link_type_name is None:
                skipped += 1
                continue
            try:
                row = build_row(
                    ts_us,
                    link_type_name,
                    interface,
                    captured_length,
                    packet_length,
                    raw_data,
                    want_tcp_payload=reassembler is not None,
                )
            except _MalformedPacket:
                skipped += 1
                continue
            ts = row["timestamp"]
            if ts is not None:
                if since_dt is not None and ts < since_dt:
                    skipped_by_time += 1
                    continue
                if until_dt is not None and ts > until_dt:
                    skipped_by_time += 1
                    continue
            # ts is None (unparseable/missing) → keep, matching upstream behavior.
            buffer.append(source_file, file_hash, record_offset, record_bytes, row)
            parsed += 1
            if reassembler is not None and row["attributes"].get("protocol") == "tcp":
                # Fed only packets that survived --since/--until, so a filtered
                # capture reassembles exactly the packet rows it emitted.
                transactions += emit(
                    reassembler.handle(
                        row["attributes"],
                        row.get("tcp_payload", b""),
                        record_offset,
                        record_bytes,
                        ts_us,
                    )
                )

    except (struct.error, PcapParseError) as exc:
        sys.stderr.write(
            f"warning: corrupt or truncated capture, stopping at the failure point: {path} ({exc})\n"
        )
    finally:
        fh.close()

    if reassembler is not None:
        # Connections still open at end of file: their in-flight request is
        # emitted as incomplete rather than dropped.
        transactions += emit(reassembler.flush())

    return parsed, skipped, skipped_by_time, transactions


def _parse_file_worker(
    path_str: str,
    file_hash: str,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
    reassemble: str | None = None,
) -> tuple[bytes, int, int, int, int]:
    """Worker: parse one capture file, return Arrow IPC bytes + counts."""
    sink = io.BytesIO()
    writer_ipc = pa.ipc.new_stream(sink, PARQUET_EVENT_SCHEMA)

    class _IpcBuffer(_BatchBuffer):
        def __init__(self) -> None:
            self._columns = {name: [] for name in PARQUET_EVENT_SCHEMA.names}
            self.rows_written = 0

        def write_batch(self, batch: pa.RecordBatch) -> None:
            writer_ipc.write_batch(batch)
            self.rows_written += batch.num_rows

    path = Path(path_str)
    buffer = _IpcBuffer()
    parsed, skipped, skipped_by_time, transactions = _convert_file(
        path,
        path.name,
        file_hash,
        buffer,
        since_dt=since_dt,
        until_dt=until_dt,
        reassemble=reassemble,
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped, skipped_by_time, transactions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _available_ram_bytes() -> int | None:
    """Best-effort available RAM in bytes (Linux MemAvailable, else total)."""
    try:
        with open("/proc/meminfo", "rb") as fh:
            for raw in fh:
                line = raw.decode("ascii", errors="replace")
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, AttributeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Output splitting (ported from the 2timesketch converter suite's --split)
# ---------------------------------------------------------------------------

_RE_SPLIT_SIZE = re.compile(r"^(\d+)\s*([KMG])(?:I?B)?$", re.IGNORECASE)

# Upper bound on the row-batch granularity when rotating parts by size; the
# actual batch is scaled down for small size limits (see split_parquet).
_SPLIT_SIZE_BATCH_ROWS = 8192


def parse_split_spec(value: str) -> tuple[str, int]:
    """Parse a ``--split`` specification.

    Returns ``("parts", n)`` for a bare integer (split into ``n`` parts with
    an equal number of rows) or ``("size", nbytes)`` for a size specification
    such as ``"512K"``, ``"4M"``, or ``"1GiB"`` (suffixes are KiB/MiB/GiB,
    i.e. 1024-based).
    """
    text = value.strip()
    if text.isdigit():
        n = int(text)
        if n < 1:
            raise SystemExit(
                f"error: invalid --split value {value!r}: number of parts must be at least 1"
            )
        return ("parts", n)
    m = _RE_SPLIT_SIZE.match(text)
    if m:
        amount = int(m.group(1))
        if amount < 1:
            raise SystemExit(f"error: invalid --split value {value!r}: size must be at least 1")
        factor = {"K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]
        return ("size", amount * factor)
    raise SystemExit(
        f"error: invalid --split value {value!r}: use N (number of parts) or "
        "NK/NM/NG (part size in KiB/MiB/GiB, e.g. 4M)"
    )


def _part_path(output: str, index: int) -> Path:
    """Return the output path for part ``index`` (1-based)."""
    out = Path(output)
    return out.with_name(f"{out.stem}.part{index:03d}{out.suffix}")


def split_parquet(src: Path, output: str, spec: tuple[str, int], verbose: bool) -> list[Path]:
    """Repartition the single-file conversion at ``src`` into part files.

    ``("parts", n)`` distributes the rows into at most ``n`` parts of
    ``ceil(total / n)`` rows each; ``("size", nbytes)`` rotates to a new part
    once the current one reaches ``nbytes``, checked at row-batch granularity
    so a part may overshoot by up to one batch. Rows keep their original
    order, are never duplicated across parts, and every part carries the full
    interchange schema and provenance metadata, so each part is independently
    ingestible.
    """
    mode, amount = spec
    pf = pq.ParquetFile(src)
    schema = pf.schema_arrow
    # Carry footer key-value metadata added after the write loop (e.g.
    # vestigo.row_counts via add_key_value_metadata) into every part, so each
    # part keeps the full provenance. schema_arrow.metadata only holds keys set
    # at writer-open time; the rest live in the file's FileMetaData KV.
    extra = {
        k: v
        for k, v in (pf.metadata.metadata or {}).items()
        if k != b"ARROW:schema" and k not in (schema.metadata or {})
    }
    if extra:
        merged = dict(schema.metadata or {})
        merged.update(extra)
        schema = schema.with_metadata(merged)
    total = pf.metadata.num_rows
    if mode == "parts":
        rows_per_part = -(-total // amount) if total else 0
        batch_rows = max(1, min(BATCH_ROWS, rows_per_part or 1))
    else:
        rows_per_part = 0
        # The batch is the rotation granularity (a part may overshoot the
        # limit by up to one batch), so scale it to the limit; the 128 B/row
        # divisor keeps the overshoot small even for well-compressing rows.
        batch_rows = max(64, min(_SPLIT_SIZE_BATCH_ROWS, amount // 128))

    parts: list[Path] = []
    writer: pq.ParquetWriter | None = None
    part_rows = 0

    def open_next() -> pq.ParquetWriter:
        nonlocal writer, part_rows
        if writer is not None:
            writer.close()
        path = _part_path(output, len(parts) + 1)
        parts.append(path)
        part_rows = 0
        writer = pq.ParquetWriter(str(path), schema, compression="zstd")
        return writer

    try:
        for batch in pf.iter_batches(batch_size=batch_rows):
            while batch.num_rows:
                if (
                    writer is None
                    or (mode == "parts" and part_rows >= rows_per_part)
                    or (mode == "size" and part_rows > 0 and parts[-1].stat().st_size >= amount)
                ):
                    open_next()
                take = batch.num_rows
                if mode == "parts":
                    take = min(take, rows_per_part - part_rows)
                writer.write_batch(batch.slice(0, take))
                part_rows += take
                batch = batch.slice(take)
        if writer is None:
            # Zero rows: still produce a first (empty, schema-only) part.
            open_next()
    finally:
        if writer is not None:
            writer.close()
    if verbose:
        for path in parts:
            sys.stderr.write(f"  wrote {path}\n")
    return parts


def convert(
    input_path: str,
    output: str,
    workers: int,
    verbose: bool,
    split: str | None = None,
    since: str | None = None,
    until: str | None = None,
    reassemble: str | None = None,
) -> int:
    """Convert pcap/pcapng captures at ``input_path`` into ``output`` (.parquet)."""
    import json

    if reassemble not in (None, "http"):
        raise SystemExit(f"error: unsupported --reassemble protocol: {reassemble}")

    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "Vestigo server detects the ingest parser strictly by file extension."
        )

    since_dt = _parse_since_until(since)
    until_dt = _parse_since_until(until)

    split_spec = parse_split_spec(split) if split else None
    write_target = output if split_spec is None else f"{output}.tmp"

    files = find_pcap_files(input_path)

    if verbose:
        sys.stderr.write(f"hashing {len(files)} input file(s)...\n")
    provenance = []
    hashes: dict[Path, str] = {}
    for path in files:
        digest, size = hash_file(path)
        hashes[path] = digest
        stat = path.stat()
        provenance.append(
            {
                "name": path.name,
                "sha256": digest,
                "size_bytes": size,
                "path": str(path.resolve()),
                "mtime": datetime.datetime.fromtimestamp(stat.st_mtime, datetime.UTC).isoformat(),
            }
        )

    metadata = {
        META_FORMAT_VERSION: FORMAT_VERSION,
        META_CONVERTER_NAME: CONVERTER_NAME,
        META_CONVERTER_VERSION: CONVERTER_VERSION,
        META_ORIGINAL_FILES: json.dumps(provenance, sort_keys=True),
        META_CONVERTED_AT: datetime.datetime.now(datetime.UTC).isoformat(),
        META_TIMEZONE_ASSUMPTION: "packet capture timestamps are UTC (epoch-derived)",
        # ``reassemble`` changes which rows exist, not only which are kept:
        # with it, derived network:http:transaction rows join the packet rows.
        META_PARSE_DECISIONS: json.dumps(
            {"since": since, "until": until, "reassemble": reassemble}, sort_keys=True
        ),
    }

    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
    transactions_total = 0
    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(write_target, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)

        if workers > 1 and len(files) > 1:
            if verbose:
                sys.stderr.write(f"parsing {len(files)} file(s) across {workers} workers...\n")
            ram = _available_ram_bytes()
            largest = max(path.stat().st_size for path in files)
            # Rough per-worker estimate: decoded packet rows + Arrow IPC copy.
            estimated = min(workers, len(files)) * largest * 3
            if ram and estimated > ram * 0.75:
                sys.stderr.write(
                    f"warning: {workers} workers on captures up to "
                    f"{largest // (1024 * 1024)} MiB may need "
                    f"~{estimated // (1024 * 1024)} MiB RAM; "
                    f"~{ram // (1024 * 1024)} MiB available. Reduce -w if memory runs out.\n"
                )
            ctx = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers, mp_context=ctx
            ) as pool:
                # Submit a bounded window and consume strictly in submit order:
                # rows land in the output in input-file order (forensic
                # requirement), and at most ~2*workers file results exist in
                # the parent at once, so finished-but-unwritten Arrow IPC
                # results cannot pile up and OOM the parent.
                file_iter = iter(files)
                pending: collections.deque = collections.deque()

                def _submit_next() -> None:
                    for path in file_iter:
                        pending.append(
                            pool.submit(
                                _parse_file_worker,
                                str(path),
                                hashes[path],
                                since_dt,
                                until_dt,
                                reassemble,
                            )
                        )
                        return

                for _ in range(workers * 2):
                    _submit_next()
                while pending:
                    (
                        ipc_bytes,
                        parsed,
                        skipped,
                        skipped_by_time,
                        transactions,
                    ) = pending.popleft().result()
                    _submit_next()
                    parsed_total += parsed
                    skipped_total += skipped
                    skipped_by_time_total += skipped_by_time
                    transactions_total += transactions
                    reader = pa.ipc.open_stream(ipc_bytes)
                    for batch in reader:
                        if batch.num_rows:
                            buffer.write_batch(batch)
        else:
            for path in files:
                if verbose:
                    sys.stderr.write(f"parsing {path}...\n")
                parsed, skipped, skipped_by_time, transactions = _convert_file(
                    path,
                    path.name,
                    hashes[path],
                    buffer,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    reassemble=reassemble,
                )
                parsed_total += parsed
                skipped_total += skipped
                skipped_by_time_total += skipped_by_time
                transactions_total += transactions

        buffer.flush()
        writer.add_key_value_metadata(
            {
                META_ROW_COUNTS: json.dumps(
                    {
                        "parsed": parsed_total + transactions_total,
                        "packets": parsed_total,
                        "http_transactions": transactions_total,
                        "skipped_malformed": skipped_total,
                        "skipped_by_time": skipped_by_time_total,
                    }
                )
            }
        )

    time_note = f", {skipped_by_time_total} outside --since/--until" if (since or until) else ""
    if reassemble:
        time_note += f", {transactions_total} HTTP transaction row(s)"
    parsed_total += transactions_total
    if split_spec is not None:
        try:
            parts = split_parquet(Path(write_target), output, split_spec, verbose)
        finally:
            Path(write_target).unlink(missing_ok=True)
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {len(parts)} part "
            f"file(s) [{parts[0].name} .. {parts[-1].name}] "
            f"({skipped_total} packets skipped{time_note})\n"
        )
    else:
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
            f"({skipped_total} packets skipped{time_note})\n"
        )
    return 0 if parsed_total > 0 else 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Convert pcap/pcapng packet captures (file or directory) to a "
            "Vestigo Parquet file for direct upload."
        )
    )
    parser.add_argument(
        "-i", "--input", required=True, help="pcap/pcapng file or directory to search recursively"
    )
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=min(getattr(os, "process_cpu_count", os.cpu_count)() or 4, DEFAULT_MAX_WORKERS),
        help="parallel parser processes across input files (default: min(CPU count, %(default)s))",
    )
    parser.add_argument(
        "--split",
        metavar="N|SIZE",
        help="split the output into multiple .parquet files: N = N parts with "
        "an equal number of rows (e.g. 4); SIZE = rotate to a new part once "
        "it reaches SIZE, with a K/M/G suffix meaning KiB/MiB/GiB (e.g. "
        "512M). Parts are named <name>.partNNN.parquet.",
    )
    parser.add_argument(
        "--since",
        help="Only packets at or after this ISO 8601 timestamp "
        "(e.g. 2026-07-01T00:00:00Z). Packets with no parseable timestamp are kept.",
    )
    parser.add_argument(
        "--until",
        help="Only packets at or before this ISO 8601 timestamp "
        "(e.g. 2026-07-01T23:59:59Z). Packets with no parseable timestamp are kept.",
    )
    parser.add_argument(
        "--reassemble",
        choices=("http",),
        metavar="http",
        help="also emit one derived network:http:transaction row per HTTP/1.x "
        "request/response, reassembled from the TCP streams. Packet rows are "
        "still emitted in full — transaction rows are added, never substituted. "
        "Limits: cleartext HTTP/1.0 and 1.1 only. HTTPS is not decrypted (so "
        "most real traffic yields nothing), HTTP/2 and HTTP/3 are not parsed "
        "(binary framing + HPACK/QPACK), and a capture taken with a snaplen "
        "shorter than the frame or recording only one direction cannot be "
        "reassembled — such transactions come out flagged incomplete or not at "
        "all. A reassembled row's content_hash covers the concatenated "
        "contributing packet records, not a contiguous span on disk.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(
        args.input,
        args.output,
        max(1, args.workers),
        args.verbose,
        split=args.split,
        since=args.since,
        until=args.until,
        reassemble=args.reassemble,
    )


if __name__ == "__main__":
    sys.exit(main())
