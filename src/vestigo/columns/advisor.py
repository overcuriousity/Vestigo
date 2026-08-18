"""One typed LLM call that picks grid columns from pre-scored candidates.

This is deliberately **not** an agent turn. There is no conversation, no tool
server, no sandbox to apply from, and nothing the model can reach beyond the
candidate table handed to it — the deterministic sweep over the field-stats
cache has already run, and the model's entire job is judgement about which of
those fields an analyst would rather look at. That is the "deliberate subset of
existing tools, run deterministically but not actively called by the AI" the
issue asks for: the evidence gathering is code, the taste is the model's.

Every agent invariant that can apply here does (`docs/AGENT.md`):

* **Invisible unless configured.** Gated on the same cached
  :func:`~vestigo.agent.availability.agent_available` probe ``/api/health``
  uses, so an unconfigured or unreachable endpoint costs nothing and changes
  nothing.
* **Scope safety, stated exactly.** What crosses the wire is the candidate
  table and nothing else: for at most
  :data:`MAX_CANDIDATES_IN_PROMPT` fields, the field name, how many sources
  carry it, its fill rate, its distinct count, and up to three **real sample
  values from the case's events**, each truncated to 40 characters. Those
  samples are evidence — usernames, hostnames, addresses, paths — so this is
  egress, and an analyst opts into it per timeline: nothing reaches this
  module unless someone pressed "Suggest with AI" on that timeline, having
  read the disclosure naming this endpoint and model. Ingest, timeline
  creation, the CLI and the demo build never call it. No event row, no
  case/source/timeline id, no API key and no analyst identity are sent.
* **Sandboxed.** The result is a *default*, not a mutation: it lands in
  ``Timeline.recommended_columns`` and any analyst's own column choice
  outranks it.
* **Bounded trust.** Anything the model returns that is not in the candidate
  set is discarded. A malformed, empty, timed-out or oversized response is
  indistinguishable from "no LLM configured" — the heuristic ranking stands
  and the persisted ``method`` says so.

Like the rest of the agent and OIDC, this is independent of
``VESTIGO_ALLOW_ONLINE`` (``docs/TECH_STACK.md`` §6): it reaches only the
endpoint the operator explicitly configured.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from vestigo.columns.recommend import ColumnCandidate
from vestigo.core.config import get_settings

logger = logging.getLogger(__name__)

#: Fallback ceiling for the whole call — the availability probe and config
#: resolution included, not just the model request. A column suggestion is a
#: nicety; it must never hold an ingest-triggered job open the way an agent
#: turn may. The operator's ``column_advisor_timeout_seconds`` is what
#: actually applies; this is only what a caller with no settings would get.
ADVISOR_TIMEOUT_SECONDS = 45.0

#: Candidates shown to the model. Enough for a real choice, small enough that
#: the prompt stays a few hundred tokens.
MAX_CANDIDATES_IN_PROMPT = 20

#: Reason clauses longer than this are truncated — they render in a tooltip.
_MAX_REASON_LENGTH = 160

_INSTRUCTIONS = """\
You are choosing the default columns for a forensic log timeline's event grid.

An analyst opens this timeline and sees a table. A `timestamp` column is
already pinned as the first column — do not choose it. Choose {k_min} to
{k_max} more columns from the candidate list below, ordered most useful first.

Judge by what a security analyst scanning this timeline would want to read at
a glance: who did it, from where, to what, and with what outcome. Prefer
fields that are present across several sources and that group events into
comparable values. Avoid fields that merely restate another chosen column.

Rules:
- Choose ONLY from the candidate tokens listed. Never invent a field name.
- Return between {k_min} and {k_max} tokens.
- Give each choice a reason of at most 12 words, phrased for an analyst.

Candidates (token — sources carrying it, fill rate, distinct values, samples):
{candidates}
"""


class ColumnChoice(BaseModel):
    """The model's structured answer."""

    columns: list[str] = Field(description="Chosen candidate tokens, most useful first.")
    reasons: dict[str, str] = Field(
        default_factory=dict, description="Chosen token -> short reason."
    )


