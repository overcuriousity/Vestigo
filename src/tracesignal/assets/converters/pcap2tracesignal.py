#!/usr/bin/env python3
"""Convert packet captures (pcap/pcapng) to a TraceSignal Parquet file.

Parses raw network captures produced by Wireshark/tcpdump — both the classic
libpcap format and the newer block-based pcapng format — locally and writes
one ``.parquet`` file in the TraceSignal interchange format (version 1).
Upload the result to the TraceSignal web interface or ingest it with
``tsig ingest`` — no CSV/JSONL intermediate, no server re-parse.

One row is emitted per packet, decoded down to the Ethernet/Linux-SLL/raw-IP,
IPv4/IPv6, and TCP/UDP/ICMP/ARP headers. No TCP stream reassembly and no
multi-packet application-layer decoding is performed.

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

No gzip support: raw captures only (matches the vendored converter).

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow pcap2tracesignal.py ...

Usage:

    python pcap2tracesignal.py -i capture.pcap -o capture.parquet
    python pcap2tracesignal.py -i /var/captures/ -o captures.parquet -w 8
"""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import io
import ipaddress
import multiprocessing
import os
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO, Iterator

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "error: pyarrow is required to write TraceSignal Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow pcap2tracesignal.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "pcap2tracesignal"
CONVERTER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# TraceSignal Parquet interchange format v1 — embedded copy of the spec in
# src/tracesignal/ingestion/parquet_format.py (this script is a standalone
# download and cannot import it; the repo test suite asserts both stay equal).
# ---------------------------------------------------------------------------

FORMAT_VERSION = "1"
META_FORMAT_VERSION = "tracesignal.format_version"
META_CONVERTER_NAME = "tracesignal.converter_name"
META_CONVERTER_VERSION = "tracesignal.converter_version"
META_ORIGINAL_FILES = "tracesignal.original_files"

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
            if candidate.suffix.lower() in _PCAP_EXTENSIONS:
                files.add(candidate)
            elif not candidate.suffix and _looks_like_capture(candidate):
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
                f"pcapng block length {block_total_length} exceeds the "
                f"{_MAX_RECORD_BYTES}-byte cap"
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
    return datetime.datetime.fromtimestamp(ts_us / 1_000_000, tz=datetime.timezone.utc)


