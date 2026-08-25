"""Emit the constants ``docs/sizing/index.html`` sizes a deployment with.

The calculator is a static page on GitHub Pages, so it cannot import anything
from the app. Generating its constants — rather than transcribing them — is what
keeps a public sizing page from recommending values the app stopped using.
``tests/test_sizing_constants.py`` fails when the checked-in JSON is stale.

Run: ``uv run python scripts/gen_sizing_constants.py`` (writes the file), or
``--stdout`` (what the parity test compares against).
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from vestigo.core.config import Settings  # noqa: E402
from vestigo.db._scan import _COUNTED_CACHES, _FALLBACK_MAX_THREADS  # noqa: E402

MEMORY_XML = REPO / "deploy" / "clickhouse" / "memory.xml"


def _memory_xml() -> dict[str, str]:
    """memory.xml's settings, ignoring its (many) comment nodes."""
    root = ET.parse(MEMORY_XML).getroot()
    return {child.tag: (child.text or "").strip() for child in root if isinstance(child.tag, str)}


def build() -> dict[str, object]:
    fields = Settings.model_fields
    pinned = _memory_xml()
    return {
        "memory_ratio": fields["stat_scan_memory_ratio"].default,
        "default_concurrency": fields["stat_scan_concurrency"].default,
        "fallback_max_threads": _FALLBACK_MAX_THREADS,
        "min_threads_per_scan": 2,
        "counted_caches": list(_COUNTED_CACHES),
        "shipped_caches": {name: int(pinned[name]) for name in _COUNTED_CACHES if name in pinned},
        # The ceiling the reference stack pins, and the container it is sized
        # for. The page scales both together, exactly as memory.xml's comment
        # tells an operator to.
        "reference": {
            "clickhouse_mem_limit_bytes": 12 * 1024**3,
            "clickhouse_ceiling_bytes": int(pinned.get("max_server_memory_usage", 0)),
            "ceiling_to_limit_ratio": float(
                pinned.get("max_server_memory_usage_to_ram_ratio", 0.8)
            ),
            "postgres_mem_limit_bytes": 4 * 1024**3,
            "qdrant_mem_limit_bytes": 4 * 1024**3,
            "app_mem_limit_bytes": 4 * 1024**3,
        },
        # Rough shape of the 300M-event corpus the scan guardrails were sized
        # against: compressed bytes per event on disk, and how much of a scan's
        # aggregation state a million events tends to cost. Both are order-of-
        # magnitude planning figures, not guarantees — which is why the page
        # says so and points at /api/health for what actually resolved.
        "bytes_per_event_on_disk": 220,
        "scan_working_set_bytes_per_million_events": 12 * 1024**2,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args()
    text = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if args.stdout:
        sys.stdout.write(text)
    else:
        out = REPO / "docs" / "sizing" / "sizing-constants.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"wrote {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
