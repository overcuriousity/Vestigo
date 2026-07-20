"""Guards on the *advertised* tool schemas (roadmap A13).

Tool schemas are resent with every model request, so their size is a
per-request tax and a silent regression is expensive. Nothing tested here
existed before A13, which is why the schemas were free to grow to ~69k chars.

The load-bearing tests are:
- `test_slimming_preserves_callability` — the advertised schema is slimmed but
  arguments are still validated against the full pydantic model. An early
  version of `slim_schema` stripped the *parameter named* `title` from
  propose_finding/propose_chart because it shared a name with the JSON-schema
  keyword; this test is what catches that class of bug.
- `test_total_schema_budget` — makes the size win durable.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.client import Client as FastMCPClient

from vestigo.agent.schema_slim import (
    SHARED_SPEC_NAMES,
    slim_schema,
    slim_tool_schema,
    spec_reference_block,
)
from vestigo.agent.tools import (
    SPEC_REFERENCE,
    AgentScope,
    ChartSpec,
    FilterSpec,
    build_tool_server,
)
from vestigo.db.postgres import User

# Ceiling for the serialized tool list. Measured 69,382 chars before A13 and
# ~33,000 after; the headroom absorbs a few new tools before this fires.
# If a change pushes past it, that is a real context regression — re-measure
# and update docs/AGENT.md rather than just raising the number.
SCHEMA_BUDGET_CHARS = 40_000


def _scope(case_id: str = "c1", timeline_id: str = "t1") -> AgentScope:
    return AgentScope(
        case_id=case_id,
        timeline_id=timeline_id,
        user=User(id="u1", username="tester", is_admin=True, is_active=True),
        source_ids=[],
        field_mappings=None,
        source_offsets=None,
        conversation_id="conv1",
    )


async def _advertised(server) -> list[Any]:
    async with FastMCPClient(server) as client:
        return await client.list_tools()


_NAME_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def _walk(node: Any):
    """Yield every *schema* dict in a JSON-schema tree.

    Skips the name-map levels themselves (`properties`, `$defs`, ...) — those
    are dicts of names, not schemas, so their keys are user data.
    """
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in _NAME_MAPS and isinstance(value, dict):
                for sub in value.values():
                    yield from _walk(sub)
            else:
                yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# --- the pure transform ---------------------------------------------------


def test_slim_schema_is_pure():
    original = {"title": "X", "properties": {"a": {"title": "A", "type": "string"}}}
    snapshot = json.dumps(original)
    slim_schema(original)
    assert json.dumps(original) == snapshot


def test_slim_schema_keeps_property_names_that_collide_with_keywords():
    """A parameter really can be called `title` (propose_finding has one)."""
    schema = {
        "title": "fooArguments",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "default": {"title": "Default", "type": "string"},
        },
        "required": ["title", "default"],
    }
    out = slim_schema(schema)
    assert set(out["properties"]) == {"title", "default"}
    assert "title" not in out  # the keyword went
    assert out["properties"]["title"] == {"type": "string"}  # the parameter stayed


def test_slim_schema_collapses_optional_arms_and_null_defaults():
    out = slim_schema(
        {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": "keep me",
        }
    )
    assert out == {"type": "string", "description": "keep me"}


def test_slim_schema_preserves_meaningful_keys():
    out = slim_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"mode": {"enum": ["a", "b"], "type": "string", "default": "a"}},
            "required": ["mode"],
        }
    )
    assert out["additionalProperties"] is False
    assert out["required"] == ["mode"]
    assert out["properties"]["mode"]["enum"] == ["a", "b"]
    assert out["properties"]["mode"]["default"] == "a"  # non-null default survives


def test_slim_schema_keeps_the_null_arm_on_a_required_field():
    """Dropping it is only sound because the field is optional. On a required
    field the arm *is* the statement that an explicit null is accepted, and
    removing it would advertise a narrower contract than pydantic validates —
    which a provider enforcing the schema client-side would act on."""
    out = slim_schema(
        {
            "type": "object",
            "properties": {
                "must": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Must"},
                "may": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["must"],
        }
    )
    assert out["properties"]["must"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "title" not in out["properties"]["must"]  # still slimmed otherwise
    assert out["properties"]["may"] == {"type": "string"}


def test_slim_schema_leaves_multi_arm_unions_alone():
    node = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    assert slim_schema(node)["anyOf"] == node["anyOf"]


def test_strip_def_descriptions_targets_only_shared_specs():
    schema = slim_tool_schema(
        {
            "properties": {"filters": {"$ref": "#/$defs/FilterSpec"}},
            "$defs": {
                "FilterSpec": {
                    "description": "the model docstring",
                    "properties": {"q": {"type": "string", "description": "drop me"}},
                },
                "Other": {"properties": {"z": {"type": "string", "description": "keep me"}}},
            },
        }
    )
    filter_def = schema["$defs"]["FilterSpec"]
    assert "description" not in filter_def["properties"]["q"]
    assert filter_def["description"] == "the model docstring"  # docstring stays
    assert schema["$defs"]["Other"]["properties"]["z"]["description"] == "keep me"


# --- the advertised schemas -----------------------------------------------


async def test_no_title_keyword_or_null_arm_survives(store):
    await store.init_schema()
    for tool in await _advertised(build_tool_server(_scope())):
        for node in _walk(tool.inputSchema):
            # `title` may only appear as a property *name* (which `_walk`
            # never descends into as a schema), never as a keyword.
            assert "title" not in node, f"{tool.name} still advertises a title keyword"
            any_of = node.get("anyOf")
            if isinstance(any_of, list) and len(any_of) == 2:
                assert {"type": "null"} not in any_of, f"{tool.name} keeps a null arm"


async def test_required_fields_all_have_properties(store):
    """The regression that the first slim_schema introduced: `required`
    naming a property that the transform had deleted."""
    await store.init_schema()
    for tool in await _advertised(build_tool_server(_scope())):
        for node in _walk(tool.inputSchema):
            required = node.get("required")
            if isinstance(required, list) and isinstance(node.get("properties"), dict):
                missing = set(required) - set(node["properties"])
                assert not missing, f"{tool.name}: required without property: {missing}"


async def test_shared_specs_carry_no_per_field_prose(store):
    await store.init_schema()
    for tool in await _advertised(build_tool_server(_scope())):
        for name in SHARED_SPEC_NAMES:
            body = (tool.inputSchema.get("$defs") or {}).get(name)
            if body is None:
                continue
            for field, prop in (body.get("properties") or {}).items():
                assert "description" not in prop, f"{tool.name}/{name}.{field} kept its prose"


async def test_slimming_preserves_callability(store):
    """Slimming touches `Tool.parameters` (what is advertised), never
    `fn_metadata` (what is validated) — so every tool still accepts its real
    arguments, including a FilterSpec passed as a nested object."""
    await store.init_schema()
    server = build_tool_server(_scope())
    async with FastMCPClient(server) as client:
        listed = await client.list_tools()
        result = await client.call_tool(
            "field_terms",
            {"field": "artifact", "filters": {"q": "x", "filters": {"host": ["a"]}}},
        )
        assert result is not None
        # A parameter sharing a name with a schema keyword is still advertised.
        by_name = {t.name: t for t in listed}
        assert "title" in by_name["propose_finding"].inputSchema["properties"]


async def test_total_schema_budget(store):
    await store.init_schema()
    tools = await _advertised(build_tool_server(_scope()))
    total = sum(
        len(json.dumps({"name": t.name, "description": t.description, "schema": t.inputSchema}))
        for t in tools
    )
    assert total < SCHEMA_BUDGET_CHARS, (
        f"tool schemas grew to {total} chars (budget {SCHEMA_BUDGET_CHARS}); "
        "see roadmap A13 / docs/AGENT.md before raising this"
    )


async def test_tool_manager_accessor_still_exists(store):
    """`_apply_schema_slimming` reaches into FastMCP internals; fail loudly
    here rather than silently stopping to slim after an SDK bump."""
    await store.init_schema()
    server = build_tool_server(_scope())
    tools = server._tool_manager.list_tools()
    assert tools and all(hasattr(t, "parameters") for t in tools)


# --- the relocated prose --------------------------------------------------


def test_spec_reference_documents_every_filter_field():
    """Drift guard: a new FilterSpec field must not silently lose its prose,
    which the schemas no longer carry."""
    for field in FilterSpec.model_fields:
        assert f"- {field} (" in SPEC_REFERENCE, f"{field} missing from the spec reference"
    for field in ChartSpec.model_fields:
        assert f"- {field} (" in SPEC_REFERENCE


def test_spec_reference_carries_the_prose_verbatim():
    block = spec_reference_block((FilterSpec,))
    for name, field in FilterSpec.model_fields.items():
        if field.description:
            assert " ".join(field.description.split()) in block, name


def test_spec_reference_renders_readable_types():
    assert "- q (string, optional)" in SPEC_REFERENCE
    assert "- artifacts (string[], optional)" in SPEC_REFERENCE
    assert "- filters ({string: string[]}, optional)" in SPEC_REFERENCE
    assert "- start (datetime, optional)" in SPEC_REFERENCE
    assert "- filters (FilterSpec, optional)" in SPEC_REFERENCE  # ChartSpec's ref


def test_system_prompt_includes_the_reference():
    from vestigo.agent.runtime import RESULT_FORMAT_NOTE, SYSTEM_PROMPT

    assert SPEC_REFERENCE in SYSTEM_PROMPT
    assert RESULT_FORMAT_NOTE in SYSTEM_PROMPT


def test_spec_reference_renders_enums_as_json_literals():
    """The block sits next to JSON schemas; a Python repr ('count') is not
    something the model can copy into a tool call."""
    assert '- metric ("count" | ' in SPEC_REFERENCE
    assert "- metric ('count' | " not in SPEC_REFERENCE


async def test_external_mcp_instructions_carry_the_relocated_guidance(store):
    """The slimming applies to the external /mcp surface too, and `instructions`
    is the only channel that can tell those clients how to read a prose-free
    $defs or a columnar result. Relocating for the in-app agent alone would
    leave external clients with strictly less guidance than before A13."""
    from vestigo.agent.tools import RESULT_FORMAT_NOTE

    await store.init_schema()
    instructions = build_tool_server(_scope()).instructions or ""
    assert SPEC_REFERENCE in instructions
    assert RESULT_FORMAT_NOTE in instructions
