"""Columnar tool-result encoding (roadmap A13).

Tool results are replayed into the model on every subsequent turn, so their
size compounds. `agent/encoding.py` states column names once instead of once
per row. The forensic requirement is that this is a *reshaping*: every value
must survive byte-identical, so the round-trip tests here are the point.
"""

from __future__ import annotations

import json

from vestigo.agent.encoding import columnar, columnar_auto
from vestigo.agent.tools import MAX_LIST_ROWS, _columnize, _compact_timeseries, _listing


def _decode(payload: dict) -> list[dict]:
    return [dict(zip(payload["columns"], row, strict=True)) for row in payload["rows"]]


def test_columnar_round_trips_exactly():
    rows = [
        {"value": "alice", "count": 12},
        {"value": "bob", "count": 3},
    ]
    assert _decode(columnar_auto(rows)) == rows


def test_columnar_preserves_awkward_values():
    """No stringification, no truncation, no numeric coercion."""
    rows = [
        {"v": None, "n": 0},
        {"v": "", "n": -1},
        {"v": "a,b|c\td\n", "n": 3.5},
        {"v": "héllo — ünïcode", "n": 10**18},
        {"v": {"nested": ["dict", 1]}, "n": True},
    ]
    assert _decode(columnar_auto(rows)) == rows


def test_columnar_explicit_columns_select_and_order():
    rows = [{"b": 2, "a": 1, "drop": "me"}]
    payload = columnar(rows, ["a", "b"])
    assert payload["columns"] == ["a", "b"]
    assert payload["rows"] == [[1, 2]]


def test_columnar_fills_missing_keys_with_none():
    payload = columnar([{"a": 1}], ["a", "absent"])
    assert payload["rows"] == [[1, None]]


def test_columnar_auto_unions_ragged_rows():
    rows = [{"a": 1}, {"b": 2}]
    payload = columnar_auto(rows)
    assert payload["columns"] == ["a", "b"]
    assert _decode(payload) == [{"a": 1, "b": None}, {"a": None, "b": 2}]


def test_columnar_empty_still_reports_shape():
    """ "No rows" must be distinguishable from "no such shape"."""
    assert columnar([], ["value", "count"]) == {"columns": ["value", "count"], "rows": []}


def test_columnize_only_touches_named_keys():
    result = {
        "total": 2,
        "values": [{"value": "a", "count": 1}],
        "untouched": [{"x": 1}],
        "scalar": "keep",
    }
    out = _columnize(result, "values")
    assert out["values"]["columns"] == ["value", "count"]
    assert out["untouched"] == [{"x": 1}]
    assert out["scalar"] == "keep"
    assert out["total"] == 2


def test_columnize_leaves_non_dict_lists_alone():
    """field_scatter's points are already positional pairs."""
    result = {"points": [[1.0, 2.0], [3.0, 4.0]]}
    assert _columnize(result, "points") == result


def test_columnize_is_a_noop_on_empty_lists():
    assert _columnize({"values": []}, "values") == {"values": []}


def test_compact_timeseries_hoists_the_shared_axis():
    result = {
        "field": "user",
        "series": [
            {
                "value": "alice",
                "buckets": [{"start": "T0", "count": 1}, {"start": "T1", "count": 2}],
            },
            {
                "value": "bob",
                "buckets": [{"start": "T0", "count": 0}, {"start": "T1", "count": 5}],
            },
        ],
    }
    out = _compact_timeseries(result)
    assert out["bucket_starts"] == ["T0", "T1"]
    assert _decode(out["series"]) == [
        {"value": "alice", "counts": [1, 2]},
        {"value": "bob", "counts": [0, 5]},
    ]
    assert out["field"] == "user"


def test_compact_timeseries_bails_out_when_axes_disagree():
    """Hoisting a shared axis is only valid if it really is shared — a future
    query change must degrade to the verbose shape, never drop data."""
    result = {
        "series": [
            {"value": "a", "buckets": [{"start": "T0", "count": 1}]},
            {"value": "b", "buckets": [{"start": "T9", "count": 1}]},
        ]
    }
    assert _compact_timeseries(result) == result


def test_compact_timeseries_ignores_unexpected_shapes():
    assert _compact_timeseries({"series": []}) == {"series": []}
    assert _compact_timeseries({"series": "nope"}) == {"series": "nope"}
    assert _compact_timeseries({"series": [{"value": "a"}]}) == {"series": [{"value": "a"}]}
    assert _compact_timeseries("not a dict") == "not a dict"


def test_encoding_actually_saves_space():
    rows = [{"value": f"host-{i}.example.internal", "count": i} for i in range(100)]
    assert len(json.dumps(columnar_auto(rows))) < len(json.dumps(rows)) * 0.75


# --- capped listings ------------------------------------------------------


def test_listing_reports_returned_alongside_total():
    """A capped list must not read as a complete one — the model would
    otherwise reason over a silently partial set."""
    rows = [{"id": str(i)} for i in range(MAX_LIST_ROWS + 50)]
    out = _listing("things", rows, len(rows))
    assert out["total"] == MAX_LIST_ROWS + 50
    assert out["returned"] == MAX_LIST_ROWS
    assert len(out["things"]["rows"]) == MAX_LIST_ROWS


def test_listing_returned_equals_total_when_nothing_was_dropped():
    out = _listing("things", [{"id": "a"}, {"id": "b"}], 2)
    assert out["total"] == out["returned"] == 2


def test_listing_of_nothing_still_reports_the_key():
    out = _listing("things", [], 0)
    assert out == {"total": 0, "returned": 0, "things": {"columns": [], "rows": []}}
