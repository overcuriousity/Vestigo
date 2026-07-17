"""pySigma backend compiling Sigma rules to ClickHouse boolean expressions.

Produces one SQL condition per rule, ready to drop into
``WHERE case_id = {...} AND source_id IN (...) AND (<condition>)``. Field
names resolve through a three-step chain (see :class:`FieldResolver`):
ruleset fieldmap → timeline canonical ``field_mappings`` → raw
``attributes['<field>']`` fallback — so Milestone 7's column renames are
absorbed in the resolver layer, never in rule text or runner code.

Security note — everything user-controlled that reaches SQL text goes
through exactly two audited quoting functions: :func:`quote_ch_string`
(ClickHouse single-quoted string literals; backslash and quote escaped) and
:func:`_like_escape` (LIKE-pattern metacharacters). pySigma's text-backend
model embeds values as literals; there is no bind-parameter path here, so
these two functions are the injection boundary and carry dedicated
adversarial tests (``tests/test_sigma_backend.py``).

Matching semantics follow the Sigma spec: plain string values match
case-insensitively (``ILIKE``) with ``*``/``?`` wildcards; ``|cased`` uses
``LIKE``; ``|re`` uses ClickHouse ``match()`` (RE2 — the same engine the
API's regex guard assumes); ``|cidr`` guards ``isIPAddressInRange`` behind
``isIPv4String``/``isIPv6String`` because ClickHouse throws on non-IP input
(verified against 24.10). Numbers compare as exact strings for equality and
through ``toFloat64OrNull`` for lt/gt comparisons. ``null`` and missing
fields are both the empty string — ClickHouse Map lookups return ``''`` for
absent keys, matching the coalesce convention in ``db/field_mappings.py``.
Keyword (field-less) values search the lowercased ``search_blob`` column.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from sigma.collection import SigmaCollection
from sigma.conditions import ConditionFieldEqualsValueExpression, ConditionValueExpression
from sigma.conversion.base import TextQueryBackend
from sigma.conversion.state import ConversionState
from sigma.exceptions import SigmaError
from sigma.rule import SigmaRule as PySigmaRule
from sigma.types import (
    SigmaCompareExpression,
    SigmaRegularExpressionFlag,
    SigmaString,
    SpecialChars,
)

from vestigo.db._columns import TOP_LEVEL_NON_STRING_COLUMNS, resolve_column_token
from vestigo.db.field_mappings import FieldMappings, resolve_mapping


def quote_ch_string(value: str) -> str:
    """Return *value* as a safe ClickHouse single-quoted string literal.

    The injection boundary for every literal this backend emits. ClickHouse
    string literals use C-style backslash escapes; escaping the backslash
    first and the quote second is load-bearing.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _like_escape(literal: str) -> str:
    """Escape LIKE metacharacters in a literal fragment (pattern level).

    Operates at the LIKE-pattern level: the result still passes through
    :func:`quote_ch_string`, which doubles the backslashes for the SQL text
    layer. Backslash first, again load-bearing.
    """
    return literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(value: SigmaString) -> str:
    """Build a LIKE pattern from a SigmaString's literal/wildcard parts."""
    parts: list[str] = []
    for part in value.s:
        if part == SpecialChars.WILDCARD_MULTI:
            parts.append("%")
        elif part == SpecialChars.WILDCARD_SINGLE:
            parts.append("_")
        else:
            parts.append(_like_escape(str(part)))
    return "".join(parts)


_COMPARE_OPS = {
    SigmaCompareExpression.CompareOperators.LT: "<",
    SigmaCompareExpression.CompareOperators.LTE: "<=",
    SigmaCompareExpression.CompareOperators.GT: ">",
    SigmaCompareExpression.CompareOperators.GTE: ">=",
}

_RE_FLAG_CHARS = {
    SigmaRegularExpressionFlag.IGNORECASE: "i",
    SigmaRegularExpressionFlag.MULTILINE: "m",
    SigmaRegularExpressionFlag.DOTALL: "s",
}


@dataclass
class FieldResolver:
    """Resolves a Sigma field name to a ClickHouse expression (inline literals).

    Chain: ruleset ``fieldmap`` token → timeline canonical mapping (inline
    coalesce over raw attribute keys) → top-level ``events`` column →
    ``attributes['<name>']`` fallback. Fields that land on the raw fallback
    are recorded in ``fallback_fields`` so the run record and UI can flag
    "matched on a raw key that no mapping vouches for".
    """

    field_mappings: FieldMappings | None = None
    fieldmap: dict[str, str] = dataclass_field(default_factory=dict)
    fallback_fields: set[str] = dataclass_field(default_factory=set)

    def resolve(self, sigma_field: str) -> str:
        token = self.fieldmap.get(sigma_field, sigma_field)
        via_fieldmap = sigma_field in self.fieldmap

        raw_keys = resolve_mapping(token, self.field_mappings)
        if raw_keys:
            inner = ", ".join(f"nullif(attributes[{quote_ch_string(k)}], '')" for k in raw_keys)
            return f"coalesce({inner}, '')"

        column, attr_key = resolve_column_token(token)
        if column is not None:
            if column in TOP_LEVEL_NON_STRING_COLUMNS:
                return f"toString({column})"
            return column
        if not via_fieldmap:
            self.fallback_fields.add(sigma_field)
        return f"attributes[{quote_ch_string(attr_key or token)}]"


