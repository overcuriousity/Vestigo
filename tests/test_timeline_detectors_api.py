"""API tests for the per-timeline configured detectors (Milestone 12).

The list is the only thing the Investigate rail runs, so what it can hold is
held to the runner's own contract: an entry the findings endpoint would 422
on must not be storable, a baseline frame must name a baseline that exists on
this timeline, and every change is audited. One entry per method — a PUT on a
configured method edits it in place.
"""

from __future__ import annotations

import pytest

from tests.conftest import as_admin


def _case_and_timeline(client) -> tuple[str, str]:
    case_id = client.post("/api/cases/", json={"name": "Detector case"}).json()["case"]["id"]
    tid = client.post(f"/api/cases/{case_id}/timelines", json={"name": "t"}).json()["timeline"][
        "id"
    ]
    return case_id, tid


def _put(client, case_id, tid, method, body):
    return client.put(f"/api/cases/{case_id}/timelines/{tid}/detectors/{method}", json=body)


def _get(client, case_id, tid):
    return client.get(f"/api/cases/{case_id}/timelines/{tid}").json()["timeline"]


def _baseline(client, case_id, tid) -> str:
    resp = client.post(
        f"/api/cases/{case_id}/timelines/{tid}/baselines",
        json={
            "name": "week before",
            "baseline_start": "2026-01-01T00:00:00Z",
            "baseline_end": "2026-01-15T00:00:00Z",
            "suspect_windows": [
                {"label": "day", "start": "2026-02-02T00:00:00Z", "end": "2026-02-04T00:00:00Z"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["baseline"]["id"]


def test_timeline_starts_with_no_detectors(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    body = _get(client, case_id, tid)
    assert body["detectors"] == []
    assert "muted_methods" not in body


def test_put_stores_the_entry_and_get_round_trips_it(client, admin_bootstrap):
    admin = as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, "value_novelty", {"params": {"fields": ["user", "process"]}})
    assert resp.status_code == 200, resp.text
    [entry] = resp.json()["timeline"]["detectors"]
    assert entry["method"] == "value_novelty"
    assert entry["params"] == {"fields": ["user", "process"]}
    assert entry["frame"] == "self"
    assert entry["baseline_id"] is None
    assert entry["added_by"] == admin["id"]
    assert entry["added_at"].startswith("20")

    assert _get(client, case_id, tid)["detectors"] == [entry]


def test_put_on_a_configured_method_replaces_it_in_place(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _put(client, case_id, tid, "value_novelty", {"params": {}})
    _put(client, case_id, tid, "timestamp_order", {"params": {}})
    resp = _put(client, case_id, tid, "value_novelty", {"params": {"fields": ["user"]}})
    detectors = resp.json()["timeline"]["detectors"]
    # One per method, and the edited one keeps its position.
    assert [e["method"] for e in detectors] == ["value_novelty", "timestamp_order"]
    assert detectors[0]["params"] == {"fields": ["user"]}


def test_delete_removes_only_that_method(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _put(client, case_id, tid, "value_novelty", {"params": {}})
    _put(client, case_id, tid, "timestamp_order", {"params": {}})
    resp = client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/value_novelty")
    assert resp.status_code == 200, resp.text
    assert [e["method"] for e in resp.json()["timeline"]["detectors"]] == ["timestamp_order"]


def test_delete_of_an_unconfigured_method_is_404(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/entropy")
    assert resp.status_code == 404


def test_unknown_method_is_rejected(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, "timestamp_oder", {"params": {}})
    assert resp.status_code == 422
    assert "timestamp_oder" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("value_novelty", {"fields": ["user"], "z_threshold": 2}),  # not a value_novelty key
        ("frequency", {"z_threshold": -1}),  # gt=0
        ("sequence_novelty", {"ngram_size": 9}),  # le=5
        ("log_template", {"order": "sideways"}),  # not in the Literal
    ],
)
def test_params_the_findings_endpoint_rejects_are_not_storable(
    client, admin_bootstrap, method, params
):
    """Exactly the validation `/analysis/findings` applies — same models."""
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, method, {"params": params})
    assert resp.status_code == 422, resp.text
    assert f"Invalid parameters for {method}" in resp.json()["detail"]
    assert _get(client, case_id, tid)["detectors"] == []


def test_frame_and_baseline_must_agree(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    assert _put(client, case_id, tid, "frequency", {"frame": "baseline"}).status_code == 422
    assert (
        _put(client, case_id, tid, "frequency", {"frame": "self", "baseline_id": "b1"}).status_code
        == 422
    )


def test_baseline_must_exist_on_this_timeline(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    resp = _put(client, case_id, tid, "frequency", {"frame": "baseline", "baseline_id": "nope"})
    assert resp.status_code == 422
    assert "baseline" in resp.json()["detail"].lower()

    baseline_id = _baseline(client, case_id, tid)
    resp = _put(
        client, case_id, tid, "frequency", {"frame": "baseline", "baseline_id": baseline_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["timeline"]["detectors"][0]["baseline_id"] == baseline_id


@pytest.mark.asyncio
async def test_changes_are_audited(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    _put(client, case_id, tid, "entropy", {"params": {}})
    client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/entropy")

    entries = await store.query_audit(case_id=case_id)
    set_row = next(e for e in entries if e.action == "timeline.set_detector")
    assert set_row.detail["method"] == "entropy"
    assert set_row.detail["previous"] is None
    assert set_row.detail["new"]["method"] == "entropy"
    rm_row = next(e for e in entries if e.action == "timeline.remove_detector")
    assert rm_row.detail["previous"]["method"] == "entropy"
    assert rm_row.detail["new"] is None


def test_read_only_member_cannot_configure(client, admin_bootstrap):
    """Contribute access, like field overrides: the list changes what everyone runs."""
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    client.post(
        "/api/admin/users",
        json={"username": "reader", "password": "reader-pass-123", "is_admin": False},
    )
    client.post(
        f"/api/cases/{case_id}/members",
        json={"username": "reader", "access_level": "read"},
    )
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "reader", "password": "reader-pass-123"})

    assert _put(client, case_id, tid, "entropy", {"params": {}}).status_code == 403
    assert (
        client.delete(f"/api/cases/{case_id}/timelines/{tid}/detectors/entropy").status_code == 403
    )


@pytest.mark.asyncio
async def test_deleting_a_baseline_unconfigures_the_detectors_framed_on_it(
    client, admin_bootstrap, store
):
    """A configured entry whose baseline is gone is a question nobody can ask.

    Left behind, it 404s `/analysis/findings` on every rail open, renders as a
    bare "vs baseline" chip and seeds the wizard with an id its own picker
    cannot show. So the definition takes them with it — audited one by one, and
    named in the response so the UI can say what went.
    """
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    baseline_id = _baseline(client, case_id, tid)
    other_id = _baseline(client, case_id, tid)

    assert (
        _put(
            client, case_id, tid, "frequency", {"frame": "baseline", "baseline_id": baseline_id}
        ).status_code
        == 200
    )
    assert (
        _put(
            client, case_id, tid, "value_novelty", {"frame": "baseline", "baseline_id": other_id}
        ).status_code
        == 200
    )
    assert _put(client, case_id, tid, "entropy", {"params": {}}).status_code == 200

    resp = client.delete(f"/api/cases/{case_id}/timelines/{tid}/baselines/{baseline_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["unconfigured_detectors"] == ["frequency"]

    # The other baseline's entry and the self-framed one are untouched.
    assert sorted(e["method"] for e in _get(client, case_id, tid)["detectors"]) == [
        "entropy",
        "value_novelty",
    ]

    entries = await store.query_audit(case_id=case_id)
    rm_row = next(
        e
        for e in entries
        if e.action == "timeline.remove_detector" and e.detail["method"] == "frequency"
    )
    assert rm_row.detail["reason"] == f"baseline {baseline_id} deleted"
    delete_row = next(e for e in entries if e.action == "baseline.delete")
    assert delete_row.detail["unconfigured_detectors"] == ["frequency"]


def test_deleting_an_unused_baseline_leaves_the_detectors_alone(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tid = _case_and_timeline(client)
    baseline_id = _baseline(client, case_id, tid)
    assert _put(client, case_id, tid, "entropy", {"params": {}}).status_code == 200

    resp = client.delete(f"/api/cases/{case_id}/timelines/{tid}/baselines/{baseline_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["unconfigured_detectors"] == []
    assert [e["method"] for e in _get(client, case_id, tid)["detectors"]] == ["entropy"]
