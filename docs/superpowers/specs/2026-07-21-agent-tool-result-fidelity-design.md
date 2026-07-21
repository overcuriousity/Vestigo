# Budget-aware tool-result fidelity (agent)

Design round for the item `docs/ROADMAP.md` §Milestone 3 records after PR145.
Status: **implemented** (PR146). Shipped shape and the two decisions taken
during review are recorded under *Decisions* at the end; `docs/AGENT.md`
§Tool-result fidelity is the reference for what runs today.

## Problem

Every agent tool result is slimmed to a *fixed* shape (`docs/AGENT.md` §A13(d)):
`search_events` keeps 40 attributes at 200 chars, anomaly findings keep
`event_id` + a 200-char `message`, `list_annotations` truncates bodies at 160.
Those constants were sized for the smallest window we support, because a single
broad turn against a 65,536-token model overflowed at 74,673 (PR145).

That leaves two costs:

- **Large windows pay for caution they do not need.** A 200k-token model could
  hold the full example events comfortably, and the agent reasons better with
  them — the whole point of the `message` carve-out in PR145 was that the
  omitted content is decision-relevant. Today it is omitted regardless.
- **Small windows can still overshoot.** The caps bound each *result*, not the
  *turn*. Seven detectors plus a search plus a pivot still sum to whatever they
  sum to. PR145 moved the ceiling; it did not install one.

## Constraint that shapes the whole design

`CLAUDE.md`: *forensic reproducibility/explainability is a hard requirement for
basically any subsystem*. For this feature that cashes out as one property:

> **Replaying a conversation's tool calls against the same configuration must
> produce byte-identical results.**

An analyst reading an exported conversation has to be able to ask "why did the
agent not mention the source IP here, when it did there?" and get an answer
from the record rather than from a race.

This is what rules out the obvious implementation (a running per-turn budget
that spends down as tools are called): identical calls would return different
data depending on what ran before them in the same turn, and nothing in the
transcript would explain the difference.

## Design

### 1. A named fidelity tier, not an ad-hoc byte count

```python
class Fidelity(StrEnum):
    FULL = "full"        # example events inline, attributes at today's caps
    MESSAGE = "message"  # PR145's shape: event_id + truncated message
    MINIMAL = "minimal"  # ids and aggregates only
```

Three tiers, because there are exactly three useful answers to "how much of an
example record does the model get". Each tool that emits record-shaped payloads
(`run_anomaly_detector`, `search_events`, `get_event`, `list_annotations`,
`similar_events`, `semantic_search`) maps the tier onto its existing constants;
the `MAX_*` caps stay the ceiling at every tier.

`MESSAGE` is the tier PR145 ships unconditionally, so "no configuration" keeps
today's behavior exactly.

### 2. Where the tier comes from — an explicit setting, deterministic inputs

```
tier = min(configured_tier, tier_for_attempt(attempt))
```

**`tool_fidelity` is an operator setting**, resolved through the same
env-wins-per-field layering as every other agent field (`docs/AGENT.md` §A7):
`VESTIGO_AGENT_TOOL_FIDELITY` / the `agent_settings` row / default. Values:
`full` (default), `message`, `minimal`, `auto`.

- **Default `full`** — decided 2026-07-21. An unconfigured deployment gets the
  richest results, and the overflow backstop below (which costs a retry, not a
  lost turn) catches the models that cannot hold them. The tradeoff is explicit:
  *a broad turn on an unconfigured small model now overflows on attempt 0 and
  succeeds on the retry, where PR145 alone made it succeed first time.* An
  operator who wants that back sets `tool_fidelity=message` — or `auto`.
- **`auto`** derives the tier from `context_window` (unset or `< 100k` →
  `message`, `>= 100k` → `full`), for operators who would rather configure the
  window once and let this follow.
- **`tier_for_attempt`** — the overflow backstop. Attempt 0 uses the configured
  tier; each retry after a recognised overflow drops one tier.

Every input is either static config or the attempt number, so the tier is
reconstructible from the export, and nothing depends on call order within a
turn — the reproducibility property above holds by construction.

### 3. Why the retry path needs no history rewriting

The natural worry is that degrading after a 400 means rewriting the
`ToolReturnPart` already sitting in history — making the persisted record
diverge from what the model saw.

It does not, because the overflow retry in `_message_stream_inner` already
re-enters `stream_turn`, which calls `build_tool_server(scope)` afresh and
**re-executes the tools** (this is why `agent.tool_call` audit rows already
carry an `attempt` field). So a lower tier on attempt N is simply a different
`scope`:

```python
scope_for_attempt = replace(scope, fidelity=tier)
```

`AgentScope` is a frozen dataclass built once before the retry loop
(`api/routers/agent.py:463`), and `stream_turn(scope, ...)` is called inside it
— the plumbing point already exists. No history mutation, no divergence.

### 4. Escalation order: cheap lever before expensive one

