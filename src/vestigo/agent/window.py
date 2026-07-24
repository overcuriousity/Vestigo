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

**What the budget must cover.** Three things ship in a request and only one is
in ``messages``: the history, the system prompt, and the advertised tool
schemas. :func:`budget_for` reserves all three. Omitting the tool schemas is
what let a 76k-token request through a 49k budget on 2026-07-23 — 28 tools with
``FilterSpec`` inlined ~14 times are invisible to a processor that only sees
the message list. See ``agent/schema_slim.py``, which measures and shrinks them.

**Estimating is calibrated, not assumed.** ``chars/N`` is a heuristic, not a
tokenizer, and no constant N is right for every payload: prose runs near 4,
while escaped JSON with base64 parameters, dotted-quad IPs and UUIDs runs near
2.35 (measured). A provider overflow that names the request's token count is a
free exact measurement — :func:`calibrate_chars_per_token` turns it into a
ratio the router persists and later turns reuse. No tokenizer is downloaded;
the deployment is airgapped by default (``CLAUDE.md``).

**Determinism is the design constraint**, inherited from ``agent/fidelity.py``:
:func:`apply_window` is a pure function of (messages, budget, chars_per_token),
so replaying a conversation under the same configuration elides the same bytes.
The ratio is therefore an argument, resolved once per turn and recorded in the
persisted window row — never ambient state that drifts mid-turn. The stored
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
    "CHARS_PER_TOKEN_DEFAULT",
    "CHARS_PER_TOKEN_MAX",
    "CHARS_PER_TOKEN_MIN",
    "ELISION_NOTE",
    "MIN_KEEP_CHARS",
    "TRUNCATION_NOTE",
    "TURN_DROP_MARKER",
    "WindowStats",
    "apply_window",
    "budget_for",
    "calibrate_chars_per_token",
    "estimate_tokens",
    "make_window_processor",
]

#: Characters per token assumed when nothing better is known.
#:
#: 4 was the original figure, and it holds for prose. It does not hold for what
#: this agent actually sends: escaped JSON (``\"``, ``\\/``), base64 ``state=``
#: parameters, dotted-quad IPs and UUID event ids all tokenize near 1:2. The
#: 2026-07-23 overflow measured **2.35** chars/token over a 178896-char request
#: the provider counted as 75967 tokens — a 70% under-estimate at 4, which
#: ``MARGIN`` cannot absorb.
#:
#: 3.0 is a conservative default, not a correct one: no single constant is
#: right for every payload mix. :func:`calibrate_chars_per_token` learns the
#: real ratio from a provider overflow and that value takes precedence.
CHARS_PER_TOKEN_DEFAULT = 3.0

