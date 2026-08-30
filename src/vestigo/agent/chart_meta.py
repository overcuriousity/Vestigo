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

from dataclasses import dataclass, field
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
    "cumulative",
    "calendar",
    "change",
    "lanes",
    "pivot",
    "sankey",
    "scatter",
    "corr",
    "table",
]
Scale = Literal["nominal", "ordinal", "interval", "ratio"]
Metric = Literal["count", "delta", "rate", "ratio", "cumulative"]
DataKind = Literal[
    "time",
    "terms",
    "numeric",
    "timeseries",
    "punchcard",
    "pivot",
    "scatter",
    "corr",
    "table",
    "cumulative",
    "calendar",
    "change",
    "lanes",
]

#: What a figure asks the analyst for, from a fixed vocabulary. The Visualize
#: rail renders one control per declared key and nothing else — there is no
#: figure-specific JSX in the rail — and the agent's ``propose_chart`` reports
#: a missing ``required`` key by name. Every key is declared by a shipped
#: figure; the vocabulary and the rail's renderers are checked against one list.
InputKey = Literal[
    "field",
    "second_field",
    "fields",
    "start_filter",
    "end_filter",
    "pairing",
    "columns",
]
Requirement = Literal["required", "optional"]
#: A derivation is a change of scale applied before aggregation — never
#: anything domain-specific. ``bins``: number → ordered ranges. ``time_part``:
#: time → hour/weekday/day/week/month. Both yield the ordinal scale.
Derive = Literal["bins", "time_part"]

INPUT_KEYS: tuple[InputKey, ...] = get_args(InputKey)

CHART_TYPES: tuple[ChartType, ...] = get_args(ChartType)

#: The scales a field may be treated as for each derivation to make sense —
#: bins group a number (a measure or a number-or-time), a calendar part is
#: taken from a number-or-time. The first entry is what an omitted scale
#: resolves to. Mirrors ``frontend/src/components/viz/lib/derive.ts``
#: ``deriveOptionsFor``, read the other way round.
DERIVE_SOURCE_SCALES: dict[str, tuple[Scale, ...]] = {
    "bins": ("ratio", "interval"),
    "time_part": ("interval",),
}


def derive_source_scale(kind: str, scale: str | None) -> Scale:
    """The treat-as a derived chart should carry: *scale* if it is one the
    derivation admits, else the derivation's natural one.

    ``ordinal`` is the *effective* scale of every derived field and what the
    agent contract accepted alone for a while; stored and linked as the
    treat-as it would hide the page's Derive control (the rail offers no
    derivation on categories), so it resolves like an omitted scale.
    """
    admitted = DERIVE_SOURCE_SCALES[kind]
    return scale if scale in admitted else admitted[0]  # type: ignore[return-value]


SCALES: tuple[Scale, ...] = get_args(Scale)
METRICS: tuple[Metric, ...] = get_args(Metric)

#: Chart types that chart the whole event count and take no field at all.
FIELD_FREE_DATA_KINDS: frozenset[DataKind] = frozenset({"time", "punchcard"})

