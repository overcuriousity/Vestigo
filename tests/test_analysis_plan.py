"""Gate predicate tests — the structural preconditions, at their boundaries.

The gate's contract is narrow and load-bearing: ``not_applicable`` means the
method *cannot* produce a finding on this data, never that it is unlikely to.
Every test here pins one boundary of one predicate, because a predicate that
drifts one value in the wrong direction silently stops offering a method that
would have found something.
"""

from __future__ import annotations

import pytest

from vestigo.core.config import get_settings
from vestigo.db.analysis_plan import (
    METHOD_IDS,
    MethodPlan,
    PlanInputs,
    build_plan,
    message_tokens_from_inventory,
    numeric_tokens_from_stats,
)


@pytest.fixture()
def cfg():
    get_settings.cache_clear()
    return get_settings()


def _inputs(**over) -> PlanInputs:
    """A timeline every method is applicable on, minus whatever the test removes."""
    base = {
        "inventory": [("artifact", 5, 1000), ("message", 900, 1000), ("attr:src_ip", 41, 1000)],
        "numeric_tokens": ["attr:bytes_out"],
        "message_tokens": ["message"],
        "series_distinct": 8,
        "events_total": 1000,
        "span_seconds": 86_400.0,
        "frame": "baseline",
        "has_active_baseline": True,
    }
    base.update(over)
    return PlanInputs(**base)


def _by_id(plans: list[MethodPlan]) -> dict[str, MethodPlan]:
    return {p.method: p for p in plans}


def test_plan_covers_every_method_exactly_once(cfg):
    plans = build_plan(_inputs(), cfg)
    assert [p.method for p in plans] == list(METHOD_IDS)


def test_rich_timeline_leaves_everything_applicable(cfg):
    plans = _by_id(build_plan(_inputs(), cfg))
    assert all(p.status == "applicable" for p in plans.values())


def test_numeric_range_gated_off_without_numeric_fields(cfg):
    plan = _by_id(build_plan(_inputs(numeric_tokens=[]), cfg))["numeric_range"]
    assert plan.status == "not_applicable"
    assert plan.reason_facts == {"numeric_fields": 0, "sampled": 3, "threshold": 0.9}


def test_two_window_methods_need_setup_in_self_frame(cfg):
    plans = _by_id(build_plan(_inputs(frame="self", has_active_baseline=False), cfg))
    for method in ("proportion_shift", "value_distribution_drift"):
        assert plans[method].status == "needs_setup"
        assert "baseline" in plans[method].reason


def test_two_window_methods_applicable_once_a_baseline_is_active(cfg):
    plans = _by_id(build_plan(_inputs(frame="baseline", has_active_baseline=True), cfg))
    assert plans["proportion_shift"].status == "applicable"
    assert plans["value_distribution_drift"].status == "applicable"


def test_charset_and_entropy_gated_off_on_enum_only_fields(cfg):
    enum_only = [("artifact", 3, 1000), ("attr:level", 4, 1000)]
    plans = _by_id(build_plan(_inputs(inventory=enum_only, message_tokens=[]), cfg))
    for method in ("charset", "entropy"):
        assert plans[method].status == "not_applicable"
        assert plans[method].reason_facts["max_distinct"] == 4
        assert plans[method].reason_facts["threshold"] == 5


def test_charset_applicable_one_value_above_the_enum_ceiling(cfg):
    """Boundary: distinct == threshold is enum-like, threshold + 1 is not."""
    fields = [("artifact", 3, 1000), ("attr:path", 6, 1000)]
    plans = _by_id(build_plan(_inputs(inventory=fields), cfg))
    assert plans["charset"].status == "applicable"


def test_value_combo_needs_two_categorical_fields(cfg):
    plans = _by_id(build_plan(_inputs(inventory=[("artifact", 5, 1000)]), cfg))
    assert plans["value_combo"].status == "not_applicable"
    assert plans["value_combo"].reason_facts == {"categorical_fields": 1, "required": 2}


