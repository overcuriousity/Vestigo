"""Derivations are a change of scale computed before aggregation. These pin
the pure parts: the spec's validation, the bin-edge maths, the labels, and
the shape of the SQL — `test_derive_clickhouse.py` proves the SQL means it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vestigo.db.derive import (
    TIME_PART_TOKENS,
    DeriveSpec,
    ResolvedDerive,
    bin_edges,
    bin_labels,
    bins_expr,
    parse_derive,
    time_part_expr,
)


def test_bins_width_edges_are_interior_and_equal() -> None:
    assert bin_edges("width", 4, 0.0, 100.0) == [25.0, 50.0, 75.0]


def test_bins_log_edges_are_log_spaced() -> None:
    assert bin_edges("log", 3, 1.0, 1000.0) == pytest.approx([10.0, 100.0])


def test_bins_degenerate_range_has_no_edges() -> None:
    assert bin_edges("width", 8, 5.0, 5.0) == []
    assert bin_edges("log", 8, 5.0, 5.0) == []


def test_bins_edges_are_strictly_increasing_when_float64_runs_out() -> None:
    """A range narrow relative to its magnitude cannot carry ``count - 1`` edges.

    ``lo + k * step`` is absorbed back to ``lo`` for the first few *k* and then
    repeats itself — an epoch-nanosecond attribute binned over a few hours. Every
    consumer takes the edges to be strictly increasing: duplicate labels are one
    bin in the result and several in the caption, ``bins_expr`` emits arms no row
    can reach and ``label_order_expr`` collapses the repeats onto one rank. So
    the collapsed bins are dropped rather than passed on.
    """
    lo = 1.7e18
    edges = bin_edges("width", 50, lo, lo + 10_000)
    assert edges == sorted(set(edges))
    assert all(lo < e < lo + 10_000 for e in edges)
    assert len(edges) < 49  # fewer than asked for — the point of the test
    assert len(set(bin_labels(edges, negative_bin=False))) == len(edges) + 1


def test_bin_labels_are_open_ended_and_human() -> None:
    assert bin_labels([1024.0, 10240.0], negative_bin=False) == [
        "< 1,024",
        "1,024 – 10,240",
        "≥ 10,240",
    ]
    assert bin_labels([1024.0], negative_bin=True) == ["≤ 0", "< 1,024", "≥ 1,024"]
    assert bin_labels([], negative_bin=False) == ["all values"]


def test_bin_labels_name_the_edge_they_delimit() -> None:
    """Three significant digits is the readable default, but a label is what
    the reader checks a value against — ``< 0.123`` over a boundary at
    0.123456 puts 0.1234 on the wrong side of its own label."""
    assert bin_labels([0.123456, 2.5], negative_bin=False) == [
        "< 0.123456",
        "0.123456 – 2.5",
        "≥ 2.5",
    ]
    # Whole numbers keep the readable form — three digits already name them.
    assert bin_labels([1024.0, 10240.0], negative_bin=False) == [
        "< 1,024",
        "1,024 – 10,240",
        "≥ 10,240",
    ]


def test_bin_labels_stop_at_six_decimals_for_an_irrational_edge() -> None:
    """A log-spaced edge has no finite decimal, so the fidelity escalation is
    capped — six places name it to a relative 1e-6 and stay readable."""
    assert bin_labels(bin_edges("log", 3, 1.0, 10.0), negative_bin=False) == [
        "< 2.154435",
        "2.154435 – 4.641589",
        "≥ 4.641589",
    ]


def test_bins_expr_is_a_multi_if_over_the_cast_value() -> None:
    sql = bins_expr("v", [1024.0, 10240.0], negative_bin=True)
    assert sql.startswith("multiIf(isNull(v), '', v <= 0, '≤ 0', v < 1024.0, '< 1,024', ")
    assert sql.endswith("'≥ 10,240')")


def test_time_part_expr_parses_then_reuses_the_time_field_spec() -> None:
    sql = time_part_expr("attributes[{field_key:String}]", "hour")
    assert "parseDateTimeBestEffortOrNull(toString(attributes[{field_key:String}]), 'UTC')" in sql
    assert "toHour(" in sql and "'UTC'" in sql
    assert sql.startswith("ifNull(")


def test_time_part_tokens_cover_every_part() -> None:
    assert set(TIME_PART_TOKENS) == {"hour", "weekday", "day", "week", "month"}
    assert TIME_PART_TOKENS["weekday"] == "time:day_of_week"


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind":"bins"}',
        '{"kind":"bins","mode":"width"}',
        '{"kind":"bins","mode":"width","count":1}',
        '{"kind":"bins","mode":"custom","edges":[3,1]}',
        '{"kind":"bins","mode":"custom","edges":[]}',
        '{"kind":"time_part"}',
        '{"kind":"time_part","part":"minute"}',
        '{"kind":"bins","mode":"log","count":8,"part":"hour"}',
        "not json",
    ],
)
def test_parse_derive_rejects_malformed_specs(raw: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        parse_derive(raw)


def test_parse_derive_accepts_the_three_shapes() -> None:
    assert parse_derive(None) is None
    assert parse_derive('{"kind":"bins","mode":"log","count":8}') == DeriveSpec(
        kind="bins", mode="log", count=8
    )
    assert parse_derive('{"kind":"bins","mode":"custom","edges":[0,1024]}').edges == [0.0, 1024.0]
    assert parse_derive('{"kind":"time_part","part":"weekday"}').part == "weekday"


def test_bin_labels_never_collide_when_three_significant_digits_cannot_tell_edges_apart() -> None:
    """Sub-integer steps above 1,000 rounded to the same label, so `multiIf`
    emitted identical literals and GROUP BY merged bins the labels list still
    named twice — the labels take whatever precision the edges need."""
    labels = bin_labels(bin_edges("width", 8, 4000.0, 4001.0), negative_bin=False)
    assert len(labels) == 8 and len(set(labels)) == 8
    assert labels[:3] == ["< 4,000.125", "4,000.125 – 4,000.25", "4,000.25 – 4,000.375"]


def test_echo_carries_the_edge_labels_the_bins_are_cut_at() -> None:
    """The caption prints these rather than rounding the floats client-side —
    the sentence naming the edges and the axis naming the bins are then the
    same text, cut at the same precision."""
    edges = [4000.125, 4000.875]
    spec = DeriveSpec(kind="bins", mode="custom", edges=edges)
    labels = bin_labels(edges, negative_bin=False)
    echo = ResolvedDerive(
        spec=spec,
        expr=bins_expr("v", edges, negative_bin=False),
        labels=labels,
        edges=edges,
        negative_bin=False,
    ).echo()
    assert echo["edges"] == edges
    assert echo["edge_labels"] == ["4,000.125", "4,000.875"]
    assert echo["labels"][1] == "4,000.125 – 4,000.875"
