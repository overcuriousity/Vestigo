"""The findings endpoint: params object in, cached and scope-stamped results out."""

from __future__ import annotations

import asyncio
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
    """Count detector invocations so cache hits are observable.

    ``calls["results"]`` is what the stub returns as findings — tests that care
    about response shaping (dismissals, confirmations) fill it in.
    """
    calls: dict[str, list] = {"detector": [], "templates": [], "results": []}

    async def _fake_detector(case_id, timeline_id, source_ids, **kwargs):
        calls["detector"].append(kwargs)
        findings = [dict(f) for f in calls["results"]]

        class _Result:
            status = "ok"
            detector = kwargs["detector"]
            method = "stub"
            baseline_size = 0
            results: list = findings
            z_threshold = None
            warnings: list = []
            windows = None
            total_findings = len(findings)

        return _Result(), {
            "baseline_id": kwargs.get("baseline_id"),
            "windows": None,
            "windows_hash": None,
            "dispositions_hash": "dh-stub",
            "dispositions_count": 0,
        }

    async def _fake_templates(
        case_id,
        timeline_id,
        source_ids,
        params,
        limit,
        baseline_id=None,
        field_mappings=None,
        source_offsets=None,
    ):
        calls["templates"].append(
            {
                **params,
                "_baseline_id": baseline_id,
                "_field_mappings": field_mappings,
                "_source_offsets": source_offsets,
            }
        )
        return {"status": "ok", "results": [], "total_findings": 0, "warnings": []}

    def _fake_serialize(result):
        # The stub's findings are already wire-shaped dicts, so the real
        # dataclass-dispatching serializer has nothing to dispatch on.
        return {
            "status": result.status,
            "detector": result.detector,
            "method": result.method,
            "baseline_size": result.baseline_size,
            "results": result.results,
            "z_threshold": result.z_threshold,
            "warnings": result.warnings,
            "windows": result.windows,
            "total_findings": result.total_findings,
        }

    monkeypatch.setattr(analysis_router, "_run_stat_detector", _fake_detector)
    monkeypatch.setattr(analysis_router, "_run_log_templates", _fake_templates)
    monkeypatch.setattr(analysis_router, "_serialize_stat_result", _fake_serialize)
    return calls


@pytest.fixture()
def scoped_source(monkeypatch):
    """Give the timeline one source id, so event-scoped verdicts are in scope.

    ``list_dispositions`` reaches event-scoped rows through the timeline's
    sources (they carry ``timeline_id = NULL``), and a freshly created case has
    none. Ingesting a real file to get one would be a detour for a test about
    response shaping.
    """

    async def _fake_scope(case_id, timeline_id):
        return ["s-1"], {}, {}

    monkeypatch.setattr(analysis_router, "_resolve_timeline_scope", _fake_scope)
    return "s-1"


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


def test_the_mode_the_detector_ran_in_survives_the_method_id(client, seeded, stub_detector):
    """``method`` on a detector result is the *mode* it ran in, not the method id.

    "cadence", "self-baseline", "temporal", "z-score" — provenance the older
    ``/anomalies`` responses carried. Stamping the requested method id over the
    key used to drop it from both the response and the cached payload.
    """
    case_id, timeline_id = seeded
    url = _url(case_id, timeline_id, "value_novelty")
    body = client.get(url).json()
    assert body["method"] == "value_novelty"
    assert body["analysis_mode"] == "stub"
    # And it is cached with it, not recovered by rerunning.
    cached = client.get(url).json()
    assert cached["cache"] == "hit"
    assert cached["analysis_mode"] == "stub"


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
    assert stub_detector["templates"] == [
        {
            "field": "message",
            "order": "count",
            "only_new": False,
            "_baseline_id": None,
            # Handed down from the caller's single scope resolve, not resolved
            # again inside the runner.
            "_field_mappings": None,
            "_source_offsets": None,
        }
    ]
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


def test_include_dismissed_reuses_the_cache(client, seeded, stub_detector):
    """A presentation-only reveal must not force a rescan.

    Dismissals are applied to the response *after* the cache, so revealing them
    changes what is shown and never what is computed — the same cached answer
    serves both, and the reveal costs nothing.
    """
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "value_novelty"))
    r = client.get(_url(case_id, timeline_id, "value_novelty", extra="&include_dismissed=true"))
    assert len(stub_detector["detector"]) == 1
    assert r.json()["cache"] == "hit"


def test_a_different_limit_is_a_different_answer(client, seeded, stub_detector):
    """The runners truncate to `limit`, so 50 rows is not the answer to 500."""
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "value_novelty", extra="&limit=50"))
    r = client.get(_url(case_id, timeline_id, "value_novelty", extra="&limit=500"))
    assert len(stub_detector["detector"]) == 2
    assert r.json()["cache"] == "miss"


