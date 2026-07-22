"""Sliding context window: keep every model request inside the context budget.

Applied before *every* model request (mid-turn included) via pydantic-ai's
``ProcessHistory`` capability, so a single broad turn that piles up tool
results — the case that actually overflowed a 64k model (2026-07-21) — shrinks
as it grows instead of dying on the provider's 400. Two passes, cheapest first:

1. **Elide** — oldest-first, each ``ToolReturnPart``'s content is replaced by a
   small stub until the estimated prompt fits the budget. Message structure is
   untouched, so tool_call/tool_result pairing and role alternation stay valid
   on every provider protocol. The stub names the recovery path (re-run the
   tool, ``get_event``) so the model can adapt rather than guess.
2. **Drop turns** — if elision is not enough, whole oldest user turns are
   replaced by one marker pair. Splitting only at user-prompt boundaries keeps
   tool exchanges intact (same invariant the retired compaction held).
3. **Truncate the newest returns** — last resort, for the case the first two
   passes cannot touch at all: one tool result larger than the whole budget,
   sitting in the request the model is about to reason over. Its content is
   cut to a leading slice rather than stubbed, so the model keeps the shape of
   its own result instead of overflowing twice and losing the turn.

Never elided: the first user request (it carries the case/timeline context),
tool returns of the most recent request (what the model is about to reason
over — pass 3 may still truncate them), the last user turn, and all assistant
prose (the findings narrative — small, high value).

**Determinism is the design constraint**, inherited from ``agent/fidelity.py``:
:func:`apply_window` is a pure function of (messages, budget), so replaying a
conversation under the same configuration elides the same bytes. The stored
history blob stays complete — the window applies at send time only.

See ``docs/superpowers/specs/2026-07-22-agent-sliding-window-design.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields, replace
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ELISION_NOTE",
    "MIN_KEEP_CHARS",
    "TRUNCATION_NOTE",
    "TURN_DROP_MARKER",
    "WindowStats",
    "apply_window",
    "budget_for",
    "estimate_tokens",
    "make_window_processor",
]

#: What replaces an elided tool result. Visible to the model in the replayed
#: history — transparency is the point: the model can re-run the tool with
#: narrower filters instead of reasoning over a silent gap.
ELISION_NOTE = (
    "Result elided to fit the context window — re-run the tool with narrower "
    "filters or use get_event to recover specifics."
)

#: What replaces a *truncated* tool result — pass 3's stub keeps a leading
#: slice of the original under ``head``, so the model can still see what the
#: tool answered even when the whole result does not fit anywhere.
TRUNCATION_NOTE = (
    "Result truncated to fit the context window — only the beginning is shown; "
    "re-run the tool with narrower filters or use get_event for full records."
)

#: Prefix of the stub user message standing in for dropped turns — also what an
#: analyst sees when inspecting raw_history in exports.
TURN_DROP_MARKER = "[Older turns dropped to fit the context window]"

#: Leading characters a truncated result keeps no matter how tight the budget.
#: A bare note is worse than useless — the model cannot tell an empty result
#: from a reduced one — so pass 3 stops shrinking here and lets the router's
#: overflow backstop handle a budget that small.
MIN_KEEP_CHARS = 500

#: Share of the context window the prompt may use; the rest is headroom for the
#: completion and the estimate's error (chars/4 is a heuristic, not a tokenizer).
MARGIN = 0.8


@dataclass
class WindowStats:
    """What the window did to *one* model request.

    Across a turn the processor keeps the single largest reduction rather than
    per-field maxima, so the persisted row's ``estimated_before``/``after``
    always describe the same request — a synthetic pair would report a delta
    that never happened.
    """

    budget: int = 0
    results_elided: int = 0
    results_truncated: int = 0
    turns_dropped: int = 0
    estimated_before: int = 0
    estimated_after: int = 0

    @property
    def reduced(self) -> bool:
        return self.results_elided > 0 or self.results_truncated > 0 or self.turns_dropped > 0

    @property
    def saved(self) -> int:
        """Estimated tokens this request's reduction removed."""
        return self.estimated_before - self.estimated_after


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def estimate_tokens(messages: list[ModelMessage]) -> int:
    """Rough prompt size of a history, in tokens (chars/4 over the JSON dump)."""
    return len(ModelMessagesTypeAdapter.dump_json(messages)) // 4


