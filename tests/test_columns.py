"""Tests for vestigo.db._columns — shared field-token routing (F10)."""

from __future__ import annotations

from vestigo.db._columns import (
    TOP_LEVEL_EVENT_COLUMNS,
    decode_fixed_string_columns,
    resolve_column_token,
)


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


def test_template_id_resolves_to_tostring_template_hash():
    assert resolve_column_token("template_id") == ("toString(template_hash)", None)


def test_template_id_is_case_and_whitespace_insensitive():
    assert resolve_column_token("  Template_Id  ") == ("toString(template_hash)", None)


def test_attr_prefix_wins_over_synthetic_column_too():
    assert resolve_column_token("attr:template_id") == (None, "template_id")


# ── FixedString(64) decoding ──────────────────────────────────────────────────
#
# clickhouse-connect hands these three columns back as NUL-padded `bytes`.
# Undecoded, the export serializers stringify them via `repr`, so an exported
# `content_hash` reads `b'<hex>'` and never compares equal to the real SHA-256.

_HEX = "bc3c7de86cf364bb245f1906601a51abcd1a30048f79947dbc1df3309d8ec058"


def test_decode_strips_repr_wrapper_from_populated_hashes():
    row = {"content_hash": _HEX.encode(), "file_hash": _HEX.encode()}
    assert decode_fixed_string_columns(row) == {"content_hash": _HEX, "file_hash": _HEX}


def test_decode_maps_all_nul_padding_to_empty_string():
    # An unset embedding_config_hash is 64 NULs, not b"" — it must not read
    # back as a truthy 64-character string.
    row = {"embedding_config_hash": b"\x00" * 64}
    assert decode_fixed_string_columns(row)["embedding_config_hash"] == ""


def test_decode_is_idempotent_on_already_decoded_rows():
    row = {"content_hash": _HEX, "file_hash": "", "embedding_config_hash": ""}
    assert decode_fixed_string_columns(dict(row)) == row


def test_decode_leaves_other_columns_untouched():
    row = {"message": "hello", "content_hash": _HEX.encode()}
    assert decode_fixed_string_columns(row)["message"] == "hello"