def test_a_list_of_fields_is_accepted(client, seeded, stub_detector):
    """A JSON list is the natural encoding of a multi-field knob."""
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "value_novelty", {"fields": ["host", "user"]}))
    assert r.status_code == 200, r.text
    assert stub_detector["detector"][0]["fields"] == "host,user"


@pytest.mark.parametrize(
    "method,params",
    [
        ("frequency", {"z_threshold": "abc"}),
        ("frequency", {"z_threshold": 0}),
        ("sequence_novelty", {"ngram_size": 9}),
        ("proportion_shift", {"fdr_q": 2.0}),
        ("log_template", {"order": "sideways"}),
    ],
)
def test_out_of_contract_param_values_are_422_not_500(
    client, seeded, stub_detector, method, params
):
    """The runners take these unchecked — an unvalidated value is a 500."""
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, method, params))
    assert r.status_code == 422, r.text
    assert stub_detector["detector"] == []


def test_detector_receives_field_mappings_and_offsets(client, seeded, stub_detector):
    """Both come from _resolve_timeline_scope; dropping either silently loses
    canonical field aliases or per-source clock-skew correction."""
    case_id, timeline_id = seeded
    client.get(_url(case_id, timeline_id, "value_novelty"))
    kwargs = stub_detector["detector"][0]
    assert "field_mappings" in kwargs
    assert "source_offsets" in kwargs


def _dispose(client, case_id, timeline_id, body):
    r = client.post(f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_a_dismissed_finding_stays_dismissed_across_a_cache_hit(
    client, seeded, stub_detector, scoped_source
):
    """The regression this endpoint shipped with: dismissals were never applied,
    so a finding the analyst dismissed came straight back on the next refetch."""
    case_id, timeline_id = seeded
    stub_detector["results"] = [
        {
            "type": "value_novelty",
            "score": 1.0,
            "details": {"allowlist_field": "f", "allowlist_value": "v"},
        },
        {
            "type": "value_novelty",
            "score": 0.5,
            "details": {"allowlist_field": "f", "allowlist_value": "keep"},
        },
    ]
    url = _url(case_id, timeline_id, "value_novelty")
    assert len(client.get(url).json()["results"]) == 2

    _dispose(
        client,
        case_id,
        timeline_id,
        {"kind": "dismissed", "detector": "value_novelty", "field": "f", "value": "v"},
    )
    body = client.get(url).json()
    assert body["cache"] == "hit"
    assert [f["details"]["allowlist_value"] for f in body["results"]] == ["keep"]
    assert body["dismissed_count"] == 1


def test_include_dismissed_flags_rather_than_filters(client, seeded, stub_detector, scoped_source):
    case_id, timeline_id = seeded
    stub_detector["results"] = [
        {
            "type": "value_novelty",
            "score": 1.0,
            "details": {"allowlist_field": "f", "allowlist_value": "v"},
        },
    ]
    _dispose(
        client,
        case_id,
        timeline_id,
        {"kind": "dismissed", "detector": "value_novelty", "field": "f", "value": "v"},
    )
    body = client.get(
        _url(case_id, timeline_id, "value_novelty", extra="&include_dismissed=true")
    ).json()
    assert len(body["results"]) == 1
    assert body["results"][0]["dismissed"] is True
    assert body["dismissed_count"] == 1


def test_a_confirmed_finding_carries_the_flag_its_badge_reads(
    client, seeded, stub_detector, scoped_source
):
    case_id, timeline_id = seeded
    stub_detector["results"] = [
        {"type": "value_novelty", "score": 1.0, "event_id": "e-1", "details": {}},
        {"type": "value_novelty", "score": 0.5, "event_id": "e-2", "details": {}},
    ]
    _dispose(
        client,
        case_id,
        timeline_id,
        {
            "kind": "confirmed",
            "detector": "value_novelty",
            "source_id": scoped_source,
            "event_id": "e-1",
        },
    )
    body = client.get(_url(case_id, timeline_id, "value_novelty")).json()
    flags = {f["event_id"]: f.get("confirmed") for f in body["results"]}
    assert flags == {"e-1": True, "e-2": None}


def test_the_cached_payload_is_not_rewritten_by_a_verdict(
    client, seeded, stub_detector, scoped_source
):
    """Dispositions are applied to the response, never to the stored answer.

    If the filtered payload were cached, revealing dismissed findings later
    could not show what was filtered — the row would no longer contain it.
    """
    case_id, timeline_id = seeded
    stub_detector["results"] = [
        {
            "type": "value_novelty",
            "score": 1.0,
            "details": {"allowlist_field": "f", "allowlist_value": "v"},
        },
    ]
    client.get(_url(case_id, timeline_id, "value_novelty"))
    _dispose(
        client,
        case_id,
        timeline_id,
        {"kind": "dismissed", "detector": "value_novelty", "field": "f", "value": "v"},
    )
    assert client.get(_url(case_id, timeline_id, "value_novelty")).json()["results"] == []
    revealed = client.get(
        _url(case_id, timeline_id, "value_novelty", extra="&include_dismissed=true")
    ).json()
    assert len(revealed["results"]) == 1
    # One computation served all three requests.
    assert len(stub_detector["detector"]) == 1


def test_findings_requires_case_read(client, seeded, stub_detector):
    case_id, timeline_id = seeded
    client.post("/api/auth/logout")
    r = client.get(_url(case_id, timeline_id, "value_novelty"))
    assert r.status_code in (401, 403)


def test_editing_a_baseline_in_place_invalidates_its_cached_answers(client, seeded, stub_detector):
    """A definition keeps its id across a PUT, so the id alone is not the key.

    Without the definition's content hash in the fingerprint, an analyst who
    moves a suspect window and reopens Investigate is served the pre-edit
    findings — stamped with the new baseline's name and reported as a hit,
    which the cache docstring calls proof the answer still holds.
    """
    case_id, timeline_id = seeded
    definition = client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/baselines",
        json={
            "name": "feb",
            "baseline_start": "2024-02-01T00:00:00Z",
            "baseline_end": "2024-02-08T00:00:00Z",
            "suspect_windows": [
                {"label": "w1", "start": "2024-02-08T00:00:00Z", "end": "2024-02-09T00:00:00Z"}
            ],
        },
    ).json()["baseline"]
    url = _url(
        case_id,
        timeline_id,
        "frequency",
        extra=f"&frame=baseline&baseline_id={definition['id']}",
    )
    assert client.get(url).json()["cache"] == "miss"
    assert client.get(url).json()["cache"] == "hit"

    client.put(
        f"/api/cases/{case_id}/timelines/{timeline_id}/baselines/{definition['id']}",
        json={
            "name": "feb",
            "baseline_start": "2024-02-01T00:00:00Z",
            "baseline_end": "2024-02-08T00:00:00Z",
            "suspect_windows": [
                {"label": "w1", "start": "2024-02-10T00:00:00Z", "end": "2024-02-11T00:00:00Z"}
            ],
        },
    )
    assert client.get(url).json()["cache"] == "miss"
    assert len(stub_detector["detector"]) == 2