def budget_for(context_window: int, system_prompt: str) -> int:
    """Token budget for the message history, from the configured window.

    ``MARGIN`` leaves completion headroom; the system prompt rides outside the
    message list, so its estimated share is subtracted here once. Clamped to a
    floor of 1: a non-positive budget would silently maximally elide every
    request, which is a misconfiguration worth a warning, not a quiet default.
    """
    budget = int(context_window * MARGIN) - len(system_prompt) // 4
    if budget < 1:
        logger.warning(
            "Configured context_window %d leaves no room for messages after the "
            "system prompt (~%d tokens) — every request will be maximally elided. "
            "Raise context_window.",
            context_window,
            len(system_prompt) // 4,
        )
        return 1
    return budget


def _stub() -> dict[str, Any]:
    return {"elided": True, "note": ELISION_NOTE}


def _is_stub(content: Any) -> bool:
    return isinstance(content, dict) and content.get("elided") is True


def _is_truncated(content: Any) -> bool:
    return isinstance(content, dict) and content.get("truncated") is True


def _user_turn_boundaries(messages: list[ModelMessage]) -> list[int]:
    """Indices of requests that start a user turn (contain a UserPromptPart).

    Requests that only carry tool returns are *not* boundaries — splitting
    there would orphan a tool_use from its tool_result on replay.
    """
    return [
        i
        for i, message in enumerate(messages)
        if isinstance(message, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in message.parts)
    ]


def _last_request_index(messages: list[ModelMessage]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], ModelRequest):
            return i
    return -1


def _elide(messages: list[ModelMessage], budget: int, running: int) -> tuple[int, int]:
    """Pass 1: stub out tool-return contents oldest-first, in place.

    ``messages`` is the caller's private copy. Returns (results_elided,
    running estimate). The most recent request's returns are protected — they
    are what the model is about to reason over.
    """
    protected = _last_request_index(messages)
    stub_cost = _serialized_size(_stub()) // 4
    elided = 0
    for i, message in enumerate(messages):
        if running <= budget:
            break
        if i == protected or not isinstance(message, ModelRequest):
            continue
        parts = list(message.parts)
        changed = False
        for j, part in enumerate(parts):
            if running <= budget:
                break
            if not isinstance(part, ToolReturnPart) or _is_stub(part.content):
                continue
            saving = _serialized_size(part.content) // 4 - stub_cost
            if saving <= 0:
                # The stub would be no smaller than the content — replacing it
                # grows the prompt and burns an "elided" count on a no-op.
                continue
            parts[j] = replace(part, content=_stub())
            changed = True
            elided += 1
            running -= saving
        if changed:
            messages[i] = replace(message, parts=parts)
    return elided, running


def _truncate_newest(messages: list[ModelMessage], budget: int, running: int) -> tuple[int, int]:
    """Pass 3: cut the newest request's tool returns down to a leading slice.

    The last resort, and the only pass that touches the request the model is
    about to reason over. One tool result larger than the whole budget is
    invisible to both other passes — elision protects this request and turn
    dropping cannot reach inside it — so without this the turn overflows,
    retries identically, and dies. Truncating instead of stubbing keeps the
    beginning of the result: the model can see what the tool answered and
    narrow the re-run itself.

    Oldest part first (a request's parts are in call order), stopping as soon
    as the estimate fits. Same private-copy contract as :func:`_elide`.
    """
    index = _last_request_index(messages)
    if index < 0:
        return 0, running
    message = messages[index]
    if not isinstance(message, ModelRequest):
        return 0, running
    parts = list(message.parts)
    truncated = 0
    for j, part in enumerate(parts):
        if running <= budget:
            break
        if not isinstance(part, ToolReturnPart) or _is_stub(part.content):
            continue
        if _is_truncated(part.content):
            continue
        text = json.dumps(part.content, default=str)
        keep = max(MIN_KEEP_CHARS, len(text) - (running - budget) * 4)
        if keep >= len(text):
            continue
        content = {"truncated": True, "note": TRUNCATION_NOTE, "head": text[:keep]}
        saving = len(text) // 4 - _serialized_size(content) // 4
        if saving <= 0:
            # The wrapper costs more than the cut saves — leave the original.
            continue
        parts[j] = replace(part, content=content)
        truncated += 1
        running -= saving
    if truncated:
        messages[index] = replace(message, parts=parts)
    return truncated, running


