"""Shrink the JSON schemas advertised for agent tools (roadmap A13).

Tool schemas are resent with *every* model request, so their size is a
per-request tax rather than a one-off cost. Measured before this module
existed: 28 tools serialized to ~69k chars (~17k tokens) — half a 32k
local-model context window before the analyst typed anything.

Two transforms, both applied in :func:`vestigo.agent.tools.build_tool_server`:

``slim_schema``
    Drops keys that carry no information for a model: pydantic's generated
    ``title``, the ``{"type": "null"}`` arm of *optional* fields, and
    ``default: null``. Purely mechanical, no semantics lost. The null arm is
    kept on fields named in ``required``, where dropping it really would
    narrow the contract — see :func:`slim_schema`.

``strip_def_descriptions``
    ``FilterSpec``'s schema alone is 3.8k chars and is re-serialized into 12
    tools; ``ChartSpec`` adds 10k more. Their per-field prose is removed from
    the repeated ``$defs`` and relocated *verbatim* into the system prompt by
    :func:`spec_reference_block`, so the model still gets every word of
    guidance — once per request instead of twelve times.

Both operate on the *advertised* schema only. FastMCP validates incoming
arguments against ``Tool.fn_metadata.arg_model`` (the real pydantic model),
which these transforms never touch: we advertise slim and validate full.

Tool descriptions and top-level tool-parameter descriptions are deliberately
left alone — those are what a small model reads to *pick* a tool.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

# The shared spec models whose per-field prose moves to the system prompt.
# Order is the order they appear in the reference block.
SHARED_SPEC_NAMES: tuple[str, ...] = (
    "FilterSpec",
    "ChartSpec",
    "ChartCompareSpec",
    "ChartOptionsSpec",
)

_NULL = {"type": "null"}

# Keywords whose value maps *names* to subschemas. Their keys are user data
# (a tool really can take a parameter called "title"), so they must survive
# the title-stripping below — only the schema keyword `title` is noise.
_NAME_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def slim_schema(node: Any) -> Any:
    """Return *node* with informationless JSON-schema keys removed.

    Recursive and pure — the input is not mutated. Preserves everything a
    model or a validator could act on: ``enum``, ``format``, ``required``,
    ``additionalProperties`` (load-bearing for ``ChartOptionsSpec``'s
    ``extra="forbid"``), and every non-null ``default``.
    """
    return _slim(node, collapse_null_arm=True)


def _slim(node: Any, *, collapse_null_arm: bool) -> Any:
    """Recursive worker for :func:`slim_schema`.

    ``collapse_null_arm`` is False for a property listed in its parent's
    ``required``: for those, ``anyOf[T, null]`` is the whole statement that an
    explicit null is admissible, and dropping the arm would advertise a
    narrower contract than the pydantic model actually validates. On an
    optional field the arm says nothing the absence from ``required`` does not
    already say, so it goes. (Nothing required is nullable today; this keeps
    it that way by construction rather than by luck, and matters for providers
    that enforce the advertised schema client-side before the call is made.)
    """
    if isinstance(node, list):
        return [_slim(item, collapse_null_arm=True) for item in node]
    if not isinstance(node, dict):
        return node

    required = node.get("required")
    required_names = set(required) if isinstance(required, list) else set()

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _NAME_MAPS and isinstance(value, dict):
            out[key] = {
                name: _slim(
                    sub,
                    collapse_null_arm=not (key == "properties" and name in required_names),
                )
                for name, sub in value.items()
            }
        elif key != "title":
            out[key] = _slim(value, collapse_null_arm=True)

    # `X | None` renders as anyOf[T, null].
    any_of = out.get("anyOf")
    if collapse_null_arm and isinstance(any_of, list) and len(any_of) == 2 and _NULL in any_of:
        other = next(arm for arm in any_of if arm != _NULL)
        if isinstance(other, dict):
            del out["anyOf"]
            # Siblings on the survivor (description, default) win over the
            # arm's own keys — they describe the field, not the type.
            out = {**other, **out}

    if "default" in out and out["default"] is None:
        del out["default"]

    return out


def strip_def_descriptions(schema: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Remove per-field prose from the named ``$defs`` entries of *schema*.

    Only descriptions of a def's *properties* are dropped; the def's own
    description (the model's docstring) stays, since it is paid once per tool
    and says what the object is. Non-matching defs are untouched.
    """
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    out = dict(schema)
    out["$defs"] = {
        name: (_drop_property_descriptions(body) if name in names else body)
        for name, body in defs.items()
    }
    return out


def _drop_property_descriptions(body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    properties = body.get("properties")
    if not isinstance(properties, dict):
        return body

    out = dict(body)
    out["properties"] = {
        name: (
            {k: v for k, v in prop.items() if k != "description"}
            if isinstance(prop, dict)
            else prop
        )
        for name, prop in properties.items()
    }
    return out


def slim_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply both transforms to one tool's parameter schema."""
    return strip_def_descriptions(slim_schema(schema), SHARED_SPEC_NAMES)


def _type_label(prop: dict[str, Any]) -> str:
    """A short human/model-readable type for one property schema."""
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]

    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        arms = [_type_label(arm) for arm in any_of if arm != _NULL]
        label = " | ".join(dict.fromkeys(arms)) or "any"
        return label

    if "enum" in prop:
        # JSON literals, not Python reprs — the model reads this next to JSON
        # schemas and has to be able to copy a value straight out of it.
        return " | ".join(json.dumps(value) for value in prop["enum"])

    kind = prop.get("type")
    if kind == "array":
        return f"{_type_label(prop.get('items', {}))}[]"
    if kind == "object":
        extra = prop.get("additionalProperties")
        if isinstance(extra, dict):
            return f"{{string: {_type_label(extra)}}}"
        return "object"
    if kind == "string" and prop.get("format") == "date-time":
        return "datetime"
    return str(kind or "any")


def _render_model(model: type[BaseModel]) -> list[str]:
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    # A nested model's own properties can land in $defs rather than at the top
    # level. Resolve `properties` and `required` from the *same* body — reading
    # them from different levels would flag every field as optional.
    body = schema if "properties" in schema else schema.get("$defs", {}).get(model.__name__, {})
    properties = body.get("properties") or {}

    lines = [f"### {model.__name__}"]
    doc = (model.__doc__ or "").strip().split("\n\n")[0]
    if doc:
        lines.append(" ".join(doc.split()))
    required = set(body.get("required", []))
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        description = " ".join(str(prop.get("description", "")).split())
        flag = "" if name in required else ", optional"
        suffix = f" — {description}" if description else ""
        lines.append(f"- {name} ({_type_label(prop)}{flag}){suffix}")
    return lines


def spec_reference_block(models: tuple[type[BaseModel], ...]) -> str:
    """Render the prose that ``strip_def_descriptions`` removed.

    Generated from the models' own ``Field(description=...)`` values rather
    than hand-copied, so a new field on ``FilterSpec`` documents itself and
    the block can never silently drift from the schemas.
    """
    lines = [
        "## Filter and chart spec reference",
        "",
        "Tool parameters of these types share the definitions below. Their schemas are",
        "advertised without per-field prose to keep the tool list small, so this is the",
        "authoritative description of every field. Omit any field you do not need.",
        "",
    ]
    for model in models:
        lines.extend(_render_model(model))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
