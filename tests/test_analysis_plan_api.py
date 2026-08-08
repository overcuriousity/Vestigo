"""The plan endpoint: gate verdicts over real timeline data.

Uses the shared SQLite store + TestClient fixtures from conftest (auth is
cookie-based, established by ``as_admin``). The ClickHouse-dependent piece —
assembling the inventory snapshot — is stubbed at the router's collector
boundary; the predicates themselves are covered exhaustively in
``tests/test_analysis_plan.py``, and stubbing here keeps the endpoint's own
contract (shape, scope echo, auth) testable without a ClickHouse.
"""

from __future__ import annotations

import pytest

from tests.conftest import as_admin
from vestigo.api.routers import analysis as analysis_router
from vestigo.db.analysis_plan import METHOD_IDS, PlanInputs


@pytest.fixture()
def seeded(client, admin_bootstrap) -> tuple[str, str]:
    """Log in as admin, create a case, return (case_id, its default timeline_id)."""
    as_admin(client, admin_bootstrap)
    case = client.post("/api/cases/", json={"name": "gate-case"}).json()["case"]
    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    return case["id"], timelines[0]["id"]


@pytest.fixture()
def stub_inputs(monkeypatch):
    """Replace the ClickHouse-backed collector with a fixed snapshot."""

    def _apply(**over):
        base = {
            "inventory": [("artifact", 5, 1000), ("message", 900, 1000)],
            "numeric_tokens": [],
            "series_distinct": 5,
            "events_total": 1000,
            "span_seconds": 86_400.0,
            "frame": "self",
            "has_active_baseline": False,
        }
        base.update(over)

        async def _fake(case_id, timeline_id, source_ids, frame, baseline_id, field_mappings):
            return PlanInputs(**base)

        monkeypatch.setattr(analysis_router, "_collect_plan_inputs", _fake)

    return _apply


def _plan_url(case_id: str, timeline_id: str, query: str = "") -> str:
    return f"/api/cases/{case_id}/timelines/{timeline_id}/analysis/plan{query}"


def test_plan_lists_every_method_with_a_status(client, seeded, stub_inputs):
    stub_inputs()
    case_id, timeline_id = seeded
    r = client.get(_plan_url(case_id, timeline_id))
    assert r.status_code == 200, r.text
    methods = r.json()["methods"]
    assert [m["method"] for m in methods] == list(METHOD_IDS)
    assert all(m["status"] in {"applicable", "not_applicable", "needs_setup"} for m in methods)
    assert all("cost_class" in m for m in methods)


def test_plan_reports_reason_facts_for_a_gated_method(client, seeded, stub_inputs):
    stub_inputs(numeric_tokens=[])
    case_id, timeline_id = seeded
    r = client.get(_plan_url(case_id, timeline_id))
    numeric = next(m for m in r.json()["methods"] if m["method"] == "numeric_range")
    assert numeric["status"] == "not_applicable"
    assert numeric["reason_facts"]["numeric_fields"] == 0


def test_plan_echoes_the_scope_it_planned_under(client, seeded, stub_inputs):
    stub_inputs()
    case_id, timeline_id = seeded
    r = client.get(_plan_url(case_id, timeline_id))
    scope = r.json()["scope"]
    assert scope["frame"] == "self"
    assert scope["baseline_id"] is None


def test_plan_rejects_an_unknown_baseline(client, seeded, stub_inputs):
    """A scope naming a baseline that does not exist must not silently plan self-frame."""
    stub_inputs()
    case_id, timeline_id = seeded
    r = client.get(_plan_url(case_id, timeline_id, "?frame=baseline&baseline_id=nope"))
    assert r.status_code == 404


def test_plan_requires_case_read(client, seeded, stub_inputs):
    stub_inputs()
    case_id, timeline_id = seeded
    client.post("/api/auth/logout")
    r = client.get(_plan_url(case_id, timeline_id))
    assert r.status_code in (401, 403)
