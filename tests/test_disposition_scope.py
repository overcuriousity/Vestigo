"""A verdict must record the comparison it was reached under.

Without this, "confirmed on 4 March" says nothing about *what the finding was
compared against*, so a report cannot state which baseline produced the finding
the analyst escalated. The column is called ``analysis_scope`` because
``FindingDisposition`` already uses "scope" for the value-vs-event distinction.
"""

from __future__ import annotations

import pytest

from tests.conftest import as_admin

SCOPE = {"frame": "baseline", "baseline_id": "bl-1", "baseline_name": "Feb 24 – Mar 1"}


@pytest.fixture()
def seeded(client, admin_bootstrap) -> tuple[str, str]:
    as_admin(client, admin_bootstrap)
    case = client.post("/api/cases/", json={"name": "verdict-case"}).json()["case"]
    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    return case["id"], timelines[0]["id"]


def _confirm(client, case_id, timeline_id, analysis_scope=None, value="curl/7.68.0"):
    body = {
        "kind": "normal",
        "detector": "value_novelty",
        "field": "attr:user_agent",
        "value": value,
    }
    if analysis_scope is not None:
        body["analysis_scope"] = analysis_scope
    return client.post(f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions", json=body)


def _list(client, case_id, timeline_id):
    return client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions").json()[
        "dispositions"
    ]


def test_scope_round_trips_on_a_verdict(client, seeded):
    case_id, timeline_id = seeded
    created = _confirm(client, case_id, timeline_id, SCOPE)
    assert created.status_code == 200, created.text
    assert created.json()["disposition"]["analysis_scope"] == SCOPE
    assert _list(client, case_id, timeline_id)[0]["analysis_scope"] == SCOPE


def test_a_verdict_without_scope_reads_as_not_recorded(client, seeded):
    """Pre-existing rows and API clients that omit it must stay valid."""
    case_id, timeline_id = seeded
    created = _confirm(client, case_id, timeline_id, None)
    assert created.status_code == 200, created.text
    assert created.json()["disposition"]["analysis_scope"] is None


def test_scope_does_not_enter_the_dispositions_hash():
    """Scope is provenance, not a detection input.

    Extending the hash would invalidate the reproducibility record of every
    existing DetectorRun for no detection benefit.
    """
    from vestigo.db.postgres import FindingDisposition, dispositions_hash

    def _row(analysis_scope):
        return FindingDisposition(
            id="d1",
            case_id="c",
            timeline_id="t",
            kind="normal",
            detector="value_novelty",
            field="attr:user_agent",
            value="curl/7.68.0",
            analysis_scope=analysis_scope,
        )

    assert dispositions_hash([_row(None)]) == dispositions_hash([_row(SCOPE)])


def test_two_verdicts_under_different_scopes_both_persist(client, seeded):
    """Two comparisons are two assertions; neither overwrites the other."""
    case_id, timeline_id = seeded
    _confirm(client, case_id, timeline_id, SCOPE, value="a")
    other = {**SCOPE, "baseline_id": "bl-2", "baseline_name": "Mar 1 – Mar 2"}
    _confirm(client, case_id, timeline_id, other, value="b")

    scopes = sorted(d["analysis_scope"]["baseline_id"] for d in _list(client, case_id, timeline_id))
    assert scopes == ["bl-1", "bl-2"]


def test_repeating_a_verdict_under_the_same_scope_stays_idempotent(client, seeded):
    """Dedupe is by scope key; recording the same verdict twice is a no-op."""
    case_id, timeline_id = seeded
    first = _confirm(client, case_id, timeline_id, SCOPE).json()["disposition"]
    second = _confirm(client, case_id, timeline_id, SCOPE).json()["disposition"]
    assert first["id"] == second["id"]
    assert len(_list(client, case_id, timeline_id)) == 1


def test_a_confirmed_verdict_is_per_scope_but_a_normal_one_is_not(client, seeded):
    """The two verdict families mean different things.

    ``confirmed`` asserts something about a *comparison*: escalating a finding
    against the February baseline and again against the March one are two
    claims, and collapsing them loses one. ``normal`` is a standing declaration
    about a value, effective under every frame — duplicating it per scope would
    inflate the dispositions hash and the burndown without saying anything new.
    """
    case_id, timeline_id = seeded
    other = {**SCOPE, "baseline_id": "bl-2", "baseline_name": "Mar 1 – Mar 2"}

    def _post(kind, analysis_scope):
        return client.post(
            f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions",
            json={
                "kind": kind,
                "detector": "value_novelty",
                **(
                    {"source_id": "s1", "event_id": "e1"}
                    if kind == "confirmed"
                    else {"field": "attr:user_agent", "value": "curl/7.68.0"}
                ),
                "analysis_scope": analysis_scope,
            },
        ).json()["disposition"]

    assert _post("confirmed", SCOPE)["id"] != _post("confirmed", other)["id"]
    assert _post("normal", SCOPE)["id"] == _post("normal", other)["id"]


