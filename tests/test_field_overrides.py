"""Analyst-declared fields: the override the recommenders consult.

The recommenders type fields *syntactically* and say so in their own docstrings:
an HTTP status code parses as a number, so ``numeric_range`` offers it, learns a
band over {200, 404, 500} and reports the 500s as outliers forever. No probe
discovers that it is a categorical field wearing digits — only the analyst does,
and ``Timeline.field_overrides`` is where they say it, per method.

These tests pin the two halves that could quietly rot: the declaration must
actually change which fields an auto-selecting detector scans, and it must never
become a lock — an explicit ``fields=[…]`` still scans an excluded field, and a
run that held one back says so in its warnings rather than returning a quietly
narrower answer that reads as "clean".
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.test_anomaly_stats import FakeQueryResult, _svc
from vestigo.db.anomaly_stats import apply_field_overrides

# ---------------------------------------------------------------------------
# apply_field_overrides — the shared helper every auto path runs through
# ---------------------------------------------------------------------------


def test_no_declaration_leaves_the_recommendation_alone():
    fields, notes = apply_field_overrides(["a", "b"], None)
    assert fields == ["a", "b"]
    assert notes == []


def test_declared_off_field_is_dropped_and_disclosed():
    fields, notes = apply_field_overrides(["a", "b"], {"b": False})
    assert fields == ["a"]
    # A held-back field that produced no note is indistinguishable from a field
    # that produced no finding — the one thing an override must never look like.
    assert len(notes) == 1
    assert "b" in notes[0]


def test_pinned_field_goes_first_so_the_cap_cannot_cut_it():
    # Being ranked below the cut is exactly why an analyst pins a field, so a
    # pin appended to the tail would be a control that does nothing.
    fields, _ = apply_field_overrides(["a", "b"], {"z": True}, known=["a", "b", "z"])
    assert fields == ["z", "a", "b"]


def test_a_pin_already_in_the_selection_is_not_duplicated():
    fields, _ = apply_field_overrides(["a", "b"], {"a": True}, known=["a", "b"])
    assert fields == ["a", "b"]


def test_pin_naming_a_field_this_timeline_lacks_is_dropped_and_disclosed():
    # Sending it on would query a column that is always empty and report
    # "nothing found", which reads as evidence rather than as a typo.
    fields, notes = apply_field_overrides(["a"], {"ghost": True}, known=["a"])
    assert fields == ["a"]
    assert len(notes) == 1
    assert "ghost" in notes[0]


def test_declaration_of_an_unrecommended_field_needs_no_known_universe():
    # Callers whose candidate list *is* the universe (the drift branches) pass
    # it as both, and nothing is dropped.
    fields, notes = apply_field_overrides(["a"], {"a": True}, known=None)
    assert fields == ["a"]
    assert notes == []


# ---------------------------------------------------------------------------
# The detector this exists for
# ---------------------------------------------------------------------------


def _range_svc(monkeypatch, recommended: list[str]) -> object:
    """A range detector whose recommender offers *recommended*, with no findings.

    One response per query the detector issues: the corpus count, then a
    per-field stats query. Padding them out means a scan of an extra field is
    visible as an extra consumed response rather than as a crash.
    """
    responses = [FakeQueryResult(result_rows=[(1000,)], column_names=["count()"])]
    for _ in recommended:
        responses.append(
            FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"])
        )
        responses.append(
            FakeQueryResult(result_rows=[], column_names=["val", "cnt", "first_seen", "evt_id"])
        )
    svc = _svc(responses)

    class _Rec:
        def __init__(self, token: str) -> None:
            self.token = token
            self.recommended = True

    monkeypatch.setattr(
        type(svc),
        "recommend_numeric_fields",
        lambda self, *a, **k: [_Rec(t) for t in recommended],
    )
    return svc


def _scanned_fields(svc) -> set[str]:
    """Attribute keys the detector actually bound into a query.

    Per-field queries bind the key as ``fk``; ``value_novelty`` batches its
    plain-attribute fields into one ARRAY JOIN pass and binds them as the
    ``nkeys`` list, so both spellings count as "this field was scanned".
    """
    scanned: set[str] = set()
    for params in svc.ch.client._all_parameters:
        for k, v in params.items():
            if k.startswith("fk") and isinstance(v, str):
                scanned.add(v)
            elif k == "nkeys":
                scanned.update(str(x) for x in v)
    return scanned


def test_declared_off_field_leaves_the_range_scan(monkeypatch):
    svc = _range_svc(monkeypatch, ["attr:bytes", "attr:status_code"])
    result = svc.find_range_violations("c1", ["s1"], field_overrides={"attr:status_code": False})
    assert "status_code" not in _scanned_fields(svc)
    assert "bytes" in _scanned_fields(svc)
    assert any("attr:status_code" in w for w in result.warnings)


def test_naming_the_excluded_field_explicitly_still_scans_it(monkeypatch):
    # The whole "advice, never a lock" contract, at the detector itself.
    svc = _range_svc(monkeypatch, ["attr:status_code"])
    result = svc.find_range_violations(
        "c1",
        ["s1"],
        fields=["attr:status_code"],
        field_overrides={"attr:status_code": False},
    )
    assert "status_code" in _scanned_fields(svc)
    # And nothing is disclosed, because nothing was held back.
    assert result.warnings == []


def test_value_novelty_scans_a_pinned_field(monkeypatch):
    """The other direction: a field the recommender ranked below the cut.

    ``value_novelty`` reads its candidates from the novelty recommender, so a
    field classified ``identifier`` never reaches the scan on its own — pinning
    it is the analyst saying the classification is wrong for this data.
    """
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[], column_names=["key", "val", "cnt", "fs", "evt"]),
        FakeQueryResult(result_rows=[], column_names=["val", "cnt", "fs", "evt"]),
    ]
    svc = _svc(responses)

    class _Rec:
        def __init__(self, token: str, recommended: bool) -> None:
            self.token = token
            self.recommended = recommended
            self.kind = "categorical" if recommended else "identifier"

    monkeypatch.setattr(
        type(svc),
        "recommend_novelty_fields",
        lambda self, *a, **k: [_Rec("attr:user", True), _Rec("attr:session_id", False)],
    )
    svc.find_value_novelty("c1", ["s1"], field_overrides={"attr:session_id": True})
    assert "session_id" in _scanned_fields(svc)


def test_a_declaration_is_per_method(monkeypatch):
    """Nothing global: the same field is meaningless to one method, ideal to another.

    The range detector is handed only its own slice of the timeline's
    declarations, so a ``value_novelty`` entry can never reach it — which is
    what makes "off for numeric_range, on for value_novelty" expressible.
    """
    svc = _range_svc(monkeypatch, ["attr:status_code"])
    svc.find_range_violations("c1", ["s1"], field_overrides={"attr:other": False})
    assert "status_code" in _scanned_fields(svc)


def test_window_warnings_survive_an_override_note(monkeypatch):
    """A run can have both kinds of thing to say, and must say both."""
    from tests.test_anomaly_stats import _one_suspect

    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 2, 0, 0, 1, tzinfo=UTC),
    )
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # _window_totals: baseline + one suspect
        FakeQueryResult(result_rows=[(900, 1)], column_names=["b", "s0"]),
        FakeQueryResult(result_rows=[], column_names=["key", "val", "cnt", "fs", "evt"]),
    ]
    svc = _svc(responses)

    class _Rec:
        token = "attr:user"
        recommended = True
        kind = "categorical"

    monkeypatch.setattr(type(svc), "recommend_novelty_fields", lambda self, *a, **k: [_Rec()])
    result = svc.find_value_novelty(
        "c1", ["s1"], windows=windows, field_overrides={"attr:user": False}
    )
    assert any("attr:user" in w for w in result.warnings)
    assert len(result.warnings) > 1


def test_the_string_detectors_disclose_a_held_back_field(monkeypatch):
    """The charset/entropy auto path filters before its quota, and still says so.

    Dropping an excluded field before ``_select_auto_scan_tokens`` is what lets
    the other kind backfill the slot instead of the scan quietly shrinking below
    its own cap — but a filter that runs ahead of the disclosure would leave
    nothing to disclose, which is the failure mode this pins.
    """
    svc = _svc([])

    class _Rec:
        def __init__(self, token: str, kind: str) -> None:
            self.token = token
            self.kind = kind
            self.recommended = kind == "categorical"

    monkeypatch.setattr(
        type(svc),
        "recommend_novelty_fields",
        lambda self, *a, **k: [_Rec("attr:msg", "identifier"), _Rec("attr:host", "categorical")],
    )
    picked, notes = svc._auto_string_fields(
        "c1", ["s1"], 1000, None, None, None, {"attr:msg": False}
    )
    assert picked == ["attr:host"]
    assert any("attr:msg" in n for n in notes)
