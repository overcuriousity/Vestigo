"""API tests for per-timeline muted analysis methods.

A mute keeps a method out of the *unprompted* sweep — its findings, and the
histogram marks derived from them. It is deliberately not a gate, and these
tests pin that distinction down at the boundary where it could quietly rot:
the analysis plan must keep reporting a muted method exactly as it did before,
and ``/analysis/findings`` must keep running it when asked for by name. If
either of those ever changed, the UI's "run anyway" on a muted method would
become a lie and the mute would have turned into the lock it is not.
"""

from __future__ import annotations

from tests.conftest import as_admin
from vestigo.db.analysis_plan import METHOD_IDS


def _case_and_timeline(client) -> tuple[str, str]:
    case_id = client.post("/api/cases/", json={"name": "Mute case"}).json()["case"]["id"]
    tid = client.post(f"/api/cases/{case_id}/timelines", json={"name": "t"}).json()["timeline"][
        "id"
    ]
    return case_id, tid


def test_timeline_starts_with_nothing_muted(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.get(f"/api/cases/{case_id}/timelines/{tid}")
    assert resp.json()["timeline"]["muted_methods"] == []


def test_mute_round_trips_through_get(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)

    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": ["timestamp_order", "entropy"]},
    )
    assert resp.status_code == 200, resp.text
    # Stored sorted, so the audit trail compares sets rather than orderings.
    assert resp.json()["timeline"]["muted_methods"] == ["entropy", "timestamp_order"]

    resp = client.get(f"/api/cases/{case_id}/timelines/{tid}")
    assert resp.json()["timeline"]["muted_methods"] == ["entropy", "timestamp_order"]


def test_mute_deduplicates(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": ["entropy", "entropy"]},
    )
    assert resp.json()["timeline"]["muted_methods"] == ["entropy"]


def test_empty_list_clears_every_mute(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": ["entropy"]},
    )
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": []},
    )
    assert resp.json()["timeline"]["muted_methods"] == []


def test_unknown_method_is_rejected_rather_than_stored(client, admin_bootstrap):
    # A typo that persisted silently would read in the audit trail as a
    # deliberate mute of a method that does not exist, and would mute nothing.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": ["timestamp_order", "timestamp_oder"]},
    )
    assert resp.status_code == 422
    assert "timestamp_oder" in resp.json()["detail"]

    assert (
        client.get(f"/api/cases/{case_id}/timelines/{tid}").json()["timeline"]["muted_methods"]
        == []
    )


def test_every_method_id_is_mutable(client, admin_bootstrap):
    # The UI offers a chip per method; one that 422s would be a dead control.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": list(METHOD_IDS)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["timeline"]["muted_methods"] == sorted(METHOD_IDS)


def test_missing_timeline_is_404(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "Mute case"}).json()["case"]["id"]
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/nope/muted-methods",
        json={"muted_methods": ["entropy"]},
    )
    assert resp.status_code == 404


async def test_mute_changes_are_audited(client, admin_bootstrap, store):
    # Muting is shared state that changes what every other analyst on the case
    # sees, so it owes the same before/after record a mapping edit does.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": ["entropy"]},
    )
    client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": []},
    )

    entries = await store.query_audit(case_id=case_id)
    updates = [e for e in entries if e.action == "timeline.update_muted_methods"]
    assert len(updates) == 2
    details = [e.detail for e in updates]
    assert {"previous": [], "new": ["entropy"]} in details
    assert {"previous": ["entropy"], "new": []} in details


def test_muting_does_not_change_the_analysis_plan(client, admin_bootstrap):
    """The gate reports what the *data* allows, never what an analyst prefers.

    Folding a mute into ``not_applicable`` would make the plan claim a method
    cannot produce a finding here, which is a different — and false —
    statement, and would break the invariant that the plan never withholds a
    method.
    """
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)

    before = client.get(f"/api/cases/{case_id}/timelines/{tid}/analysis/plan")
    assert before.status_code == 200, before.text
    client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": list(METHOD_IDS)},
    )
    after = client.get(f"/api/cases/{case_id}/timelines/{tid}/analysis/plan")
    assert after.status_code == 200, after.text
    # Non-vacuously: the plan reports one entry per METHOD_IDS, always.
    assert len(before.json()["methods"]) == len(METHOD_IDS)
    assert after.json()["methods"] == before.json()["methods"]


def test_a_muted_method_still_runs_when_asked_for_by_name(client, admin_bootstrap):
    # The whole "advice, never a lock" contract, at the endpoint the sheet's
    # Run anyway calls.
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/muted-methods",
        json={"muted_methods": ["value_novelty"]},
    )
    resp = client.get(
        f"/api/cases/{case_id}/timelines/{tid}/analysis/findings",
        params={"method": "value_novelty", "frame": "self", "limit": 5},
    )
    assert resp.status_code == 200, resp.text