def build_row(
    ts_us: int,
    link_type: str,
    interface: str,
    captured_length: int,
    packet_length: int,
    raw_bytes: bytes,
) -> dict[str, Any]:
    """Decode one packet into an event row dict.

    Raises ``_MalformedPacket`` when the L2/L3 headers cannot be decoded.
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
            l4 = _decode_l4(protocol, ip["payload"])
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
        l4 = _decode_l4(protocol, ip["payload"])
        if l4:
            attrs.update(l4)
    else:
        attrs["ethertype"] = f"0x{ethertype:04x}"

    attrs["protocol"] = protocol
    attrs["src_ip"] = normalize_ip(src_ip)
    attrs["dst_ip"] = normalize_ip(dst_ip)

    return {
        "message": _build_message(attrs),
        "timestamp": _ts_to_dt(ts_us),
        "timestamp_desc": "Packet Capture Time",
        "artifact": _artifact_for(protocol),
        "artifact_long": "network:packet:capture",
        "attributes": attrs,
    }


# ---------------------------------------------------------------------------
# Row batching / Parquet writing
# ---------------------------------------------------------------------------

BATCH_ROWS = 50_000


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


def _convert_file(path: Path, source_file: str, file_hash: str, buffer: _BatchBuffer) -> tuple[int, int]:
    """Parse one capture file into the buffer. Returns ``(parsed, skipped)``.

    Never raises for corrupt/truncated data at the file level — it is
    reported to stderr instead, so one bad file doesn't abort the run.
    """
    parsed = 0
    skipped = 0

    try:
        fh = open(path, "rb")
    except OSError as exc:
        sys.stderr.write(f"warning: cannot open {path}: {exc}\n")
        return parsed, skipped

    try:
        magic = fh.read(4)
        if magic in (_MAGIC_US_BE, _MAGIC_US_LE, _MAGIC_NS_BE, _MAGIC_NS_LE):
            byte_order = ">" if magic in (_MAGIC_US_BE, _MAGIC_NS_BE) else "<"
            nanosecond = magic in (_MAGIC_NS_BE, _MAGIC_NS_LE)
            header_rest = fh.read(20)
            if len(header_rest) < 20:
                sys.stderr.write(f"warning: truncated pcap global header, skipping: {path}\n")
                return parsed, skipped
            _, _, _, _, _, network = struct.unpack(byte_order + "HHiIII", header_rest)
            link_type = _LINK_TYPE_NAMES.get(network)
            packet_source = _iter_pcap_classic(fh, byte_order, nanosecond, link_type)
        elif magic == _PCAPNG_MAGIC:
            fh.seek(0)
            packet_source = _iter_pcap_ng(fh)
        else:
            sys.stderr.write(f"warning: unrecognized capture format, skipping: {path}\n")
            return parsed, skipped

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
                row = build_row(ts_us, link_type_name, interface, captured_length, packet_length, raw_data)
            except _MalformedPacket:
                skipped += 1
                continue
            buffer.append(source_file, file_hash, record_offset, record_bytes, row)
            parsed += 1

    except (struct.error, PcapParseError) as exc:
        sys.stderr.write(
            f"warning: corrupt or truncated capture, stopping at the failure point: {path} ({exc})\n"
        )
    finally:
        fh.close()

    return parsed, skipped


def _parse_file_worker(path_str: str, file_hash: str) -> tuple[bytes, int, int]:
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
    parsed, skipped = _convert_file(path, path.name, file_hash, buffer)
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def convert(input_path: str, output: str, workers: int, verbose: bool) -> int:
    """Convert pcap/pcapng captures at ``input_path`` into ``output`` (.parquet)."""
    import json

    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "TraceSignal server detects the ingest parser strictly by file extension."
        )

    files = find_pcap_files(input_path)

    if verbose:
        sys.stderr.write(f"hashing {len(files)} input file(s)...\n")
    provenance = []
    hashes: dict[Path, str] = {}
    for path in files:
        digest, size = hash_file(path)
        hashes[path] = digest
        provenance.append({"name": path.name, "sha256": digest, "size_bytes": size})

    metadata = {
        META_FORMAT_VERSION: FORMAT_VERSION,
        META_CONVERTER_NAME: CONVERTER_NAME,
        META_CONVERTER_VERSION: CONVERTER_VERSION,
        META_ORIGINAL_FILES: json.dumps(provenance, sort_keys=True),
    }

    parsed_total = 0
    skipped_total = 0
    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(output, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)

        if workers > 1 and len(files) > 1:
            if verbose:
                sys.stderr.write(f"parsing {len(files)} file(s) across {workers} workers...\n")
            ctx = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                futures = [
                    pool.submit(_parse_file_worker, str(path), hashes[path]) for path in files
                ]
                for future in concurrent.futures.as_completed(futures):
                    ipc_bytes, parsed, skipped = future.result()
                    parsed_total += parsed
                    skipped_total += skipped
                    reader = pa.ipc.open_stream(ipc_bytes)
                    for batch in reader:
                        if batch.num_rows:
                            buffer.write_batch(batch)
        else:
            for path in files:
                if verbose:
                    sys.stderr.write(f"parsing {path}...\n")
                parsed, skipped = _convert_file(path, path.name, hashes[path], buffer)
                parsed_total += parsed
                skipped_total += skipped

        buffer.flush()

    sys.stderr.write(
        f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
        f"({skipped_total} packets skipped)\n"
    )
    return 0 if parsed_total > 0 else 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Convert pcap/pcapng packet captures (file or directory) to a "
            "TraceSignal Parquet file for direct upload."
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
        default=getattr(os, "process_cpu_count", os.cpu_count)() or 4,
        help="parallel parser processes across input files (default: CPU count)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(args.input, args.output, max(1, args.workers), args.verbose)


if __name__ == "__main__":
    sys.exit(main())
