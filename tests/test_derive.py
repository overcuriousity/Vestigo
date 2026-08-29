"""Derivations are a change of scale computed before aggregation. These pin
the pure parts: the spec's validation, the bin-edge maths, the labels, and
the shape of the SQL — `test_derive_clickhouse.py` proves the SQL means it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vestigo.db.derive import (
    TIME_PART_TOKENS,
    DeriveSpec,
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


def test_bin_labels_are_open_ended_and_human() -> None:
    assert bin_labels([1024.0, 10240.0], negative_bin=False) == [
        "< 1,024",
        "1,024 – 10,240",
        "≥ 10,240",
    ]
    assert bin_labels([1024.0], negative_bin=True) == ["≤ 0", "< 1,024", "≥ 1,024"]
    assert bin_labels([], negative_bin=False) == ["all values"]


def test_bin_labels_round_non_integers_to_three_significant_digits() -> None:
    assert bin_labels([0.123456, 2.5], negative_bin=False) == ["< 0.123", "0.123 – 2.5", "≥ 2.5"]


def test_bins_expr_is_a_multi_if_over_the_cast_value() -> None:
    sql = bins_expr("v", [1024.0, 10240.0], negative_bin=True)
    assert sql.startswith("multiIf(isNull(v), '', v <= 0, '≤ 0', v < 1024.0, '< 1,024', ")
    assert sql.endswith("'≥ 10,240')")


def test_time_part_expr_parses_then_reuses_the_time_field_spec() -> None:
    sql = time_part_expr("attributes[{field_key:String}]", "hour")
    assert "parseDateTimeBestEffortOrNull(toString(attributes[{field_key:String}]))" in sql
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