def _drop_turns(messages: list[ModelMessage], budget: int, running: int) -> tuple[int, int]:
    """Pass 2: replace the oldest droppable turns with one marker pair.

    The first turn (case context) and the last turn (the question being
    answered) are never dropped. Dropping is contiguous from the second turn,
    so one stub pair stands for the whole removed span — cheaper than a marker
    per turn, and just as explicit. Mutates and returns ``messages``'s content
    via slice assignment on the caller's private copy.
    """
    boundaries = _user_turn_boundaries(messages)
    # Droppable spans: turn k covers boundaries[k]..boundaries[k+1]; the first
    # and last turns are protected, so k ranges over 1..len-2.
    dropped = 0
    end = boundaries[1] if len(boundaries) > 2 else None
    for k in range(1, len(boundaries) - 1):
        if running <= budget:
            break
        span_end = boundaries[k + 1]
        running -= estimate_tokens(messages[boundaries[k] : span_end])
        end = span_end
        dropped += 1
    if not dropped:
        return 0, running
    marker: list[ModelMessage] = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        f"{TURN_DROP_MARKER} — {dropped} earlier turn(s) removed. Earlier "
                        "findings persist as annotations and proposals; use list_annotations "
                        "or re-ask if something is missing."
                    )
                )
            ]
        ),
        ModelResponse(
            parts=[TextPart(content="Understood. Continuing with the remaining context.")]
        ),
    ]
    messages[boundaries[1] : end] = marker
    return dropped, running + estimate_tokens(marker)


def apply_window(
    messages: list[ModelMessage], budget: int
) -> tuple[list[ModelMessage], WindowStats]:
    """Fit ``messages`` under ``budget`` tokens; pure — the input is not mutated.

    Best effort: a history that cannot fit even after both passes is returned
    as reduced as the invariants allow, and the router's overflow handling
    remains the backstop.
    """
    before = estimate_tokens(messages)
    stats = WindowStats(budget=budget, estimated_before=before, estimated_after=before)
    if before <= budget:
        return list(messages), stats
    out = list(messages)
    stats.results_elided, running = _elide(out, budget, before)
    if running > budget:
        stats.turns_dropped, running = _drop_turns(out, budget, running)
    if running > budget:
        stats.results_truncated, running = _truncate_newest(out, budget, running)
    stats.estimated_after = estimate_tokens(out)
    if stats.reduced:
        logger.info(
            "Context window applied: %d results elided, %d truncated, %d turns dropped "
            "(est. %d -> %d, budget %d)",
            stats.results_elided,
            stats.results_truncated,
            stats.turns_dropped,
            stats.estimated_before,
            stats.estimated_after,
            budget,
        )
    if stats.estimated_after > budget:
        # Every pass has run and the history still does not fit — a single
        # protected payload dominates the budget. Said out loud here because
        # the analyst-facing symptom (the router's context_overflow error)
        # reads as "conversation too long", which this is not.
        logger.warning(
            "Context window could not fit the history: est. %d tokens still over budget %d "
            "after eliding %d results, dropping %d turns and truncating %d — the newest "
            "request's payload alone exceeds the budget.",
            stats.estimated_after,
            budget,
            stats.results_elided,
            stats.turns_dropped,
            stats.results_truncated,
        )
    return out, stats


def make_window_processor(budget: int, stats: WindowStats):
    """History processor for ``ProcessHistory``, keeping the turn's worst request.

    The processor runs once per model request; ``stats`` is overwritten
    wholesale by the request with the largest reduction, so the router can
    persist one honest row per turn rather than one per request. Field-wise
    maxima would be cheaper but would pair one request's ``estimated_before``
    with another's ``estimated_after`` — a delta that never happened, in a
    record that has to stand up as evidence.
    """

    def process(messages: list[ModelMessage]) -> list[ModelMessage]:
        out, request_stats = apply_window(messages, budget)
        if not stats.reduced or request_stats.saved > stats.saved:
            for f in fields(WindowStats):
                setattr(stats, f.name, getattr(request_stats, f.name))
        stats.budget = budget
        return out

    return process
