"""Chart-type legality: which scales, comparisons and metrics each mark admits.

**This module is the source of truth.** ``frontend/src/components/viz/lib/
chartMeta.ts`` is generated from it by ``scripts/gen_chart_meta.py`` and must
not be hand-edited; a test asserts regeneration is a no-op.

Why the definition lives in Python, when the table describes a frontend
concern: the Visualize page enforces these rules through affordances an
analyst cannot defeat — ``chartTypesFor(scale)`` shrinks the chart-type
dropdown, the Compare control is disabled with a reason, an illegal metric is
force-reset. The agent has no dropdown. Its equivalent is a validation error
naming the legal alternatives, which means the backend has to know the same
rules. Two hand-maintained copies of a legality table is precisely the drift
that let ``propose_chart`` accept a pie chart and silently render a bar, so
one side generates the other. The cost is real and worth naming: chart labels
and this prose now live in Python, and a frontend-only chart-type change needs
a ``uv run python scripts/gen_chart_meta.py``.

The vocabulary, in the order an analyst picks it:

``Scale``
    Scale of measurement of the field being charted (Stevens): nominal,
    ordinal, interval, ratio. This is what a chart type is legal *for*.
``ChartType``
    The visual mark — what the analyst chooses and what gets drawn.
``DataKind``
    Which aggregation feeds the mark. Several chart types share one: pivot and
    sankey are two marks over the same field×field aggregation, so switching
    between them refetches nothing.
``Metric``
    A pure client-side transform of the returned counts (see
    ``frontend/src/components/viz/lib/transforms.ts``). The backend never
    computes a metric — it only validates that the requested one is defined
    for the chart, and echoes it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

ChartType = Literal[
    "time",
    "bar",
    "pie",
    "waffle",
    "heatmap",
    "line",
    "histogram",
    "box",
    "violin",
    "ecdf",
    "punchcard",
    "pivot",
    "sankey",
    "scatter",
    "corr",
]
Scale = Literal["nominal", "ordinal", "interval", "ratio"]
Metric = Literal["count", "delta", "rate", "ratio", "cumulative"]
DataKind = Literal[
    "time", "terms", "numeric", "timeseries", "punchcard", "pivot", "scatter", "corr"
]

CHART_TYPES: tuple[ChartType, ...] = get_args(ChartType)
SCALES: tuple[Scale, ...] = get_args(Scale)
METRICS: tuple[Metric, ...] = get_args(Metric)

#: Chart types that chart the whole event count and take no field at all.
FIELD_FREE_DATA_KINDS: frozenset[DataKind] = frozenset({"time", "punchcard"})

#: Above this many slices a pie stops being readable — angle comparison is the
#: least accurate visual cue there is (Cleveland & McGill 1985), and small
#: differences between neighbouring slices become invisible. Both the analyst's
#: chart and ``propose_chart`` warn past it and point at bar/waffle. Emitted
#: into the generated TypeScript so one number governs both sides.
PIE_COMFORTABLE_MAX = 4


@dataclass(frozen=True)
class ChartMeta:
    """Everything that decides whether a chart request is well-formed.

    ``reads_options`` is the set of ``ChartOptionsSpec`` keys this mark
    actually consumes. Passing one it ignores is a warning, never an error —
    a stray cosmetic option should not cost the analyst a chart — but the
    warning has to exist, or the option silently does nothing.
    """

    label: str
    scales: tuple[Scale, ...]
    data_kind: DataKind
    #: Scale assumed when the caller does not state one. Deliberately *not*
    #: "first legal scale": it mirrors the Visualize page's numeric auto-probe
    #: (``count > 0`` → ratio, else nominal), which is what makes an omitted
    #: scale resolve to the same chart a human would have landed on.
    default_scale: Scale
    reads_options: tuple[str, ...] = ()
    supports_compare: bool = False
    requires_second_field: bool = False
    #: Charts a LIST of fields (``fields``) rather than field/field_y — the
    #: correlation matrix. Mutually exclusive with the two flags below.
    multi_field: bool = False
    #: Can be drawn once per value of a categorical field (small multiples).
    #: Restricted to the cheap single-layer aggregations: a facet grid runs
    #: one query per facet value, so a mark whose single query is already a
    #: heavy multi-scan would multiply that cost by the facet count.
    supports_facet: bool = False
    #: Single-field chart that ALSO accepts an optional grouping field
    #: (box/violin: numeric response × categorical group via ``field_y``).
    #: Mutually exclusive with ``requires_second_field``.
    accepts_second_field: bool = False
    #: Design rationale carried into the generated TypeScript as a comment.
    note: str = ""


CHART_META: dict[ChartType, ChartMeta] = {
    "time": ChartMeta(
        label="Time histogram (events over time)",
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="time",
        default_scale="nominal",
        supports_facet=True,
        reads_options=("buckets",),
        supports_compare=True,
        note=(
            "Event count over time needs no field, so it is meaningful whatever "
            "scale the currently-picked field has — available under every scale."
        ),
    ),
    "bar": ChartMeta(
        label="Bar",
        scales=("nominal", "ordinal"),
        data_kind="terms",
        default_scale="nominal",
        supports_facet=True,
        reads_options=("top_n", "orientation", "sort", "log_scale"),
        supports_compare=True,
    ),
    "pie": ChartMeta(
        label="Pie / Donut",
        scales=("nominal",),
        data_kind="terms",
        default_scale="nominal",
        supports_facet=True,
        reads_options=("top_n",),
        note=(
            "pie/box/violin/ecdf have no honest two-layer encoding, so they are "
            "left without supportsCompare — the rail hides Compare for them."
        ),
    ),
    "waffle": ChartMeta(
        label="Waffle (10×10 share grid)",
        scales=("nominal",),
        data_kind="terms",
        default_scale="nominal",
        supports_facet=True,
        reads_options=("top_n",),
        note=(
            "Same terms aggregation as bar/pie — switching between them refetches "
            "nothing. Preferred over pie once there are five or more categories: "
            "counting cells beats judging angles."
        ),
    ),
    "heatmap": ChartMeta(
        label="Heatmap (value × time)",
        scales=("nominal", "ordinal", "interval"),
        data_kind="timeseries",
        default_scale="nominal",
        reads_options=("top_n", "buckets"),
    ),
    "line": ChartMeta(
        label="Line / Area (value × time)",
        scales=("interval", "ratio"),
        data_kind="timeseries",
        default_scale="ratio",
        reads_options=("top_n", "buckets", "series_mode", "legend", "show_points"),
        note=(
            "show_points marks the actual measured buckets. Graphical integrity "
            "(Tufte): a line between two points asserts values that were never "
            "measured — markers show where the data really is."
        ),
    ),
    "histogram": ChartMeta(
        label="Histogram",
        scales=("interval", "ratio"),
        data_kind="numeric",
        default_scale="ratio",
        supports_facet=True,
        reads_options=("bins", "log_scale", "show_density"),
        supports_compare=True,
    ),
    "box": ChartMeta(
        label="Box plot",
        scales=("ratio",),
        data_kind="numeric",
        default_scale="ratio",
        supports_facet=True,
        reads_options=("bins", "groups", "show_points"),
        accepts_second_field=True,
        note=(
            "box/violin accept an OPTIONAL second field (accepts_second_field): a "
            "categorical grouping variable, giving one box/violin per top group."
        ),
    ),
    "violin": ChartMeta(
        label="Violin plot",
        scales=("ratio",),
        data_kind="numeric",
        default_scale="ratio",
        supports_facet=True,
        reads_options=("bins", "groups", "show_points"),
        accepts_second_field=True,
    ),
    "ecdf": ChartMeta(
        label="ECDF",
        scales=("ratio",),
        data_kind="numeric",
        default_scale="ratio",
        supports_facet=True,
        reads_options=("bins",),
    ),
    "punchcard": ChartMeta(
        label="Punch card (day × hour)",
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="punchcard",
        default_scale="nominal",
        note="Field-free like `time` — meaningful whatever the picked field's scale is.",
    ),
    "pivot": ChartMeta(
        label="Heatmap (field × field)",
        scales=("nominal", "ordinal"),
        data_kind="pivot",
        default_scale="nominal",
        reads_options=("limit_x", "limit_y"),
        requires_second_field=True,
        note=(
            "pivot and sankey are two marks over the SAME field×field "
            "aggregation — switching between them refetches nothing."
        ),
    ),
    "sankey": ChartMeta(
        label="Flow / Sankey (field × field)",
        scales=("nominal", "ordinal"),
        data_kind="pivot",
        default_scale="nominal",
        reads_options=("limit_x", "limit_y"),
        requires_second_field=True,
    ),
    "scatter": ChartMeta(
        label="Scatter (numeric × numeric)",
        scales=("interval", "ratio"),
        data_kind="scatter",
        default_scale="ratio",
        reads_options=("sample_limit", "log_scale"),
        requires_second_field=True,
    ),
    "corr": ChartMeta(
        label="Correlation matrix (numeric fields)",
        # Available under every scale like the field-free marks: this chart
        # ignores the currently-picked field entirely (its own `fields` list
        # is what it charts), so the picked field's scale says nothing about
        # whether it is legal.
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="corr",
        default_scale="ratio",
        multi_field=True,
        note=(
            "Takes `fields` (2-8 numeric tokens) instead of field/field_y — the "
            "one mark that charts more than two fields at once. Preferred over "
            "reading scatter plots one pair at a time past three or four "
            "quantitative variables."
        ),
    ),
}


@dataclass(frozen=True)
class MetricMeta:
    """One derived metric, mirroring ``transforms.ts``' ``METRIC_INFO``.

    ``formula`` is the exact string charts print in captions and exports —
    quoted verbatim in validation errors too, so the agent is told what the
    metric *means*, not merely that it was rejected.
    """

    label: str
    formula: str
    requires_compare: bool = False
    time_bucketed_only: bool = False


METRIC_INFO: dict[Metric, MetricMeta] = {
    "count": MetricMeta(label="Count", formula="count[i]"),
    "delta": MetricMeta(
        label="Δ per bin",
        formula="count[i] − count[i−1] (first bin undefined)",
        time_bucketed_only=True,
    ),
    "rate": MetricMeta(
        label="Rate (events/s)",
        formula="count[i] / bucket_interval_seconds",
        time_bucketed_only=True,
    ),
    "ratio": MetricMeta(
        label="% of baseline",
        formula="primary[i] / comparison[i] × 100 (undefined where comparison[i] = 0)",
        requires_compare=True,
    ),
    "cumulative": MetricMeta(label="Cumulative", formula="Σ count[0..i]", time_bucketed_only=True),
}


def chart_types_for(scale: Scale) -> list[ChartType]:
    """Chart types legal for *scale* — the agent's equivalent of the dropdown."""
    return [c for c in CHART_TYPES if scale in CHART_META[c].scales]


