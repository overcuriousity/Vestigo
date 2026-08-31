"""`open_url` is the page's own URL: the fixture the frontend parses back is the contract."""

from __future__ import annotations

import json
from pathlib import Path

from vestigo.agent.deep_link import unrepresentable_filter_members, visualize_url
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
        # The treat-as the derivation was computed from — what the rail needs
        # to show its Derive control; "ordinal" is the *effective* scale.
        "c_scale=ratio",
        "c_field=attr%3Auser",
        "c_compare=custom",
        "c_compare_filters=",
        "c_opts=",
        "c_derive=",
    ):
        assert key in url, key
    assert "c_metric" not in url and "c_marks" not in url and "c_inputs" not in url


def test_visualize_url_carries_the_treat_as_a_derived_field_needs() -> None:
    def scale_of(spec: dict) -> str:
        parsed = ChartSpec.model_validate(spec)
        url = visualize_url("c", "t", spec_to_stored_chart_config(parsed), None)
        return url.split("c_scale=")[1].split("&")[0]

    bins = {"kind": "bins", "mode": "width", "count": 4}
    part = {"kind": "time_part", "part": "hour"}
    assert scale_of({"chart_type": "bar", "field": "attr:bytes", "derive": bins}) == "ratio"
    assert scale_of({"chart_type": "bar", "field": "attr:ts", "derive": part}) == "interval"
    # An explicit treat-as passes through; a legacy "ordinal" resolves like an omitted one.
    assert (
        scale_of({"chart_type": "bar", "field": "attr:bytes", "scale": "interval", "derive": bins})
        == "interval"
    )
    assert (
        scale_of({"chart_type": "bar", "field": "attr:bytes", "scale": "ordinal", "derive": bins})
        == "ratio"
    )


def test_unrepresentable_filter_members_names_the_three_url_less_members() -> None:
    """Mirror of `chartConfig.ts` URL_UNREPRESENTABLE_FILTERS: the narrowings a
    `c_*` link silently drops, so a link that would widen the chart says so."""
    from vestigo.agent.tools import FilterSpec

    assert unrepresentable_filter_members(None) == []
    assert unrepresentable_filter_members(FilterSpec(q="x")) == []
    members = unrepresentable_filter_members(
        FilterSpec(event_ids=["e1"], run_id="r1", collapse_routine=True)
    )
    assert members == ["a fixed event set", "a detector run", "routine collapse"]


def test_public_base_url_makes_the_link_absolute() -> None:
    """An external MCP client has no host to resolve a path against.

    The absolute form must be the relative one with a prefix and nothing else:
    a link that reorders or re-encodes a parameter is a different figure.
    """
    spec = ChartSpec.model_validate({"chart_type": "time"})
    config = spec_to_stored_chart_config(spec)
    relative = visualize_url("c", "t", config, None)
    assert relative.startswith("/cases/")
    absolute = visualize_url("c", "t", config, None, base_url="https://vestigo.example.org")
    assert absolute == f"https://vestigo.example.org{relative}"


def test_public_base_url_trailing_slash_does_not_double_up() -> None:
    """`https://host/` is what an operator pastes out of a browser bar."""
    spec = ChartSpec.model_validate({"chart_type": "time"})
    config = spec_to_stored_chart_config(spec)
    assert visualize_url("c", "t", config, None, base_url="https://host//") == visualize_url(
        "c", "t", config, None, base_url="https://host"
    )
