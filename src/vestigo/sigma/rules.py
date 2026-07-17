"""Loading and identity of Sigma rules (global directory + case uploads).

Global rules live as ``*.yml``/``*.yaml`` files under
``Settings.sigma_rules_path`` — an offline file drop (e.g. a vendored
SigmaHQ clone). They are re-read and re-hashed on every listing/run so the
run record always reflects what was actually on disk. Case-scoped uploads
live in Postgres (``db/postgres.py::SigmaRule``) and are parsed through the
same path.

A ruleset may ship an optional field-mapping file named
``vestigo-fieldmap.yml`` at the ruleset root (or next to any rule file, the
nearest one wins) mapping Sigma field names to Vestigo field tokens::

    CommandLine: attr:cmdline
    Image: process_path        # a timeline canonical field
    EventID: attr:event_id

Tokens follow the grammar of ``db/_columns.py::resolve_column_token``
(top-level column name, canonical field name, or ``attr:``-prefixed raw
attribute key).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sigma.exceptions import SigmaError
from sigma.rule import SigmaRule as PySigmaRule

logger = logging.getLogger(__name__)

FIELDMAP_FILENAME = "vestigo-fieldmap.yml"

# Uploaded rules are analyst-curated single YAML documents, not bulk data.
MAX_RULE_BYTES = 1024 * 1024


@dataclass
class LoadedRule:
    """One parsed Sigma rule ready for compilation.

    ``rule_key`` is the 32-hex identity stamped into ``Annotation.detector``
    for every hit: the rule's Sigma ``id`` UUID with dashes stripped when
    present, else the first 32 hex chars of ``content_hash``.
    """

    origin: str  # "global" | "case"
    ref: str  # relative path (global) or SigmaRule row id (case)
    rule_key: str
    title: str
    yaml_content: str
    content_hash: str
    rule_uuid: str | None = None
    level: str | None = None
    logsource: dict = field(default_factory=dict)
    parsed: PySigmaRule | None = None
    error: str | None = None
    fieldmap: dict[str, str] = field(default_factory=dict)


def content_hash(yaml_content: str) -> str:
    """SHA-256 of the exact YAML text — the rule's forensic identity."""
    return hashlib.sha256(yaml_content.encode("utf-8")).hexdigest()


def rule_key_for(rule_uuid: str | None, yaml_hash: str) -> str:
    """32-hex rule identity (fits ``Annotation.detector`` String(32))."""
    if rule_uuid:
        stripped = rule_uuid.replace("-", "").lower()
        if len(stripped) == 32 and all(c in "0123456789abcdef" for c in stripped):
            return stripped
    return yaml_hash[:32]


def parse_rule_yaml(yaml_content: str) -> tuple[PySigmaRule | None, str | None]:
    """Parse one YAML document as a Sigma rule; ``(rule, None)`` or ``(None, error)``."""
    try:
        rule = PySigmaRule.from_yaml(yaml_content)
    except (SigmaError, yaml.YAMLError) as exc:
        return None, str(exc)
    return rule, None


def load_rule_text(
    origin: str, ref: str, yaml_content: str, fieldmap: dict[str, str]
) -> LoadedRule:
    """Build a :class:`LoadedRule` from raw YAML text (shared by both origins)."""
    yaml_hash = content_hash(yaml_content)
    parsed, error = parse_rule_yaml(yaml_content)
    if parsed is None:
        return LoadedRule(
            origin=origin,
            ref=ref,
            rule_key=yaml_hash[:32],
            title=ref,
            yaml_content=yaml_content,
            content_hash=yaml_hash,
            error=error,
            fieldmap=fieldmap,
        )
    rule_uuid = str(parsed.id) if parsed.id else None
    logsource = {
        k: v
        for k, v in (
            ("product", parsed.logsource.product),
            ("category", parsed.logsource.category),
            ("service", parsed.logsource.service),
        )
        if v
    }
    return LoadedRule(
        origin=origin,
        ref=ref,
        rule_key=rule_key_for(rule_uuid, yaml_hash),
        title=parsed.title or ref,
        yaml_content=yaml_content,
        content_hash=yaml_hash,
        rule_uuid=rule_uuid,
        level=str(parsed.level.name).lower() if parsed.level else None,
        logsource=logsource,
        parsed=parsed,
        fieldmap=fieldmap,
    )


def _load_fieldmap(path: Path) -> dict[str, str]:
    """Parse one ``vestigo-fieldmap.yml``; malformed maps log and yield {}."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Ignoring malformed Sigma fieldmap %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring Sigma fieldmap %s: not a mapping", path)
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str | int | float)}


def load_global_rules(rules_path: str) -> list[LoadedRule]:
    """Walk the global ruleset directory and load every ``*.yml``/``*.yaml``.

    Malformed files become :class:`LoadedRule` entries with ``error`` set —
    reported, never fatal. Fieldmap files: the nearest ``vestigo-fieldmap.yml``
    walking up from the rule file toward the ruleset root wins; maps are not
    merged across levels.
    """
    if not rules_path:
        return []
    root = Path(rules_path).expanduser()
    if not root.is_dir():
        logger.warning("Sigma rules path %s is not a directory", root)
        return []

    fieldmap_cache: dict[Path, dict[str, str]] = {}

    def fieldmap_for(rule_file: Path) -> dict[str, str]:
        for parent in [rule_file.parent, *rule_file.parent.parents]:
            candidate = parent / FIELDMAP_FILENAME
            if candidate in fieldmap_cache:
                return fieldmap_cache[candidate]
            if candidate.is_file():
                loaded = _load_fieldmap(candidate)
                fieldmap_cache[candidate] = loaded
                return loaded
            if parent == root:
                break
        return {}

    rules: list[LoadedRule] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".yml", ".yaml"):
            continue
        if path.name == FIELDMAP_FILENAME:
            continue
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            rules.append(
                LoadedRule(
                    origin="global",
                    ref=rel,
                    rule_key=hashlib.sha256(rel.encode()).hexdigest()[:32],
                    title=rel,
                    yaml_content="",
                    content_hash="",
                    error=f"unreadable: {exc}",
                )
            )
            continue
        rules.append(load_rule_text("global", rel, text, fieldmap_for(path)))
    return rules


def load_case_rule(row_id: str, yaml_content: str, fieldmap: dict[str, str]) -> LoadedRule:
    """Load one case-scoped uploaded rule (Postgres row) as a :class:`LoadedRule`."""
    return load_rule_text("case", row_id, yaml_content, fieldmap)
