"""Columnar tool-result encoding (roadmap A13).

Tool results are replayed into the model on every subsequent turn, so their
size compounds. `agent/encoding.py` states column names once instead of once
per row. The forensic requirement is that this is a *reshaping*: every value
must survive byte-identical, so the round-trip tests here are the point.
"""

from __future__ import annotations

import json

from vestigo.agent.encoding import columnar, columnar_auto
from vestigo.agent.fidelity import Fidelity
from vestigo.agent.tools import (
    MAX_LIST_ROWS,
    SLIM_MESSAGE_TRUNCATE,
    _columnize,
    _compact_timeseries,
    _deflate_findings,
    _listing,
)


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


# --- detector finding deflation -------------------------------------------


def test_deflate_findings_keeps_the_message_and_drops_the_rest_of_the_event():
    payload = {
        "status": "ok",
        "results": [
            {
                "type": "value_novelty",
                "event_id": "e1",
                "event": {
                    "message": "login attempt [svc/rock] succeeded",
                    "attr": {"a": "x" * 5000},
                },
                "score": 12.7,
            },
            {"type": "value_novelty", "event_id": "e2", "event": {"message": "y"}, "score": 9.1},
        ],
    }
    out = _deflate_findings(payload, Fidelity.MESSAGE)
    assert [r["event_id"] for r in out["results"]] == ["e1", "e2"]
    assert all("event" not in r for r in out["results"])
    # succeeded-vs-failed is the finding — it must survive the slimming.
    assert out["results"][0]["message"] == "login attempt [svc/rock] succeeded"
    assert out["results"][0]["score"] == 12.7
    assert out["status"] == "ok"
    # input untouched — the persisted copy keeps its events
    assert payload["results"][0]["event"]["attr"] == {"a": "x" * 5000}


def test_deflate_findings_truncates_a_long_message():
    payload = {"results": [{"event_id": "e1", "event": {"message": "m" * 5000}}]}
    message = _deflate_findings(payload, Fidelity.MESSAGE)["results"][0]["message"]
    assert len(message) == SLIM_MESSAGE_TRUNCATE + 1  # + the ellipsis
    assert message.endswith("…")


def test_deflate_findings_admits_the_omission():
    """The model must never believe it saw the whole record."""
    out = _deflate_findings(
        {"results": [{"event_id": "e1", "event": {"message": "m", "attributes": {"k": "v"}}}]},
        Fidelity.MESSAGE,
    )
    assert "get_event" in out["note"]
    # nothing dropped, nothing claimed — neither for a finding that carried no
    # event at all, nor for one whose event held only the line that survives.
    assert "note" not in _deflate_findings({"results": [{"event_id": "e1"}]}, Fidelity.MESSAGE)
    assert "note" not in _deflate_findings(
        {"results": [{"event_id": "e1", "event": {"message": "m"}}]}, Fidelity.MESSAGE
    )
    assert "note" not in _deflate_findings(
        {"results": [{"event_id": "e1", "event": None}]}, Fidelity.MESSAGE
    )


def test_deflate_findings_saves_the_bulk_of_the_bytes():
    """The event was ~85% of a finding on the real overflow (2026-07-20)."""
    payload = {
        "results": [
            {"event_id": f"e{i}", "event": {"message": "m" * 80, "blob": "z" * 2000}, "score": i}
            for i in range(15)
        ]
    }
    before = len(json.dumps(payload))
    after = len(json.dumps(_deflate_findings(payload, Fidelity.MESSAGE)))
    assert after < before * 0.2


def test_deflate_findings_ignores_unexpected_shapes():
    assert _deflate_findings({"results": "nope"}, Fidelity.MESSAGE) == {"results": "nope"}
    assert _deflate_findings({"status": "skipped"}, Fidelity.MESSAGE) == {"status": "skipped"}
    assert _deflate_findings("not a dict", Fidelity.MESSAGE) == "not a dict"
    # a finding with no event keeps its rows — only the tier stamp is added,
    # and no note, since nothing was actually dropped
    passthrough = _deflate_findings({"results": [{"event_id": "e1"}]}, Fidelity.MESSAGE)
    assert passthrough["results"] == [{"event_id": "e1"}]
    assert passthrough["fidelity"] == "message" and "note" not in passthrough
    # an event without a message still loses the event, and says so
    out = _deflate_findings(
        {"results": [{"event_id": "e1", "event": {"blob": "z"}}]}, Fidelity.MESSAGE
    )
    assert out["results"] == [{"event_id": "e1"}]
    assert "note" in out