def test_frequency_gated_off_on_a_span_under_the_bucket_minimum(cfg):
    """A span shorter than one second per bucket cannot separate the buckets."""
    plans = _by_id(build_plan(_inputs(span_seconds=8.0), cfg))
    assert plans["frequency"].status == "not_applicable"
    assert plans["frequency"].reason_facts["span_seconds"] == 8.0
    assert plans["frequency"].reason_facts["required_buckets"] == 12


def test_interval_gated_off_without_enough_events_per_series_value(cfg):
    """8 series values × 3 periods needs 24 events; 20 cannot supply it."""
    plans = _by_id(build_plan(_inputs(events_total=20), cfg))
    assert plans["interval_periodicity"].status == "not_applicable"
    assert plans["interval_periodicity"].reason_facts["events_per_series_value"] == 2


def test_sequence_gated_off_below_the_series_distinct_minimum(cfg):
    plans = _by_id(build_plan(_inputs(series_distinct=2), cfg))
    assert plans["sequence_novelty"].status == "not_applicable"
    assert plans["sequence_novelty"].reason_facts == {"series_distinct": 2, "required": 3}


def test_log_template_gated_off_without_a_message_field(cfg):
    plans = _by_id(build_plan(_inputs(message_tokens=[]), cfg))
    assert plans["log_template"].status == "not_applicable"


def test_novelty_and_order_are_never_gated_off(cfg):
    """Both work on any timeline with events; gating them could only ever be wrong."""
    thin = _inputs(
        inventory=[("artifact", 2, 10)],
        numeric_tokens=[],
        message_tokens=[],
        series_distinct=1,
        events_total=10,
        span_seconds=1.0,
        frame="self",
        has_active_baseline=False,
    )
    plans = _by_id(build_plan(thin, cfg))
    assert plans["value_novelty"].status == "applicable"
    assert plans["timestamp_order"].status == "applicable"


def test_every_plan_carries_a_cost_class(cfg):
    for plan in build_plan(_inputs(), cfg):
        assert plan.cost_class in {"cheap", "heavy"}


def test_numeric_tokens_read_sampled_values_not_a_clickhouse_probe():
    stats = {
        "src-1": (
            100,
            {
                "top_level": {},
                "attributes": {
                    "bytes_out": {
                        "distinct": 90,
                        "coverage": 100,
                        "values": [["1024", 40], ["2048", 40], ["4096", 20]],
                    },
                    "user_agent": {
                        "distinct": 12,
                        "coverage": 100,
                        "values": [["curl/7.68.0", 60], ["Mozilla/5.0", 40]],
                    },
                },
            },
        )
    }
    assert numeric_tokens_from_stats(stats, 0.9) == ["attr:bytes_out"]


def test_numeric_tokens_reject_a_mostly_numeric_field_below_the_ratio():
    stats = {
        "src-1": (
            100,
            {
                "top_level": {},
                "attributes": {
                    "mixed": {
                        "distinct": 3,
                        "coverage": 100,
                        "values": [["1", 80], ["2", 5], ["n/a", 15]],
                    },
                },
            },
        )
    }
    assert numeric_tokens_from_stats(stats, 0.9) == []


def test_numeric_tokens_merge_the_ratio_across_sources():
    """Per-source lists are partial views; the decision is over their sum."""
    stats = {
        "src-1": (
            50,
            {"top_level": {}, "attributes": {"port": {"values": [["443", 50]]}}},
        ),
        "src-2": (
            50,
            {"top_level": {}, "attributes": {"port": {"values": [["n/a", 50]]}}},
        ),
    }
    assert numeric_tokens_from_stats(stats, 0.9) == []


def test_message_tokens_pick_the_free_text_columns():
    inventory = [("artifact", 5, 100), ("message", 98, 100), ("attr:src_ip", 12, 100)]
    assert message_tokens_from_inventory(inventory) == ["message"]
