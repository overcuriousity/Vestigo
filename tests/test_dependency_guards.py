"""Guards that fail when an external pin we are waiting on is lifted.

A dependency we cannot upgrade is easy to forget: the reason lives in someone
else's metadata, changes without us noticing, and nothing in this repo would
say so. Each test here asserts that a block is *still* in place, so the day it
lifts the suite fails and names the work that just became possible.

These read installed distribution metadata only — no network, per the
offline-by-default design goal.
"""

from importlib.metadata import PackageNotFoundError, requires, version

import pytest
from packaging.requirements import Requirement


def _requirement_on(distribution: str, dependency: str) -> Requirement:
    """The requirement *distribution* declares on *dependency*.

    Extras are collapsed: a dependency declared under several extras carries the
    same specifier in every case we care about, and asserting on the union would
    mean re-deriving which extras our own install activates.
    """
    try:
        declared = requires(distribution) or []
    except PackageNotFoundError:  # pragma: no cover - install is broken, not our concern
        pytest.fail(f"{distribution} is not installed")
    matches = {
        str(req.specifier) for line in declared if (req := Requirement(line)).name == dependency
    }
    if not matches:
        pytest.fail(
            f"{distribution} no longer declares a requirement on {dependency} — "
            "this guard is measuring nothing; re-derive it or delete it."
        )
    if len(matches) > 1:
        pytest.fail(
            f"{distribution} declares conflicting specifiers on {dependency}: "
            f"{sorted(matches)}. This guard assumed one; re-derive it."
        )
    return Requirement(f"{dependency}{matches.pop()}")


def test_mcp_2_is_still_out_of_reach():
    """Fail once the SDK cap lifts — that is the signal to migrate to mcp 2.x.

    The chain is `vestigo -> pydantic-ai-slim[mcp] -> fastmcp-slim[client] ->
    mcp`, and the `<2.0` cap is fastmcp-slim's, not pydantic-ai's (pydantic-ai
    already allows `fastmcp-slim<5`). fastmcp-slim 4.0.0b5 requires `mcp>=2.0`,
    so 4.0 going stable is what unblocks this — and forces it, since our own
    `mcp<2` pin would then make that bump unresolvable rather than merely
    unhelpful.

    Our side of the migration is small and already surveyed: mcp 2.x renames
    `FastMCP` to `mcp.server.mcpserver.MCPServer` and leaves every internal
    ``agent/tools.py`` reaches for unchanged (``_tool_manager.list_tools()``,
    ``Tool.parameters``, ``Tool.fn_metadata``, ``remove_tool``).
    """
    cap = _requirement_on("fastmcp-slim", "mcp")
    assert not cap.specifier.contains("2.0.0"), (
        f"fastmcp-slim {version('fastmcp-slim')} now allows mcp 2.x ({cap}). "
        "The upgrade this repo has been blocked on is possible — do it:\n"
        "  - agent/tools.py: FastMCP -> mcp.server.mcpserver.MCPServer\n"
        "  - agent/mcp_http.py: server.settings.{stateless_http,\n"
        "    streamable_http_path,transport_security} are now keyword arguments\n"
        "    to streamable_http_app()\n"
        "  - drop the `mcp<2` pin in pyproject.toml\n"
        "  - drop the `mcp` ignore entry in .github/dependabot.yml\n"
        "  - delete this test"
    )
