"""Timeline field mappings: canonical query-time field aggregation (issue #10).

A timeline may carry ``field_mappings`` metadata that merges differently-named
raw attribute keys from badly normalized sources into one canonical field::

    {"ip_address": ["src_ip", "ip_addr"], "user_name": ["user", "username"]}

Keys are canonical field names; values are ordered lists of raw ``attributes``
Map keys — order defines coalesce precedence when one event carries several of
the raw keys. Mappings are pure timeline metadata applied at query time: the
ingested events are never rewritten (forensic requirement), and the ``attr:``
token prefix keeps addressing raw keys directly, bypassing any mapping.

This module owns the three mapping concerns shared by the query layer
(`queries.py`), the statistical detectors (`anomaly_stats.py`), and the API
validation (`routers/cases.py`): building the coalesce SQL expression,
validating a mapping dict, and rewriting field inventories for discovery
endpoints.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tracesignal.db._columns import TOP_LEVEL_EVENT_COLUMNS

# Practical guard rails — a mapping is analyst-curated metadata, not bulk data.
MAX_CANONICAL_FIELDS = 64
MAX_RAW_FIELDS_PER_CANONICAL = 16

FieldMappings = dict[str, list[str]]


def mapping_coalesce_expr(
    raw_keys: list[str],
    parameters: dict[str, Any],
    param_name: str | Callable[[], str],
) -> str:
    """Return the ClickHouse expression for a canonical field.

    Each raw attribute key becomes a bound parameter; empty string counts as
    "absent" (ClickHouse Map lookups return ``''`` for missing keys), so the
    first non-empty raw value in mapping order wins::

        coalesce(nullif(attributes[{m0:String}], ''), ..., '')

    ``param_name`` follows the `_field_column_expr` contract: a callable mints
    fresh names; a string seeds ``{name}_m{i}`` suffixed names (the viz callers
    pass one caller-chosen name and never more).
    """
    parts = []
    for i, key in enumerate(raw_keys):
        name = param_name() if callable(param_name) else f"{param_name}_m{i}"
        parameters[name] = key
        parts.append(f"nullif(attributes[{{{name}:String}}], '')")
    return f"coalesce({', '.join(parts)}, '')"


def resolve_mapping(field_token: str, field_mappings: FieldMappings | None) -> list[str] | None:
    """Return the raw keys for *field_token* if it names a canonical field.

    ``attr:``-prefixed tokens always address a raw attribute key directly and
    never resolve through a mapping — the analyst's escape hatch to inspect
    one source's original field.
    """
    if not field_mappings or field_token.startswith("attr:"):
        return None
    return field_mappings.get(field_token.strip())


def validate_field_mappings(
    mappings: FieldMappings,
    available_attribute_keys: set[str],
) -> list[str]:
    """Validate a mapping dict against the timeline's actual attribute keys.

    Returns a list of human-readable problems (empty = valid). Enforced rules:

    - canonical names must be non-empty, must not collide with core event
      columns or with a raw attribute key present in the sources (that would
      silently shadow real data), and must not use the ``attr:`` prefix;
    - each raw key may appear in at most one mapping (and once per mapping);
    - every raw key must exist in at least one member source — mapping a
      nonexistent field is almost always a typo and would silently coalesce
      to nothing.
    """
    problems: list[str] = []
    if len(mappings) > MAX_CANONICAL_FIELDS:
        problems.append(f"At most {MAX_CANONICAL_FIELDS} canonical fields are supported.")

    core_lower = {c.lower() for c in TOP_LEVEL_EVENT_COLUMNS}
    seen_raw: dict[str, str] = {}
    for canonical, raw_keys in mappings.items():
        name = canonical.strip()
        if not name:
            problems.append("Canonical field names must not be empty.")
            continue
        if name.startswith("attr:"):
            problems.append(f"'{name}': canonical names must not use the 'attr:' prefix.")
        if name.lower() in core_lower:
            problems.append(f"'{name}' collides with a core event column.")
        if name in available_attribute_keys:
            problems.append(
                f"'{name}' collides with an existing raw attribute key in the "
                "timeline's sources — it would shadow that field."
            )
        if not isinstance(raw_keys, list) or not raw_keys:
            problems.append(f"'{name}': must map at least one raw field.")
            continue
        if len(raw_keys) > MAX_RAW_FIELDS_PER_CANONICAL:
            problems.append(
                f"'{name}': at most {MAX_RAW_FIELDS_PER_CANONICAL} raw fields per mapping."
            )
        for raw in raw_keys:
            if raw in seen_raw:
                problems.append(
                    f"Raw field '{raw}' appears in both '{seen_raw[raw]}' and '{name}' — "
                    "each raw field may be mapped only once."
                )
            seen_raw[raw] = name
            if raw not in available_attribute_keys:
                problems.append(
                    f"'{name}': raw field '{raw}' does not exist in any of the "
                    "timeline's sources. Check the field coverage list for the exact key."
                )
    return problems


def apply_mappings_to_attribute_keys(
    attribute_keys: list[str],
    field_mappings: FieldMappings | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Rewrite a raw attribute-key inventory for field-discovery endpoints.

    Mapped raw keys are hidden and replaced by their canonical name so pickers,
    detectors, and wizards operate on the merged field. Returns the rewritten
    (sorted) key list plus a provenance list the UI renders as
    ``ip_address ← src_ip, ip_addr`` — only for mappings whose raw keys
    actually occur in *attribute_keys*.
    """
    if not field_mappings:
        return attribute_keys, []
    present = set(attribute_keys)
    hidden: set[str] = set()
    provenance: list[dict[str, Any]] = []
    for canonical, raw_keys in field_mappings.items():
        raws_here = [r for r in raw_keys if r in present]
        if raws_here:
            hidden.update(raws_here)
            provenance.append({"name": canonical, "raw_fields": raw_keys})
    keys = sorted((present - hidden) | {p["name"] for p in provenance})
    return keys, provenance
