"""Making a mid-turn history snapshot replayable.

A turn's history is checkpointed as it runs (see ``agent/runtime.py``), so the
snapshot the router persists is whatever ``AgentRun.new_messages()`` holds at
that instant. Two shapes in that snapshot need attention:

1. **Unpaired tool calls.** pydantic-ai assembles the ``ModelRequest`` carrying
   ``ToolReturnPart``s only once a whole tool batch finishes, so a snapshot
   taken mid-batch has calls with no answer. Providers on the Anthropic protocol
   reject an unpaired ``tool_use``, and ``agent/window.py`` assumes pairing holds
   when it picks turn boundaries. pydantic-ai repairs this too
   (``_repair_dangling_tool_calls``), but only with a generic stub — the point of
   doing it here is that a call the turn *did* answer is replayed with the tool's
   own ``ToolReturnPart``, so the resumed history agrees with the run about what
   the tool returned. Only a call that genuinely never returned gets
   :data:`INTERRUPTED_RESULT`.

   A trailing ``ToolCallPart`` whose JSON arguments were cut off mid-stream is
   *kept* and answered like any other, not dropped: removing a part rewrites the
   shape of a ``ModelResponse`` whose thinking signature was computed over the
   turn that included the call, and this blob is the only place those signatures
   live. Malformed arguments are already sendable — serializers degrade them via
   ``ToolCallPart.args_as_dict`` — so replaying the call costs nothing.
   Same reasoning pydantic-ai gives for its own pass.

2. **A trailing request.** A completed turn's history always ends in a
   ``ModelResponse``; a repaired snapshot would end in the synthesized
   ``ModelRequest`` of tool returns. pydantic-ai 2.17.0 already merges such a
   trailing request into the next turn's prompt request
   (``_merge_consecutive_messages``, run on every request build), so the unmerged
   shape is not itself a protocol error today. :data:`RESUME_MARKER` closes the
   pair anyway, for two reasons: that merge is private API and the dependency pin
   is ``>=``, and a checkpoint blob that ends where a completed turn's ends is one
   less shape for everything downstream to reason about.
   ``tests/test_agent_resume.py`` asserts the library still merges, so a future
   bump that changes it fails loudly rather than silently.

:func:`repair_partial` fixes both, and is deliberately pure: same snapshot in,
same blob out, which is the determinism constraint the sliding window and the
fidelity tiers already hold to.

Kept honest: a synthesized answer is either the part the tool actually produced
or an explicit ``interrupted`` marker. Never anything invented.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

#: Content stored for a tool call the turn never got an answer for. Shaped
#: like the window's elision stub, and naming the recovery path, so the model
#: reads it as "re-run this" rather than as an empty result. The part carrying
#: it is also stamped ``outcome="interrupted"``, so a reader of a `raw_history`
#: export can tell synthesized answers from real ones without parsing prose.
INTERRUPTED_RESULT: dict[str, Any] = {
    "interrupted": True,
    "note": "The previous turn ended before this tool returned. Re-run the call if you still need its result.",
}

#: Closes a repaired snapshot that would otherwise end in a ``ModelRequest``
#: (see the module docstring, point 2). Visible to the analyst in raw_history
#: exports.
RESUME_MARKER = "Understood. Continuing from where the previous turn was interrupted."

#: Sent as the next turn's ``instructions`` when the stored history is a
#: mid-turn checkpoint. Neutral about the cause — a provider error and an
#: analyst pressing Stop both land here — and it directs continuity of
#: *context*, not of the model's old plan, because a Stop usually means
#: "redirect". Deliberately *not* folded into the user prompt: that text is
#: persisted verbatim as a ``UserPromptPart``, so the history would claim the
#: analyst wrote it, and a repeatedly interrupted conversation would stack one
#: stale note per interruption. Instructions belong to the request being made,
#: not to the replayed history (verified against pydantic-ai 2.17.0: the model
#: takes them from the current run's ``instruction_parts``, never from a
#: historical ``ModelRequest.instructions``).
RESUME_NOTE = (
    "Note: the previous turn ended early (an error, or the analyst stopped it). "
    "Everything it established is already in the conversation history above — "
    "build on those findings instead of repeating the orientation steps, and "
    "answer the analyst's message below."
)


def _answered_call_ids(messages: list[ModelMessage]) -> set[str]:
    """Tool call ids that already have an answer.

    A ``RetryPromptPart`` answers a rejected call just as a ``ToolReturnPart``
    answers a successful one — synthesizing a second answer for it would break
    the pairing it is meant to restore.
    """
    return {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id
    }


def _is_tool_result_part(part: Any) -> bool:
    """Whether a request part answers a tool call (and so must lead the request)."""
    return isinstance(part, (ToolReturnPart, RetryPromptPart)) and bool(part.tool_call_id)


def _answer(
    call: ToolCallPart, results: dict[str, ToolReturnPart | RetryPromptPart]
) -> ToolReturnPart | RetryPromptPart:
    """The part that answers ``call``.

    The streamed part itself when the tool returned (or was rejected) — not a
    reconstruction of it, so ``content``'s type, ``outcome`` and ``metadata``
    all match what the run saw, which is what forensic reproducibility asks
    for. Otherwise an explicit interrupted marker.
    """
    streamed = results.get(call.tool_call_id)
    if streamed is not None:
        return streamed
    return ToolReturnPart(
        tool_name=call.tool_name,
        content=INTERRUPTED_RESULT,
        tool_call_id=call.tool_call_id,
        outcome="interrupted",
    )


def repair_partial(
    messages: list[ModelMessage],
    *,
    results: dict[str, ToolReturnPart | RetryPromptPart],
) -> list[ModelMessage]:
    """Return a replay-safe copy of a mid-turn snapshot.

    Args:
        messages: The snapshot, as returned by ``AgentRun.new_messages()``.
        results: The parts the turn streamed in answer to its tool calls, keyed
            by tool call id.

    Returns:
        A new list; ``messages`` is never mutated. Guarantees every tool call is
        answered, each answer sits in the request immediately following the
        response that made the call, and the history ends in a ``ModelResponse``.
    """
    if not messages:
        return []

    # Pass 1: answer every dangling call, in the request that follows the
    # response which made it. Batching them all onto the end instead would
    # separate an answer from its call by intervening responses, which the
    # Anthropic protocol rejects and no later normalization reorders.
    answered = _answered_call_ids(messages)
    dangling: dict[int, list[ToolCallPart]] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, ModelResponse):
            continue
        open_calls = [
            part
            for part in message.parts
            if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
        ]
        if open_calls:
            dangling[index] = open_calls

    repaired: list[ModelMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        repaired.append(message)
        open_calls = dangling.get(index)
        if open_calls is None:
            index += 1
            continue
        synthesized = [_answer(call, results) for call in open_calls]
        following = messages[index + 1] if index + 1 < len(messages) else None
        if isinstance(following, ModelRequest):
            # Ahead of the request's user-facing parts, behind its existing tool
            # results — where providers expect tool results to sit.
            insert_at = next(
                (
                    part_index + 1
                    for part_index in range(len(following.parts) - 1, -1, -1)
                    if _is_tool_result_part(following.parts[part_index])
                ),
                0,
            )
            repaired.append(
                replace(
                    following,
                    parts=[
                        *following.parts[:insert_at],
                        *synthesized,
                        *following.parts[insert_at:],
                    ],
                )
            )
            index += 2
            continue
        # No request follows, so the answers need one of their own. `state`
        # records *why* it exists; pydantic-ai reads the same flag to decide a
        # trailing request's open calls will never be executed.
        repaired.append(ModelRequest(parts=synthesized, state="interrupted"))
        index += 1

    # Pass 2: end on a ModelResponse (see the module docstring, point 2).
    # Idempotent: the marker is itself a ModelResponse, so a second repair adds
    # nothing.
    if isinstance(repaired[-1], ModelRequest):
        repaired.append(ModelResponse(parts=[TextPart(content=RESUME_MARKER)]))
    return repaired
