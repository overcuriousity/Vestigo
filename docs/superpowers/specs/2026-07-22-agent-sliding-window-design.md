# Agent sliding-window context management — design

Date: 2026-07-22. Brainstormed against a real failure: an exported conversation
(`ornith:9b`, 64k window) overflowed twice in a single turn. The fidelity ladder
dropped a tier and re-ran the whole turn (doubling the work), and compaction
could not fire because it was the conversation's first turn.

## Problem

The failing class is **mid-turn overflow**: tool results accumulating inside one
`agent.run`. The two existing mechanisms both miss it:

- the fidelity overflow ladder re-runs the whole turn one tier down — the model
  re-issues the same broad plan, so the retry is nearly as large;
- compaction folds *previous* turns — a first-turn overflow has nothing to fold.

## Decision

One deterministic mechanism replaces both dynamic ones: a **sliding window**
applied before *every* model request (mid-turn included) via pydantic-ai's
`ProcessHistory` capability.

- **Pass 1 — elide:** oldest-first, each `ToolReturnPart.content` is replaced by
  a small stub (`{"elided": true, "note": ...}`) until the estimated prompt fits
  the budget. Message structure is untouched, so tool_call/tool_result pairing
  and role alternation stay valid on every provider protocol.
- **Pass 2 — drop turns:** if still over budget, whole oldest user turns (never
  the first — it carries the case context) are replaced by a marker pair.
- **Protected:** the first user request, tool returns of the most recent request
  cycle, and all assistant prose (the findings narrative).
- **Transparent to the model:** stubs are visible in the replayed history and a
  system-prompt note explains the mechanism, so the model can adapt (narrower
  queries, `get_event` to recover specifics).
- **Deterministic:** `apply_window(messages, budget)` is a pure function of its
  inputs — replaying a conversation under the same configuration elides the
  same bytes. This is the same forensic constraint that shaped the fidelity
  tiers. The DB history blob stays complete; the window applies at send time.

### Retired

- The fidelity **overflow ladder** (`degrade`, `next_tier`, turn re-runs).
  Static `tool_fidelity` config (full/message/minimal/auto) stays — shaping
  results up front still saves round trips on small models.
- **LLM compaction** (`agent/compaction.py`), entirely. The summarizer ran on
  the same (weak) investigation model, its output was nondeterministic, and its
  niche — very long conversations — is covered by pass 2 plus "start a new
  conversation".
- The `compact_threshold` setting.

### Backstop for unset `context_window`

On a provider context-overflow error with no window configured: derive a budget
from the failed request's estimated size (0.8×), enable the window, retry the
turn **once**. If the window was already active, shrink to 0.6× and retry once.
A second overflow surfaces the existing friendly `context_overflow` error.

### Audit trail

A turn whose window elided or dropped anything persists one `role="window"`
transcript row (human sentence + stats: results elided, turns dropped, budget,
estimated before/after) and an `agent.window` audit row — the same pattern that
made the fidelity drop diagnosable from an export alone.
