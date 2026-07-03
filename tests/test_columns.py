"""Tests for tracesignal.db._columns — shared field-token routing (F10)."""

from __future__ import annotations

from tracesignal.db._columns import TOP_LEVEL_EVENT_COLUMNS, resolve_column_token


def test_resolve_top_level_column():
    assert resolve_column_token("artifact") == ("artifact", None)


def test_resolve_is_case_and_whitespace_insensitive():
    assert resolve_column_token("  Artifact  ") == ("artifact", None)


def test_resolve_attr_prefix_always_wins_even_if_it_matches_a_column():
    """An `attr:` prefix always means "attribute", even if the stripped name
    happens to collide with a real top-level column name."""
    assert resolve_column_token("attr:artifact") == (None, "artifact")


def test_resolve_bare_non_column_token_is_attribute_key():
    assert resolve_column_token("user_agent") == (None, "user_agent")


def test_all_columns_in_allowlist_resolve_to_themselves():
    for column in TOP_LEVEL_EVENT_COLUMNS:
        assert resolve_column_token(column) == (column, None)
