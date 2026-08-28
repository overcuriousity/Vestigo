"""The sizing calculator's constants are the app's constants.

`docs/sizing/index.html` computes recommended hardware and setting values from a
checked-in JSON. That JSON is generated from `core/config.py`, `db/_scan.py` and
`deploy/clickhouse/memory.xml`, and this test fails when the two drift — which
is the only thing standing between a public sizing page and advice the app
stopped following.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONSTANTS = REPO / "docs" / "sizing" / "sizing-constants.json"
GENERATOR = REPO / "scripts" / "gen_sizing_constants.py"


def test_checked_in_constants_match_the_generator():
    fresh = subprocess.run(
        [sys.executable, str(GENERATOR), "--stdout"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    ).stdout
    assert json.loads(fresh) == json.loads(CONSTANTS.read_text()), (
        "docs/sizing/sizing-constants.json is stale — run "
        "`uv run python scripts/gen_sizing_constants.py`"
    )


def test_constants_carry_what_the_page_needs():
    data = json.loads(CONSTANTS.read_text())
    assert data["memory_ratio"] > 0
    assert data["default_concurrency"] >= 1
    assert data["fallback_max_threads"] == 8
    assert data["foreground_concurrency"] == 4
    assert set(data["shipped_caches"]) <= set(data["counted_caches"])
    assert data["reference"]["clickhouse_mem_limit_bytes"] > 0
    assert data["reference"]["clickhouse_ceiling_bytes"] > 0


def test_page_makes_no_external_requests():
    """Airgapped-by-default is a design goal, and an operator sizing an airgap
    deployment may well be offline. The page must load its own constants and
    nothing else — documentation hyperlinks are fine, fetched assets are not.
    """
    import re

    html = (REPO / "docs" / "sizing" / "index.html").read_text()
    assert 'fetch("sizing-constants.json")' in html

    # Anything the browser *loads*: src= on any tag, plus href= on <link> only.
    # An <a href> to the docs on github.com is a hyperlink, not a request.
    loaded = re.findall(r'src\s*=\s*"([^"]+)"', html)
    loaded += re.findall(r'<link\b[^>]*?href\s*=\s*"([^"]+)"', html)
    for url in loaded:
        assert not url.startswith(("http://", "https://", "//")), (
            f"{url} is fetched from the network"
        )

    # Anything else that pulls a resource in at render time.
    for pattern in ("@import", "url(http", "url('http", 'url("http'):
        assert pattern not in html, f"{pattern} reaches the network"


def test_page_divides_by_the_reserved_slots():
    """The chart lane holds `_FOREGROUND_SLOTS` heavy slots (#300, PR #305 review);
    the page must not hand them out twice."""
    html = (REPO / "docs" / "sizing" / "index.html").read_text()
    assert "scanTotal / (concurrency + K.foreground_slots)" in html
    assert "foreground per-chart cap" in html


def test_recommended_ceiling_leaves_room_for_the_chart_lane():
    """`minimumPlan` sizes the ceiling with the same divisor `budgetFor` uses.

    Sizing it for the heavy slots alone hands each heavy scan
    `perScan x concurrency/(concurrency+slots)` — at the default concurrency,
    half the working set the page just computed the deployment needs, so every
    detector scan spills on the hardware it recommended (PR #305 review).
    """
    html = (REPO / "docs" / "sizing" / "index.html").read_text()
    assert "(perScan * concurrency) / K.memory_ratio" not in html
    assert "(perScan * (concurrency + K.foreground_slots)) / K.memory_ratio" in html


def test_page_sizes_against_hardware_in_hand():
    """The page answers "is this machine enough", not only "what should I buy".

    An admin enters the RAM and cores they have and gets a verdict plus two
    columns of settings: the minimum the workload needs, and what the hardware
    supports at full spend.
    """
    html = (REPO / "docs" / "sizing" / "index.html").read_text()
    for marker in ('id="have-ram"', 'id="have-cores"', 'id="verdict"'):
        assert marker in html, marker
    assert "function minimumPlan(" in html
    assert "function maximumPlan(" in html
    assert "Max for your hardware" in html


def test_constants_carry_the_foreground_slot_count():
    """The page divides by it, so it cannot be transcribed by hand."""
    data = json.loads((REPO / "docs" / "sizing" / "sizing-constants.json").read_text())
    assert data["foreground_slots"] == 2
