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

Skipped without Node locally — a tool, not a service; the page is plain
JavaScript. Under ``CI`` a missing Node fails instead: the backend job installs
it on purpose (``actions/setup-node`` in ``ci.yml``), and a runner image without
it would otherwise turn this file into a green no-op.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "sizing" / "index.html"
CONSTANTS = REPO / "docs" / "sizing" / "sizing-constants.json"
NODE = shutil.which("node")
GIB = 1024**3


@pytest.fixture(autouse=True)
def _node_or_bust() -> None:
    """Skip without Node locally; fail under CI, where its absence is a broken job."""
    if NODE is not None:
        return
    if os.environ.get("CI"):
        pytest.fail(
            "node is not installed; the backend CI job's setup-node step is missing or broken"
        )
    pytest.skip("node is not installed")


_HARNESS = r"""
const fs = require("fs");
const [page, constants, request] = process.argv.slice(1);
const html = fs.readFileSync(page, "utf8");
const js = html.slice(html.indexOf("<script>") + 8, html.indexOf('fetch("sizing-constants.json")'));
const K = JSON.parse(fs.readFileSync(constants, "utf8"));
// renderVerdict writes into the page; give it one element to write into.
const verdictEl = { className: "", innerHTML: "" };
global.document = { getElementById: (id) => (id === "verdict" ? verdictEl : {}) };
const api = new Function("K_in", js.replace("let K = null;", "let K = K_in;") +
  "; return { minimumPlan, maximumPlan, renderVerdict, eventsFor };")(K);
const { input, sliderMax } = JSON.parse(request);
let out;
if (sliderMax) {
  out = { sliderMax: api.eventsFor(100) };
} else {
  const min = api.minimumPlan(input);
  const max = api.maximumPlan(input);
  api.renderVerdict(min, { haveRam: input.haveRam, haveCores: input.haveCores }, max);
  out = {
    min,
    max,
    verdict: { className: verdictEl.className, text: verdictEl.innerHTML.replace(/<[^>]+>/g, "") },
  };
}
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


def _plans(
    events: float,
    *,
    ram_gib: int = 96,
    cores: int = 20,
    enrichment: bool = True,
    analysts: int = 6,
) -> dict:
    return _run(
        {
            "input": {
                "events": events,
                "analysts": analysts,
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


def test_a_host_that_meets_the_minimum_but_admits_fewer_scans_is_not_called_sufficient():
    """The minimum sizes every scan 4 threads wide; a host with more cores runs
    them wider, and past the measured width a scan needs more per query. 60 GiB
    and 12 cores meet the minimum for 1B events and 4 analysts, yet at full
    spend admit one scan where the minimum asks for two — the verdict must
    compare that concurrency, not only the per-query cap (PR #376 review)."""
    plans = _plans(1e9, ram_gib=60, cores=12, enrichment=False, analysts=4)
    need, full, verdict = plans["min"], plans["max"], plans["verdict"]
    assert full["perQuery"] >= need["perQuery"], "per-query alone would have passed"
    assert full["concurrency"] < need["concurrency"]
    assert not full["tight"]
    assert verdict["className"] == "note warn"
    assert not verdict["text"].startswith("Sufficient")
    assert f"admits {full['concurrency']} concurrent scan" in verdict["text"]
    assert f"the {need['concurrency']} of" in verdict["text"]


def test_a_host_at_the_minimum_width_is_sufficient():
    """The same workload on 8 cores runs scans at the width the minimum sized
    for, and admits the slots it asks for."""
    plans = _plans(1e9, ram_gib=60, cores=8, enrichment=False, analysts=4)
    assert plans["max"]["concurrency"] >= plans["min"]["concurrency"]
    assert plans["verdict"]["className"] == "note"
    assert plans["verdict"]["text"].startswith("Sufficient")
