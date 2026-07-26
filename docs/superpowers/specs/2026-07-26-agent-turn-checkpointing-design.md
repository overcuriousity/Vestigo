# Agent turn checkpointing and interrupted-turn resume — design

Date: 2026-07-26. Brainstormed against a real failure: an exported conversation
(`agentconv_0bf32269`, `ornith:9b`, exported at `vestigo_version` 1.8.0) shows
152 persisted message rows — 125 tool calls/results across ~12 minutes of
investigation — and a `raw_history` of length **0**. The analyst's second turn
started with no context at all and re-ran the same orientation sweep
(`list_fields`, `list_artifacts`) the first turn had already completed.

## Problem

A conversation has two stores and only one of them is replayed.

- `AgentMessage` rows are written incrementally, one per streamed event
  (thinking segment, tool call, tool result, assistant text). They are the
  human-readable forensic record and the UI's transcript.
- `AgentConversation.history` — the pydantic-ai wire blob — is the *only* input
  to the next turn (`api/routers/agent.py`, `history = load_history(conversation.history)`).
  The message rows never feed it.

`history` is written in exactly one place: inside `if event["type"] == "result"`,
which fires only on `AgentRunResultEvent`, which `run_stream_events` emits only
on clean completion. Every other exit drops the entire turn's `new_messages`:

- the stop/cancel path writes an `"… [stopped]"` assistant row and no history;
- the `ModelHTTPError` / `UnexpectedModelBehavior` / `UsageLimitExceeded` paths
  write an `"… [interrupted]"` row and no history;
- a client disconnect or a process restart runs no persistence code at all.

The loss is permanent, not deferred. When a later turn does complete, it writes
`dump_history(history + turn.new_messages)` from a `history` that is still the
pre-interruption one — so the interrupted turn's work is overwritten rather than
merely delayed, while the UI keeps showing all 125 tool results that the model
can no longer see.

This is the difference between a continuous assistant and a chat box that
silently forgets: an interrupted five-minute investigation must resume, not
restart.

## Decision

Checkpoint the turn's message history as it is produced, repair each snapshot so
it is replay-safe, and tell the model on resume that it was interrupted.

No new truncation mechanism. Reconstructed history is ordinary
`message_history` and flows through the existing sliding window
(`agent/window.py`, `2026-07-22-agent-sliding-window-design.md`) before every
model request.

### 1. `agent/resume.py` — new module, pure functions, no I/O

`repair_partial(new_messages, streamed_results) -> list[ModelMessage]`

A mid-turn snapshot of `AgentRun.new_messages()` is not directly replayable.
Two defects have to be fixed:

- **Truncated tail.** When a model stream dies mid-flight, pydantic-ai appends
  the *partial* `ModelResponse` to `message_history` (`_agent_graph.py`,
  `_resolve_interrupted_stream_state`). Its trailing `ToolCallPart` can carry
  half-written JSON arguments. Any `ToolCallPart` for which no
  `FunctionToolCallEvent` was observed is dropped.
- **Unpaired tool calls.** pydantic-ai assembles the `ModelRequest` carrying
  `ToolReturnPart`s only after a whole tool batch finishes, so an interrupt
  mid-batch leaves calls with no returns. Providers on the Anthropic protocol
  reject unpaired `tool_use`, and `window.py`'s `_user_turn_boundaries` /
  `_last_request_index` assume pairing holds.

For every surviving `ToolCallPart` without a matching `ToolReturnPart`, a
`ModelRequest` of synthesized `ToolReturnPart`s is appended. Content comes from
the results the turn already streamed, buffered by `tool_call_id` — the tool ran
and its output is real, so it is kept rather than thrown away. Only calls that
genuinely never returned get `{"interrupted": true, "note": …}`, phrased like
the window's elision stub so the model knows re-running the tool is the recovery
path.

Postcondition, asserted in tests: the returned list contains zero unpaired tool
calls and no `ToolCallPart` with unparseable arguments.

`RESUME_NOTE` — the sentence prefixed to the next turn's context when the stored
blob is partial.

### 2. `stream_turn` moves from `run_stream_events` to `agent.iter`

`run_stream_events` wraps `run` and exposes no handle on the in-progress run, so
partial messages are unreachable. `agent.iter` yields the `AgentRun`, whose
`new_messages()` returns what the run has produced *so far*.

Verified against pydantic-ai 2.17.0: `ModelRequestNode.stream(ctx)` and
`CallToolsNode.stream(ctx)` are async context managers yielding the same
`AgentStreamEvent` types the current loop already maps, so the event-mapping
body is unchanged. What changes:

- the loop becomes `async for node in run:` with `async with node.stream(run.ctx)`
  for the two streaming node types;
- `AgentRunResultEvent` no longer arrives — `TurnResult` is built from
  `run.result` after the loop (same `output`, `new_messages()`, `usage.input_tokens`,
  `usage.output_tokens`);
