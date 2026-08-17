#!/usr/bin/env python3
"""syslog2vestigo — RFC 3164 syslog to Vestigo Parquet (test fixture; model-written style).

Assumptions: year missing in RFC 3164 → taken from the file mtime; no zone → UTC.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

CONVERTER_NAME = "syslog2vestigo"
CONVERTER_VERSION = "__CONVERTER_VERSION__"

SCHEMA = pa.schema(
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
LINE_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}) (?P<time>\d{2}:\d{2}:\d{2}) "
    r"(?P<host>\S+) (?P<proc>[^\[:]+)(?:\[(?P<pid>\d+)\])?: (?P<msg>.*)$"
)
MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def _open(path: Path):
    return gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")


def hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def parse_line(line: str, year: int) -> dict | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    hh, mm, ss = (int(x) for x in m["time"].split(":"))
    ts = datetime.datetime(year, MONTHS[m["mon"]], int(m["day"]), hh, mm, ss, tzinfo=datetime.UTC)
    proc = m["proc"].strip()
    attrs = {"host": m["host"], "process": proc}
    if m["pid"]:
        attrs["pid"] = m["pid"]
    ip = re.search(r"from (\d+\.\d+\.\d+\.\d+)", m["msg"])
    if ip:
        attrs["src_ip"] = ip.group(1)
    user = re.search(r"for (?:invalid user )?(\w+)", m["msg"])
    if user:
        attrs["user"] = user.group(1)
    return {
        "timestamp": ts,
        "message": f"{proc}: {m['msg']}",
        "artifact": f"syslog:{proc}",
        "artifact_long": f"linux:syslog:{proc}",
        "attributes": attrs,
    }


def convert(inp: Path, out: Path, verbose: bool) -> int:
    file_hash, size = hash_file(inp)
    mtime = datetime.datetime.fromtimestamp(inp.stat().st_mtime, datetime.UTC)
    rows: dict[str, list] = {f.name: [] for f in SCHEMA}
    parsed = malformed = 0
    offset = 0
    with _open(inp) as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            rec = parse_line(line, mtime.year)
            rows["source_file"].append(inp.name)
            rows["file_hash"].append(file_hash)
            rows["byte_offset"].append(offset)
            rows["content_hash"].append(hashlib.sha256(raw).hexdigest())
            if rec is None:
                malformed += 1
                rows["message"].append(line)
                rows["timestamp"].append(None)
                rows["artifact"].append("")
                rows["artifact_long"].append("")
                rows["attributes"].append({"parse_status": "unparsed"})
            else:
                parsed += 1
                rows["message"].append(rec["message"])
                rows["timestamp"].append(rec["timestamp"])
                rows["artifact"].append(rec["artifact"])
                rows["artifact_long"].append(rec["artifact_long"])
                rows["attributes"].append(rec["attributes"])
            rows["timestamp_desc"].append("Event Logged")
            rows["display_name"].append(inp.name)
            rows["tags"].append([])
            offset += len(raw)
            if verbose and (parsed + malformed) % 1000 == 0:
                print(f"progress {parsed + malformed}", file=sys.stderr)
    meta = {
        "vestigo.format_version": "1",
        "vestigo.converter_name": CONVERTER_NAME,
        "vestigo.converter_version": CONVERTER_VERSION,
        "vestigo.original_files": json.dumps(
            [
                {
                    "name": inp.name,
                    "sha256": file_hash,
                    "size_bytes": size,
                    "path": str(inp.resolve()),
                    "mtime": mtime.isoformat(),
                }
            ]
        ),
        "vestigo.converted_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "vestigo.row_counts": json.dumps(
            {"parsed": parsed, "skipped_malformed": malformed, "skipped_by_time": 0}
        ),
        "vestigo.timezone_assumption": (
            f"RFC 3164 has no year/zone: year {mtime.year} from file mtime, UTC assumed"
        ),
        "vestigo.parse_decisions": json.dumps({"multiline": "not applicable"}),
    }
    table = pa.Table.from_pydict(rows, schema=SCHEMA)
    with pq.ParquetWriter(out, SCHEMA.with_metadata(meta), compression="zstd") as w:
        w.write_table(table)
    if verbose:
        print(f"progress {parsed + malformed}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    inp = Path(a.input)
    if not inp.is_file():
        print(f"input must be a file: {inp}", file=sys.stderr)
        return 1
    return convert(inp, Path(a.output), a.verbose)


if __name__ == "__main__":
    sys.exit(main())