Today an overflow escalates compaction first (2 recent turns → 1 → give up),
which spends a summarizer LLM call each time and **structurally cannot help a
single-turn overflow** — there is nothing older to fold. The new order:

1. overflow → drop one fidelity tier, retry (no LLM call)
2. still overflowing → compact, retry (as today)
3. exhausted → `error{code="context_overflow"}` (as today)

Step 1 is the one that matches the failure PR145 was written for.

### 5. Every degraded result says so

A payload below `FULL` carries `fidelity` plus the note PR145 introduced, so
the model knows what it is missing and how to get it (`get_event`), and the
exported transcript states which tier produced each result. Reporting only the
data, with the tier implied by config the reader does not have, is the same
failure mode `_listing` avoids by reporting `returned` alongside `total`.

The SSE `compaction` event has a sibling: a `fidelity` event when step 1 fires,
so the analyst sees "results were reduced to fit" in the chat rather than
silently getting a thinner investigation.

## Files

| file | change |
|---|---|
| `src/vestigo/agent/fidelity.py` (new) | `Fidelity`, `resolve_tier`, `tier_for_attempt`, the per-tier constant table |
| `src/vestigo/agent/tools.py` | `AgentScope.fidelity`; `_deflate_findings` takes the tier |
| `src/vestigo/agent/config.py` | `tool_fidelity` in `_FIELD_MAP`/`_DEFAULTS`/`AgentConfig` |
| `src/vestigo/core/config.py` | `agent_tool_fidelity` Settings field (pattern-validated) |
| `src/vestigo/db/postgres.py` + Alembic revision | `agent_settings.tool_fidelity` column |
| `src/vestigo/api/routers/admin.py` | field in the settings schema + validator |
| `frontend/src/pages/admin/AdminAgentPage.tsx` | the control, beside reasoning effort |
| `src/vestigo/api/routers/agent.py` | attempt loop: tier before compaction; `fidelity` SSE event |
| `docs/AGENT.md` §A13(d), §A7 table | replace "static choice" with the tier table |
| `docs/ROADMAP.md` | remove the item |

Decided against a `Fidelity` field on the *conversation*: the tier is a
deployment property, and a per-conversation override would let two conversations
on the same model produce different records with nothing in the case file
explaining why.

## Verification

- Unit: `tier_for_window`/`tier_for_attempt` are pure and total; each tier's
  deflation is idempotent and never *adds* data.
- **Reproducibility test** (the point of the design): the same tool call under
  the same `(config, attempt)` returns byte-identical payloads regardless of
  what ran before it in the turn.
- Router: an overflow drops a tier and retries *before* any summarizer call;
  a second overflow then compacts; the `fidelity` SSE event is emitted once.
- Replay the PR145 transcript at `context_window=65536` (expect `MESSAGE`, no
  overflow) and unset (expect `MESSAGE`, unchanged from today).

## Decisions (2026-07-21)

1. **Fidelity is an explicit operator setting, defaulting to `full`**, rather
   than derived from `context_window`. `auto` remains available for operators
   who prefer the derived behavior. Consequence accepted: an unconfigured small
   model takes an overflow-and-retry on a broad turn.
2. **`full` restores the anomaly-finding example events only.**
   `MAX_ATTRS_PER_EVENT` (40) and `ATTR_VALUE_TRUNCATE` (200) hold at every
   tier — they guard against a single pathological event (a megabyte of JSON in
   one attribute), which is an input-shape risk unrelated to window size, and
   they have never been implicated in an overflow.

## Decisions taken during review (2026-07-21, PR146)

3. **The tier governs every multi-record event payload, not just findings.**
   The first implementation wired `scope.fidelity` into `run_anomaly_detector`
   alone, which made decision 2 above read narrower than it should: a broad
   `search_events` or `similar_events` result overflows a small window just as
   readily. `_slim_event` — the shared choke point for `search_events`,
   `semantic_search` and `similar_events` — now takes the tier too, and
   `FIDELITY_TIERED_TOOLS` names the set. `get_event`,
   `get_event_annotations` and `list_annotations` are exempt, for the reasons
   in `docs/AGENT.md` §Tool-result fidelity.
4. **A drop that cannot change the prompt is not spent.** The escalation in
   §4 assumed every overflow has tool payloads to give up. An overflow on a
   turn that fetched no event records — a long conversation, no tiered tool
   called — would otherwise burn two byte-identical provider round trips
   before reaching the compaction that can actually help. `next_tier` takes
   the attempt's tool names and returns `None` when none of them honours the
   tier. This keeps the determinism property: the input is what the attempt
   *ran*, which the transcript records, not a running budget.
5. **Every tiered result stamps `fidelity`, `full` included.** A result with
   no marker at all cannot be told apart from one produced before the setting
   existed, which an exported conversation has to be able to do. `note` still
   appears only when something was actually dropped.
