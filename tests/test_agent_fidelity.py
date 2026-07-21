"""Tool-result fidelity: tier resolution, deflation per tier, determinism.

The design constraint under test is reproducibility (`CLAUDE.md`): the tier is
a function of static configuration and the retry attempt, never of what already
ran in the turn, so replaying a conversation's tool calls under the same config
returns byte-identical payloads. See
`docs/superpowers/specs/2026-07-21-agent-tool-result-fidelity-design.md`.
"""

from __future__ import annotations

import json

import pytest

from vestigo.agent.fidelity import (
    DEFAULT_FIDELITY,
    FIDELITY_VALUES,
    MAX_FIDELITY_DROPS,
    Fidelity,
    degrade,
    next_tier,
    resolve_fidelity,
)
from vestigo.agent.tools import (
    FIDELITY_TIERED_TOOLS,
    TOOL_NAMES,
    _deflate_findings,
    _slim_event,
)


def _event() -> dict:
    return {
        "event_id": "e1",
        "timestamp": "2026-07-20T10:00:00Z",
        "source_id": "s1",
        "artifact": "auth",
        "message": "login attempt [svc-a/rock] succeeded",
        "attributes": {"user": "svc-a", "ip": "10.0.0.9"},
    }


def _payload() -> dict:
    return {
        "status": "ok",
        "results": [
            {
                "type": "value_novelty",
                "field": "username",
                "value": "svc-a",
                "event_id": "e1",
                "event": {"message": "login attempt [svc-a/rock] succeeded", "attr": {"k": "v"}},
                "details": {"surprise": 12.7},
            }
        ],
    }


# --- tier resolution -------------------------------------------------------


def test_unset_means_full():
    """An operator who declared no constraint is assumed to have room — the
    overflow backstop costs a retry, not the turn."""
    assert DEFAULT_FIDELITY is Fidelity.FULL
    assert resolve_fidelity(DEFAULT_FIDELITY.value, None) is Fidelity.FULL


@pytest.mark.parametrize("value", ["full", "message", "minimal"])
def test_explicit_setting_wins_over_any_window(value):
    """The admin knows the model; the window must not override them."""
    assert resolve_fidelity(value, 8192) is Fidelity(value)
    assert resolve_fidelity(value, 1_000_000) is Fidelity(value)


@pytest.mark.parametrize(
    ("window", "expected"),
    [(None, Fidelity.MESSAGE), (65_536, Fidelity.MESSAGE), (200_000, Fidelity.FULL)],
)
def test_auto_derives_from_the_window(window, expected):
    assert resolve_fidelity("auto", window) is expected


def test_unknown_setting_degrades_to_the_default_rather_than_raising():
    """A tier is not worth failing a turn over — the admin schema validates
    the value at write time, this is the last line of defence."""
    assert resolve_fidelity("nonsense", None) is DEFAULT_FIDELITY
    assert resolve_fidelity(None, None) is DEFAULT_FIDELITY


def test_every_accepted_setting_resolves():
    for value in FIDELITY_VALUES:
        assert isinstance(resolve_fidelity(value, 128_000), Fidelity)


def test_degrade_walks_down_once_and_bottoms_out():
    assert degrade(Fidelity.FULL) is Fidelity.MESSAGE
    assert degrade(Fidelity.MESSAGE) is Fidelity.MINIMAL
    assert degrade(Fidelity.MINIMAL) is None


def test_max_drops_matches_the_tier_table():
    """The router sizes its retry budget from this rather than a literal."""
    assert len(list(Fidelity)) - 1 == MAX_FIDELITY_DROPS


# --- when a drop is worth an attempt ---------------------------------------


def test_next_tier_drops_when_a_tiered_tool_ran():
    assert next_tier(Fidelity.FULL, {"list_fields", "search_events"}) is Fidelity.MESSAGE
    assert next_tier(Fidelity.MESSAGE, {"run_anomaly_detector"}) is Fidelity.MINIMAL


def test_next_tier_refuses_when_a_drop_cannot_change_the_prompt():
    """An overflow on a turn that fetched no event records is a history
    problem: re-sending a byte-identical request only delays the compaction
    that can actually help."""
    assert next_tier(Fidelity.FULL, set()) is None
    assert next_tier(Fidelity.FULL, {"list_fields", "get_event", "list_annotations"}) is None


