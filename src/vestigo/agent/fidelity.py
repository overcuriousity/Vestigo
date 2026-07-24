"""How much of an example record a tool result hands the model.

Tool results are slimmed before they reach the agent (``docs/AGENT.md``
§A13(d)). *How far* they are slimmed is this module's one decision, expressed
as three named tiers rather than a pile of per-call byte counts:

``FULL``
    The event stays inline whole. What the Analysis page shows.
``MESSAGE``
    Identity fields plus the event's ``message`` line — enough to tell a
    succeeded login from a failed one, at a fraction of the size.
``MINIMAL``
    Identity fields alone. The model calls ``get_event`` for anything more.

The tier governs the tools that return *many* event records
(:data:`FIDELITY_TIERED_TOOLS`). The single-record fetches it
points at — ``get_event``, ``get_event_annotations`` — are deliberately exempt:
they are the escape hatch every reduced payload names, and tiering them would
leave the model looping on a reduction it cannot undo.

The per-event attribute caps (``MAX_ATTRS_PER_EVENT``, ``ATTR_VALUE_TRUNCATE``)
are deliberately *not* tiered either: they guard against a single pathological
event (a megabyte of JSON in one attribute), which is an input-shape risk
unrelated to the model's window, and have never been implicated in an overflow.

**Determinism is the design constraint.** The tier is a function of static
configuration alone — never of what already ran in this turn. A running
per-turn budget would be more adaptive and would make identical
calls return different data depending on call order, with nothing in the
exported conversation to explain the difference. Forensic reproducibility
(``CLAUDE.md``) means replaying a conversation's tool calls under the same
configuration must produce byte-identical results, so this module has no state.

The tier is static per conversation: overflow handling is the sliding
window's job (``agent/window.py``), which elides old results in place instead
of re-running the turn at a lower tier (the retired "overflow ladder").

**Boundary with the request guard** (``agent/runtime.py``): that guard *does*
make one request's later tool returns order-dependent — it dedupes identical
calls and caps the running byte total. That is deliberately not this module's
concern and does not violate the rule above. This module chooses a *tier* (the
shape one call returns), which must never depend on call order. The guard
reduces for *fit* within a single request and records every reduction on the
window row, exactly as elision and truncation do — a reduction the export
explains, not a silent tier change. The two never touch: a call's tier is
picked here from static config; the guard only decides whether that already-
shaped result is emitted whole, deduped, or capped.

See ``docs/superpowers/specs/2026-07-21-agent-tool-result-fidelity-design.md``
and ``docs/superpowers/specs/2026-07-22-agent-sliding-window-design.md``.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)

__all__ = [
    "Fidelity",
    "FIDELITY_TIERED_TOOLS",
    "FIDELITY_VALUES",
    "DEFAULT_FIDELITY",
    "resolve_fidelity",
]


class Fidelity(StrEnum):
    """How much of an example record survives into the model's copy."""

    FULL = "full"
    MESSAGE = "message"
    MINIMAL = "minimal"


#: The tools whose payloads honour ``AgentScope.fidelity`` — those that return
#: *many* event records, where the tier meaningfully shrinks the prompt.
#:
#: ``get_event`` and ``get_event_annotations`` are deliberately absent. They
#: fetch one record on purpose, and they are the escape hatch every reduced
#: payload's ``note`` names — tiering them would leave the model looping on a
#: reduction it has no way to undo. ``list_annotations`` is absent too:
#: annotation bodies are analyst-written evidence, not an illustrative record,
#: and are already bounded by ``MAX_LIST_ROWS`` x
#: ``ANNOTATION_LIST_CONTENT_TRUNCATE``.
#:
#: This is a policy fact rather than a tool fact, so it lives here beside the
#: tiers it selects — ``tools.py`` reads it, not the other way round.
FIDELITY_TIERED_TOOLS: frozenset[str] = frozenset(
    {"search_events", "semantic_search", "similar_events", "run_anomaly_detector"}
)

#: Accepted values of the ``tool_fidelity`` setting. ``"auto"`` is a resolution
#: *mode*, not a tier — it derives one from the configured context window — so
#: it lives here rather than in the enum, which stays the set of real tiers.
FIDELITY_VALUES = (*(f.value for f in Fidelity), "auto")

