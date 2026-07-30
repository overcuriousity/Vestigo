"""Firewall/netflow records, as a generic CSV export.

Deliberately cross-source: every proxy beacon has a matching allow record here
in the same second, so the repeating-motif miner has a pattern that spans two
sources rather than a pattern one source could have produced alone.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from tools.demo_case import scenario
from tools.demo_case.sources import proxy

NETFLOW_HEADER: tuple[str, ...] = (
    "datetime",
    "timestamp_desc",
    "source",
    "message",
    "src_ip",
    "dst_ip",
    "dst_port",
    "protocol",
    "bytes",
    "packets",
    "duration",
    "action",
)

C2_IP = "185.199.42.77"
EXFIL_IP = "45.153.160.204"
FILE_SERVER_IP = "10.30.4.20"
_JUMP_HOST_IP = "10.30.4.11"

_INTERNAL_DESTS = (
    ("10.30.4.20", 445),
    ("10.30.4.11", 22),
    ("10.30.1.10", 53),
    ("10.30.1.11", 53),
    ("10.30.2.30", 443),
    ("10.30.2.31", 80),
)


def _row(
    moment,
    src_ip: str,
    dst_ip: str,
    dst_port: int,
    protocol: str,
    size: int,
    packets: int,
    duration: float,
    action: str,
) -> dict[str, str]:
    return {
        "datetime": moment.isoformat(),
        "timestamp_desc": "Content Modification Time",
        "source": "NET",
        "message": f"{action} {src_ip} -> {dst_ip}:{dst_port} {protocol} bytes={size}",
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "dst_port": str(dst_port),
        "protocol": protocol,
        "bytes": str(size),
        "packets": str(packets),
        "duration": f"{duration:.2f}",
        "action": action,
    }


def _baseline() -> Iterator[dict[str, str]]:
    """Short internal sessions, plus a steady trickle of blocked traffic."""
    r = scenario.rng("netflow-baseline")
    for moment in scenario.walk(scenario.SCENARIO_START, scenario.SCENARIO_END, 1250, r):
        src = f"10.20.{r.randrange(1, 20)}.{r.randrange(2, 250)}"
        dst, port = r.choice(_INTERNAL_DESTS)
        allowed = r.random() < 0.93
        size = max(64, int(r.lognormvariate(8.6, 1.0)))
        yield _row(
            moment,
            src,
            dst,
            port,
            "tcp" if port != 53 else "udp",
            size,
            max(1, size // r.randrange(300, 1400)),
            r.uniform(0.05, 28.0),
            "allow" if allowed else "deny",
        )


def _beacon_mirror() -> Iterator[dict[str, str]]:
    """One allow record per proxy beacon, in the same second.

    Reading the proxy stream rather than regenerating the schedule keeps the
    two sources genuinely aligned — a motif that only exists because both
    files agree, which is what makes it worth mining.
    """
    r = scenario.rng("netflow-beacon")
    for row in proxy.proxy_rows():
        if row["host"] != scenario.C2_HOST:
            continue
        size = int(row["bytes_out"]) + int(row["bytes_in"])
        yield _row(
            datetime.fromisoformat(row["datetime"]),
            _JUMP_HOST_IP,
            C2_IP,
            443,
            "tcp",
            size,
            r.randrange(4, 9),
            r.uniform(0.2, 1.4),
            "allow",
        )


def _exfil_sessions() -> Iterator[dict[str, str]]:
    """Long, fat sessions to the upload destination."""
    r = scenario.rng("netflow-exfil")
    moment = scenario.PHASES[3].start + timedelta(hours=1, minutes=52)
    for _ in range(260):
        size = r.randrange(8_000_000, 42_000_000)
        yield _row(
            moment,
            _JUMP_HOST_IP,
            EXFIL_IP,
            443,
            "tcp",
            size,
            size // 1400,
            r.uniform(90.0, 340.0),
            "allow",
        )
        moment += timedelta(minutes=r.uniform(1.5, 6.0))


def _lateral_scan() -> Iterator[dict[str, str]]:
    """The share-enumeration burst: mostly denied SMB against the file server."""
    r = scenario.rng("netflow-lateral")
    phase = scenario.PHASES[2]
    moment = phase.start + timedelta(hours=2, minutes=48)
    while moment < phase.end:
        for _ in range(r.randrange(20, 45)):
            yield _row(
                moment,
                _JUMP_HOST_IP,
                FILE_SERVER_IP,
                445,
                "tcp",
                r.randrange(120, 900),
                r.randrange(1, 4),
                r.uniform(0.01, 0.4),
                "deny",
            )
            moment += timedelta(seconds=r.uniform(0.4, 4.0))
        moment += timedelta(hours=r.uniform(2, 6))


def netflow_rows() -> Iterator[dict[str, str]]:
    """Yield every netflow row, ascending by timestamp."""
    rows: list[dict[str, str]] = []
    for builder in (_baseline, _beacon_mirror, _exfil_sessions, _lateral_scan):
        rows.extend(builder())
    rows.sort(key=lambda r: r["datetime"])
    return iter(rows)


def write_netflow_csv(path: Path) -> int:
    """Write the CSV and return the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=NETFLOW_HEADER)
        writer.writeheader()
        for row in netflow_rows():
            writer.writerow(row)
            written += 1
    return written
