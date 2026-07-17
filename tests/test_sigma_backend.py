"""Sigma → ClickHouse compiler: SQL shape, semantics, and escaping.

The escaping cases are the security tests for the injection boundary
(`quote_ch_string` / `_like_escape` in ``vestigo.sigma.backend``): every
user-controlled value must stay inside its string literal no matter what
quotes, backslashes, or LIKE metacharacters it carries.
"""

from __future__ import annotations

import pytest

from vestigo.sigma.backend import compile_rule, quote_ch_string
from vestigo.sigma.rules import load_rule_text, parse_rule_yaml, rule_key_for


def _compile(yaml_text: str, field_mappings=None, fieldmap=None):
    rule, error = parse_rule_yaml(yaml_text)
    assert rule is not None, error
    return compile_rule(rule, field_mappings, fieldmap or {})


def _rule(detection_body: str) -> str:
    return f"""
title: t
logsource: {{product: test}}
detection:
{detection_body}
"""


# ---------------------------------------------------------------- quoting


def test_quote_ch_string_plain():
    assert quote_ch_string("abc") == "'abc'"


def test_quote_ch_string_escapes_quote_and_backslash():
    assert quote_ch_string("a'b") == "'a\\'b'"
    assert quote_ch_string("a\\b") == "'a\\\\b'"
    # Backslash-then-quote must not collapse into an escaped quote.
    assert quote_ch_string("a\\'b") == "'a\\\\\\'b'"


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE events; --",
        "\\'; SELECT 1; --",
        "' OR '1'='1",
        "\\\\' UNION SELECT",
        "{p:String}",
    ],
)
def test_hostile_values_stay_inside_the_literal(hostile):
    """A hostile rule value must never terminate its string literal.

    The rule YAML is built with ``yaml.safe_dump`` so the hostile value
    reaches the compiler byte-for-byte. The emitted SQL must consist of the
    field expression, one ILIKE, and one string literal that runs to the end
    of the statement — an unescaped quote terminating the literal early
    would leave trailing SQL after the final delimiter.
    """
    import yaml as _yaml

    doc = _yaml.safe_dump(
        {
            "title": "t",
            "logsource": {"product": "test"},
            "detection": {"sel": {"f": hostile}, "condition": "sel"},
        }
    )
    sql = _compile(doc).sql
    assert sql is not None
    assert sql.startswith("attributes['f'] ILIKE '")
    assert sql.count("ILIKE") == 1
    assert sql.endswith("'")
    # Every quote inside the literal body must be backslash-escaped (odd run
    # of preceding backslashes) — i.e. the literal only ends at the last char.
    body = sql[len("attributes['f'] ILIKE '") : -1]
    i = 0
    while i < len(body):
        if body[i] == "'":
            back = 0
            j = i - 1
            while j >= 0 and body[j] == "\\":
                back += 1
                j -= 1
            assert back % 2 == 1, f"unescaped quote inside literal at {i}: {body!r}"
        i += 1


# ---------------------------------------------------------------- matching


def test_contains_endswith_startswith_wildcards():
    sql = _compile(
        _rule(
            "    sel:\n"
            "        a|contains: mid\n"
            "        b|startswith: pre\n"
            "        c|endswith: post\n"
            "    condition: sel"
        )
    ).sql
    assert "attributes['a'] ILIKE '%mid%'" in sql
    assert "attributes['b'] ILIKE 'pre%'" in sql
    assert "attributes['c'] ILIKE '%post'" in sql


def test_plain_eq_is_case_insensitive_exact():
    sql = _compile(_rule("    sel:\n        f: Value\n    condition: sel")).sql
    assert sql == "attributes['f'] ILIKE 'Value'"


def test_cased_modifier_uses_like():
    sql = _compile(_rule("    sel:\n        f|cased: Value\n    condition: sel")).sql
    assert sql == "attributes['f'] LIKE 'Value'"


def test_wildcards_convert_and_literals_escape():
    sql = _compile(_rule('    sel:\n        f: "a*b?c"\n    condition: sel')).sql
    assert "ILIKE 'a%b_c'" in sql
    sql = _compile(_rule('    sel:\n        f|contains: "50%"\n    condition: sel')).sql
    # Literal % must be LIKE-escaped (\%), doubled for the SQL text layer.
    assert "ILIKE '%50\\\\%%'" in sql
    sql = _compile(_rule('    sel:\n        f|contains: "a_b"\n    condition: sel')).sql
    assert "ILIKE '%a\\\\_b%'" in sql


def test_backslash_value_double_escaped():
    # One data backslash → LIKE-level \\ → SQL-text-level \\\\.
    sql = _compile(_rule('    sel:\n        f|endswith: "\\\\cmd.exe"\n    condition: sel')).sql
    assert "ILIKE '%\\\\\\\\cmd.exe'" in sql


def test_number_eq_and_compare():
    sql = _compile(
        _rule("    sel:\n        id: 4624\n        size|gt: 100\n    condition: sel")
    ).sql
    assert "attributes['id'] = '4624'" in sql
    assert "toFloat64OrNull(attributes['size']) > 100" in sql