- a caller-owned `TurnRecorder` joins `window_stats` as an out-parameter.

```python
@dataclass
class TurnRecorder:
    """Replay-safe snapshot of what one turn has produced so far."""
    messages: list[ModelMessage] = field(default_factory=list)
    revision: int = 0
```

`stream_turn` buffers each `FunctionToolResultEvent` payload by `tool_call_id`,
and after **each tool result and each completed node** sets
`recorder.messages = repair_partial(run.new_messages(), buffered)`, bumps
`revision`, and yields `{"type": "checkpoint"}` — consumed by the router, never
forwarded to the SSE client.

Checkpointing at both granularities is deliberate. Per-node alone would lose a
long tool batch to a `kill -9`; the export's turn issued up to four tools per
batch, each taking seconds against ClickHouse.

### 3. Router persistence — `history` stops being write-once

| Exit path | `history` | `history_partial_at` |
|---|---|---|
| `checkpoint` | write | set to now |
| `result` | write (unchanged) | cleared |
| cancel — `[stopped]` | write | cleared |
| `ModelHTTPError` / `UnexpectedModelBehavior` / `UsageLimitExceeded` — `[interrupted]` | write | set |
| hard kill, OOM, container restart | last checkpoint stands | stays set |

Every write is `dump_history(history + recorder.messages)` computed from the
*pre-turn* `history` base, and `recorder` resets per `attempt`. So the overflow
re-run (attempt 1) supersedes attempt 0's checkpoints instead of concatenating
with them.

A user-initiated stop clears the flag: the analyst stopped on purpose and must
not be nudged to continue on their next message. The work is still preserved —
only the resume marker differs.

### 4. Schema

`AgentConversation.history_partial_at: Mapped[datetime | None]`, nullable, plus
an autogenerated Alembic revision. Migrations run against SQLite in tests, so
the revision stays dialect-portable.

A column rather than sniffing the blob: a completed turn and an interrupted one
both end in a `ModelResponse`, so structural detection is ambiguous.

`PostgresStore.update_agent_conversation` gains the parameter. It needs a
sentinel default (`_UNSET`), not `None` — `None` already means "no change" in
that method, and clearing the flag is a real update. Same trap the existing
`disabled_tools` docstring calls out.

### 5. Resume marker

When `conversation.history_partial_at` is set, the router prefixes `RESUME_NOTE`
to the context string `stream_turn` builds: the previous turn was interrupted
mid-investigation, the findings so far are in the history, continue from them
rather than re-orienting. It rides in the last user turn, which the window never
elides.

### 6. Context constraints on reconstruction

No new truncation code. The reconstructed blob is passed as `message_history`
and `ProcessHistory` applies `apply_window` before every model request —
elide, drop-turn, truncate, unchanged. What the fix leans on:

- repaired snapshots preserve tool-call pairing, so the window's boundary logic
  stays valid on resumed history;
- the learned window budget and the calibrated `chars_per_token` already persist
  per conversation and are reused, so a resumed turn starts with the budget the
  interrupted turn paid to learn;
- with no configured window, the reactive overflow retry still catches the
  first request, and its `estimate_tokens(history, …)` derived budget now sees
  the real history instead of an empty one — more accurate, not less.

Known limit, unchanged but now reachable more often: window pass 2 splits only
at user-prompt boundaries, and a resumed partial turn is a single large user
turn, so pass 2 can only drop it wholesale. Passes 1 and 3 carry that case.

## Alternatives rejected

- **Rebuild history from `AgentMessage` rows.** No migration, but lossy: the
  rows hold no thinking signatures and no provider part structure, so Kimi-style
  reasoning blocks and byte-identical replay — the property `history` exists for
  — would be gone. Orphan repair would still be needed.
- **Persist only on graceful exits.** One write per turn, but a hard kill still
  loses everything, which is the reported failure.
- **Drop the trailing `ModelResponse` on interrupt.** Always structurally valid,
  but discards the model's reasoning and every tool call in the final batch.
- **Stub every unpaired call.** Correct by construction, but makes the model
  re-run tools whose output was already captured and streamed to the analyst.

## Testing

`tests/test_agent_resume.py`

- `repair_partial`: unpaired call gets a synthesized return from the streamed
  buffer; never-returned call gets the interrupted stub; trailing `ToolCallPart`
  with no observed call event is dropped; a complete snapshot is returned
  unchanged.
- Fake-model turn killed mid-tool-batch: the stored blob replays as
  `message_history` without a provider protocol error.
- Router: an errored turn sets `history_partial_at` and persists a non-empty
  blob; a cancelled turn persists the blob and clears the flag; a completed turn
  clears it.
- Window: a repaired partial blob survives `apply_window` with pairing intact.

## Docs

`docs/AGENT.md` gains the checkpoint/resume semantics in the same commit
(required by `CLAUDE.md`), and `docs/PROGRESS.md` gets the session entry.
