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
    Fidelity,
    degrade,
    resolve_fidelity,
)
from vestigo.agent.tools import _deflate_findings


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


# --- deflation per tier ----------------------------------------------------


def test_full_is_a_noop():
    payload = _payload()
    assert _deflate_findings(payload, Fidelity.FULL) == payload


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


def test_the_same_call_at_the_same_tier_is_byte_identical():
    """The reproducibility property the whole design exists to protect: no
    hidden per-turn state, so call order cannot change a payload."""
    first = json.dumps(_deflate_findings(_payload(), Fidelity.MESSAGE), sort_keys=True)
    for _ in range(5):
        assert json.dumps(_deflate_findings(_payload(), Fidelity.MESSAGE), sort_keys=True) == first
