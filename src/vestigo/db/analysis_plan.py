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

#: The methods that select fields for themselves, and so the only ones a
#: timeline's ``field_overrides`` can steer. The other four take no field
#: selection to steer: ``frequency`` and ``sequence_novelty`` take a single
#: ``series_field`` the analyst names outright, ``timestamp_order`` reads no
#: field at all, and ``log_template`` clusters the message text. A declaration
#: stored against one of those would be audited, rendered as "declared" and
#: then quietly apply to nothing — which is the same lie an unknown method id
#: tells, so the field-overrides endpoint rejects both.
FIELD_OVERRIDE_METHOD_IDS: frozenset[str] = frozenset(
    {
        "value_novelty",
        "value_combo",
        "numeric_range",
        "charset",
        "entropy",
        "proportion_shift",
        "value_distribution_drift",
        "interval_periodicity",
    }
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
    #: How many field tokens ``numeric_tokens_from_stats`` actually tested. The
    #: denominator the gate quotes must be the one its numerator came from —
    #: ``len(inventory)`` counts a different, capped population, and
    #: ``reason_facts`` exists to be checked.
    numeric_tokens_examined: int
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


@dataclass(frozen=True)
class NumericTokenScan:
    """Which tokens qualified as numeric, and how many were tested.

    Both halves are returned together because the gate quotes them as a
    fraction. Deriving the denominator from anywhere else — the merged
    inventory, say, which caps attribute keys and appends canonical entries —
    produces a "0 of 19" whose 19 was never the population the 0 came from.
    """

    tokens: list[str]
    examined: int


def numeric_tokens_from_stats(
    stats: dict[str, tuple[int, dict[str, Any]]], min_ratio: float
) -> NumericTokenScan:
    """Return the field tokens whose sampled values are at least *min_ratio* numeric.

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
    return NumericTokenScan(
        tokens=sorted(
            token
            for token, (hit, total) in ratios.items()
            if total > 0 and hit / total >= min_ratio
        ),
        examined=len(ratios),
    )


def series_distinct_from_stats(
    stats: dict[str, tuple[int, dict[str, Any]]], token: str, inventory_distinct: int
) -> int:
    """Estimate a field's distinct count across sources, for the series-field gate.

    ``merged_inventory`` merges ``distinct`` as **max-across-sources** — a
    documented approximation, since union cardinality is not derivable from
    per-source counts (see ``db/field_stats.py``). For the series field that
    approximation is not merely imprecise, it is biased in the harmful
    direction: a timeline whose sources each carry one artifact type reports
    ``distinct = 1`` however many types the timeline holds in total, and the
    gate would then stop offering the sequence methods on data they work on.

    Unioning the per-source *sampled* top values recovers the common case
    exactly (low-cardinality fields like ``artifact`` fit well inside the
    sample), and the max-merged value is kept as a floor for the case where the
    sample truncated. Both are lower bounds, so the result can still understate
    a very wide field — which only matters against a threshold of a few values.
    """
    section = "attributes" if token.startswith("attr:") else "top_level"
    lookup = token[len("attr:") :] if token.startswith("attr:") else token
    seen: set[str] = set()
    for _total, payload in stats.values():
        entry = (payload.get(section) or {}).get(lookup) or {}
        for raw, _count in entry.get("values") or []:
            seen.add(str(raw))
    return max(len(seen), inventory_distinct)


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

    # Always applicable: these work on any timeline that has events at all, so
    # a precondition could only ever be wrong.
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
                "sampled": inputs.numeric_tokens_examined,
                "threshold": cfg.analysis_gate_min_numeric_ratio,
            },
        )
    )

    # Charset learns each field's alphabet *from the field's own values*. When
    # every field is enum-like, the learned alphabet is the union of a handful
    # of literals and every value is drawn from it — so a never-seen character
    # cannot occur by construction. That is a structural impossibility, which is
    # the only thing this gate is allowed to act on.
    #
    # Entropy deliberately does NOT share it. Its band is learned across values
    # and an outlier is measured against that band, so one of five enum literals
    # can still sit far outside it — a base64 blob among four short words scores
    # exactly as it should. Gating it here marked a scoreable method
    # not_applicable, which is the silent failure this module exists to avoid.
    enum_only = max_distinct <= cfg.analysis_gate_max_enum_distinct
    plans["charset"] = (
        _no(
            "charset",
            f"every field is enum-like (at most {cfg.analysis_gate_max_enum_distinct} distinct values), so no character can be novel",
            {"max_distinct": max_distinct, "threshold": cfg.analysis_gate_max_enum_distinct},
        )
        if enum_only
        else _ok("charset")
    )
    plans["entropy"] = _ok("entropy")

    # One bucket cannot be an outlier against itself, and buckets narrower than
    # a second collapse into each other. The threshold is in *seconds of span*,
    # not in buckets — the detector always splits the span into
    # `stat_frequency_buckets` of them, so both numbers are quoted rather than
    # letting the reason imply the setting names the divisor.
    needed = cfg.analysis_gate_min_frequency_buckets
    plans["frequency"] = (
        _ok("frequency")
        if inputs.span_seconds >= float(needed)
        else _no(
            "frequency",
            f"span is under {needed}s, too short to bucket",
            {
                "span_seconds": inputs.span_seconds,
                "required_seconds": needed,
                "bucket_count": cfg.stat_frequency_buckets,
            },
        )
    )

    # Two-window tests: no second window, no test. Recoverable by an analyst
    # action, so needs_setup rather than not_applicable. `interval_periodicity`
    # and `sequence_novelty` belong here too — both return `insufficient_data`
    # the moment their window pair is missing, so their own data-shape gates
    # below only ever get to speak when a baseline exists. Gating them on shape
    # alone used to count them as applicable in an unbaselined frame, run them,
    # and render a dash where the "Set a baseline" button belongs.
    needs_windows = (
        "proportion_shift",
        "value_distribution_drift",
        "interval_periodicity",
        "sequence_novelty",
    )
    for method in needs_windows:
        if not two_windows:
            plans[method] = _setup(
                method,
                "needs a baseline window to compare against",
                {"frame": inputs.frame, "has_active_baseline": inputs.has_active_baseline},
            )
    for method in ("proportion_shift", "value_distribution_drift"):
        plans.setdefault(method, _ok(method))

    plans.setdefault(
        "interval_periodicity",
        _ok("interval_periodicity")
        if per_series >= cfg.analysis_gate_min_interval_periods
        else _no(
            "interval_periodicity",
            "too few repeats per series value to fit a cadence",
            {
                "events_per_series_value": per_series,
                "required": cfg.analysis_gate_min_interval_periods,
            },
        ),
    )

    # A single distinct series value yields exactly one n-gram, repeated: no
    # ordering can be novel against a reference set containing only itself.
    # Two values already yield 2**n distinct n-grams, and a rare one among them
    # is perfectly scoreable — so the floor is 2, not "enough values to look
    # interesting". A two-artifact-type timeline is an ordinary two-source case,
    # and it used to lose this method silently.
    plans.setdefault(
        "sequence_novelty",
        _ok("sequence_novelty")
        if inputs.series_distinct >= cfg.analysis_gate_min_series_distinct
        else _no(
            "sequence_novelty",
            "the series field holds one value, so every n-gram is the same one",
            {
                "series_distinct": inputs.series_distinct,
                "required": cfg.analysis_gate_min_series_distinct,
            },
        ),
    )

    # Log templating clusters the `message` materialized column, which is part
    # of the events schema and therefore always present. There is no data shape
    # that makes it structurally unable to produce a template, so gating it
    # could only ever be wrong — as it was: `message` is deliberately absent
    # from _NOVELTY_CANDIDATE_TOP_LEVEL (it is free text, a poor novelty
    # candidate), so an inventory-based precondition never matched anywhere.
    plans["log_template"] = _ok("log_template")

    return [plans[m] for m in METHOD_IDS]
