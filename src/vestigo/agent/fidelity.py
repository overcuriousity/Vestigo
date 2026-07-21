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
configuration and the retry attempt number — never of what already ran in this
turn. A running per-turn budget would be more adaptive and would make identical
calls return different data depending on call order, with nothing in the
exported conversation to explain the difference. Forensic reproducibility
(``CLAUDE.md``) means replaying a conversation's tool calls under the same
configuration must produce byte-identical results, so this module has no state.

See ``docs/superpowers/specs/2026-07-21-agent-tool-result-fidelity-design.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from enum import StrEnum

logger = logging.getLogger(__name__)

__all__ = [
    "Fidelity",
    "FIDELITY_TIERED_TOOLS",
    "FIDELITY_VALUES",
    "DEFAULT_FIDELITY",
    "MAX_FIDELITY_DROPS",
    "resolve_fidelity",
    "degrade",
    "next_tier",
]


class Fidelity(StrEnum):
    """How much of an example record survives into the model's copy."""

    FULL = "full"
    MESSAGE = "message"
    MINIMAL = "minimal"


#: The tools whose payloads honour ``AgentScope.fidelity`` — those that return
#: *many* event records, where a tier drop actually shrinks the prompt. The
#: router consults this before spending an overflow retry on a drop
#: (:func:`next_tier`): an overflow on a turn that called none of these cannot
#: be helped by one, and must fall through to compaction instead.
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
#: gets the richest results; the overflow backstop (a retry one tier down,
#: costing a round trip rather than the turn) catches the ones who were wrong.
#: `"message"` or `"auto"` are the settings for a small local model.
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

_ORDER = (Fidelity.FULL, Fidelity.MESSAGE, Fidelity.MINIMAL)

#: How many times :func:`degrade` can drop before bottoming out — the overflow
#: ladder's share of the retry budget, derived here so the router's attempt
#: bound cannot drift from the tier table.
MAX_FIDELITY_DROPS = len(_ORDER) - 1


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


def degrade(tier: Fidelity) -> Fidelity | None:
    """The next tier down, or None when there is nothing left to give up.

    Drives the overflow backstop: dropping a tier and re-running the turn costs
    a round trip and no summarizer call, so it is tried before compaction —
    which cannot help a single-turn overflow at all, having nothing older to
    fold.
    """
    index = _ORDER.index(tier)
    return _ORDER[index + 1] if index + 1 < len(_ORDER) else None


def next_tier(current: Fidelity, tools_used: Collection[str]) -> Fidelity | None:
    """The tier to retry an overflowed attempt at, or None to stop trying.

    ``tools_used`` is the set of tools that actually returned a result during
    the attempt that overflowed. A drop only changes the prompt if one of them
    honours the tier (:data:`FIDELITY_TIERED_TOOLS`), so an overflow on a turn
    that called none — a long conversation with no event payloads in it — must
    fall straight through to compaction rather than burning two provider round
    trips re-sending a byte-identical request.
    """
    if not FIDELITY_TIERED_TOOLS.intersection(tools_used):
        return None
    return degrade(current)
