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
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from vestigo.core.config import Settings  # noqa: E402
from vestigo.db._scan import (  # noqa: E402
    _COUNTED_CACHES,
    _FALLBACK_MAX_THREADS,
    _FOREGROUND_CONCURRENCY,
    _FOREGROUND_SLOTS,
)

MEMORY_XML = REPO / "deploy" / "clickhouse" / "memory.xml"

# What one heavy scan needs as its per-query cap, *measured* rather than
# assumed. It replaced a flat 12 MiB per million events, which was wrong both
# ways: it sized a 22.5M-event production scan at 270 MiB (that scan failed at
# 819 MiB) and a 10B-event one at 117 GiB.
#
# Measured 2026-09-11 on ClickHouse 26.6.1.1193 with the heavy clause: the
# window-sort detector (interval_periodicity's shape, the one that failed in
# production) over 200-byte values, five threads, finding the smallest cap each
# corpus completed under. Sixteen times the events cost 2.4 times the memory,
# because the sort spills, so the model is a line through log2(events) — fitted
# from these points below rather than transcribed, so the fit cannot drift from
# its inputs. At 16M events, 10 and 20 threads peaked ~2.45 GiB against 1.97 at
# five, which is the wide-thread factor. A cap within a few percent of the peak
# passed once and failed elsewhere, hence the headroom. A high-cardinality
# GROUP BY needed about half as much at every size, so the window sort is the
# binding scan. Beyond the largest corpus measured the page is extrapolating
# along this curve, and says so.
MEASURED_PEAKS_GIB: tuple[tuple[int, float], ...] = (
    (2_000_000, 0.97),
    (4_000_000, 1.21),
    (8_000_000, 1.39),
    (16_000_000, 1.97),
    (32_000_000, 2.36),
)
REFERENCE_EVENTS = MEASURED_PEAKS_GIB[0][0]


def fit_peak_line(points: tuple[tuple[int, float], ...]) -> tuple[float, float]:
    """Ordinary least squares of peak GiB on ``log2(events / reference)``.

    Returns ``(intercept, slope)``: the peak at the reference corpus and the
    growth per doubling. Plain closed-form OLS — five points do not need numpy.
    """
    xs = [math.log2(events / REFERENCE_EVENTS) for events, _ in points]
    ys = [peak for _, peak in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - mean_x) ** 2 for x in xs
    )
    return mean_y - slope * mean_x, slope


def _memory_xml() -> dict[str, str]:
    """memory.xml's settings, ignoring its (many) comment nodes."""
    root = ET.parse(MEMORY_XML).getroot()
    return {child.tag: (child.text or "").strip() for child in root if isinstance(child.tag, str)}


def build() -> dict[str, object]:
    fields = Settings.model_fields
    pinned = _memory_xml()
    base_gib, slope_gib = fit_peak_line(MEASURED_PEAKS_GIB)
    return {
        "memory_ratio": fields["stat_scan_memory_ratio"].default,
        "default_concurrency": fields["stat_scan_concurrency"].default,
        # The chart lane (#300): two heavy slots' worth of the budget, split
        # this many ways. The heavy cap therefore divides by concurrency + 2.
        "foreground_concurrency": _FOREGROUND_CONCURRENCY,
        "foreground_slots": _FOREGROUND_SLOTS,
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
        # against: compressed bytes per event on disk. An order-of-magnitude
        # planning figure, not a guarantee — which is why the page says so and
        # points at /api/health for what actually resolved.
        "bytes_per_event_on_disk": 220,
        # The fitted line through MEASURED_PEAKS_GIB (above); the page evaluates
        # it at the corpus size an operator enters.
        "scan_memory_model": {
            "reference_events": REFERENCE_EVENTS,
            "base_peak_bytes": int(base_gib * 1024**3),
            "peak_bytes_per_doubling": int(slope_gib * 1024**3),
            "measured_max_events": max(events for events, _ in MEASURED_PEAKS_GIB),
            "measured_threads": 5,
            "wide_thread_factor": 1.25,
            "cap_over_peak": 1.25,
        },
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
