"""Making a mid-turn history snapshot replayable.

A turn's history is checkpointed as it runs (see ``agent/runtime.py``), so the
snapshot the router persists is whatever ``AgentRun.new_messages()`` holds at
that instant. Two shapes in that snapshot cannot be replayed as-is:

1. **Unpaired tool calls.** pydantic-ai assembles the ``ModelRequest`` carrying
   ``ToolReturnPart``s only once a whole tool batch finishes, so a snapshot
   taken mid-batch has calls with no answer. Providers on the Anthropic
   protocol reject an unpaired ``tool_use``, and ``agent/window.py`` assumes
   pairing holds when it picks turn boundaries.
2. **A truncated trailing response.** When a model stream dies mid-flight,
   pydantic-ai appends the *partial* ``ModelResponse`` to the history so
   nothing is lost. Its last ``ToolCallPart`` can carry half-written JSON
   arguments, and it never reached the tool executor.
3. **A trailing request.** A completed turn's history always ends in a
   ``ModelResponse``; a repaired snapshot would end in the synthesized
   ``ModelRequest`` of tool returns. pydantic-ai appends the next turn's prompt
   as its own ``ModelRequest``, and the Anthropic mapping emits one
   ``role: "user"`` message per request with no merging — two consecutive user
   messages, which Anthropic-protocol endpoints (the Kimi ``/coding`` endpoint
   included, see ``agent/runtime.py``) reject with a role-alternation error.
   :data:`RESUME_MARKER` closes the pair, exactly as ``agent/window.py``'s turn
   drop does for the same reason.

:func:`repair_partial` fixes all three, and is deliberately pure: same snapshot in,
same blob out, which is the determinism constraint the sliding window and the
fidelity tiers already hold to.

Kept honest: a synthesized return carries the result the tool actually
streamed, or :data:`INTERRUPTED_RESULT`. Never anything invented.
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
#: reads it as "re-run this" rather than as an empty result.
INTERRUPTED_RESULT: dict[str, Any] = {
    "interrupted": True,
    "note": "The previous turn ended before this tool returned. Re-run the call if you still need its result.",
}

#: Closes a repaired snapshot that would otherwise end in a ``ModelRequest``
#: (see the module docstring, point 3). Same request/response *pair* shape the
#: window's turn drop uses, and visible to the analyst in raw_history exports.
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


def repair_partial(
    messages: list[ModelMessage],
    *,
    called_ids: set[str],
    results: dict[str, Any],
) -> list[ModelMessage]:
    """Return a replay-safe copy of a mid-turn snapshot.

    Args:
        messages: The snapshot, as returned by ``AgentRun.new_messages()``.
        called_ids: Tool call ids the turn actually dispatched (one per
            observed ``FunctionToolCallEvent``).
        results: Streamed tool results, keyed by tool call id.

    Returns:
        A new list; ``messages`` is never mutated. Guarantees no unpaired tool
        call and no tool call that was never dispatched.
    """
    if not messages:
        return []
    repaired = list(messages)

    # Pass 1: prune phantom calls from the trailing response only. An earlier
    # response's calls demonstrably ran (their returns follow), so a missing id
    # there would mean our bookkeeping lost it — dropping it would delete a
    # real investigation step from the record.
    last = repaired[-1]
    if isinstance(last, ModelResponse):
        kept = [
            part
            for part in last.parts
            if not (isinstance(part, ToolCallPart) and part.tool_call_id not in called_ids)
        ]
        if len(kept) != len(last.parts):
            if kept:
                repaired[-1] = replace(last, parts=kept)
            else:
                repaired.pop()

    # Pass 2: answer every call still hanging.
    answered = _answered_call_ids(repaired)
    pending = [
        part
        for message in repaired
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
    ]
    if pending:
        repaired.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=part.tool_name,
                        content=results.get(part.tool_call_id, INTERRUPTED_RESULT),
                        tool_call_id=part.tool_call_id,
                    )
                    for part in pending
                ]
            )
        )

    # Pass 3: end on a ModelResponse. The next turn's prompt arrives as its own
    # ModelRequest, and the Anthropic protocol rejects two consecutive user
    # messages — so a snapshot ending in a request (the synthesized returns
    # above, or a retry prompt) needs the pair closed. Idempotent: the marker
    # is itself a ModelResponse, so a second repair adds nothing.
    if repaired and isinstance(repaired[-1], ModelRequest):
        repaired.append(ModelResponse(parts=[TextPart(content=RESUME_MARKER)]))
    return repaired
