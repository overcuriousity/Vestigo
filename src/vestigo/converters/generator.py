"""One typed model call that writes (or rewrites) a converter script.

Not an agent turn — same shape as :mod:`vestigo.columns.advisor`: no tools, no
history, one request, typed output. Availability is the agent's cached probe;
the caller (``converters/job.py``) owns retries, recording, and the loop.
Unlike the advisor this raises on failure: a converter that could not be
written is a failed attempt the analyst must see, not a silent fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from vestigo.agent.oneshot import typed_completion
from vestigo.converters.prompt import SYSTEM_PROMPT_VERSION

if TYPE_CHECKING:
    from vestigo.agent.config import AgentConfig

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[^a-z0-9_]+")
_MAX_STEM = 32
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n(.*?)\n```\s*$", re.DOTALL)


class GenerationUnavailable(RuntimeError):
    """No configured/reachable model — the caller must not retry."""


class ScriptDraft(BaseModel):
    """The model's structured answer."""

    name: str = Field(description="Converter identifier, e.g. myapp2vestigo")
    artifact: str = Field(default="", description="Short artifact type chosen for the events")
    script: str = Field(description="Complete Python source of the converter")


@dataclass(frozen=True)
class GeneratedScript:
    """A sanitized draft plus the provenance the row records."""

    name: str
    artifact: str
    script: str
    model: str
    provider_endpoint: str | None
    prompt_hash: str


def sanitize_name(raw: str) -> str:
    """Coerce a model-proposed name to ``^[a-z0-9_]{1,32}2vestigo$``."""
    stem = raw.strip().lower()
    stem = stem.removesuffix("2vestigo").removesuffix("2timesketch")
    stem = _NAME_RE.sub("_", stem).strip("_")[:_MAX_STEM].strip("_")
    return f"{stem or 'custom'}2vestigo"


def prompt_hash(system: str, task: str) -> str:
    """Stable digest of exactly what was sent, plus the system-prompt version."""
    return hashlib.sha256(f"{SYSTEM_PROMPT_VERSION}\n{system}\n{task}".encode()).hexdigest()


def _strip_fences(script: str) -> str:
    m = _FENCE_RE.match(script.strip())
    return m.group(1) if m else script


async def _complete(config: AgentConfig, system: str, task: str, timeout_s: float) -> ScriptDraft:
    """The wire call (:func:`vestigo.agent.oneshot.typed_completion`); tests replace this."""
    return await typed_completion(
        config, task, output_type=ScriptDraft, instructions=system, timeout_s=timeout_s
    )


async def generate_script(system: str, task: str, *, timeout_s: float = 180.0) -> GeneratedScript:
    """Ask the configured model for a converter. Raises on any failure; never degrades silently."""
    from vestigo.agent.availability import agent_available
    from vestigo.agent.config import resolve_agent_config

    async with asyncio.timeout(timeout_s):
        if not await agent_available():
            raise GenerationUnavailable("no configured or reachable model endpoint")
        config = await resolve_agent_config()
        if not config.model:
            raise GenerationUnavailable("no model configured")
        draft = await _complete(config, system, task, timeout_s)
    script = _strip_fences(draft.script).strip("\n") + "\n"
    return GeneratedScript(
        name=sanitize_name(draft.name),
        artifact=draft.artifact.strip()[:64],
        script=script,
        model=config.model,
        provider_endpoint=config.api_base_url,
        prompt_hash=prompt_hash(system, task),
    )
