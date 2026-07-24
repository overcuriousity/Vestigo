"""Tool-result fidelity: tier resolution, deflation per tier, determinism.

The design constraint under test is reproducibility (`CLAUDE.md`): the tier is
a function of static configuration alone, never of what already ran in the
turn, so replaying a conversation's tool calls under the same config returns
byte-identical payloads. See
`docs/superpowers/specs/2026-07-21-agent-tool-result-fidelity-design.md`.
"""

from __future__ import annotations

import json

import pytest

from vestigo.agent.fidelity import (
    AUTO_FULL_MIN_WINDOW,
    AUTO_MESSAGE_MIN_WINDOW,
    DEFAULT_FIDELITY,
    FIDELITY_TIERED_TOOLS,
    FIDELITY_VALUES,
    Fidelity,
    fidelity_config_warning,
    resolve_fidelity,
)
from vestigo.agent.tools import (
    SLIM_MESSAGE_TRUNCATE,
    TOOL_NAMES,
    _deflate_findings,
    _event_reduced,
    _finding_event_reduced,
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
    [
        # Unset: nothing to derive from, and an admin who picked `auto` asked
        # to be kept inside a window rather than assumed to have room.
        (None, Fidelity.MESSAGE),
        (0, Fidelity.MESSAGE),
        (8_192, Fidelity.MINIMAL),
        (AUTO_MESSAGE_MIN_WINDOW - 1, Fidelity.MINIMAL),
        (AUTO_MESSAGE_MIN_WINDOW, Fidelity.MESSAGE),
        (65_536, Fidelity.MESSAGE),
        (AUTO_FULL_MIN_WINDOW - 1, Fidelity.MESSAGE),
        (AUTO_FULL_MIN_WINDOW, Fidelity.FULL),
        (200_000, Fidelity.FULL),
    ],
)
def test_auto_derives_from_the_window(window, expected):
    assert resolve_fidelity("auto", window) is expected


def test_auto_never_skips_a_tier_as_the_window_shrinks():
    """The ladder is monotone: a smaller window never gets *more* detail."""
    tiers = [resolve_fidelity("auto", w) for w in (200_000, 100_000, 64_000, 32_000, 8_000)]
    order = [Fidelity.FULL, Fidelity.MESSAGE, Fidelity.MINIMAL]
    positions = [order.index(t) for t in tiers]
    assert positions == sorted(positions)


def test_unknown_setting_degrades_to_the_default_rather_than_raising():
    """A tier is not worth failing a turn over — the admin schema validates
    the value at write time, this is the last line of defence."""
    assert resolve_fidelity("nonsense", None) is DEFAULT_FIDELITY
    assert resolve_fidelity(None, None) is DEFAULT_FIDELITY


def test_every_accepted_setting_resolves():
    for value in FIDELITY_VALUES:
        assert isinstance(resolve_fidelity(value, 128_000), Fidelity)


def test_full_fidelity_under_a_small_window_warns():
    """The exact config that overflowed on 2026-07-23: full + 65536."""
    warning = fidelity_config_warning("full", 65_536)
    assert warning is not None
    assert "65536" in warning
    assert str(AUTO_FULL_MIN_WINDOW) in warning


def test_full_fidelity_with_a_large_window_is_silent():
    assert fidelity_config_warning("full", AUTO_FULL_MIN_WINDOW) is None
    assert fidelity_config_warning("full", 200_000) is None


def test_full_fidelity_with_no_window_is_silent():
    # Nothing to compare against — an unset window is the "no constraint
    # declared" default, not an underpowered one.
    assert fidelity_config_warning("full", None) is None


@pytest.mark.parametrize("setting", ["auto", "message", "minimal", None])
def test_non_full_settings_never_warn(setting):
    # auto steps itself down; the reduced tiers already fit — none of these is
    # the shape the guard-rail is about.
    assert fidelity_config_warning(setting, 8_000) is None


def test_tiered_tools_are_real_tools():
    """A typo here would silently exempt a bulky tool from the tier."""
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


# --- "was anything actually dropped" --------------------------------------


def test_full_never_claims_a_reduction():
    assert _event_reduced(_event(), Fidelity.FULL) is False


def test_a_bare_event_survives_message_intact():
    """A tier below FULL does not by itself mean data was lost. Claiming a
    reduction that did not happen puts an untruth in the exported record."""
    bare = {"event_id": "e1", "source_id": "s1", "message": "short line"}
    assert _event_reduced(bare, Fidelity.MESSAGE) is False
    assert _event_reduced({"event_id": "e1"}, Fidelity.MINIMAL) is False


def test_dropped_attributes_and_dropped_lines_are_reductions():
    assert _event_reduced(_event(), Fidelity.MESSAGE) is True  # attributes go
    assert _event_reduced({"message": "x"}, Fidelity.MINIMAL) is True  # the line goes
    long = {"message": "m" * (SLIM_MESSAGE_TRUNCATE + 1)}
    assert _event_reduced(long, Fidelity.MESSAGE) is True  # truncated


def test_a_finding_whose_event_carried_nothing_claims_no_reduction():
    """The anomaly path's version of the same rule: an `event` key that held
    nothing (or nothing but a short line) loses nothing when it goes."""
    assert _finding_event_reduced({}, Fidelity.MESSAGE) is False
    assert _finding_event_reduced(None, Fidelity.MINIMAL) is False
    assert _finding_event_reduced({"message": "short line"}, Fidelity.MESSAGE) is False


def test_a_finding_that_loses_the_event_object_admits_it():
    """Unlike a search hit, a finding loses the *whole* event object — so a
    timestamp or a source_id going is already a reduction at MESSAGE."""
    assert _finding_event_reduced({"timestamp": "2026-07-20T10:00:00Z"}, Fidelity.MESSAGE) is True
    assert _finding_event_reduced({"message": "short"}, Fidelity.MINIMAL) is True
    long = {"message": "m" * (SLIM_MESSAGE_TRUNCATE + 1)}
    assert _finding_event_reduced(long, Fidelity.MESSAGE) is True
    assert _finding_event_reduced({"message": "short"}, Fidelity.FULL) is False


def test_a_payload_that_lost_nothing_carries_no_note():
    """The `note` says "call get_event for the full record"; on a payload
    where the event held only the line the model already has, that is an
    untruth in the export — the failure `_listing` avoids by reporting
    `returned` beside `total`."""
    payload = {
        "status": "ok",
        "results": [{"type": "frequency", "event_id": "e1", "event": {"message": "short line"}}],
    }
    out = _deflate_findings(payload, Fidelity.MESSAGE)
    assert out["fidelity"] == "message"  # the tier is still stated
    assert "note" not in out  # ...but nothing was dropped
    assert out["results"][0]["message"] == "short line"


def test_one_reduced_finding_is_enough_for_the_note():
    payload = {
        "status": "ok",
        "results": [
            {"type": "frequency", "event_id": "e1", "event": {"message": "short line"}},
            {"type": "frequency", "event_id": "e2", "event": {"attributes": {"k": "v"}}},
        ],
    }
    assert "get_event" in _deflate_findings(payload, Fidelity.MESSAGE)["note"]


def test_the_same_call_at_the_same_tier_is_byte_identical():
    """The reproducibility property the whole design exists to protect: no
    hidden per-turn state, so call order cannot change a payload."""
    first = json.dumps(_deflate_findings(_payload(), Fidelity.MESSAGE), sort_keys=True)
    for _ in range(5):
        assert json.dumps(_deflate_findings(_payload(), Fidelity.MESSAGE), sort_keys=True) == first