def scales_for(chart_type: ChartType) -> list[Scale]:
    """Scales *chart_type* can honestly encode."""
    return list(CHART_META[chart_type].scales)


def compare_capable() -> list[ChartType]:
    """Chart types that admit a comparison layer."""
    return [c for c in CHART_TYPES if CHART_META[c].supports_compare]


def requires_field(chart_type: ChartType) -> bool:
    """False only for the two marks that chart the whole event count."""
    return CHART_META[chart_type].data_kind not in FIELD_FREE_DATA_KINDS


def metric_available(metric: Metric, chart_type: ChartType, compare_on: bool) -> bool:
    """Whether *metric* is defined for *chart_type*.

    Mirrors ``VisualizePage.tsx``' ``metricAvailable`` exactly, including its
    blunt final clause: outside ``data_kind == "time"`` **only** ``count`` is
    ever legal. The derived metrics need ordered time bins (delta, rate,
    cumulative) or a second layer (ratio), and the time histogram is the one
    chart that has them.
    """
    info = METRIC_INFO[metric]
    data_kind = CHART_META[chart_type].data_kind
    if info.requires_compare and not compare_on:
        return False
    if info.time_bucketed_only and data_kind != "time":
        return False
    return metric == "count" or data_kind == "time"


#: Frozen translation of the retired nine-value ``ChartSpec.kind`` enum, kept
#: only so a conversation whose model still holds the old tool schema in
#: context keeps working across a server restart. Not part of the model-facing
#: schema — a visible alias would double what the model reads and the old
#: shape would never die. Deletable once no live conversation predates the
#: change. Values are pinned by test against the frontend's frozen
#: CHART_TYPE_BY_KIND / SCALE_BY_KIND maps.
LEGACY_KIND_MAP: dict[str, tuple[ChartType, Scale]] = {
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
