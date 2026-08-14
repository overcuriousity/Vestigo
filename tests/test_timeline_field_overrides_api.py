"""API tests for per-timeline, per-method field declarations.

A declaration answers a question the recommenders cannot: they type fields
syntactically, so an HTTP status code is offered to the numeric-range detector
and reported as an outlier forever. Which detector reads which field is the
analyst's call.

Like a mute, it is deliberately not a gate, and these tests pin that where it
could quietly rot: the analysis plan must keep reporting the method exactly as
before, and ``/analysis/findings`` with an explicit ``fields`` must keep scanning
an excluded field.
"""

from __future__ import annotations

from tests.conftest import as_admin


def _case_and_timeline(client) -> tuple[str, str]:
    case_id = client.post("/api/cases/", json={"name": "Declare case"}).json()["case"]["id"]
    tid = client.post(f"/api/cases/{case_id}/timelines", json={"name": "t"}).json()["timeline"][
        "id"
    ]
    return case_id, tid


def _patch(client, case_id, tid, overrides):
    return client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/field-overrides",
        json={"field_overrides": overrides},
    )


def test_timeline_starts_with_nothing_declared(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.get(f"/api/cases/{case_id}/timelines/{tid}")
    assert resp.json()["timeline"]["field_overrides"] == {}


def test_declaration_round_trips_through_get(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    declared = {
        "numeric_range": {"attr:status_code": False},
        "value_novelty": {"attr:status_code": True},
    }
    resp = _patch(client, case_id, tid, declared)
    assert resp.status_code == 200, resp.text
    assert resp.json()["timeline"]["field_overrides"] == declared

    resp = client.get(f"/api/cases/{case_id}/timelines/{tid}")
    assert resp.json()["timeline"]["field_overrides"] == declared


def test_a_method_with_nothing_left_is_dropped_rather_than_stored_empty(client, admin_bootstrap):
    # "Undeclared" needs one representation, or the audit trail compares
    # leftovers instead of decisions.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _patch(client, case_id, tid, {"numeric_range": {}})
    assert resp.json()["timeline"]["field_overrides"] == {}


def test_empty_payload_clears_every_declaration(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _patch(client, case_id, tid, {"entropy": {"attr:msg": False}})
    resp = _patch(client, case_id, tid, {})
    assert resp.json()["timeline"]["field_overrides"] == {}


def test_unknown_method_is_rejected_rather_than_stored(client, admin_bootstrap):
    # A typo that persisted silently would read in the audit trail as a
    # deliberate declaration that never applied to anything.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _patch(client, case_id, tid, {"numeric_rnage": {"attr:status_code": False}})
    assert resp.status_code == 422
    assert "numeric_rnage" in resp.json()["detail"]
    assert (
        client.get(f"/api/cases/{case_id}/timelines/{tid}").json()["timeline"]["field_overrides"]
        == {}
    )


def test_empty_field_token_is_rejected(client, admin_bootstrap):
    # An empty token can never match a field, so it would be a declaration that
    # holds nothing back while claiming to.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _patch(client, case_id, tid, {"numeric_range": {"  ": False}})
    assert resp.status_code == 422


def test_missing_timeline_is_404(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "Declare case"}).json()["case"]["id"]
    resp = _patch(client, case_id, "nope", {"entropy": {"attr:msg": False}})
    assert resp.status_code == 404


async def test_declaration_changes_are_audited(client, admin_bootstrap, store):
    # Shared state that changes what every other analyst on the case is shown,
    # so it owes the same before/after record a mapping edit does.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _patch(client, case_id, tid, {"numeric_range": {"attr:status_code": False}})
    _patch(client, case_id, tid, {})

    entries = await store.query_audit(case_id=case_id)
    updates = [e for e in entries if e.action == "timeline.update_field_overrides"]
    assert len(updates) == 2
    details = [e.detail for e in updates]
    assert {"previous": {}, "new": {"numeric_range": {"attr:status_code": False}}} in details
    assert {"previous": {"numeric_range": {"attr:status_code": False}}, "new": {}} in details


def test_declaring_does_not_change_the_analysis_plan(client, admin_bootstrap):
    """The gate reports what the *data* allows, never what an analyst declared.

    Folding a declaration into ``not_applicable`` would make the plan claim a
    method cannot produce a finding here — a different, and false, statement.
    """
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)

    before = client.get(f"/api/cases/{case_id}/timelines/{tid}/analysis/plan")
    assert before.status_code == 200, before.text
    _patch(client, case_id, tid, {"numeric_range": {"attr:status_code": False}})
    after = client.get(f"/api/cases/{case_id}/timelines/{tid}/analysis/plan")
    assert after.status_code == 200, after.text
    assert after.json()["methods"] == before.json()["methods"]


def test_an_excluded_field_still_runs_when_named(client, admin_bootstrap):
    # The "advice, never a lock" contract at the endpoint the sheet's picker
    # calls: an explicit `fields` bypasses the declaration entirely.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _patch(client, case_id, tid, {"numeric_range": {"attr:status_code": False}})
    resp = client.get(
        f"/api/cases/{case_id}/timelines/{tid}/analysis/findings",
        params={
            "method": "numeric_range",
            "frame": "self",
            "limit": 5,
            "params": '{"fields": ["attr:status_code"]}',
        },
    )
    assert resp.status_code == 200, resp.text


def test_declaring_invalidates_the_findings_cache(client, admin_bootstrap):
    """A cached answer computed before the declaration answers a question nobody asked.

    The fingerprint covers the method's own slice, so the same request after a
    declaration must recompute rather than serve the pre-declaration findings.
    """
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    params = {"method": "value_novelty", "frame": "self", "limit": 5}
    url = f"/api/cases/{case_id}/timelines/{tid}/analysis/findings"

    assert client.get(url, params=params).json()["cache"] == "miss"
    assert client.get(url, params=params).json()["cache"] == "hit"

    _patch(client, case_id, tid, {"value_novelty": {"attr:user": False}})
    assert client.get(url, params=params).json()["cache"] == "miss"
