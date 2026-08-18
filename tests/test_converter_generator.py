"""The generator: availability gate, name sanitizing, prompt hash, fence stripping."""

from __future__ import annotations

import hashlib

import pytest

from vestigo.agent import availability
from vestigo.agent.config import AgentConfig
from vestigo.converters import generator as G
from vestigo.converters.prompt import SYSTEM_PROMPT_VERSION
from vestigo.core.config import get_settings


@pytest.mark.parametrize(
    "raw,want",
    [
        ("myapp2vestigo", "myapp2vestigo"),
        ("My App", "my_app2vestigo"),
        ("nginx", "nginx2vestigo"),
        ("x" * 80, ("x" * 32) + "2vestigo"),
        ("", "custom2vestigo"),
        ("2vestigo", "custom2vestigo"),
        ("Foo2Timesketch", "foo2vestigo"),
    ],
)
def test_sanitize_name(raw, want):
    assert G.sanitize_name(raw) == want


@pytest.fixture()
def _clean():
    availability.reset_probe_cache()
    yield
    availability.reset_probe_cache()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unavailable_when_agent_not_configured(monkeypatch, _clean):
    monkeypatch.delenv("VESTIGO_AGENT_MODEL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(G.GenerationUnavailable):
        await G.generate_script("s", "t")


@pytest.mark.asyncio
async def test_generate_uses_completion_and_hashes_prompt(monkeypatch, _clean):
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "test-model")
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "http://localhost:9/v1")
    get_settings.cache_clear()

    async def probe_ok(config):
        return True

    monkeypatch.setattr(availability, "_probe", probe_ok)

    seen = {}

    async def fake_complete(config: AgentConfig, system: str, task: str, timeout_s: float):
        seen["system"], seen["task"] = system, task
        return G.ScriptDraft(
            name="Web Server", artifact="nginx:access", script="```python\nprint(1)\n```\n"
        )

    monkeypatch.setattr(G, "_complete", fake_complete)

    out = await G.generate_script("SYS", "TASK")
    assert seen == {"system": "SYS", "task": "TASK"}
    assert out.name == "web_server2vestigo" and out.script == "print(1)\n"
    assert out.model == "test-model" and out.provider_endpoint == "http://localhost:9/v1"
    expected = hashlib.sha256(f"{SYSTEM_PROMPT_VERSION}\nSYS\nTASK".encode()).hexdigest()
    assert out.prompt_hash == expected