#: Band a *learned* ratio must fall inside to be believed. Outside it, the
#: parse is more likely wrong than the model exotic: below 1.5 no tokenizer
#: fragments plain JSON that hard, and above 5.0 the estimate would be looser
#: than the default it replaced. A rejected ratio leaves the default in place.
CHARS_PER_TOKEN_MIN = 1.5
CHARS_PER_TOKEN_MAX = 5.0

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
#: completion and the estimate's residual error. It is *not* a safety net for a
#: wrong divisor: 0.8 cannot absorb the 70% under-estimate chars/4 produced on
#: 2026-07-23. Accuracy is :data:`CHARS_PER_TOKEN_DEFAULT`'s job and
#: calibration's; this only reserves room to answer.
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
    #: The divisor the estimates above were taken with — the default, or a
    #: ratio learned from a provider overflow. Persisted with the row: without
    #: it the numbers cannot be reproduced, and reproducing them is the point.
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT
    #: High-water mark of the *serialized* size actually sent, in characters,
    #: across the turn's requests. Unlike every other field this is a running
    #: maximum rather than one request's figure: it exists so that when the
    #: provider rejects a request and names its token count, the router can
    #: pair the two into a real chars-per-token reading. The largest request is
    #: the one that overflowed, and the router never sees mid-turn messages
    #: itself (they live inside ``agent.run``).
    max_request_chars: int = 0
    #: Tool-side defenses applied inside one model request (see the request
    #: guard in ``agent/runtime.py``), not message reductions the window made:
    #: identical calls collapsed to a back-reference, and returns dropped once
    #: one request's tool output passed its byte ceiling. Recorded on the same
    #: window row so replaying the conversation shows they happened.
    duplicate_calls: int = 0
    results_capped: int = 0

    @property
    def reduced(self) -> bool:
        return (
            self.results_elided > 0
            or self.results_truncated > 0
            or self.turns_dropped > 0
            or self.duplicate_calls > 0
            or self.results_capped > 0
        )

    @property
    def saved(self) -> int:
        """Estimated tokens this request's reduction removed."""
        return self.estimated_before - self.estimated_after


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def estimate_tokens(
    messages: list[ModelMessage],
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> int:
    """Rough prompt size of a history, in tokens, over its JSON dump.

    ``chars_per_token`` defaults to :data:`CHARS_PER_TOKEN_DEFAULT` and is
    overridden by a ratio learned from a provider overflow. Passing it
    explicitly keeps this a pure function of its arguments — the window's
    determinism contract — rather than of ambient learned state.
    """
    return int(len(ModelMessagesTypeAdapter.dump_json(messages)) / chars_per_token)


def calibrate_chars_per_token(request_chars: int, reported_tokens: int) -> float | None:
    """The real chars-per-token ratio implied by a provider's overflow error.

    An overflow body that names the request's token count ("request (75967
    tokens) exceeds …") is a free, exact measurement: we know what we sent, and
    the provider just told us what it cost. That is strictly better than any
    constant, and it is the only tokenizer signal available to a deployment
    that must not download one (``CLAUDE.md``: airgapped by default).

    Returns None when the pair is unusable — a non-positive input, or a ratio
    outside :data:`CHARS_PER_TOKEN_MIN` .. :data:`CHARS_PER_TOKEN_MAX` — so the
    caller keeps the default rather than adopting a nonsense divisor.
    """
    if request_chars <= 0 or reported_tokens <= 0:
        return None
    ratio = request_chars / reported_tokens
    if not CHARS_PER_TOKEN_MIN <= ratio <= CHARS_PER_TOKEN_MAX:
        logger.warning(
            "Ignoring implausible chars-per-token ratio %.2f (%d chars / %d tokens) — "
            "outside the %.1f-%.1f band, so the error body was probably misparsed.",
            ratio,
            request_chars,
            reported_tokens,
            CHARS_PER_TOKEN_MIN,
            CHARS_PER_TOKEN_MAX,
        )
        return None
    return ratio


def budget_for(
    context_window: int,
    system_prompt: str,
    tool_schema_chars: int = 0,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> int:
    """Token budget for the message history, from the configured window.

    Three things ship in every request and only one of them is in ``messages``:
    ``MARGIN`` leaves completion headroom, and the system prompt and the
    advertised tool schemas both ride outside the message list, so their
    estimated shares are subtracted here.

    ``tool_schema_chars`` is not optional in spirit — a caller that passes 0
    when tools are advertised will overrun by exactly the size of the tool
    list, which is what happened on 2026-07-23 (28 tools, ~14 inlined copies of
    ``FilterSpec``). It defaults to 0 only so that callers with genuinely no
    tools need not say so. Measure it from the schemas actually advertised for
    the scope (``tools.advertised_schema_chars``); do not estimate it.

    Clamped to a floor of 1: a non-positive budget would silently maximally
    elide every request, which is a misconfiguration worth a warning, not a
    quiet default.
    """
    system_tokens = int(len(system_prompt) / chars_per_token)
    tool_tokens = int(tool_schema_chars / chars_per_token)
    budget = int(context_window * MARGIN) - system_tokens - tool_tokens
    if budget < 1:
        logger.warning(
            "Configured context_window %d leaves no room for messages after the "
            "system prompt (~%d tokens) and the tool schemas (~%d tokens) — every "
            "request will be maximally elided. Raise context_window, or disable "
            "tools the investigation does not need.",
            context_window,
            system_tokens,
            tool_tokens,
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


def _elide(
    messages: list[ModelMessage],
    budget: int,
    running: int,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> tuple[int, int]:
    """Pass 1: stub out tool-return contents oldest-first, in place.

    ``messages`` is the caller's private copy. Returns (results_elided,
    running estimate). The most recent request's returns are protected — they
    are what the model is about to reason over.
    """
    protected = _last_request_index(messages)
    stub_cost = int(_serialized_size(_stub()) / chars_per_token)
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
            saving = int(_serialized_size(part.content) / chars_per_token) - stub_cost
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


def _truncate_newest(
    messages: list[ModelMessage],
    budget: int,
    running: int,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> tuple[int, int]:
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
        keep = max(MIN_KEEP_CHARS, len(text) - int((running - budget) * chars_per_token))
        if keep >= len(text):
            continue
        content = {"truncated": True, "note": TRUNCATION_NOTE, "head": text[:keep]}
        saving = int(len(text) / chars_per_token) - int(_serialized_size(content) / chars_per_token)
        if saving <= 0:
            # The wrapper costs more than the cut saves — leave the original.
            continue
        parts[j] = replace(part, content=content)
        truncated += 1
        running -= saving
    if truncated:
        messages[index] = replace(message, parts=parts)
    return truncated, running


def _drop_turns(
    messages: list[ModelMessage],
    budget: int,
    running: int,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> tuple[int, int]:
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
        running -= estimate_tokens(messages[boundaries[k] : span_end], chars_per_token)
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
    return dropped, running + estimate_tokens(marker, chars_per_token)


def apply_window(
    messages: list[ModelMessage],
    budget: int,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> tuple[list[ModelMessage], WindowStats]:
    """Fit ``messages`` under ``budget`` tokens; pure — the input is not mutated.

    Best effort: a history that cannot fit even after both passes is returned
    as reduced as the invariants allow, and the router's overflow handling
    remains the backstop.

    ``chars_per_token`` is an *argument* rather than ambient state on purpose:
    a learned ratio changes what this function does, and the determinism
    contract (module docstring) is that replaying a conversation under the same
    configuration elides the same bytes. Same (messages, budget, ratio) in,
    same bytes out — so the ratio is recorded alongside the budget in the
    persisted window row.
    """
    before = estimate_tokens(messages, chars_per_token)
    stats = WindowStats(
        budget=budget,
        estimated_before=before,
        estimated_after=before,
        chars_per_token=chars_per_token,
    )
    if before <= budget:
        return list(messages), stats
    out = list(messages)
    stats.results_elided, running = _elide(out, budget, before, chars_per_token)
    if running > budget:
        stats.turns_dropped, running = _drop_turns(out, budget, running, chars_per_token)
    if running > budget:
        stats.results_truncated, running = _truncate_newest(out, budget, running, chars_per_token)
    stats.estimated_after = estimate_tokens(out, chars_per_token)
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


def make_window_processor(
    budget: int,
    stats: WindowStats,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
):
    """History processor for ``ProcessHistory``, keeping the turn's worst request.

    The processor runs once per model request; ``stats`` is overwritten
    wholesale by the request with the largest reduction, so the router can
    persist one honest row per turn rather than one per request. Field-wise
    maxima would be cheaper but would pair one request's ``estimated_before``
    with another's ``estimated_after`` — a delta that never happened, in a
    record that has to stand up as evidence.

    ``chars_per_token`` is fixed for the whole turn: resolved once by the
    router (learned value, else the default) and applied to every request. A
    ratio that shifted mid-turn would make two identical requests reduce
    differently, which the determinism contract forbids.
    """

    def process(messages: list[ModelMessage]) -> list[ModelMessage]:
        out, request_stats = apply_window(messages, budget, chars_per_token)
        # Measured on what is actually sent (post-reduction) — that is what the
        # provider counts, and therefore the only figure a token count from its
        # error body can honestly be divided into.
        sent_chars = len(ModelMessagesTypeAdapter.dump_json(out))
        high_water = max(stats.max_request_chars, sent_chars)
        if not stats.reduced or request_stats.saved > stats.saved:
            for f in fields(WindowStats):
                setattr(stats, f.name, getattr(request_stats, f.name))
        stats.budget = budget
        stats.chars_per_token = chars_per_token
        # Survives the wholesale overwrite above: it is a turn-level maximum,
        # not a property of whichever request reduced the most.
        stats.max_request_chars = high_water
        return out

    return process
