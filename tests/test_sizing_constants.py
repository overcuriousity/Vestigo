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
