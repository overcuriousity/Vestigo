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


def test_a_renamed_baseline_does_not_split_one_verdict_into_two(client, seeded):
    """Only ``frame`` and ``baseline_id`` identify a comparison.

    The scope object an analyst echoes back also carries display material and a
    ``dispositions_hash`` that moves every time any verdict is recorded.
    Comparing the whole object would make a rename — or the analyst's own
    previous click — read as a different comparison, so re-confirming one
    finding would write a second row and a second system annotation for one
    claim. This is the same narrowing the findings endpoints badge by.
    """
    case_id, timeline_id = seeded
    renamed = {**SCOPE, "baseline_name": "Feb 24 – Mar 1 (revised)"}
    rehashed = {**SCOPE, "dispositions_hash": "0" * 64}

    first = _post_confirmed(client, case_id, timeline_id, SCOPE)
    assert _post_confirmed(client, case_id, timeline_id, renamed)["id"] == first["id"]
    assert _post_confirmed(client, case_id, timeline_id, rehashed)["id"] == first["id"]
    # The row keeps the scope it was written with: the narrowing governs
    # identity only, never what the audit column records.
    assert first["analysis_scope"] == SCOPE


def test_bulk_dedupe_narrows_the_scope_the_same_way(client, seeded):
    """The bulk path expresses the identity rule through the same helper.

    Two expressions of it drift, and a drifted dedupe writes the duplicate the
    single-row path just refused.
    """
    case_id, timeline_id = seeded
    first = _post_confirmed(client, case_id, timeline_id, SCOPE)
    r = client.post(
        f"/api/cases/{case_id}/timelines/{timeline_id}/dispositions/bulk",
        json={
            "items": [
                {
                    "kind": "confirmed",
                    "detector": "value_novelty",
                    "source_id": "s1",
                    "event_id": "e1",
                    "analysis_scope": {**SCOPE, "baseline_name": "renamed"},
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispositions"][0]["id"] == first["id"]


def test_an_unstamped_verdict_is_never_backfilled_with_a_scope(client, seeded):
    """A ``confirmed`` row carrying no scope stays that way, forever.

    Every such row was written before scope provenance existed, under a
    comparison nobody recorded. Stamping today's scope onto it would make the
    audit column assert a frame the verdict was not reached under — which is
    exactly what migration 0026 left the column nullable to avoid. Re-confirming
    writes a new, stamped row instead.
    """
    case_id, timeline_id = seeded
    before = _post_confirmed(client, case_id, timeline_id, None)
    assert before["analysis_scope"] is None

    after = _post_confirmed(client, case_id, timeline_id, SCOPE)
    assert after["id"] != before["id"]
    assert after["analysis_scope"] == SCOPE

    # And the old row is untouched by that write: it still answers for "no
    # scope recorded", which is the only claim it can honestly make.
    again = _post_confirmed(client, case_id, timeline_id, None)
    assert again["id"] == before["id"]
    assert again["analysis_scope"] is None


def test_each_scope_gets_exactly_one_row(client, seeded):
    """Identity is the exact scope — no row answers for a scope it lacks."""
    case_id, timeline_id = seeded
    unstamped = _post_confirmed(client, case_id, timeline_id, None)
    other = {**SCOPE, "baseline_id": "bl-2", "baseline_name": "Mar 1 – Mar 2"}
    first = _post_confirmed(client, case_id, timeline_id, other)
    assert first["id"] != unstamped["id"]
    stamped = _post_confirmed(client, case_id, timeline_id, SCOPE)
    assert stamped["id"] not in {unstamped["id"], first["id"]}
    # Repeating a scope returns the row carrying it, never a sibling.
    assert _post_confirmed(client, case_id, timeline_id, SCOPE)["id"] == stamped["id"]
    assert _post_confirmed(client, case_id, timeline_id, other)["id"] == first["id"]
    assert _post_confirmed(client, case_id, timeline_id, None)["id"] == unstamped["id"]


def test_bulk_does_not_backfill_either(client, seeded):
    """The identity rule is stated in both paths; a drift rewrites history."""
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
    assert r.json()["dispositions"][0]["id"] != before["id"]
    assert r.json()["dispositions"][0]["analysis_scope"] == SCOPE


def _persist(client, case_id, analysis_scope, event_id="e1"):
    """Confirm through the endpoint the UI's Confirm button actually calls."""
    body = {
        "detector": "value_novelty",
        "content": "Manually confirmed finding",
        "details": {},
    }
    if analysis_scope is not None:
        body["analysis_scope"] = analysis_scope
    r = client.post(
        f"/api/cases/{case_id}/sources/s1/events/{event_id}/anomalies/persist", json=body
    )
    assert r.status_code == 200, r.text
    return r.json()["disposition"]


def test_the_confirm_button_records_the_scope_it_was_pressed_under(client, seeded):
    """The persist endpoint is the *only* path the UI's Confirm button reaches.

    Scope provenance is a ``confirmed``-only feature — it is the one kind whose
    ``disposition_identity`` includes the scope. So a persist path that dropped
    it would make every confirmed row in a real deployment carry ``NULL``, and
    the whole column, its migration and the per-scope dedupe would be dead code
    that no test on the dispositions router could catch.
    """
    case_id, _timeline_id = seeded
    assert _persist(client, case_id, SCOPE)["analysis_scope"] == SCOPE


def test_confirming_one_finding_under_two_baselines_makes_two_claims(client, seeded):
    """Escalating against February and again against March are two claims."""
    case_id, _timeline_id = seeded
    other = {**SCOPE, "baseline_id": "bl-2", "baseline_name": "Mar 1 – Mar 2"}
    first = _persist(client, case_id, SCOPE)
    second = _persist(client, case_id, other)
    assert first["id"] != second["id"]
    # And repeating one is still idempotent, so a double-click is not a claim.
    assert _persist(client, case_id, SCOPE)["id"] == first["id"]