@dataclass(frozen=True)
class AdvisorResult:
    """A validated LLM recommendation."""

    columns: list[str]
    reasons: dict[str, str]
    model: str


def _format_candidates(candidates: list[ColumnCandidate]) -> str:
    """Render the candidate table the prompt carries."""
    lines = []
    for c in candidates:
        samples = ", ".join(s[:40] for s in c.samples) or "—"
        lines.append(
            f"- {c.token} — {c.sources_present}/{c.sources_total} sources, "
            f"{round(c.fill * 100)}% filled, {c.distinct} distinct; samples: {samples}"
        )
    return "\n".join(lines)


def _validate(
    choice: ColumnChoice,
    candidates: list[ColumnCandidate],
    *,
    k_min: int,
    k_max: int,
) -> tuple[list[str], dict[str, str]] | None:
    """Intersect the model's answer with the candidate set, or reject it.

    Order is the model's; membership is ours. Duplicates collapse, unknown
    tokens are dropped silently (they are the common small-model failure and
    say nothing useful in a log), and a result that no longer reaches *k_min*
    after filtering is rejected outright rather than padded — a half-honoured
    recommendation is worse than the deterministic one it replaced.
    """
    allowed = {c.token for c in candidates}
    columns: list[str] = []
    for token in choice.columns:
        cleaned = token.strip()
        if cleaned in allowed and cleaned not in columns:
            columns.append(cleaned)
    if len(columns) < k_min:
        return None
    columns = columns[:k_max]
    reasons = {
        token: str(choice.reasons[token])[:_MAX_REASON_LENGTH]
        for token in columns
        if token in choice.reasons and str(choice.reasons[token]).strip()
    }
    return columns, reasons


async def rank_columns_with_llm(
    candidates: list[ColumnCandidate],
    *,
    k_min: int = 3,
    k_max: int = 5,
) -> AdvisorResult | None:
    """Ask the configured LLM to pick columns from *candidates*.

    Returns ``None`` — meaning "keep the heuristic answer" — when the agent is
    not configured or reachable, when the call fails or times out, or when the
    response does not survive :func:`_validate`. Never raises: this runs inside
    an ingest-triggered background job where the recommendation is the least
    important thing happening.
    """
    if len(candidates) < k_min:
        return None

    # Imported here, not at module scope: pydantic-ai and the agent runtime are
    # a heavy import chain, and this module is reachable from the CLI ingest
    # path where the LLM is usually not configured at all.
    from vestigo.agent.availability import agent_available
    from vestigo.agent.config import resolve_agent_config
    from vestigo.agent.oneshot import typed_completion

    try:
        # One budget for the whole call. An endpoint that accepts a connection
        # and then stalls can hold the availability probe or config resolution
        # open just as long as a completion, and the post-ingest job that owns
        # this coroutine has no reason to wait for either.
        budget = get_settings().column_advisor_timeout_seconds
        async with asyncio.timeout(budget):
            if not await agent_available():
                return None

            config = await resolve_agent_config()
            if not config.model:
                return None

            shown = candidates[:MAX_CANDIDATES_IN_PROMPT]
            prompt = _INSTRUCTIONS.format(
                k_min=k_min, k_max=k_max, candidates=_format_candidates(shown)
            )
            # No toolsets and no system prompt beyond the instruction: this
            # call has nothing to call and nothing to remember.
            output = await typed_completion(
                config, prompt, output_type=ColumnChoice, timeout_s=budget
            )

        validated = _validate(output, shown, k_min=k_min, k_max=k_max)
        if validated is None:
            logger.info("Column advisor response did not survive validation; keeping heuristic")
            return None
        columns, reasons = validated
        return AdvisorResult(columns=columns, reasons=reasons, model=config.model)
    except Exception:  # noqa: BLE001 — advisory call: degrade, never fail the job
        logger.warning("Column advisor call failed; keeping heuristic ranking", exc_info=True)
        return None