def test_key_order_does_not_split_one_verdict_into_two(client, seeded):
    """Dedupe compares the stored scope, and the two dialects disagree on what
    that means: JSONB normalizes key order, SQLite's JSON text does not. Both
    only agree if the canonical form is what gets written and compared."""
    case_id, timeline_id = seeded
    reordered = {
        "baseline_name": SCOPE["baseline_name"],
        "frame": SCOPE["frame"],
        "baseline_id": SCOPE["baseline_id"],
    }

    def _post(analysis_scope):
        return client.post(
            f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions",
            json={
                "kind": "confirmed",
                "detector": "value_novelty",
                "source_id": "s1",
                "event_id": "e1",
                "analysis_scope": analysis_scope,
            },
        ).json()["disposition"]

    assert _post(SCOPE)["id"] == _post(reordered)["id"]


def test_bulk_verdicts_carry_scope_too(client, seeded):
    case_id, timeline_id = seeded
    r = client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions/bulk",
        json={
            "items": [
                {
                    "kind": "normal",
                    "detector": "value_novelty",
                    "field": "attr:user_agent",
                    "value": v,
                    "analysis_scope": SCOPE,
                }
                for v in ("curl/7.68.0", "wget/1.21")
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert all(d["analysis_scope"] == SCOPE for d in r.json()["dispositions"])


def _post_confirmed(client, case_id, timeline_id, analysis_scope):
    body = {
        "kind": "confirmed",
        "detector": "value_novelty",
        "source_id": "s1",
        "event_id": "e1",
    }
    if analysis_scope is not None:
        body["analysis_scope"] = analysis_scope
    return client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions", json=body
    ).json()["disposition"]


def test_reconfirming_an_unstamped_verdict_adopts_the_scope_rather_than_duplicating(
    client, seeded
):
    """Every ``confirmed`` row written before scope provenance existed has none.

    The frontend now always sends a scope, so without adoption the first
    re-confirm after the upgrade writes a second row for the same event and
    inflates the triage counts once per upgraded case.
    """
    case_id, timeline_id = seeded
    before = _post_confirmed(client, case_id, timeline_id, None)
    assert before["analysis_scope"] is None

    after = _post_confirmed(client, case_id, timeline_id, SCOPE)
    assert after["id"] == before["id"]
    assert after["analysis_scope"] == SCOPE

    # Adoption is once, not a wildcard: a genuinely different comparison is
    # still a separate claim.
    other = {**SCOPE, "baseline_id": "bl-2", "baseline_name": "Mar 1 – Mar 2"}
    assert _post_confirmed(client, case_id, timeline_id, other)["id"] != after["id"]


def test_an_exact_scope_match_wins_over_an_unstamped_row(client, seeded):
    """Adoption must not shadow a row that already carries this scope."""
    case_id, timeline_id = seeded
    unstamped = _post_confirmed(client, case_id, timeline_id, None)
    other = {**SCOPE, "baseline_id": "bl-2", "baseline_name": "Mar 1 – Mar 2"}
    # The unstamped row adopts `other`, then a second scope creates its own row.
    adopted = _post_confirmed(client, case_id, timeline_id, other)
    assert adopted["id"] == unstamped["id"]
    stamped = _post_confirmed(client, case_id, timeline_id, SCOPE)
    assert stamped["id"] != unstamped["id"]
    # Repeating the exact scope returns the row carrying it, not the older one.
    assert _post_confirmed(client, case_id, timeline_id, SCOPE)["id"] == stamped["id"]


def test_bulk_reconfirming_adopts_the_scope_too(client, seeded):
    """The identity rule is stated in both paths; a drift writes a duplicate."""
    case_id, timeline_id = seeded
    before = _post_confirmed(client, case_id, timeline_id, None)
    r = client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions/bulk",
        json={
            "items": [
                {
                    "kind": "confirmed",
                    "detector": "value_novelty",
                    "source_id": "s1",
                    "event_id": "e1",
                    "analysis_scope": SCOPE,
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispositions"][0]["id"] == before["id"]
    assert r.json()["dispositions"][0]["analysis_scope"] == SCOPE
