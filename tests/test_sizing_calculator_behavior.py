"""The sizing calculator's recommendations, run rather than grepped.

`tests/test_sizing_constants.py` keeps the page's constants honest; this runs
the page's own arithmetic (`docs/sizing/index.html`, everything before the
`fetch`) under Node against the checked-in constants and asserts what it
recommends. The expectations come from measurement, not from the page:

- The window-sort detector (`interval_periodicity`'s shape, 200-byte values,
  five threads) failed at a 2 GiB cap on 32M events and completed at 4 GiB
  (peak 2.36 GiB), on ClickHouse 26.6.1.1193.
- Across 2M → 32M events its peak grew 0.97 → 2.36 GiB: sixteen times the events
  for 2.4 times the memory, because the sort spills.

Skipped without Node — a tool, not a service; the page is plain JavaScript.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "sizing" / "index.html"
CONSTANTS = REPO / "docs" / "sizing" / "sizing-constants.json"
NODE = shutil.which("node")
GIB = 1024**3

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_HARNESS = r"""
const fs = require("fs");
const [page, constants, request] = process.argv.slice(1);
const html = fs.readFileSync(page, "utf8");
const js = html.slice(html.indexOf("<script>") + 8, html.indexOf('fetch("sizing-constants.json")'));
const K = JSON.parse(fs.readFileSync(constants, "utf8"));
const api = new Function("K_in", js.replace("let K = null;", "let K = K_in;") +
  "; return { minimumPlan, maximumPlan, eventsFor };")(K);
const { input, sliderMax } = JSON.parse(request);
const out = sliderMax ? { sliderMax: api.eventsFor(100) } : {
  min: api.minimumPlan(input),
  max: api.maximumPlan(input),
};
process.stdout.write(JSON.stringify(out));
"""


def _run(request: dict) -> dict:
    done = subprocess.run(
        [NODE, "-e", _HARNESS, str(PAGE), str(CONSTANTS), json.dumps(request)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def _plans(events: float, *, ram_gib: int = 96, cores: int = 20, enrichment: bool = True) -> dict:
    return _run(
        {
            "input": {
                "events": events,
                "analysts": 6,
                "shape": "docker",
                "embeddings": False,
                "enrichment": enrichment,
                "haveRam": ram_gib * GIB,
                "haveCores": cores,
            }
        }
    )


def test_the_events_slider_reaches_ten_billion():
    assert _run({"sliderMax": True})["sliderMax"] >= 10_000_000_000


@pytest.mark.parametrize("events", [1e7, 1e8, 1e9, 1e10])
@pytest.mark.parametrize(("ram_gib", "cores"), [(96, 20), (256, 20), (512, 64)])
def test_full_spend_never_hands_a_scan_less_than_the_minimum_does(events, ram_gib, cores):
    """More slots on the same hardware is only "more" while each still fits the
    workload. Below that the column is recommending the N trap — unless it says
    so with `tight`."""
    plans = _plans(events, ram_gib=ram_gib, cores=cores)
    full = plans["max"]
    if full is None or full["tight"]:
        return
    assert full["perQuery"] >= plans["min"]["perQuery"]


def test_a_32m_event_scan_is_sized_above_the_cap_it_failed_at():
    plans = _plans(32_000_000, enrichment=False)
    assert plans["min"]["perQuery"] > 2 * GIB


def test_ten_times_the_events_does_not_need_ten_times_the_cap():
    one_b = _plans(1e9, enrichment=False)["min"]["perQuery"]
    ten_b = _plans(1e10, enrichment=False)["min"]["perQuery"]
    assert ten_b < 3 * one_b