def test_a_changed_detector_setting_invalidates_the_cache(
    client, seeded, stub_detector, monkeypatch
):
    """The runners fall back to these whenever a knob is omitted."""
    from vestigo.core.config import get_settings

    case_id, timeline_id = seeded
    url = _url(case_id, timeline_id, "frequency")
    assert client.get(url).json()["cache"] == "miss"
    assert client.get(url).json()["cache"] == "hit"

    cfg = get_settings()
    monkeypatch.setattr(cfg, "stat_z_threshold", cfg.stat_z_threshold + 1.0)
    assert client.get(url).json()["cache"] == "miss"


@pytest.mark.parametrize(
    "extra",
    [
        "&frame=self&baseline_id=bl-1",
        "&frame=baseline",
    ],
)
def test_a_frame_that_disagrees_with_the_baseline_id_is_rejected(
    client, seeded, stub_detector, extra
):
    """The runner keys off the id; the response is stamped with the frame.

    Accepting the mismatch would run the two-window comparison and label it
    "all events scanned" (or the reverse) — and that label is what a verdict
    records as its provenance.
    """
    case_id, timeline_id = seeded
    r = client.get(_url(case_id, timeline_id, "value_novelty", extra=extra))
    assert r.status_code == 422, r.text
    assert stub_detector["detector"] == []


def _baseline(client, case_id, timeline_id, name, day):
    return client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/baselines",
        json={
            "name": name,
            "baseline_start": "2024-02-01T00:00:00Z",
            "baseline_end": f"2024-02-0{day}T00:00:00Z",
            "suspect_windows": [
                {
                    "label": "w1",
                    "start": f"2024-02-0{day}T00:00:00Z",
                    "end": f"2024-02-0{day + 1}T00:00:00Z",
                }
            ],
        },
    ).json()["baseline"]["id"]


