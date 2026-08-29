"""Tests for the chart-legality table and its generated TypeScript mirror.

``chart_meta.py`` is the source of truth for what the *agent* is allowed to
propose; the Visualize page enforces the same rules through its dropdowns. The
tests here exist to keep those two enforcements from parting ways — that drift
is what let ``propose_chart`` accept a pie chart and silently render a bar.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from vestigo.agent.chart_meta import (
    CHART_META,
    CHART_TYPES,
    FIELD_FREE_DATA_KINDS,
    FIELD_OPTIONAL_DATA_KINDS,
    INPUT_KEYS,
    LEGACY_KIND_MAP,
    METRIC_INFO,
    METRICS,
    SCALES,
    chart_types_for,
    compare_capable,
    input_requirement,
    metric_available,
    requires_field,
    scales_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _known_option_keys() -> set[str]:
    """Every key `ChartOptionsSpec` exposes — derived, not hand-listed, so a
    new option only has to be added in tools.py to be legal in
    `reads_options`. A chart claiming an option nothing can send still fails."""
    from vestigo.agent.tools import ChartOptionsSpec

    return set(ChartOptionsSpec.model_fields)


KNOWN_OPTION_KEYS = _known_option_keys()


# ── table integrity ──────────────────────────────────────────────────────────


def test_table_covers_every_chart_type_exactly_once() -> None:
    assert len(CHART_TYPES) == 20
    assert set(CHART_META) == set(CHART_TYPES)


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_default_scale_is_one_the_chart_can_encode(chart_type: str) -> None:
    """An unstated scale must resolve to a *legal* one, or omitting `scale`
    would produce a spec the validator then rejects."""
    meta = CHART_META[chart_type]
    assert meta.default_scale in meta.scales


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_reads_options_names_real_option_keys(chart_type: str) -> None:
    assert set(CHART_META[chart_type].reads_options) <= KNOWN_OPTION_KEYS


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_every_chart_declares_at_least_one_scale(chart_type: str) -> None:
    assert CHART_META[chart_type].scales


def test_chart_types_for_and_scales_for_are_inverses() -> None:
    for scale in SCALES:
        for chart_type in chart_types_for(scale):
            assert scale in scales_for(chart_type)
    for chart_type in CHART_TYPES:
        for scale in scales_for(chart_type):
            assert chart_type in chart_types_for(scale)


def test_every_scale_offers_at_least_one_chart() -> None:
    """A scale with no legal chart would be an unreachable dead end in the
    picker — and an error the agent could never satisfy."""
    for scale in SCALES:
        assert chart_types_for(scale)


def test_field_free_charts_are_exactly_time_and_punchcard() -> None:
    assert {"time", "punchcard"} == FIELD_FREE_DATA_KINDS
    assert {c for c in CHART_TYPES if CHART_META[c].data_kind in FIELD_FREE_DATA_KINDS} == {
        "time",
        "punchcard",
    }


def test_field_optional_charts_are_exactly_cumulative_and_calendar() -> None:
    """A third state beside field-free and field-required: the figure charts
    every event without a field and a field's values with one."""

    assert {"cumulative", "calendar"} == FIELD_OPTIONAL_DATA_KINDS
    optional = {c for c in CHART_TYPES if CHART_META[c].inputs.get("field") == "optional"}
    assert optional == {"cumulative", "calendar"}
    assert not any(requires_field(c) for c in optional)
    # Field-free stays exactly what it was: these two never take a field.
    assert {c for c in CHART_TYPES if not CHART_META[c].inputs} == {"time", "punchcard"}


def test_cumulative_row_is_a_marked_time_figure_without_compare() -> None:
    meta = CHART_META["cumulative"]
    assert meta.data_kind == "cumulative"
    assert meta.scales == ("nominal", "ordinal", "interval", "ratio")
    assert meta.inputs == {"field": "optional"}
    assert meta.derives == ()
    assert set(meta.reads_options) == {"buckets", "quantity"}
    assert meta.supports_marks is True and meta.supports_compare is False


def test_calendar_row_takes_nothing_but_an_optional_field() -> None:
    meta = CHART_META["calendar"]
    assert meta.data_kind == "calendar"
    assert meta.scales == ("nominal", "ordinal", "interval", "ratio")
    assert meta.inputs == {"field": "optional"}
    assert meta.derives == () and meta.reads_options == ()
    assert meta.supports_marks is False and meta.supports_compare is False


def test_two_field_charts_are_pivot_sankey_scatter() -> None:
    assert {c for c in CHART_TYPES if CHART_META[c].requires_second_field} == {
        "pivot",
        "sankey",
        "scatter",
    }


def test_pivot_and_sankey_share_one_aggregation() -> None:
    """Two marks over the same data — switching must refetch nothing."""
    assert CHART_META["pivot"].data_kind == CHART_META["sankey"].data_kind == "pivot"


def test_compare_capable_charts() -> None:
    """pie/box/violin/ecdf have no honest two-layer encoding; the newer kinds
    simply have no compare aggregation yet; `change` is the one figure that
    *requires* the layer."""
    assert compare_capable() == ["time", "bar", "histogram", "change"]


