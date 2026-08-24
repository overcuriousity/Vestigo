"""One typed model call outside the agent loop — no tools, no history, one answer.

The shape ``docs/AGENT.md`` §"Outside the agent loop" describes: the column
advisor (:mod:`vestigo.columns.advisor`) and the converter generator
(:mod:`vestigo.converters.generator`) both need exactly this and nothing of the
agent runtime's conversation machinery. Callers own availability, timeouts,
validation and failure policy; this module owns the wire call only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from vestigo.agent.config import AgentConfig


async def typed_completion[T](
    config: AgentConfig,
    prompt: str,
    *,
    output_type: type[T],
    instructions: str | None = None,
    timeout_s: float,
) -> T:
    """Send ``prompt`` (plus optional ``instructions``) and return the typed output.

    Imports pydantic-ai lazily: both callers are reachable from paths (CLI
    ingest, the converter job) where the model is usually not configured and
    the heavy import chain should not be paid.
    """
    from pydantic_ai import Agent

    from vestigo.agent.availability import probe_headers
    from vestigo.agent.runtime import build_model, effort_model_settings

    async with httpx.AsyncClient(headers=probe_headers(config), timeout=timeout_s) as http_client:
        model = build_model(config, http_client)
        agent = Agent(
            model,
            output_type=output_type,
            toolsets=[],
            instructions=instructions,
            model_settings=effort_model_settings(config),
        )
        result = await agent.run(prompt)
        return result.output