#: Unset means "no constraint declared". An operator who has not told us the
#: model is small is assumed to be on a cloud model with a large window, and
#: gets the richest results; the sliding window (``agent/window.py``) and its
#: reactive overflow retry catch the ones who were wrong. `"message"` or
#: `"auto"` are the settings for a small local model.
DEFAULT_FIDELITY = Fidelity.FULL

#: Below this many tokens, ``auto`` stops serving inline example events. A
#: seven-detector sweep carrying them measured ~34k tokens of tool payload
#: (2026-07-20), which a 64k window cannot hold alongside history and an answer.
AUTO_FULL_MIN_WINDOW = 100_000

#: Below this many tokens, ``auto`` stops serving the ``message`` line too. The
#: same sweep's ~34k tokens of payload *is* a 32k window, so even the reduced
#: shape leaves no room for history and an answer; below that only the identity
#: fields fit, and the model reaches for ``get_event`` on the few findings it
#: actually pursues.
AUTO_MESSAGE_MIN_WINDOW = 32_000


def fidelity_config_warning(setting: str | None, context_window: int | None) -> str | None:
    """A one-line caution when the fidelity/window pair invites an overflow.

    An explicit ``tool_fidelity="full"`` keeps the richest inline tool payloads
    regardless of window size — the override an operator sets to force full
    results on a small local model. Against a window below
    :data:`AUTO_FULL_MIN_WINDOW` that is exactly the shape that died on the
    2026-07-23 overflow (``full`` + 65536): a single detector sweep's payload
    can fill the window before history and an answer. ``auto`` would have
    stepped down instead, so it is not flagged.

    Returns the message for surfacing (admin settings, logs), or ``None`` when
    the pair is fine. Advisory only — no tier or turn changes on this.
    """
    if setting == Fidelity.FULL.value and context_window and context_window < AUTO_FULL_MIN_WINDOW:
        return (
            f"tool_fidelity is 'full' but context_window is {context_window} tokens "
            f"(< {AUTO_FULL_MIN_WINDOW}); a single tool sweep can fill the window before "
            "history and an answer fit. Use 'auto' to step results down automatically, or "
            "raise context_window."
        )
    return None


def resolve_fidelity(setting: str | None, context_window: int | None) -> Fidelity:
    """The tier a turn starts at, from configuration alone.

    ``setting`` is the resolved ``tool_fidelity`` value (env > db > default).
    ``"auto"`` derives the tier from ``context_window`` for operators who would
    rather configure the window once and let this follow:

    ============================ =================
    ``context_window``           tier
    ============================ =================
    unset                        ``MESSAGE``
    >= ``AUTO_FULL_MIN_WINDOW``  ``FULL``
    >= ``AUTO_MESSAGE_MIN_WINDOW`` ``MESSAGE``
    below that                   ``MINIMAL``
    ============================ =================

    An unset window under ``auto`` resolves to ``MESSAGE`` rather than the
    ``FULL`` default: there is nothing to derive from, and an admin who picked
    ``auto`` asked to be kept inside a window rather than assumed to have room.
    (Leaving ``tool_fidelity`` unset entirely is the way to declare no
    constraint — see :data:`DEFAULT_FIDELITY`.)

    An unrecognised value falls back to the default rather than raising, since
    a tier is not worth failing a turn over (the admin schema validates it at
    write time).
    """
    if setting == "auto":
        if not context_window:
            return Fidelity.MESSAGE
        if context_window >= AUTO_FULL_MIN_WINDOW:
            return Fidelity.FULL
        if context_window >= AUTO_MESSAGE_MIN_WINDOW:
            return Fidelity.MESSAGE
        return Fidelity.MINIMAL
    try:
        return Fidelity(setting)
    except ValueError:
        if setting is not None:
            # The Settings pattern rejects a bad env value and the admin schema
            # rejects a bad write, so reaching here means a row predating one of
            # them — worth a line in the log rather than a silent downgrade.
            logger.warning(
                "Unrecognised tool_fidelity %r — falling back to %s", setting, DEFAULT_FIDELITY
            )
        return DEFAULT_FIDELITY