def test_a_verdict_from_another_baseline_is_marked_not_badged(
    client, seeded, stub_detector, scoped_source
):
    """Confirming under one comparison must not claim the row under another.

    ``confirmed`` is the one disposition kind whose identity includes the
    scope, precisely so escalating a finding against February and again against
    March are two claims. If the February verdict badged the row under March,
    the UI would disable Confirm and the second claim could never be made — the
    per-scope dedupe would be unreachable code.

    The row is not left blank either: it carries ``confirmed_other_scope``, the
    "marked so you can re-examine them" the scope-change dialog promises.
    """
    case_id, timeline_id = seeded
    feb = _baseline(client, case_id, timeline_id, "feb", 8)
    mar = _baseline(client, case_id, timeline_id, "mar", 3)
    stub_detector["results"] = [
        {"type": "value_novelty", "score": 1.0, "event_id": "e-1", "details": {}},
    ]
    _dispose(
        client,
        case_id,
        timeline_id,
        {
            "kind": "confirmed",
            "detector": "value_novelty",
            "source_id": scoped_source,
            "event_id": "e-1",
            "analysis_scope": {"frame": "baseline", "baseline_id": feb},
        },
    )

    under_feb = client.get(
        _url(case_id, timeline_id, "value_novelty", extra=f"&frame=baseline&baseline_id={feb}")
    ).json()["results"][0]
    assert under_feb.get("confirmed") is True
    assert under_feb.get("confirmed_other_scope") is None

    under_mar = client.get(
        _url(case_id, timeline_id, "value_novelty", extra=f"&frame=baseline&baseline_id={mar}")
    ).json()["results"][0]
    assert under_mar.get("confirmed") is None
    assert under_mar.get("confirmed_other_scope") is True


def test_a_verdict_recorded_before_scope_provenance_still_badges_everywhere(
    client, seeded, stub_detector, scoped_source
):
    """A NULL scope means "nobody recorded one", not "reached under self".

    Demoting those rows would silently unbadge existing evidence to assert a
    comparison the database never stored.
    """
    case_id, timeline_id = seeded
    feb = _baseline(client, case_id, timeline_id, "feb", 8)
    stub_detector["results"] = [
        {"type": "value_novelty", "score": 1.0, "event_id": "e-1", "details": {}},
    ]
    _dispose(
        client,
        case_id,
        timeline_id,
        {
            "kind": "confirmed",
            "detector": "value_novelty",
            "source_id": scoped_source,
            "event_id": "e-1",
        },
    )
    for extra in ("", f"&frame=baseline&baseline_id={feb}"):
        body = client.get(_url(case_id, timeline_id, "value_novelty", extra=extra)).json()
        assert body["results"][0].get("confirmed") is True


def test_log_templates_report_the_total_before_the_limit(monkeypatch):
    """ "Showing N of M" has to name the M that was measured, not the cap.

    Every scored method returns `total_findings` counted before the `limit`
    cap, and the rail's count reads it that way. Returning `len(results)` for
    templates made a timeline with 400 distinct shapes report exactly 50, with
    nothing on screen saying anything had been cut.
    """
    from dataclasses import dataclass

    @dataclass
    class _Result:
        field: str
        total_templates: int
        templates: list

    class _Svc:
        def list_log_templates(self, **kwargs):
            return _Result(
                field="message",
                total_templates=400,
                templates=[{"template": f"t{i}", "count": 1} for i in range(50)],
            )

    monkeypatch.setattr(analysis_router, "_get_stat_anomaly_service", lambda: _Svc())
    body = asyncio.run(
        analysis_router._run_log_templates("c-1", "tl-1", ["s-1"], {}, 50, baseline_id=None)
    )
    assert len(body["results"]) == 50
    assert body["total_findings"] == 400


def test_log_templates_do_not_re_resolve_the_timeline_scope(monkeypatch):
    """The caller resolved the scope; a second resolve is a second source list.

    Two independently-resolved lists are equal only by construction, so a later
    change to either resolution point would diverge silently — and the runner
    was already scanning the caller's list while discarding its own.
    """
    seen: dict[str, object] = {}

    class _Svc:
        def list_log_templates(self, **kwargs):
            seen.update(kwargs)

            class _R:
                field = "message"
                total_templates = 0
                templates: list = []

            from dataclasses import make_dataclass

            return make_dataclass("R", ["field", "total_templates", "templates"])("message", 0, [])

    async def _explode(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("_run_log_templates re-resolved the timeline scope")

    monkeypatch.setattr(analysis_router, "_get_stat_anomaly_service", lambda: _Svc())
    monkeypatch.setattr(analysis_router, "_resolve_timeline_scope", _explode)
    asyncio.run(
        analysis_router._run_log_templates(
            "c-1",
            "tl-1",
            ["s-1"],
            {},
            50,
            field_mappings={"host": ["attr:hostname"]},
            source_offsets={"s-1": 60},
        )
    )
    assert seen["source_ids"] == ["s-1"]
    assert seen["field_mappings"] == {"host": ["attr:hostname"]}
    assert seen["source_offsets"] == {"s-1": 60}
