"""How much of an example record a tool result hands the model.

Tool results are slimmed before they reach the agent (``docs/AGENT.md``
§A13(d)). *How far* they are slimmed is this module's one decision, expressed
as three named tiers rather than a pile of per-call byte counts:

``FULL``
    The example event stays inline. What the Analysis page shows.
``MESSAGE``
    ``event_id`` plus the event's ``message`` line — enough to tell a
    succeeded login from a failed one, at ~5% of the size.
``MINIMAL``
    ``event_id`` alone. The model calls ``get_event`` for anything more.

The per-event attribute caps (``MAX_ATTRS_PER_EVENT``, ``ATTR_VALUE_TRUNCATE``)
are deliberately *not* tiered: they guard against a single pathological event
(a megabyte of JSON in one attribute), which is an input-shape risk unrelated
to the model's window, and they have never been implicated in an overflow.

**Determinism is the design constraint.** The tier is a function of static
configuration and the retry attempt number — never of what already ran in this
turn. A running per-turn budget would be more adaptive and would make identical
calls return different data depending on call order, with nothing in the
exported conversation to explain the difference. Forensic reproducibility
(``CLAUDE.md``) means replaying a conversation's tool calls under the same
configuration must produce byte-identical results, so this module has no state.

See ``docs/superpowers/specs/2026-07-21-agent-tool-result-fidelity-design.md``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Fidelity", "FIDELITY_VALUES", "DEFAULT_FIDELITY", "resolve_fidelity", "degrade"]


class Fidelity(StrEnum):
    """How much of an example record survives into the model's copy."""

    FULL = "full"
    MESSAGE = "message"
    MINIMAL = "minimal"


#: Accepted values of the ``tool_fidelity`` setting. ``"auto"`` is a resolution
#: *mode*, not a tier — it derives one from the configured context window — so
#: it lives here rather than in the enum, which stays the set of real tiers.
FIDELITY_VALUES = (*(f.value for f in Fidelity), "auto")

#: Unset means "no constraint declared". An operator who has not told us the
#: model is small is assumed to be on a cloud model with a large window, and
#: gets the richest results; the overflow backstop (a retry one tier down,
#: costing a round trip rather than the turn) catches the ones who were wrong.
#: `"message"` or `"auto"` are the settings for a small local model.
DEFAULT_FIDELITY = Fidelity.FULL

#: Below this many tokens, ``auto`` stops serving inline example events. A
#: seven-detector sweep carrying them measured ~34k tokens of tool payload
#: (2026-07-20), which a 64k window cannot hold alongside history and an answer.
AUTO_FULL_MIN_WINDOW = 100_000

_ORDER = (Fidelity.FULL, Fidelity.MESSAGE, Fidelity.MINIMAL)


def resolve_fidelity(setting: str | None, context_window: int | None) -> Fidelity:
    """The tier a turn starts at, from configuration alone.

    ``setting`` is the resolved ``tool_fidelity`` value (env > db > default).
    ``"auto"`` derives the tier from ``context_window`` for operators who would
    rather configure the window once and let this follow; an unrecognised
    value falls back to the default rather than raising, since a tier is not
    worth failing a turn over (the admin schema validates it at write time).
    """
    if setting == "auto":
        if context_window and context_window >= AUTO_FULL_MIN_WINDOW:
            return Fidelity.FULL
        return Fidelity.MESSAGE
    try:
        return Fidelity(setting)
    except ValueError:
        return DEFAULT_FIDELITY


def degrade(tier: Fidelity) -> Fidelity | None:
    """The next tier down, or None when there is nothing left to give up.

    Drives the overflow backstop: dropping a tier and re-running the turn costs
    a round trip and no summarizer call, so it is tried before compaction —
    which cannot help a single-turn overflow at all, having nothing older to
    fold.
    """
    index = _ORDER.index(tier)
    return _ORDER[index + 1] if index + 1 < len(_ORDER) else None