def test_null_and_exists():
    sql = _compile(
        _rule("    sel:\n        gone: null\n        there|exists: true\n    condition: sel")
    ).sql
    assert "attributes['gone'] = ''" in sql
    assert "attributes['there'] != ''" in sql


def test_regex_flags_and_quoting():
    sql = _compile(_rule("    sel:\n        f|re|i: 'c:\\\\win.*'\n    condition: sel")).sql
    assert sql.startswith("match(attributes['f'], '(?i)")
    assert "\\\\\\\\win" in sql  # data \\ doubled for the SQL text layer


def test_cidr_is_guarded():
    sql = _compile(_rule("    sel:\n        ip|cidr: 10.0.0.0/8\n    condition: sel")).sql
    assert sql == (
        "if(isIPv4String(attributes['ip']) OR isIPv6String(attributes['ip']), "
        "isIPAddressInRange(attributes['ip'], '10.0.0.0/8'), 0)"
    )


def test_keywords_search_blob_lowercased():
    sql = _compile(_rule('    kw:\n        - "BadWord*"\n    condition: kw')).sql
    assert sql == "search_blob LIKE '%badword%'"


def test_boolean_value():
    sql = _compile(_rule("    sel:\n        flag: true\n    condition: sel")).sql
    assert sql == "lower(attributes['flag']) = 'true'"


def test_value_list_or_and_not():
    sql = _compile(
        _rule(
            "    sel:\n"
            "        f|contains:\n"
            "            - one\n"
            "            - two\n"
            "    flt:\n"
            "        g: skip\n"
            "    condition: sel and not flt"
        )
    ).sql
    assert "(attributes['f'] ILIKE '%one%' OR attributes['f'] ILIKE '%two%')" in sql
    assert "NOT attributes['g'] ILIKE 'skip'" in sql


# ------------------------------------------------------- field resolution


def test_canonical_mapping_coalesce():
    sql = _compile(
        _rule("    sel:\n        ip_address: 1.2.3.4\n    condition: sel"),
        field_mappings={"ip_address": ["src_ip", "ip_addr"]},
    ).sql
    assert (
        "coalesce(nullif(attributes['src_ip'], ''), nullif(attributes['ip_addr'], ''), '')" in sql
    )


def test_ruleset_fieldmap_beats_raw_and_feeds_canonical():
    compiled = _compile(
        _rule("    sel:\n        CommandLine|contains: x\n    condition: sel"),
        field_mappings={"cmd": ["cmdline"]},
        fieldmap={"CommandLine": "cmd"},
    )
    assert "coalesce(nullif(attributes['cmdline'], ''), '')" in compiled.sql
    assert compiled.fallback_fields == []


def test_fieldmap_attr_token_bypasses_mappings():
    compiled = _compile(
        _rule("    sel:\n        Image: x\n    condition: sel"),
        fieldmap={"Image": "attr:process_path"},
    )
    assert "attributes['process_path'] ILIKE 'x'" in compiled.sql
    assert compiled.fallback_fields == []


def test_top_level_column_and_timestamp_cast():
    sql = _compile(
        _rule(
            "    sel:\n        message|contains: fail\n        timestamp|contains: '2026'\n    condition: sel"
        )
    ).sql
    assert "message ILIKE '%fail%'" in sql
    assert "toString(timestamp) ILIKE '%2026%'" in sql


def test_unresolved_field_recorded_as_fallback():
    compiled = _compile(_rule("    sel:\n        NoSuchField: x\n    condition: sel"))
    assert compiled.fallback_fields == ["NoSuchField"]
    assert "attributes['NoSuchField']" in compiled.sql


# ------------------------------------------------------------- rule keys


def test_rule_key_prefers_sigma_uuid():
    assert (
        rule_key_for("5013ef44-f37f-4b1f-99e0-0dcb0e5d3ac2", "ab" * 32)
        == "5013ef44f37f4b1f99e00dcb0e5d3ac2"
    )


def test_rule_key_falls_back_to_content_hash():
    assert rule_key_for(None, "ab" * 32) == "ab" * 16
    assert rule_key_for("not-a-uuid", "cd" * 32) == "cd" * 16


def test_loaded_rule_metadata():
    r = load_rule_text(
        "global",
        "x.yml",
        """
title: Meta
id: 5013ef44-f37f-4b1f-99e0-0dcb0e5d3ac2
level: high
logsource: {product: windows, category: process_creation}
detection:
    sel:
        f: v
    condition: sel
""",
        {},
    )
    assert r.error is None
    assert r.rule_key == "5013ef44f37f4b1f99e00dcb0e5d3ac2"
    assert r.level == "high"
    assert r.logsource == {"product": "windows", "category": "process_creation"}


def test_malformed_rule_reports_error_not_raise():
    r = load_rule_text("global", "bad.yml", "title: [unclosed", {})
    assert r.error is not None
    assert r.parsed is None
    assert len(r.rule_key) == 32