class VestigoClickHouseBackend(TextQueryBackend):
    """Sigma → ClickHouse boolean-expression backend.

    One instance per run scope: it carries the timeline's field mappings and
    the active ruleset fieldmap through :class:`FieldResolver` and collects
    the fallback-field set across compiled rules.
    """

    name = "Vestigo ClickHouse backend"
    formats = {"default": "ClickHouse WHERE-clause boolean expression"}
    requires_pipeline = False

    or_token = "OR"
    and_token = "AND"
    not_token = "NOT"
    group_expression = "({expr})"
    eq_token = " = "  # unused (all eq paths overridden) but must be non-None

    def __init__(self, resolver: FieldResolver | None = None):
        super().__init__()
        self.resolver = resolver or FieldResolver()

    # -- field resolution ------------------------------------------------

    def escape_and_quote_field(self, field_name: str) -> str:
        return self.resolver.resolve(field_name)

    # -- field/value conditions -----------------------------------------

    def convert_condition_field_eq_val_str(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        field = self.escape_and_quote_field(cond.field)
        return f"{field} ILIKE {quote_ch_string(_like_pattern(cond.value))}"

    def convert_condition_field_eq_val_str_case_sensitive(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        field = self.escape_and_quote_field(cond.field)
        return f"{field} LIKE {quote_ch_string(_like_pattern(cond.value))}"

    def convert_condition_field_eq_val_num(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        field = self.escape_and_quote_field(cond.field)
        return f"{field} = {quote_ch_string(str(cond.value.number))}"

    def convert_condition_field_eq_val_bool(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        field = self.escape_and_quote_field(cond.field)
        literal = "true" if cond.value.boolean else "false"
        return f"lower({field}) = '{literal}'"

    def convert_condition_field_eq_val_re(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        field = self.escape_and_quote_field(cond.field)
        flags = "".join(char for flag, char in _RE_FLAG_CHARS.items() if flag in cond.value.flags)
        regex = (f"(?{flags})" if flags else "") + cond.value.regexp
        return f"match({field}, {quote_ch_string(regex)})"

    def convert_condition_field_eq_val_cidr(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        # isIPAddressInRange throws on non-IP input; the if() guard relies on
        # ClickHouse's short-circuit evaluation (default since 21.x).
        field = self.escape_and_quote_field(cond.field)
        cidr = quote_ch_string(cond.value.cidr)
        return (
            f"if(isIPv4String({field}) OR isIPv6String({field}), "
            f"isIPAddressInRange({field}, {cidr}), 0)"
        )

    def convert_condition_field_compare_op_val(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        field = self.escape_and_quote_field(cond.field)
        op = _COMPARE_OPS[cond.value.op]
        number = cond.value.number.number
        if not isinstance(number, int | float):
            raise SigmaError(f"non-numeric comparison value: {number!r}")
        return f"toFloat64OrNull({field}) {op} {number}"

    def convert_condition_field_eq_val_null(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        # Absent Map keys read as '' — null and missing are the same thing here.
        return f"{self.escape_and_quote_field(cond.field)} = ''"

    def convert_condition_field_exists(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        return f"{self.escape_and_quote_field(cond.field)} != ''"

    def convert_condition_field_not_exists(
        self, cond: ConditionFieldEqualsValueExpression, state: ConversionState
    ) -> str:
        return f"{self.escape_and_quote_field(cond.field)} = ''"

    # -- keyword (field-less) conditions --------------------------------
    # search_blob is a lowercased materialized concat of the event's text
    # columns; patterns are lowercased at compile time to match.

    def convert_condition_val_str(
        self, cond: ConditionValueExpression, state: ConversionState
    ) -> str:
        pattern = _like_pattern(cond.value).lower()
        if not pattern.startswith("%"):
            pattern = "%" + pattern
        if not pattern.endswith("%"):
            pattern = pattern + "%"
        return f"search_blob LIKE {quote_ch_string(pattern)}"

    def convert_condition_val_num(
        self, cond: ConditionValueExpression, state: ConversionState
    ) -> str:
        return f"search_blob LIKE {quote_ch_string(f'%{cond.value.number}%')}"

    def convert_condition_val_re(
        self, cond: ConditionValueExpression, state: ConversionState
    ) -> str:
        # The blob is lowercase, so force case-insensitive matching.
        return f"match(search_blob, {quote_ch_string('(?i)' + cond.value.regexp)})"


@dataclass
class CompiledRule:
    """The compilation outcome for one rule: SQL or a reason there is none."""

    sql: str | None
    error: str | None = None
    fallback_fields: list[str] = dataclass_field(default_factory=list)


def compile_rule(
    rule: PySigmaRule,
    field_mappings: FieldMappings | None,
    fieldmap: dict[str, str],
) -> CompiledRule:
    """Compile one parsed Sigma rule to a ClickHouse boolean expression.

    Multiple condition variants (Sigma ``condition`` lists) are OR-joined —
    the rule matches if any variant does. Unsupported constructs
    (correlations, aggregations) surface as ``error`` for the caller to
    report as ``not_applicable``.
    """
    resolver = FieldResolver(field_mappings=field_mappings, fieldmap=dict(fieldmap))
    backend = VestigoClickHouseBackend(resolver)
    try:
        queries = backend.convert(SigmaCollection([rule]))
    except SigmaError as exc:
        return CompiledRule(sql=None, error=str(exc))
    queries = [q for q in queries if isinstance(q, str) and q.strip()]
    if not queries:
        return CompiledRule(sql=None, error="rule produced no query")
    sql = queries[0] if len(queries) == 1 else "(" + ") OR (".join(queries) + ")"
    return CompiledRule(sql=sql, fallback_fields=sorted(resolver.fallback_fields))