#: Chart types that chart the whole event count *or* a field's values: with no
#: field they count events, with one they count/sum its values. A third state
#: beside field-free and field-required; the rail shows the field picker with
#: "No field" selectable, and `propose_chart` accepts either.
FIELD_OPTIONAL_DATA_KINDS: frozenset[DataKind] = frozenset({"cumulative", "calendar"})

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

    ``inputs`` is what the figure asks for. The three ``*_second_field`` /
    ``multi_field`` properties are read-only views over it, kept because the
    generated TypeScript and ``propose_chart`` already speak in those terms.
    """

    label: str
    #: The forensic question this figure answers — shown on hover in the
    #: rail's gallery, and the successor of the retired task presets.
    question: str
    scales: tuple[Scale, ...]
    data_kind: DataKind
    #: Scale assumed when the caller does not state one. Deliberately *not*
    #: "first legal scale": it mirrors the Visualize page's numeric auto-probe
    #: (``count > 0`` → ratio, else nominal), which is what makes an omitted
    #: scale resolve to the same chart a human would have landed on.
    default_scale: Scale
    inputs: dict[InputKey, Requirement] = field(default_factory=dict)
    derives: tuple[Derive, ...] = ()
    reads_options: tuple[str, ...] = ()
    supports_compare: bool = False
    #: The figure is *defined* by two windows — without a comparison layer
    #: there is nothing to draw. Implies ``supports_compare``. The rail
    #: disables Compare's *Off* for it and ``propose_chart`` refuses by name.
    requires_compare: bool = False
    #: Instants and windows drawn over the figure. Only figures with a time
    #: axis can place a mark honestly.
    supports_marks: bool = False
    #: Design rationale carried into the generated TypeScript as a comment.
    note: str = ""

    @property
    def requires_second_field(self) -> bool:
        """Two-field charts (pivot/sankey/scatter) need a second field picked."""
        return self.inputs.get("second_field") == "required"

    @property
    def accepts_second_field(self) -> bool:
        """Single-field chart that ALSO accepts an optional grouping field."""
        return self.inputs.get("second_field") == "optional"

    @property
    def multi_field(self) -> bool:
        """Charts a LIST of fields (``fields``) — the correlation matrix."""
        return "fields" in self.inputs


CHART_META: dict[ChartType, ChartMeta] = {
    "time": ChartMeta(
        label="Time histogram (events over time)",
        question="When did activity spike or go quiet under the current filters?",
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="time",
        default_scale="nominal",
        inputs={},
        supports_marks=True,
        reads_options=("buckets",),
        supports_compare=True,
        note=(
            "Event count over time needs no field, so it is meaningful whatever "
            "scale the currently-picked field has — available under every scale."
        ),
    ),
    "bar": ChartMeta(
        label="Bar",
        question="Which values of this field occur most — top talkers, noisiest artifacts?",
        scales=("nominal", "ordinal"),
        data_kind="terms",
        default_scale="nominal",
        inputs={"field": "required"},
        derives=("bins", "time_part"),
        reads_options=("top_n", "orientation", "sort", "log_scale"),
        supports_compare=True,
    ),
    "pie": ChartMeta(
        label="Pie / Donut",
        question="What share of the whole does each value hold, for a handful of values?",
        scales=("nominal",),
        data_kind="terms",
        default_scale="nominal",
        inputs={"field": "required"},
        reads_options=("top_n",),
        note=(
            "pie/box/violin/ecdf have no honest two-layer encoding, so they are "
            "left without supportsCompare — the rail hides Compare for them."
        ),
    ),
    "waffle": ChartMeta(
        label="Waffle (10×10 share grid)",
        question="What share of the whole does each value hold, counted in cells?",
        scales=("nominal",),
        data_kind="terms",
        default_scale="nominal",
        inputs={"field": "required"},
        reads_options=("top_n",),
        note=(
            "Same terms aggregation as bar/pie — switching between them refetches "
            "nothing. Preferred over pie once there are five or more categories: "
            "counting cells beats judging angles."
        ),
    ),
    "heatmap": ChartMeta(
        label="Heatmap (value × time)",
        question="Which values of this field were active when?",
        scales=("nominal", "ordinal", "interval"),
        data_kind="timeseries",
        default_scale="nominal",
        inputs={"field": "required"},
        derives=("bins", "time_part"),
        reads_options=("top_n", "buckets"),
    ),
    "line": ChartMeta(
        label="Line / Area (value × time)",
        question="How do this field's top values shift over the investigation window?",
        scales=("interval", "ratio"),
        data_kind="timeseries",
        default_scale="ratio",
        inputs={"field": "required"},
        supports_marks=True,
        reads_options=("top_n", "buckets", "series_mode", "legend", "show_points"),
        note=(
            "show_points marks the actual measured buckets. Graphical integrity "
            "(Tufte): a line between two points asserts values that were never "
            "measured — markers show where the data really is."
        ),
    ),
    "histogram": ChartMeta(
        label="Histogram",
        question="How is this number distributed — one hump, a long tail, a hard cap?",
        scales=("interval", "ratio"),
        data_kind="numeric",
        default_scale="ratio",
        inputs={"field": "required"},
        reads_options=("bins", "log_scale", "show_density"),
        supports_compare=True,
    ),
    "box": ChartMeta(
        label="Box plot",
        question="How does this number's distribution differ between groups?",
        scales=("ratio",),
        data_kind="numeric",
        default_scale="ratio",
        inputs={"field": "required", "second_field": "optional"},
        reads_options=("bins", "groups", "show_points"),
        note=(
            "box/violin accept an OPTIONAL second field (accepts_second_field): a "
            "categorical grouping variable, giving one box/violin per top group."
        ),
    ),
    "violin": ChartMeta(
        label="Violin plot",
        question="How is this number distributed within each group, including its shape?",
        scales=("ratio",),
        data_kind="numeric",
        default_scale="ratio",
        inputs={"field": "required", "second_field": "optional"},
        reads_options=("bins", "groups", "show_points"),
    ),
    "ecdf": ChartMeta(
        label="ECDF",
        question="What fraction of events fall at or below a given value?",
        scales=("ratio",),
        data_kind="numeric",
        default_scale="ratio",
        inputs={"field": "required"},
        reads_options=("bins",),
    ),
    "punchcard": ChartMeta(
        label="Punch card (day × hour)",
        question="On which weekdays and hours does activity recur — and what happens off-hours?",
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="punchcard",
        default_scale="nominal",
        inputs={},
        note="Field-free like `time` — meaningful whatever the picked field's scale is.",
    ),
    "cumulative": ChartMeta(
        label="Cumulative step (running total over time)",
        question="How did the total grow — steadily, in bursts, or all at once — and when?",
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="cumulative",
        default_scale="nominal",
        inputs={"field": "optional"},
        reads_options=("buckets", "quantity"),
        supports_marks=True,
        note=(
            "Three quantities, chosen by `quantity` or resolved from the field: no "
            "field → running event count; a measure (ratio) → running sum; categories "
            "→ distinct values seen so far. Drawn as a step, never interpolated. No "
            "Compare: two cumulatives on one axis is a shared-axis trap."
        ),
    ),
    "calendar": ChartMeta(
        label="Calendar heatmap (events per day)",
        question="Which days carried the activity — weekdays, weekends, one-off spikes?",
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="calendar",
        default_scale="nominal",
        inputs={"field": "optional"},
        note=(
            "One cell per UTC day, weeks as columns; with a field, a day counts the "
            "events whose field is non-empty. Capped at 53 weeks, latest kept, "
            "disclosed in the caption."
        ),
    ),
    "change": ChartMeta(
        label="Ranked change (share of window, two windows)",
        question="Which values gained or lost share between the reference window and this one — and which appeared or disappeared?",
        scales=("nominal", "ordinal"),
        data_kind="change",
        default_scale="nominal",
        inputs={"field": "required"},
        derives=("bins", "time_part"),
        reads_options=("top_n", "layout"),
        supports_compare=True,
        requires_compare=True,
        note=(
            "Top-N values of each window, unioned; per value the share of its own "
            "window in both, ranked by |Δ share|. Share, never count — the windows "
            "are rarely the same size. Compare is required: baseline (the whole "
            "timeline) or custom filters name the reference window."
        ),
    ),
    "lanes": ChartMeta(
        label="Interval lanes (one lane per value, bars from start to end)",
        question="How long did each value's activity run — which runs overlap, which never ended, which ended without a start?",
        scales=("nominal", "ordinal"),
        data_kind="lanes",
        default_scale="nominal",
        inputs={
            "field": "required",
            "pairing": "optional",
            "start_filter": "optional",
            "end_filter": "optional",
        },
        reads_options=("limit_y",),
        supports_marks=True,
        note=(
            "The charted field is the lane key. first_last: one bar per lane from its "
            "first to its last event. next_end: start_filter and end_filter name the "
            "events that open and close an interval; an end closes the most recent open "
            "start in its lane, an open start runs to the slice end, an orphan end is "
            "counted, not drawn. Lanes capped by event count, rows capped and disclosed."
        ),
    ),
    "pivot": ChartMeta(
        label="Heatmap (field × field)",
        question="How often does each pair of values from two fields occur together?",
        scales=("nominal", "ordinal"),
        data_kind="pivot",
        default_scale="nominal",
        inputs={"field": "required", "second_field": "required"},
        derives=("bins", "time_part"),
        reads_options=("limit_x", "limit_y"),
        note=(
            "pivot and sankey are two marks over the SAME field×field "
            "aggregation — switching between them refetches nothing."
        ),
    ),
    "sankey": ChartMeta(
        label="Flow / Sankey (field × field)",
        question="How do events flow from the values of one field to the values of another?",
        scales=("nominal", "ordinal"),
        data_kind="pivot",
        default_scale="nominal",
        inputs={"field": "required", "second_field": "required"},
        derives=("bins", "time_part"),
        reads_options=("limit_x", "limit_y"),
    ),
    "scatter": ChartMeta(
        label="Scatter (numeric × numeric)",
        question="Are these two numbers related — a line, a cluster, an outlier?",
        scales=("interval", "ratio"),
        data_kind="scatter",
        default_scale="ratio",
        inputs={"field": "required", "second_field": "required"},
        reads_options=("sample_limit", "log_scale"),
    ),
    "corr": ChartMeta(
        label="Correlation matrix (numeric fields)",
        question="Which of these numeric fields move together?",
        # Available under every scale like the field-free marks: this chart
        # ignores the currently-picked field entirely (its own `fields` list
        # is what it charts), so the picked field's scale says nothing about
        # whether it is legal.
        scales=("nominal", "ordinal", "interval", "ratio"),
        data_kind="corr",
        default_scale="ratio",
        inputs={"fields": "required"},
        note=(
            "Takes `fields` (2-8 numeric tokens) instead of field/field_y — the "
            "one mark that charts more than two fields at once. Preferred over "
            "reading scatter plots one pair at a time past three or four "
            "quantitative variables."
        ),
    ),
    "table": ChartMeta(
        label="Table (values with counts)",
        question="Which values occur, how often, what share is that, and when were they first and last seen?",
        scales=("nominal", "ordinal"),
        data_kind="table",
        default_scale="nominal",
        inputs={"field": "required", "second_field": "optional", "columns": "optional"},
        derives=("bins", "time_part"),
        reads_options=("top_n", "table_sort_by", "table_sort_dir", "highlight"),
        note=(
            "The one figure that is a table: top-N values with count, share, first/last "
            "seen and, given field_y, the number of distinct field_y values per row. A "
            "remainder row is always present when values were cut. Exports as CSV too."
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
    """False for the field-free marks (time, punchcard) and the field-optional ones."""
    kind = CHART_META[chart_type].data_kind
    return kind not in FIELD_FREE_DATA_KINDS and kind not in FIELD_OPTIONAL_DATA_KINDS


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


def input_requirement(chart_type: ChartType, key: InputKey) -> Requirement | None:
    """Whether *chart_type* asks for *key*, and how hard — None when it does not."""
    return CHART_META[chart_type].inputs.get(key)


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
