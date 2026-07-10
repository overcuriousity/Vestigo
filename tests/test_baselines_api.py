"""Tests for the baseline-definition and detector-allowlist CRUD API."""

from __future__ import annotations

from tests.conftest import as_admin


def _setup_case(client) -> tuple[str, str]:
    """Create a case and return (case_id, default_timeline_id)."""
    case = client.post("/api/cases/", json={"name": "baseline-case"}).json()["case"]
    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    return case["id"], timelines[0]["id"]


def _payload(**overrides) -> dict:
    body = {
        "name": "incident-1",
        "baseline_start": "2026-01-01T00:00:00Z",
        "baseline_end": "2026-01-15T00:00:00Z",
        "suspect_windows": [
            {
                "label": "exfil-window",
                "start": "2026-02-02T00:00:00Z",
                "end": "2026-02-04T00:00:00Z",
            }
        ],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Baseline definitions
# ---------------------------------------------------------------------------


def test_baseline_crud_round_trip(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = f"/api/cases/{case_id}/timelines/{tl_id}/baselines"

    resp = client.post(base, json=_payload())
    assert resp.status_code == 200, resp.text
    created = resp.json()["baseline"]
    assert created["name"] == "incident-1"
    assert created["baseline"]["start"].startswith("2026-01-01")
    assert created["suspect_windows"][0]["label"] == "exfil-window"
    assert len(created["config_hash"]) == 64
    assert resp.json()["warnings"] == []

    listed = client.get(base).json()["baselines"]
    assert [b["id"] for b in listed] == [created["id"]]

    # Update replaces windows; the derived config_hash changes with them.
    resp = client.put(
        f"{base}/{created['id']}",
        json=_payload(
            name="incident-1b",
            suspect_windows=[
                {
                    "label": "later-window",
                    "start": "2026-03-01T00:00:00Z",
                    "end": "2026-03-02T00:00:00Z",
                }
            ],
        ),
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["baseline"]
    assert updated["name"] == "incident-1b"
    assert updated["config_hash"] != created["config_hash"]

    resp = client.delete(f"{base}/{created['id']}")
    assert resp.status_code == 200
    assert client.get(base).json()["baselines"] == []
    assert client.delete(f"{base}/{created['id']}").status_code == 404


def test_baseline_validation_rejects_contradictions(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = f"/api/cases/{case_id}/timelines/{tl_id}/baselines"

    # Inverted baseline range.
    resp = client.post(
        base,
        json=_payload(baseline_start="2026-01-15T00:00:00Z", baseline_end="2026-01-01T00:00:00Z"),
    )
    assert resp.status_code == 422

    # Suspect window overlapping the baseline is a contradiction.
    resp = client.post(
        base,
        json=_payload(
            suspect_windows=[
                {"label": "w", "start": "2026-01-10T00:00:00Z", "end": "2026-01-20T00:00:00Z"}
            ]
        ),
    )
    assert resp.status_code == 422
    assert "overlaps the baseline" in resp.json()["detail"]

    # Inverted suspect window.
    resp = client.post(
        base,
        json=_payload(
            suspect_windows=[
                {"label": "w", "start": "2026-02-04T00:00:00Z", "end": "2026-02-02T00:00:00Z"}
            ]
        ),
    )
    assert resp.status_code == 422

    # Duplicate labels.
    resp = client.post(
        base,
        json=_payload(
            suspect_windows=[
                {"label": "w", "start": "2026-02-02T00:00:00Z", "end": "2026-02-03T00:00:00Z"},
                {"label": "w", "start": "2026-02-05T00:00:00Z", "end": "2026-02-06T00:00:00Z"},
            ]
        ),
    )
    assert resp.status_code == 422

    # Zero suspect windows.
    resp = client.post(base, json=_payload(suspect_windows=[]))
    assert resp.status_code == 422

    # More than the cap.
    windows = [
        {
            "label": f"w{i}",
            "start": f"2026-02-{i + 1:02d}T00:00:00Z",
            "end": f"2026-02-{i + 1:02d}T12:00:00Z",
        }
        for i in range(11)
    ]
    resp = client.post(base, json=_payload(suspect_windows=windows))
    assert resp.status_code == 422


def test_suspect_window_overlap_is_warning_not_error(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = f"/api/cases/{case_id}/timelines/{tl_id}/baselines"
    resp = client.post(
        base,
        json=_payload(
            suspect_windows=[
                {"label": "a", "start": "2026-02-02T00:00:00Z", "end": "2026-02-04T00:00:00Z"},
                {"label": "b", "start": "2026-02-03T00:00:00Z", "end": "2026-02-05T00:00:00Z"},
            ]
        ),
    )
    assert resp.status_code == 200, resp.text
    assert any("overlap" in w for w in resp.json()["warnings"])


def test_baseline_scoped_to_timeline_and_audited(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    other_tl = client.post(f"/api/cases/{case_id}/timelines", json={"name": "tl2"}).json()[
        "timeline"
    ]
    base = f"/api/cases/{case_id}/timelines/{tl_id}/baselines"
    created = client.post(base, json=_payload()).json()["baseline"]

    # Not visible or mutable through another timeline's collection.
    other_base = f"/api/cases/{case_id}/timelines/{other_tl['id']}/baselines"
    assert client.get(other_base).json()["baselines"] == []
    assert client.delete(f"{other_base}/{created['id']}").status_code == 404

    # Unknown timeline 404s.
    resp = client.get(f"/api/cases/{case_id}/timelines/nope/baselines")
    assert resp.status_code == 404

    # Audit rows exist for the create.
    audit = client.get("/api/admin/audit", params={"action": "baseline.create"}).json()
    assert any(row["target_id"] == created["id"] for row in audit["audit"])


# ---------------------------------------------------------------------------
# Detector allowlist
# ---------------------------------------------------------------------------


def test_allowlist_endpoints_removed(client):
    """The /allowlist endpoints were folded into the disposition taxonomy
    (see routers/dispositions.py); they must no longer exist. Checked against
    the route table because the SPA catch-all answers unknown paths with the
    app shell rather than a 404."""
    api_paths = list(client.get("/api/openapi.json").json()["paths"])
    assert not any("allowlist" in p for p in api_paths)
    assert any(p.endswith("/dispositions") for p in api_paths)
