"""The findings endpoint: params object in, cached and scope-stamped results out."""

from __future__ import annotations

import json

import pytest

from tests.conftest import as_admin
from vestigo.api.routers import analysis as analysis_router


@pytest.fixture()
def seeded(client, admin_bootstrap) -> tuple[str, str]:
    """Log in as admin, create a case, return (case_id, its default timeline_id)."""
    as_admin(client, admin_bootstrap)
    case = client.post("/api/cases/", json={"name": "findings-case"}).json()["case"]
    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    return case["id"], timelines[0]["id"]


@pytest.fixture()
def stub_detector(monkeypatch):
    """Count detector invocations so cache hits are observable."""
    calls: dict[str, list] = {"detector": [], "templates": []}

    async def _fake_detector(case_id, timeline_id, source_ids, **kwargs):
        calls["detector"].append(kwargs)

        class _Result:
            status = "ok"
            detector = kwargs["detector"]
            method = "stub"
            baseline_size = 0
            results: list = []
            z_threshold = None
            warnings: list = []
            windows = None
            total_findings = 0

        return _Result(), {
            "baseline_id": kwargs.get("baseline_id"),
            "windows": None,
            "windows_hash": None,
            "dispositions_hash": "dh-stub",
            "dispositions_count": 0,
        }

    async def _fake_templates(case_id, timeline_id, source_ids, params, limit, baseline_id=None):
        calls["templates"].append({**params, "_baseline_id": baseline_id})
        return {"status": "ok", "results": [], "total_findings": 0, "warnings": []}

    monkeypatch.setattr(analysis_router, "_run_stat_detector", _fake_detector)
    monkeypatch.setattr(analysis_router, "_run_log_templates", _fake_templates)
    return calls


def _url(case_id, timeline_id, method, params=None, extra=""):
    query = f"?method={method}{extra}"
    if params is not None:
        query += "&params=" + json.dumps(params)
    return f"/api/cases/{case_id}/timelines/{timeline_id}/analysis/findings{query}"


def test_findings_returns_results_and_scope(client, seeded, stub_detector):
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "value_novelty"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "value_novelty"
    assert body["scope"]["frame"] == "self"
    assert body["cache"] == "miss"


def test_second_identical_request_is_served_from_cache(client, seeded, stub_detector):
    case_id, timeline_id = seeded
    url = _url(case_id, timeline_id, "value_novelty")
    first = client.get(url).json()
    second = client.get(url).json()
    assert len(stub_detector["detector"]) == 1
    assert second["cache"] == "hit"
    assert second["results"] == first["results"]
    assert second["scope"] == first["scope"]


def test_changed_params_miss_the_cache(client, seeded, stub_detector):
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "frequency", {"z_threshold": 3.0}))
    r = client.get(_url(case_id, timeline_id, "frequency", {"z_threshold": 2.0}))
    assert len(stub_detector["detector"]) == 2
    assert r.json()["cache"] == "miss"


def test_unknown_method_is_rejected(client, seeded, stub_detector):
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "not_a_method"))
    assert r.status_code == 422


def test_unknown_param_key_is_rejected_rather_than_silently_dropped(client, seeded, stub_detector):
    """A typo'd knob must not produce a cached answer under the wrong key."""
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "frequency", {"z_treshold": 3.0}))
    assert r.status_code == 422
    assert "z_treshold" in r.text


def test_gated_off_method_still_runs_when_asked(client, seeded, stub_detector):
    """The gate is advice. Nothing it skips may be unreachable."""
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "numeric_range"))
    assert r.status_code == 200
    assert len(stub_detector["detector"]) == 1


def test_log_template_runs_the_template_browser_not_a_detector(client, seeded, stub_detector):
    """log_template is not a _run_stat_detector method — routing it there would
    silently run a different analysis and label it as templates."""
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "log_template", {"field": "message"}))
    assert r.status_code == 200
    assert stub_detector["templates"] == [{"field": "message", "_baseline_id": None}]
    assert stub_detector["detector"] == []


def test_log_template_takes_its_baseline_from_the_scope_not_from_params(
    client, seeded, stub_detector
):
    """only_new splits at the baseline's end, so it must be the *scope's*
    baseline — otherwise a client could ask for templates new against one
    baseline while the response claimed the scope of another."""
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "log_template", {"baseline_id": "bl-1"}))
    assert r.status_code == 422
    assert "baseline_id" in r.text


def test_each_method_gets_its_own_cache_entry(client, seeded, stub_detector):
    """Two methods under one scope must not collide on one key."""
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "value_novelty"))
    client.get(_url(case_id, timeline_id, "value_combo"))
    assert len(stub_detector["detector"]) == 2


def test_include_dismissed_bypasses_the_cache(client, seeded, stub_detector):
    """A presentation-only reveal must not populate or serve the shared entry."""
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "value_novelty"))
    r = client.get(_url(case_id, timeline_id, "value_novelty", extra="&include_dismissed=true"))
    assert len(stub_detector["detector"]) == 2
    assert r.json()["cache"] == "miss"


def test_detector_receives_field_mappings_and_offsets(client, seeded, stub_detector):
    """Both come from _resolve_timeline_scope; dropping either silently loses
    canonical field aliases or per-source clock-skew correction."""
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "value_novelty"))
    kwargs = stub_detector["detector"][0]
    assert "field_mappings" in kwargs
    assert "source_offsets" in kwargs


def test_findings_requires_case_read(client, seeded, stub_detector):
    case_id, timeline_id = seeded
    client.post("/api/auth/logout")
    r = client.get(_url(case_id, timeline_id, "value_novelty"))
    assert r.status_code in (401, 403)
