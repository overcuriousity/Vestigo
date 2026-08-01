"""Tests for vestigo.columns.recommend — the deterministic column scorer (#213).

Pure functions over the field-stats cache payload shape, so every case here is
a hand-built stats dict. The payload contract itself lives in
``db/field_stats.py::compute_source_field_stats``.
"""

from __future__ import annotations

from typing import Any

from vestigo.columns.recommend import (
    FILLER_COLUMN,
    PINNED_COLUMN,
    SCORE_FLOOR,
    ColumnCandidate,
    finalize_columns,
    pick_columns,
    score_columns,
)


def _attr(coverage: int, distinct: int, samples: list[str]) -> dict[str, Any]:
    return {"coverage": coverage, "distinct": distinct, "samples": samples}


def _source(
    total: int,
    attributes: dict[str, dict[str, Any]] | None = None,
    top_level: dict[str, dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    return (
        total,
        {
            "top_level": top_level or {},
            "attributes": attributes or {},
            "attr_keys_truncated": False,
        },
    )


def _tokens(stats: dict[str, tuple[int, dict[str, Any]]]) -> list[str]:
    return [c.token for c in score_columns(stats)]


# ── Empty / degenerate input ────────────────────────────────────────────────


def test_no_sources_yields_no_candidates():
    assert score_columns({}) == []


def test_zero_events_yields_no_candidates():
    assert score_columns({"s1": _source(0, {"user": _attr(0, 0, [])})}) == []


# ── Rejection gates ─────────────────────────────────────────────────────────


def test_constant_field_is_rejected():
    """A value that never changes is dead grid space."""
    stats = {"s1": _source(1000, {"env": _attr(1000, 1, ["prod"])})}
    assert "env" not in _tokens(stats)


def test_per_row_unique_field_is_rejected():
    stats = {"s1": _source(1000, {"row_seq": _attr(1000, 1000, ["1", "2", "3"])})}
    assert "row_seq" not in _tokens(stats)


def test_per_row_unique_field_is_rejected_across_several_sources():
    """The uniqueness test is per source: summing coverage would hide this.

    ``distinct`` is max-across-sources while ``coverage`` sums, so four
    1000-event sources carrying a per-row-unique value read as a 0.25
    aggregate ratio — full grouping credit for the emptiest column on the
    grid, and boosted by breadth on top.
    """
    stats = {
        f"s{i}": _source(1000, {"session_ref": _attr(1000, 1000, ["ref-9182", "ref-3341"])})
        for i in range(4)
    }
    assert "session_ref" not in _tokens(stats)


def test_a_grouping_field_survives_across_several_sources():
    """The per-source gate must not take real columns down with it."""
    stats = {
        f"s{i}": _source(1000, {"user": _attr(1000, 20, ["alice", "bob", "carol"])})
        for i in range(4)
    }
    assert "user" in _tokens(stats)


def test_uniqueness_gate_does_not_fire_on_a_tiny_source():
    """With 12 events, "12 distinct" says nothing — the gate must not reject."""
    stats = {"s1": _source(12, {"user": _attr(12, 12, ["alice", "bob", "carol"])})}
    assert "user" in _tokens(stats)


def test_uniqueness_is_judged_on_the_largest_source_not_the_worst_one():
    """One small outlier source must not veto a field that groups everywhere else.

    ``user`` is unique on every row of a 60-event source (one day of one host)
    and groups cleanly across three 5000-event ones. Reading the ratio off the
    *worst* source rejects the field outright — and does so more often the more
    sources a timeline merges, which is backwards for a scorer that weights
    breadth highest. The largest source's ratio is the one least likely to be
    an artifact of how little it holds.
    """
    stats = {
        "small": _source(60, {"user": _attr(60, 60, ["alice", "bob", "carol"])}),
        **{
            f"big{i}": _source(5000, {"user": _attr(5000, 25, ["alice", "bob", "carol"])})
            for i in range(3)
        },
    }
    assert "user" in _tokens(stats)


def test_a_field_unique_in_the_largest_source_is_still_rejected():
    """The rule is "the best-evidenced source", not "whichever source acquits"."""
    stats = {
        "small": _source(60, {"trace_id": _attr(60, 4, ["t-1", "t-2"])}),
        "big": _source(5000, {"trace_id": _attr(5000, 5000, ["t-1", "t-2"])}),
    }
    assert "trace_id" not in _tokens(stats)


def test_hash_valued_field_is_rejected():
    stats = {
        "s1": _source(
            1000,
            {
                "req_id": _attr(
                    1000, 400, ["a1b2c3d4e5f6a7b8", "ffeeddccbbaa9988", "0123456789abcdef"]
                )
            },
        )
    }
    assert "req_id" not in _tokens(stats)


def test_guid_valued_field_is_rejected():
    guids = [
        "6f9619ff-8b86-d011-b42d-00c04fc964ff",
        "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "{c56a4180-65aa-42ec-a945-5fd21dec0538}",
    ]
    stats = {"s1": _source(1000, {"trace": _attr(1000, 400, guids)})}
    assert "trace" not in _tokens(stats)


def test_paragraph_valued_field_is_rejected():
    """A value that cannot fit a grid cell is not a column."""
    stats = {"s1": _source(1000, {"body": _attr(1000, 400, ["x" * 200, "y" * 210, "z" * 190])})}
    assert "body" not in _tokens(stats)


# ── Exclusions ──────────────────────────────────────────────────────────────


def test_ingestion_metadata_columns_are_not_candidates():
    """parser_name is cached but describes how evidence arrived, not what happened."""
    stats = {
        "s1": _source(
            1000,
            top_level={
                "parser_name": {"distinct": 3, "coverage": 1000, "values": [["jsonl", 1000]]},
                "artifact": {"distinct": 4, "coverage": 1000, "values": [["log:nginx", 900]]},
            },
        )
    }
    tokens = _tokens(stats)
    assert "parser_name" not in tokens
    assert "artifact" in tokens


def test_attribute_colliding_with_a_grid_column_id_is_dropped():
    """The grid would render its built-in `message` column, not this attribute."""
    stats = {
        "s1": _source(
            1000,
            {
                "message": _attr(1000, 300, ["something happened", "and again", "once more"]),
                "user": _attr(1000, 12, ["alice", "bob", "carol"]),
            },
        )
    }
    tokens = _tokens(stats)
    assert "message" not in tokens
    assert "user" in tokens


def test_mapping_consumed_keys_are_excluded():
    """Neither the raw keys nor the canonical name renders in the grid today."""
    stats = {
        "s1": _source(
            1000,
            {
                "src_ip": _attr(1000, 40, ["10.0.0.1", "10.0.0.2", "8.8.8.8"]),
                "user": _attr(1000, 12, ["alice", "bob", "carol"]),
            },
        )
    }
    tokens = [c.token for c in score_columns(stats, {"ip_address": ["src_ip", "ip_addr"]})]
    assert "src_ip" not in tokens
    assert "ip_address" not in tokens
    assert "user" in tokens


# ── Ranking ─────────────────────────────────────────────────────────────────


def test_breadth_outranks_a_better_filled_single_source_field():
    """The issue's primary ask: fields present across sources come first."""
    stats = {
        "s1": _source(
            1000,
            {
                "shared": _attr(700, 20, ["a-value", "b-value", "c-value"]),
                "only_here": _attr(1000, 20, ["a-value", "b-value", "c-value"]),
            },
        ),
        "s2": _source(1000, {"shared": _attr(700, 20, ["d-value", "e-value", "f-value"])}),
    }
    tokens = _tokens(stats)
    assert tokens.index("shared") < tokens.index("only_here")


def test_meaningful_name_breaks_a_tie():
    """Identical statistics, one recognizable name — the readable one wins."""
    values = ["alpha", "bravo", "charlie"]
    stats = {
        "s1": _source(
            1000,
            {
                "username": _attr(1000, 20, values),
                "zzz_col": _attr(1000, 20, values),
            },
        )
    }
    tokens = _tokens(stats)
    assert tokens.index("username") < tokens.index("zzz_col")


def test_a_corpus_of_unrecognizable_field_names_still_yields_columns():
    """Name affinity re-ranks; it must never be the thing that lets a field in.

    The module docstring's own promise: "a corpus whose fields are all named
    ``f_17`` still gets a sensible answer". Every field here scores 0.0 on name
    affinity (segments ``{"f", "17"}``, neither in the vocabulary), so the
    statistical signals have to carry them over the floor on their own. If this
    ever stops passing, the floor and the weights have drifted apart and every
    vendor-numbered corpus quietly falls back to the built-in defaults.
    """
    values = ["alpha", "bravo", "charlie"]
    stats = {
        f"s{i}": _source(1000, {f"f_{n}": _attr(1000, 20, values) for n in range(17, 21)})
        for i in range(2)
    }
    candidates = score_columns(stats)
    assert [c.token for c in candidates] == ["f_17", "f_18", "f_19", "f_20"]
    assert all(c.score >= SCORE_FLOOR for c in candidates)
    # And they survive selection, rather than being scored well and then
    # discarded as "insufficient".
    assert pick_columns(candidates) == ["f_17", "f_18", "f_19", "f_20"]


def test_scoring_is_deterministic_and_ties_break_by_name():
    values = ["alpha", "bravo", "charlie"]
    stats = {
        "s1": _source(
            1000,
            {
                "b_field": _attr(1000, 20, values),
                "a_field": _attr(1000, 20, values),
            },
        )
    }
    first = _tokens(stats)
    assert first == _tokens(stats)
    assert first.index("a_field") < first.index("b_field")


def test_every_returned_candidate_clears_the_floor():
    stats = {
        "s1": _source(1000, {"user": _attr(1000, 12, ["alice", "bob", "carol"])}),
        "s2": _source(1000, {"host": _attr(40, 3, ["web01", "web02", "db01"])}),
    }
    assert all(c.score >= SCORE_FLOOR for c in score_columns(stats))


def test_max_candidates_caps_the_list():
    attributes = {f"user_{i}": _attr(1000, 20, ["alpha", "bravo", "charlie"]) for i in range(40)}
    assert len(score_columns({"s1": _source(1000, attributes)}, max_candidates=5)) == 5


def test_reason_names_the_evidence():
    stats = {
        "s1": _source(1000, {"user": _attr(500, 12, ["alice", "bob", "carol"])}),
        "s2": _source(1000, {"user": _attr(1000, 9, ["dave", "erin", "frank"])}),
    }
    reason = score_columns(stats)[0].reason
    assert "2/2 sources" in reason
    assert "75% filled" in reason
    assert "12 distinct" in reason


# ── Selection ───────────────────────────────────────────────────────────────


def _candidate(token: str, score: float = 0.9) -> ColumnCandidate:
    return ColumnCandidate(
        token=token,
        score=score,
        breadth=1.0,
        fill=1.0,
        distinct=10,
        coverage=1000,
        sources_present=1,
        sources_total=1,
        samples=("a", "b", "c"),
        reason="test",
    )


def test_pick_columns_caps_at_k_max():
    candidates = [_candidate(f"f{i}", 0.9 - i / 100) for i in range(9)]
    assert pick_columns(candidates) == ["f0", "f1", "f2", "f3", "f4"]


def test_pick_columns_falls_back_to_message_to_reach_the_minimum():
    assert pick_columns([_candidate("user"), _candidate("host")]) == [
        "user",
        "host",
        FILLER_COLUMN,
    ]


def test_pick_columns_reports_insufficient_rather_than_a_two_column_grid():
    assert pick_columns([_candidate("user")]) == []
    assert pick_columns([]) == []


def test_finalize_pins_timestamp_first():
    candidates = [_candidate("user"), _candidate("src_ip")]
    columns, _ = finalize_columns(["user", "src_ip"], candidates)
    assert columns == [PINNED_COLUMN, "user", "src_ip"]


def test_finalize_does_not_duplicate_a_pinned_or_repeated_column():
    candidates = [_candidate("user")]
    columns, _ = finalize_columns([PINNED_COLUMN, "user", "user"], candidates)
    assert columns == [PINNED_COLUMN, "user"]


def test_finalize_attaches_a_reason_per_column_except_the_pin():
    candidates = [_candidate("user")]
    columns, reasons = finalize_columns(["user", FILLER_COLUMN], candidates)
    assert PINNED_COLUMN in columns
    assert PINNED_COLUMN not in reasons
    assert reasons["user"] == "test"
    # The filler is not a scored candidate, so it gets the fallback wording.
    assert FILLER_COLUMN in reasons


def test_finalize_of_nothing_stays_empty():
    """An empty pick must not become a timestamp-only grid."""
    assert finalize_columns([], []) == ([], {})