# ── metric legality ──────────────────────────────────────────────────────────


def test_metric_info_covers_every_metric() -> None:
    assert set(METRIC_INFO) == set(METRICS)


def test_count_is_legal_on_every_chart_type() -> None:
    for chart_type in CHART_TYPES:
        assert metric_available("count", chart_type, compare_on=False)


@pytest.mark.parametrize("metric", ["delta", "rate", "cumulative"])
def test_time_bucketed_metrics_are_legal_only_on_the_time_histogram(metric: str) -> None:
    assert metric_available(metric, "time", compare_on=False)
    for chart_type in CHART_TYPES:
        if chart_type != "time":
            assert not metric_available(metric, chart_type, compare_on=False)


def test_ratio_needs_both_a_comparison_layer_and_the_time_histogram() -> None:
    assert not metric_available("ratio", "time", compare_on=False)
    assert metric_available("ratio", "time", compare_on=True)
    # Compare alone is not enough — bar supports compare but is not time-bucketed.
    assert not metric_available("ratio", "bar", compare_on=True)


def test_no_non_count_metric_escapes_the_time_histogram() -> None:
    """Frozen restatement of ``metricAvailable``'s blunt final clause
    (``m === "count" || dataKind === "time"``). If this ever loosens, the
    frontend must loosen in the same commit."""
    for metric in METRICS:
        for chart_type in CHART_TYPES:
            if metric == "count" or chart_type == "time":
                continue
            assert not metric_available(metric, chart_type, compare_on=True)


# ── back-compat with the retired `kind` enum ─────────────────────────────────

#: Frozen copies of the frontend's `CHART_TYPE_BY_KIND` / `SCALE_BY_KIND`
#: (`frontend/src/api/agent.ts`) as they stood when `kind` was the contract.
#: Persisted `tool_args` from old conversations still render through them, so
#: these pairs are history and must not move.
FROZEN_LEGACY = {
    "terms": ("bar", "nominal"),
    "numeric": ("histogram", "ratio"),
    "timeseries": ("line", "ratio"),
    "punchcard": ("punchcard", "nominal"),
    "pivot": ("pivot", "nominal"),
    "scatter": ("scatter", "ratio"),
    "compare_time": ("time", "nominal"),
    "compare_terms": ("bar", "nominal"),
    "compare_numeric": ("histogram", "ratio"),
}


def test_legacy_kind_map_reproduces_the_frozen_frontend_maps() -> None:
    assert LEGACY_KIND_MAP == FROZEN_LEGACY


@pytest.mark.parametrize("kind", sorted(FROZEN_LEGACY))
def test_legacy_pairs_are_still_legal_under_the_current_table(kind: str) -> None:
    """A historical (chart_type, scale) pair that the table now considers
    illegal would mean an old chart card can no longer be re-rendered."""
    chart_type, scale = LEGACY_KIND_MAP[kind]
    assert scale in CHART_META[chart_type].scales


def test_default_scale_matches_the_legacy_scale_for_every_old_kind() -> None:
    """`default_scale` was chosen to reproduce `SCALE_BY_KIND` exactly, so a
    spec that omits `scale` renders identically to its pre-change equivalent."""
    for chart_type, scale in LEGACY_KIND_MAP.values():
        assert CHART_META[chart_type].default_scale == scale


def test_marks_unreachable_through_the_legacy_kind_enum() -> None:
    """The bug this whole change exists for: `kind` could address only 7
    marks, so a requested pie silently became a bar. Newer marks (waffle)
    were never in the retired enum either and must stay out of it."""
    reachable = {c for c, _ in LEGACY_KIND_MAP.values()}
    assert set(CHART_TYPES) - reachable == {
        "pie",
        "waffle",
        "corr",
        "heatmap",
        "box",
        "violin",
        "ecdf",
        "sankey",
        "table",
        "cumulative",
        "calendar",
        "change",
        "lanes",
    }


# ── the generated TypeScript ─────────────────────────────────────────────────


