"""The analysis gate: which methods can produce a finding on this data.

Pure predicates over a snapshot of what a timeline contains — no ClickHouse, no
Postgres, no FastAPI. The snapshot itself comes from the per-source
``field_stats`` cache plus one timestamp-range probe, both already paid for
elsewhere, so planning costs nothing an analyst can feel.

The contract is deliberately narrow. A method is ``not_applicable`` **only**
when it structurally cannot produce a finding on this data: no numeric field
for the numeric-range band to learn, no second window for a two-window test, a
span too short to separate one bucket from the next. It is never gated off for
being *unlikely* to find something interesting — that judgement belongs to the
analyst, and the UI keeps every gated method one click away with the arithmetic
on screen.

``needs_setup`` is the third status and is not a weaker ``not_applicable``: it
means an analyst action (declaring a baseline) makes the method applicable, so
the UI offers that action instead of a "run anyway" escape hatch.

Consequence worth stating: a gated verdict is a claim about the *data*, not
about the method. If one of these predicates is wrong, the failure is silent —
a method that would have found something is simply not offered. That is why
``tests/test_demo_detector_coverage_clickhouse.py`` asserts the gate never
skips a method that file proves finds something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vestigo.core.config import Settings

#: Every method the Investigate rail streams, in gate-report order. The
#: ``sequence_motif`` miner is deliberately absent: it is discovery ("what is
#: routine here?"), not detection, and it suppresses findings rather than
#: raising them.
METHOD_IDS: tuple[str, ...] = (
    "value_novelty",
    "value_combo",
    "numeric_range",
    "charset",
    "entropy",
    "frequency",
    "proportion_shift",
    "value_distribution_drift",
    "interval_periodicity",
    "timestamp_order",
    "sequence_novelty",
    "log_template",
)

#: Cost class per method, used only to order the streaming run cheapest-first.
#: "heavy" means the method takes a ``HEAVY_SCAN_GATE`` slot in
#: ``db/anomaly_stats.py`` — the gate's width is what makes a full sweep add up
#: to a wait rather than to the slowest single scan.
COST_CLASS: dict[str, str] = {
    "value_novelty": "cheap",
    "value_combo": "cheap",
    "timestamp_order": "cheap",
    "numeric_range": "heavy",
    "charset": "heavy",
    "entropy": "heavy",
    "frequency": "heavy",
    "proportion_shift": "heavy",
    "value_distribution_drift": "heavy",
    "interval_periodicity": "heavy",
    "sequence_novelty": "heavy",
    "log_template": "heavy",
}

#: Top-level columns whose content is free text rather than a category — what
#: log templating clusters over.
_MESSAGE_TOKENS: tuple[str, ...] = ("message", "display_name")


@dataclass(frozen=True)
class PlanInputs:
    """Everything the gate is allowed to look at.

    ``inventory`` is ``merged_inventory``'s output: ``(token, distinct,
    coverage)`` triples. ``span_seconds`` is the timeline's dated range;
    ``series_distinct`` is the distinct count of the field the sequence and
    interval methods group by.
    """

    inventory: list[tuple[str, int, int]]
    numeric_tokens: list[str]
    message_tokens: list[str]
    series_distinct: int
    events_total: int
    span_seconds: float
    frame: str
    has_active_baseline: bool


@dataclass(frozen=True)
class MethodPlan:
    """One method's verdict, with the arithmetic behind it.

    ``reason_facts`` exists so the UI can render the numbers rather than a
    canned sentence: "no field parses as numeric (0 of 19 sampled ≥ 90%)" is
    inspectable and arguable; "not applicable" is a shrug.
    """

    method: str
    status: str
    reason: str
    cost_class: str
    reason_facts: dict[str, Any] = field(default_factory=dict)


def numeric_tokens_from_stats(
    stats: dict[str, tuple[int, dict[str, Any]]], min_ratio: float
) -> list[str]:
    """Return field tokens whose sampled values are at least *min_ratio* numeric.

    Reads the per-field sampled top values already in the ``field_stats``
    payload rather than issuing ``_numeric_ratio_probe``'s ClickHouse scan.
    Sampling makes this an approximation in the same acceptance class as the
    payload's ``distinct`` max-merge: it decides whether to *offer* a method,
    and a method it declines to offer is still one click from running.

    Ratios are merged across sources before the comparison, not decided per
    source: a field that is numeric in one source and free text in another is
    not a numeric field, and per-source decisions would call it one.
    """
    ratios: dict[str, tuple[float, float]] = {}
    for _total, payload in stats.values():
        for section, prefix in (("top_level", ""), ("attributes", "attr:")):
            for key, entry in (payload.get(section) or {}).items():
                values = entry.get("values") or []
                if not values:
                    continue
                numeric = 0.0
                seen = 0.0
                for raw, count in values:
                    seen += count
                    try:
                        float(raw)
                    except (TypeError, ValueError):
                        continue
                    numeric += count
                token = f"{prefix}{key}"
                hit, total = ratios.get(token, (0.0, 0.0))
                ratios[token] = (hit + numeric, total + seen)
    return sorted(
        token for token, (hit, total) in ratios.items() if total > 0 and hit / total >= min_ratio
    )


def message_tokens_from_inventory(inventory: list[tuple[str, int, int]]) -> list[str]:
    """Return the free-text tokens present in *inventory*, in _MESSAGE_TOKENS order."""
    present = {token for token, _distinct, coverage in inventory if coverage > 0}
    return [t for t in _MESSAGE_TOKENS if t in present]


def _categorical(inputs: PlanInputs) -> list[tuple[str, int, int]]:
    """Fields with at least two distinct values — anything a value method can rank."""
    return [row for row in inputs.inventory if row[1] >= 2]


def _ok(method: str) -> MethodPlan:
    return MethodPlan(method=method, status="applicable", reason="", cost_class=COST_CLASS[method])


def _no(method: str, reason: str, facts: dict[str, Any]) -> MethodPlan:
    return MethodPlan(
        method=method,
        status="not_applicable",
        reason=reason,
        cost_class=COST_CLASS[method],
        reason_facts=facts,
    )


def _setup(method: str, reason: str, facts: dict[str, Any]) -> MethodPlan:
    return MethodPlan(
        method=method,
        status="needs_setup",
        reason=reason,
        cost_class=COST_CLASS[method],
        reason_facts=facts,
    )


def build_plan(inputs: PlanInputs, cfg: Settings) -> list[MethodPlan]:
    """Return one :class:`MethodPlan` per entry in :data:`METHOD_IDS`, in order."""
    cats = _categorical(inputs)
    max_distinct = max((d for _t, d, _c in inputs.inventory), default=0)
    two_windows = inputs.frame == "baseline" and inputs.has_active_baseline
    per_series = inputs.events_total // max(inputs.series_distinct, 1)
    plans: dict[str, MethodPlan] = {}

    # Always applicable: both work on any timeline that has events at all, so a
    # precondition could only ever be wrong.
    plans["value_novelty"] = _ok("value_novelty")
    plans["timestamp_order"] = _ok("timestamp_order")

    plans["value_combo"] = (
        _ok("value_combo")
        if len(cats) >= 2
        else _no(
            "value_combo",
            "only one usable categorical field — a pair needs two",
            {"categorical_fields": len(cats), "required": 2},
        )
    )

    plans["numeric_range"] = (
        _ok("numeric_range")
        if inputs.numeric_tokens
        else _no(
            "numeric_range",
            "no field parses as numeric",
            {
                "numeric_fields": 0,
                "sampled": len(inputs.inventory),
                "threshold": cfg.analysis_gate_min_numeric_ratio,
            },
        )
    )

    # Charset and entropy both learn a per-field model of value *shape*. An
    # enum-like field has no shape to learn: every value is one of a handful of
    # literals, so a never-seen character or an out-of-band entropy cannot occur
    # by construction.
    enum_only = max_distinct <= cfg.analysis_gate_max_enum_distinct
    for method in ("charset", "entropy"):
        plans[method] = (
            _no(
                method,
                f"every field is enum-like (at most {cfg.analysis_gate_max_enum_distinct} distinct values)",
                {"max_distinct": max_distinct, "threshold": cfg.analysis_gate_max_enum_distinct},
            )
            if enum_only
            else _ok(method)
        )

    # One bucket cannot be an outlier against itself, and buckets narrower than
    # a second collapse into each other.
    needed = cfg.analysis_gate_min_frequency_buckets
    plans["frequency"] = (
        _ok("frequency")
        if inputs.span_seconds >= float(needed)
        else _no(
            "frequency",
            f"span is too short to fill {needed} buckets",
            {"span_seconds": inputs.span_seconds, "required_buckets": needed},
        )
    )

    # Two-window tests: no second window, no test. Recoverable by an analyst
    # action, so needs_setup rather than not_applicable.
    for method in ("proportion_shift", "value_distribution_drift"):
        plans[method] = (
            _ok(method)
            if two_windows
            else _setup(
                method,
                "needs a baseline window to compare against",
                {"frame": inputs.frame, "has_active_baseline": inputs.has_active_baseline},
            )
        )

    plans["interval_periodicity"] = (
        _ok("interval_periodicity")
        if per_series >= cfg.analysis_gate_min_interval_periods
        else _no(
            "interval_periodicity",
            "too few repeats per series value to fit a cadence",
            {
                "events_per_series_value": per_series,
                "required": cfg.analysis_gate_min_interval_periods,
            },
        )
    )

    plans["sequence_novelty"] = (
        _ok("sequence_novelty")
        if inputs.series_distinct >= cfg.analysis_gate_min_series_distinct
        else _no(
            "sequence_novelty",
            "series field has too few distinct values for n-grams to differ",
            {
                "series_distinct": inputs.series_distinct,
                "required": cfg.analysis_gate_min_series_distinct,
            },
        )
    )

    plans["log_template"] = (
        _ok("log_template")
        if inputs.message_tokens
        else _no(
            "log_template",
            "no free-text message field to cluster",
            {"message_fields": 0},
        )
    )

    return [plans[m] for m in METHOD_IDS]
