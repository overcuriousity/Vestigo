"""Block-content validation and canonical snapshot hashing."""

import pytest

from vestigo.stories.schemas import VIEW_BLOCK_ROW_CAP, canonical_hash, validate_block_content


def test_markdown_content_roundtrip():
    assert validate_block_content("markdown", {"text": "# hi"}) == {"text": "# hi"}


def test_markdown_requires_text():
    with pytest.raises(ValueError):
        validate_block_content("markdown", {})


def test_view_ref_defaults_and_cap():
    out = validate_block_content("view_ref", {"view_id": "v1", "timeline_id": "t1"})
    assert out["display"]["limit"] == 200
    with pytest.raises(ValueError):
        validate_block_content(
            "view_ref",
            {"view_id": "v1", "timeline_id": "t1", "display": {"limit": VIEW_BLOCK_ROW_CAP + 1}},
        )


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        validate_block_content("gif", {"url": "nope"})


def test_event_ref_optional_caption():
    out = validate_block_content("event_ref", {"event_id": "e1", "source_id": "s1"})
    assert out["caption"] is None


def test_canonical_hash_key_order_independent():
    a = canonical_hash({"b": 1, "a": [1, 2]})
    b = canonical_hash({"a": [1, 2], "b": 1})
    assert a == b and len(a) == 64