def test_generated_typescript_is_up_to_date() -> None:
    """`chartMeta.ts` / `timeFields.ts` are generated and committed. If this
    fails, run `uv run python scripts/gen_chart_meta.py` and commit the diff."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/gen_chart_meta.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["chartMeta.ts", "timeFields.ts"])
def test_generated_files_carry_the_do_not_edit_banner(name: str) -> None:
    path = REPO_ROOT / "frontend/src/components/viz/lib" / name
    assert path.read_text(encoding="utf-8").startswith("// Generated by scripts/gen_chart_meta.py")


# ── figure registry (inputs / derives / question) ────────────────────────────


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_every_figure_answers_a_question(chart_type: str) -> None:
    """The rail shows `question` on hover in place of the retired presets; an
    empty one would leave a thumbnail with no reason to pick it."""
    q = CHART_META[chart_type].question
    assert q.strip() and q.endswith("?"), q


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_inputs_use_only_the_fixed_vocabulary(chart_type: str) -> None:
    assert set(CHART_META[chart_type].inputs) <= set(INPUT_KEYS)
    assert set(CHART_META[chart_type].inputs.values()) <= {"required", "optional"}


def test_field_requirement_follows_field_free_kinds() -> None:
    """`field` is required exactly where the chart charts a field; the
    field-free marks and the multi-field matrix declare no `field` at all."""
    for chart_type in CHART_TYPES:
        meta = CHART_META[chart_type]
        if meta.data_kind in FIELD_FREE_DATA_KINDS or "fields" in meta.inputs:
            assert "field" not in meta.inputs, chart_type
        elif meta.data_kind in FIELD_OPTIONAL_DATA_KINDS:
            assert meta.inputs.get("field") == "optional", chart_type
        else:
            assert meta.inputs.get("field") == "required", chart_type


def test_second_field_flags_derive_from_inputs() -> None:
    assert {c for c in CHART_TYPES if CHART_META[c].requires_second_field} == {
        "pivot",
        "sankey",
        "scatter",
    }
    assert {c for c in CHART_TYPES if CHART_META[c].accepts_second_field} == {
        "box",
        "violin",
        "table",
    }
    assert {c for c in CHART_TYPES if CHART_META[c].multi_field} == {"corr"}
    assert input_requirement("pivot", "second_field") == "required"
    assert input_requirement("box", "second_field") == "optional"
    assert input_requirement("bar", "second_field") is None


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_derives_only_where_the_field_is_categorical_after_derivation(chart_type: str) -> None:
    """A derivation yields ordered categories, so only a figure that admits the
    ordinal scale may declare one — otherwise the rail would offer a step that
    makes the figure illegal."""
    meta = CHART_META[chart_type]
    if meta.derives:
        assert "ordinal" in meta.scales, chart_type


def test_marks_are_supported_on_the_time_axis_figures_only() -> None:
    assert {c for c in CHART_TYPES if CHART_META[c].supports_marks} == {
        "time",
        "line",
        "cumulative",
        "lanes",
    }


#: Input keys the Visualize rail (`components/viz/ChartRail.tsx`) renders a
#: control for. Grown in the same commit as the renderer — a key declared here
#: before its control exists would ask the analyst a question with no box to
#: answer it in.
RAIL_RENDERED_INPUTS: frozenset[str] = frozenset(
    {"field", "second_field", "fields", "columns", "pairing", "start_filter", "end_filter"}
)


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_declared_inputs_have_a_rail_renderer(chart_type: str) -> None:
    assert set(CHART_META[chart_type].inputs) <= RAIL_RENDERED_INPUTS


def test_table_row_is_a_terms_shaped_figure_with_its_own_aggregation() -> None:
    meta = CHART_META["table"]
    assert meta.data_kind == "table"
    assert meta.scales == ("nominal", "ordinal")
    assert meta.default_scale == "nominal"
    assert meta.inputs["field"] == "required"
    assert meta.inputs["second_field"] == "optional"
    assert meta.inputs["columns"] == "optional"
    assert meta.derives == ("bins", "time_part")
    assert set(meta.reads_options) == {"top_n", "table_sort_by", "table_sort_dir", "highlight"}
    assert meta.supports_compare is False and meta.supports_marks is False


def test_change_row_is_a_compare_required_terms_shaped_figure() -> None:
    meta = CHART_META["change"]
    assert meta.data_kind == "change"
    assert meta.scales == ("nominal", "ordinal")
    assert meta.inputs == {"field": "required"}
    assert meta.derives == ("bins", "time_part")
    assert set(meta.reads_options) == {"top_n", "layout"}
    assert meta.supports_compare is True and meta.requires_compare is True
    assert meta.supports_marks is False


def test_requires_compare_implies_supports_compare_and_is_only_change() -> None:
    """A figure cannot require a layer it does not draw; today exactly one requires one."""
    for chart_type in CHART_TYPES:
        meta = CHART_META[chart_type]
        if meta.requires_compare:
            assert meta.supports_compare, chart_type
    assert {c for c in CHART_TYPES if CHART_META[c].requires_compare} == {"change"}


def test_lanes_row_is_a_field_keyed_interval_figure_with_marks() -> None:
    meta = CHART_META["lanes"]
    assert meta.data_kind == "lanes"
    assert meta.scales == ("nominal", "ordinal")
    assert meta.inputs == {
        "field": "required",
        "pairing": "optional",
        "start_filter": "optional",
        "end_filter": "optional",
    }
    assert meta.derives == ()
    assert set(meta.reads_options) == {"limit_y"}
    assert meta.supports_compare is False and meta.supports_marks is True


def test_every_input_key_is_declared_by_some_shipped_figure() -> None:
    """No dead vocabulary: `lane_key` left when the lane key became `field`."""
    declared = {key for c in CHART_TYPES for key in CHART_META[c].inputs}
    assert declared == set(INPUT_KEYS)
    assert "lane_key" not in INPUT_KEYS
