"""`open_url` is the page's own URL: the fixture the frontend parses back is the contract."""

from __future__ import annotations

import json
from pathlib import Path

from vestigo.agent.deep_link import visualize_url
from vestigo.agent.tools import ChartSpec
from vestigo.stories.export import _spec_filters_to_payload, spec_to_stored_chart_config

FIXTURE = Path(__file__).resolve().parents[1] / "frontend/src/test/fixtures/viz-deep-link.json"


def test_visualize_url_matches_the_shared_fixture() -> None:
    fx = json.loads(FIXTURE.read_text())
    spec = ChartSpec.model_validate(fx["spec"])
    url = visualize_url(
        fx["case_id"],
        fx["timeline_id"],
        spec_to_stored_chart_config(spec),
        _spec_filters_to_payload(spec.filters),
    )
    assert url == fx["url"]


def test_visualize_url_writes_every_c_key_the_page_reads() -> None:
    spec = ChartSpec.model_validate(
        {
            "chart_type": "bar",
            "field": "attr:user",
            "scale": "ordinal",
            "metric": "count",
            "options": {"top_n": 20, "sort": "value"},
            "derive": {"kind": "bins", "mode": "log", "count": 4},
            "compare": {"mode": "custom", "filters": {"q": "error"}},
        }
    )
    url = visualize_url("c", "t", spec_to_stored_chart_config(spec), None)
    assert url.startswith("/cases/c/timelines/t/visualize?")
    for key in (
        "c_type=bar",
        "c_scale=ordinal",
        "c_field=attr%3Auser",
        "c_compare=custom",
        "c_compare_filters=",
        "c_opts=",
        "c_derive=",
    ):
        assert key in url, key
    assert "c_metric" not in url and "c_marks" not in url and "c_inputs" not in url