def test_next_tier_bottoms_out_even_with_tiered_tools():
    assert next_tier(Fidelity.MINIMAL, {"search_events"}) is None


def test_tiered_tools_are_real_tools():
    """A typo here would silently disable the cheapest overflow lever."""
    assert FIDELITY_TIERED_TOOLS <= TOOL_NAMES
    # The escape hatch the reduced payloads point at must never be tiered.
    assert "get_event" not in FIDELITY_TIERED_TOOLS
    assert "get_event_annotations" not in FIDELITY_TIERED_TOOLS


# --- deflation per tier ----------------------------------------------------


def test_full_keeps_every_event_and_still_names_its_tier():
    """A result with no marker cannot be told apart from one produced before
    tiers existed, so `full` stamps too — it just drops nothing and says
    nothing about missing data."""
    out = _deflate_findings(_payload(), Fidelity.FULL)
    assert out["results"] == _payload()["results"]
    assert out["fidelity"] == "full"
    assert "note" not in out


def test_message_keeps_the_line_that_carries_the_verdict():
    row = _deflate_findings(_payload(), Fidelity.MESSAGE)["results"][0]
    assert row["message"] == "login attempt [svc-a/rock] succeeded"
    assert "event" not in row
    assert row["details"] == {"surprise": 12.7}


def test_minimal_keeps_only_the_handle():
    out = _deflate_findings(_payload(), Fidelity.MINIMAL)
    row = out["results"][0]
    assert row["event_id"] == "e1"
    assert "event" not in row and "message" not in row
    # ...and still says how to get the rest.
    assert "get_event" in out["note"]


@pytest.mark.parametrize("tier", [Fidelity.MESSAGE, Fidelity.MINIMAL])
def test_a_reduced_result_declares_its_tier(tier):
    """An exported conversation must state what produced each result rather
    than leaving the reader to infer it from config they may not have."""
    assert _deflate_findings(_payload(), tier)["fidelity"] == tier.value


def test_tiers_are_ordered_by_size():
    """Findings only — the fixed-size `note` differs per tier and would swamp
    the comparison on a one-row sample."""
    sizes = [
        len(json.dumps(_deflate_findings(_payload(), tier)["results"]))
        for tier in (Fidelity.FULL, Fidelity.MESSAGE, Fidelity.MINIMAL)
    ]
    assert sizes == sorted(sizes, reverse=True)


def test_deflation_is_idempotent():
    once = _deflate_findings(_payload(), Fidelity.MESSAGE)
    assert _deflate_findings(once, Fidelity.MESSAGE) == once


# --- whole-event payloads (search, semantic_search, similar_events) --------


def test_slim_event_full_keeps_the_attribute_bag():
    out = _slim_event(_event(), Fidelity.FULL)
    assert out["attributes"] == {"user": "svc-a", "ip": "10.0.0.9"}
    assert out["message"] == "login attempt [svc-a/rock] succeeded"


def test_slim_event_message_drops_attributes_only():
    out = _slim_event(_event(), Fidelity.MESSAGE)
    assert "attributes" not in out
    assert out["message"] == "login attempt [svc-a/rock] succeeded"


def test_slim_event_minimal_keeps_identity_only():
    out = _slim_event(_event(), Fidelity.MINIMAL)
    assert "attributes" not in out and "message" not in out
    assert out["event_id"] == "e1"


@pytest.mark.parametrize("tier", list(Fidelity))
def test_slim_event_never_strips_the_means_of_un_reducing_it(tier):
    """`event_id` reaches get_event and `source_id` reaches
    get_event_annotations — a reduction that removed either would be a dead
    end rather than a pointer."""
    out = _slim_event(_event(), tier)
    assert out["event_id"] == "e1"
    assert out["source_id"] == "s1"


def test_slim_event_tiers_are_ordered_by_size():
    sizes = [len(json.dumps(_slim_event(_event(), tier))) for tier in Fidelity]
    assert sizes == sorted(sizes, reverse=True)


def test_the_same_call_at_the_same_tier_is_byte_identical():
    """The reproducibility property the whole design exists to protect: no
    hidden per-turn state, so call order cannot change a payload."""
    first = json.dumps(_deflate_findings(_payload(), Fidelity.MESSAGE), sort_keys=True)
    for _ in range(5):
        assert json.dumps(_deflate_findings(_payload(), Fidelity.MESSAGE), sort_keys=True) == first
