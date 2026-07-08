"""One-off generator for tests/data/sample.pcap and sample.pcapng.

Not part of the test suite itself — run manually if the fixtures need
regenerating:

    python3 tests/data/gen_pcap_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

HERE = Path(__file__).parent


def eth(dst_mac: bytes, src_mac: bytes, ethertype: int, payload: bytes) -> bytes:
    return dst_mac + src_mac + struct.pack(">H", ethertype) + payload


def ipv4(src_ip: bytes, dst_ip: bytes, protocol: int, payload: bytes, ip_id: int = 0x1234) -> bytes:
    total_length = 20 + len(payload)
    header = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,  # version 4, IHL 5
        0,  # tos
        total_length,
        ip_id,
        0,  # flags/fragment offset
        64,  # ttl
        protocol,
        0,  # checksum (unchecked by parser)
        src_ip,
        dst_ip,
    )
    return header + payload


def tcp(
    src_port: int, dst_port: int, seq: int, ack: int, flags: int, payload: bytes = b""
) -> bytes:
    data_offset = 5 << 4
    header = struct.pack(
        ">HHIIBBHHH",
        src_port,
        dst_port,
        seq,
        ack,
        data_offset,
        flags,
        65535,  # window
        0,  # checksum
        0,  # urgent pointer
    )
    return header + payload


def udp(src_port: int, dst_port: int, payload: bytes = b"") -> bytes:
    length = 8 + len(payload)
    header = struct.pack(">HHHH", src_port, dst_port, length, 0)
    return header + payload


def arp_request(sender_mac: bytes, sender_ip: bytes, target_mac: bytes, target_ip: bytes) -> bytes:
    return struct.pack(
        ">HHBBH6s4s6s4s",
        1,  # htype ethernet
        0x0800,  # ptype ipv4
        6,  # hlen
        4,  # plen
        1,  # opcode: request
        sender_mac,
        sender_ip,
        target_mac,
        target_ip,
    )


MAC1 = bytes.fromhex("aabbccddee01")
MAC2 = bytes.fromhex("aabbccddee02")
IP1 = bytes([10, 0, 0, 1])
IP2 = bytes([10, 0, 0, 2])

tcp_frame = eth(MAC2, MAC1, 0x0800, ipv4(IP1, IP2, 6, tcp(12345, 80, 1000, 0, 0x02)))
udp_frame = eth(MAC2, MAC1, 0x0800, ipv4(IP1, IP2, 17, udp(53000, 53, b"\x00\x01")))
arp_frame = eth(b"\xff\xff\xff\xff\xff\xff", MAC1, 0x0806, arp_request(MAC1, IP1, b"\x00" * 6, IP2))

FRAMES = [tcp_frame, udp_frame, arp_frame]


def write_classic_pcap(path: Path, frames: list[bytes]) -> None:
    global_header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    with open(path, "wb") as fh:
        fh.write(global_header)
        ts_sec = 1751966801  # 2026-07-08T09:46:41Z
        for i, frame in enumerate(frames):
            record_header = struct.pack("<IIII", ts_sec + i, 0, len(frame), len(frame))
            fh.write(record_header)
            fh.write(frame)


def write_pcapng(path: Path, frames: list[bytes]) -> None:
    def block(block_type: int, body: bytes) -> bytes:
        total_length = 12 + len(body)
        return (
            struct.pack("<I", block_type)
            + struct.pack("<I", total_length)
            + body
            + struct.pack("<I", total_length)
        )

    shb_body = struct.pack("<IhhQ", 0x1A2B3C4D, 1, 0, (1 << 64) - 1)
    shb = block(0x0A0D0D0A, shb_body)

    idb_body = struct.pack("<HHI", 1, 0, 65535)  # ethernet, snaplen
    idb = block(1, idb_body)

    packets = b""
    ts_us = 1751966801 * 1_000_000
    for i, frame in enumerate(frames):
        this_ts = ts_us + i * 1_000_000
        ts_high = this_ts >> 32
        ts_low = this_ts & 0xFFFFFFFF
        epb_body = struct.pack("<IIIII", 0, ts_high, ts_low, len(frame), len(frame)) + frame
        pad = (-len(epb_body)) % 4
        epb_body += b"\x00" * pad
        packets += block(6, epb_body)

    with open(path, "wb") as fh:
        fh.write(shb)
        fh.write(idb)
        fh.write(packets)


write_classic_pcap(HERE / "sample.pcap", FRAMES)
write_pcapng(HERE / "sample.pcapng", FRAMES)
print("wrote sample.pcap and sample.pcapng")
