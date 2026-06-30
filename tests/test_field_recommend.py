"""Tests for the embedding-wizard field recommender (heuristics + pairing)."""

from __future__ import annotations

import numpy as np

from tracevector.db.field_recommend import (
    classify_field,
    recommend_fields,
)

# ---------------------------------------------------------------------------
# Stage 1: value heuristics
# ---------------------------------------------------------------------------


def test_message_always_recommended():
    v = classify_field("message", [], always_text=True)
    assert v.recommended and v.kind == "text"


def test_free_text_is_recommended():
    v = classify_field(
        "attr:user_agent",
        [
            "Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537",
            "curl/7.88.1 release build",
            "python-requests/2.31 automated client",
        ],
    )
    assert v.recommended and v.kind == "text"


def test_pure_numeric_field_rejected():
    v = classify_field("attr:pid", [str(p) for p in (1, 2, 4, 8, 4242, 9001, 17)])
    assert not v.recommended and v.kind == "numeric"


def test_hash_field_rejected():
    v = classify_field(
        "attr:sha256",
        ["a" * 64, "b" * 64, "deadbeef" * 8, "c0ffee00" * 8],
    )
    assert not v.recommended and v.kind == "hash"


def test_guid_field_rejected():
    v = classify_field(
        "attr:guid",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "{6ba7b810-9dad-11d1-80b4-00c04fd430c8}",
            "00000000-0000-0000-0000-000000000001",
        ],
    )
    assert not v.recommended and v.kind == "guid"


def test_constant_field_rejected():
    v = classify_field("attr:level", ["INFO", "INFO", "INFO", "INFO"])
    assert not v.recommended and v.kind == "constant"


def test_high_cardinality_identifier_rejected():
    v = classify_field("attr:session", [f"sess{i:08d}x" for i in range(50)])
    assert not v.recommended and v.kind == "id"


def test_empty_field_rejected():
    v = classify_field("attr:blank", ["", None, "   "])
    assert not v.recommended and v.kind == "empty"


# ---------------------------------------------------------------------------
# Stage 2: embedding-based pairing
# ---------------------------------------------------------------------------


def _fake_encode_factory():
    """Deterministic fake encoder: maps a few keywords to fixed unit directions.

    Values mentioning 'ip' embed toward +x, 'login'/'logon' toward +y, and
    anything else toward +z.  Field centroids therefore cluster by topic.
    """

    def encode(texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            low = t.lower()
            if "ip" in low or "addr" in low:
                vec = [1.0, 0.0, 0.0]
            elif "login" in low or "logon" in low:
                vec = [0.0, 1.0, 0.0]
            else:
                vec = [0.0, 0.0, 1.0]
            out.append(vec)
        return out

    return encode


def test_related_fields_grouped_into_pair():
    samples = {
        "message": ["user login succeeded", "logon from console"],
        "attr:src_ip": ["ip 10.0.0.1", "addr 10.0.0.2"],
        "attr:dst_ip": ["ip 192.168.0.5", "addr 192.168.0.9"],
        "attr:event": ["login event", "logon attempt"],
    }
    rec = recommend_fields(samples, encode=_fake_encode_factory(), sim_threshold=0.9)

    assert set(rec.recommended) >= {"message", "attr:src_ip", "attr:dst_ip"}
    # The two IP fields embed to the same direction → grouped together.
    ip_group = next((g for g in rec.related_groups if "attr:src_ip" in g), None)
    assert ip_group is not None
    assert "attr:dst_ip" in ip_group
    # message + event both point in the login direction → grouped.
    login_group = next((g for g in rec.related_groups if "message" in g), None)
    assert login_group is not None and "attr:event" in login_group


def test_no_groups_without_encoder():
    samples = {
        "message": ["user login succeeded"],
        "attr:src_ip": ["10.0.0.1"],
    }
    rec = recommend_fields(samples, encode=None)
    assert rec.related_groups == []
    assert "message" in rec.recommended


def test_unrelated_fields_not_grouped():
    samples = {
        "message": ["random note one", "random note two"],
        "attr:src_ip": ["ip 10.0.0.1", "addr 10.0.0.2"],
    }
    rec = recommend_fields(samples, encode=_fake_encode_factory(), sim_threshold=0.9)
    # message → +z, src_ip → +x: orthogonal, so no group.
    assert rec.related_groups == []


def test_recommend_returns_verdicts_for_all_fields():
    samples = {
        "message": ["hello world"],
        "attr:pid": ["1", "2", "3"],
    }
    rec = recommend_fields(samples)
    tokens = {v.token for v in rec.verdicts}
    assert tokens == {"message", "attr:pid"}
    assert np.isfinite(0.0)  # sanity: numpy import used
