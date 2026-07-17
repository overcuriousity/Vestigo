"""Global Sigma ruleset loader: directory walk, fieldmaps, malformed files."""

from __future__ import annotations

from vestigo.sigma.rules import load_global_rules

VALID_RULE = """
title: Valid one
id: 5013ef44-f37f-4b1f-99e0-0dcb0e5d3ac2
level: medium
logsource: {product: test}
detection:
    sel:
        f: v
    condition: sel
"""


def test_empty_path_returns_nothing(tmp_path):
    assert load_global_rules("") == []
    assert load_global_rules(str(tmp_path / "missing")) == []


def test_walk_parses_and_hashes(tmp_path):
    (tmp_path / "a.yml").write_text(VALID_RULE)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.yaml").write_text(VALID_RULE.replace("Valid one", "Valid two"))
    (tmp_path / "notes.txt").write_text("ignored")
    rules = load_global_rules(str(tmp_path))
    assert [r.ref for r in rules] == ["a.yml", "sub/b.yaml"]
    assert all(r.origin == "global" for r in rules)
    assert all(len(r.content_hash) == 64 for r in rules)
    assert rules[0].title == "Valid one"
    assert rules[1].title == "Valid two"


def test_malformed_rule_is_reported_not_fatal(tmp_path):
    (tmp_path / "good.yml").write_text(VALID_RULE)
    (tmp_path / "bad.yml").write_text("title: [unclosed")
    rules = load_global_rules(str(tmp_path))
    by_ref = {r.ref: r for r in rules}
    assert by_ref["good.yml"].error is None
    assert by_ref["bad.yml"].error is not None
    assert by_ref["bad.yml"].parsed is None


def test_nearest_fieldmap_wins_and_is_not_a_rule(tmp_path):
    (tmp_path / "vestigo-fieldmap.yml").write_text("Root: attr:root_key\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "vestigo-fieldmap.yml").write_text("Sub: attr:sub_key\n")
    (tmp_path / "top.yml").write_text(VALID_RULE)
    (sub / "deep.yml").write_text(VALID_RULE)
    rules = load_global_rules(str(tmp_path))
    by_ref = {r.ref: r for r in rules}
    # Fieldmap files are not listed as rules.
    assert set(by_ref) == {"top.yml", "sub/deep.yml"}
    assert by_ref["top.yml"].fieldmap == {"Root": "attr:root_key"}
    # Nearest map wins; maps are not merged across levels.
    assert by_ref["sub/deep.yml"].fieldmap == {"Sub": "attr:sub_key"}


def test_malformed_fieldmap_ignored(tmp_path):
    (tmp_path / "vestigo-fieldmap.yml").write_text("- not\n- a\n- mapping\n")
    (tmp_path / "r.yml").write_text(VALID_RULE)
    rules = load_global_rules(str(tmp_path))
    assert rules[0].fieldmap == {}
