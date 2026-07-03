"""Tests for the embedding-wizard field recommender (heuristics + pairing)."""

from __future__ import annotations

import numpy as np

from tracesignal.db.field_recommend import (
    classify_field,
    cross_source_cohesion,
    recommend_fields,
    recommend_fields_across_sources,
    timeline_cohesion_summary,
    timeline_universal_cohesion,
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


# ---------------------------------------------------------------------------
# Cross-source cohesion
# ---------------------------------------------------------------------------


def test_cross_source_cohesion_identical_fields_returns_one():
    """Two sources with identical values should yield cohesion ≈ 1."""
    values_by_source = {
        "src_a": ["ip 10.0.0.1", "ip 10.0.0.2"],
        "src_b": ["ip 10.0.0.3", "addr 10.0.0.4"],
    }
    c = cross_source_cohesion(values_by_source, encode=_fake_encode_factory())
    assert c is not None
    assert c > 0.9


def test_cross_source_cohesion_orthogonal_fields_returns_zero():
    """Sources with completely different content domains → cohesion ≈ 0."""
    values_by_source = {
        "src_a": ["ip 10.0.0.1", "ip 10.0.0.2"],  # → +x
        "src_b": ["login event", "logon attempt"],  # → +y
    }
    c = cross_source_cohesion(values_by_source, encode=_fake_encode_factory())
    assert c is not None
    assert c < 0.1


def test_cross_source_cohesion_needs_at_least_two_sources():
    values_by_source = {"only_one": ["something"]}
    c = cross_source_cohesion(values_by_source, encode=_fake_encode_factory())
    assert c is None


# ---------------------------------------------------------------------------
# recommend_fields_across_sources
# ---------------------------------------------------------------------------


def _make_multi_source_samples(
    *,
    src_a_message=("user login succeeded", "logon from console"),
    src_b_message=("logon attempt", "login from network"),
    src_a_extra=None,
    src_b_extra=None,
):
    """Helper to build a two-source field_samples_by_source dict."""
    samples: dict[str, dict] = {
        "src_a": {"message": list(src_a_message)},
        "src_b": {"message": list(src_b_message)},
    }
    if src_a_extra:
        samples["src_a"].update(src_a_extra)
    if src_b_extra:
        samples["src_b"].update(src_b_extra)
    return samples


def test_shared_cohesive_field_recommended():
    """A text-rich field present in both sources with high cohesion → on."""
    field_samples_by_source = {
        "src_a": {
            "message": ["user login succeeded"],
            "attr:src_ip": ["ip 10.0.0.1", "addr 10.0.0.2"],
        },
        "src_b": {
            "message": ["logon attempt"],
            "attr:src_ip": ["ip 192.168.0.1", "addr 192.168.0.5"],
        },
    }
    rec = recommend_fields_across_sources(
        field_samples_by_source,
        source_count=2,
        encode=_fake_encode_factory(),
    )
    # message always on; src_ip is cohesive across sources.
    assert "message" in rec.recommended
    assert "attr:src_ip" in rec.recommended

    ip_verdict = next(v for v in rec.verdicts if v.token == "attr:src_ip")
    assert ip_verdict.kind == "shared-cohesive"
    assert ip_verdict.present_in_sources == 2
    assert ip_verdict.cohesion is not None and ip_verdict.cohesion > 0.9


def test_divergent_field_not_recommended():
    """A field with totally different content across sources → off, kind 'divergent'."""
    field_samples_by_source = {
        "src_a": {
            "message": ["user login"],
            "attr:note": ["ip 10.0.0.1", "addr 10.0.0.2"],  # IP-topic
        },
        "src_b": {
            "message": ["logon attempt"],
            "attr:note": ["login event", "logon audit"],  # login-topic
        },
    }
    rec = recommend_fields_across_sources(
        field_samples_by_source,
        source_count=2,
        encode=_fake_encode_factory(),
        cohesion_threshold=0.6,
    )
    note_verdict = next(v for v in rec.verdicts if v.token == "attr:note")
    assert not note_verdict.recommended
    assert note_verdict.kind == "divergent"


def test_source_specific_field_not_recommended():
    """A field present in only one source of a multi-source timeline → off."""
    field_samples_by_source = {
        "src_a": {
            "message": ["user login"],
            # Text-rich, varied values — but only in src_a.
            "attr:proc_name": [
                "lsass.exe authenticating user",
                "winlogon.exe session start",
                "svchost.exe network logon",
            ],
        },
        "src_b": {
            "message": ["logon attempt"],
            # attr:proc_name absent from src_b
        },
    }
    rec = recommend_fields_across_sources(
        field_samples_by_source,
        source_count=2,
        encode=_fake_encode_factory(),
    )
    proc_verdict = next((v for v in rec.verdicts if v.token == "attr:proc_name"), None)
    assert proc_verdict is not None
    assert not proc_verdict.recommended
    assert proc_verdict.kind == "source-specific"
    assert proc_verdict.present_in_sources == 1


def test_single_source_fallback_no_cohesion_penalty():
    """Single-source timeline: text-rich fields are recommended without cross-source penalty."""
    field_samples_by_source = {
        "src_a": {
            "message": ["user login succeeded", "logon from console"],
            "attr:src_ip": ["ip 10.0.0.1", "addr 10.0.0.2"],
        },
    }
    rec = recommend_fields_across_sources(
        field_samples_by_source,
        source_count=1,
        encode=_fake_encode_factory(),
    )
    # Both should be recommended without source-specific penalty.
    assert "message" in rec.recommended
    assert "attr:src_ip" in rec.recommended

    ip_verdict = next(v for v in rec.verdicts if v.token == "attr:src_ip")
    assert ip_verdict.kind in ("text", "shared-cohesive")  # plain text verdict


# ---------------------------------------------------------------------------
# timeline_cohesion_summary
# ---------------------------------------------------------------------------


def test_cohesion_summary_strong():
    from tracesignal.db.field_recommend import TimelineFieldVerdict

    verdicts = [
        TimelineFieldVerdict("message", True, "text", "primary", 2, 0.9),
        TimelineFieldVerdict("attr:src_ip", True, "shared-cohesive", "", 2, 0.85),
    ]
    s = timeline_cohesion_summary(verdicts, source_count=2, encode_available=True)
    assert s.level == "strong"
    assert s.mean_cohesion is not None and s.mean_cohesion > 0.8


def test_cohesion_summary_weak():
    from tracesignal.db.field_recommend import TimelineFieldVerdict

    verdicts = [
        TimelineFieldVerdict("message", True, "text", "primary", 2, 0.3),
    ]
    s = timeline_cohesion_summary(verdicts, source_count=2, encode_available=True)
    assert s.level == "weak"


def test_cohesion_summary_unavailable_no_encoder():
    s = timeline_cohesion_summary([], source_count=2, encode_available=False)
    assert s.level == "unavailable"
    assert s.mean_cohesion is None


def test_cohesion_summary_single_source():
    s = timeline_cohesion_summary([], source_count=1, encode_available=True)
    assert s.level == "unavailable"


# ---------------------------------------------------------------------------
# timeline_universal_cohesion
# ---------------------------------------------------------------------------


def test_universal_cohesion_comparable_messages_shared_cohesive():
    """Two sources with similar message content → shared-cohesive verdict."""
    # Both sources use login-flavoured messages → same embedding direction in
    # _fake_encode_factory, so cosine sim ≈ 1.
    samples_by_source = {
        "src_a": {"message": ["logon attempt", "login success", "logon from console"]},
        "src_b": {"message": ["login event", "user logon", "logon completed"]},
    }
    verdicts = timeline_universal_cohesion(
        samples_by_source,
        encode=_fake_encode_factory(),
        tokens=["message"],
        cohesion_threshold=0.6,
    )
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.token == "message"
    assert v.cohesion is not None and v.cohesion >= 0.6
    assert v.kind == "shared-cohesive"
    assert v.present_in_sources == 2


def test_universal_cohesion_divergent_messages():
    """Two sources whose message content embeds in different directions → divergent."""
    # src_a: IP-flavoured (x-axis), src_b: login-flavoured (y-axis).
    samples_by_source = {
        "src_a": {"message": ["ip addr 10.0.0.1", "addr block", "ip network scan"]},
        "src_b": {"message": ["user login event", "logon attempt", "login failed"]},
    }
    verdicts = timeline_universal_cohesion(
        samples_by_source,
        encode=_fake_encode_factory(),
        tokens=["message"],
        cohesion_threshold=0.6,
    )
    v = verdicts[0]
    assert v.token == "message"
    assert v.cohesion is not None and v.cohesion < 0.6
    assert v.kind == "divergent"


def test_universal_cohesion_skips_absent_tokens():
    """Tokens absent from a source (empty values) are not counted as present."""
    samples_by_source = {
        "src_a": {"message": ["logon from console"], "display_name": []},
        "src_b": {"message": ["logon event"], "display_name": []},
    }
    verdicts = timeline_universal_cohesion(
        samples_by_source,
        encode=_fake_encode_factory(),
        tokens=["message", "display_name"],
        cohesion_threshold=0.6,
    )
    by_token = {v.token: v for v in verdicts}
    # message is present in both, display_name is absent from both.
    assert by_token["message"].present_in_sources == 2
    assert by_token["display_name"].present_in_sources == 0
    assert by_token["display_name"].kind == "source-specific"


def test_universal_cohesion_no_encode_returns_none_cohesion():
    """Without an encoder, cohesion should be None for all tokens."""
    samples_by_source = {
        "src_a": {"message": ["logon attempt"]},
        "src_b": {"message": ["logon event"]},
    }
    verdicts = timeline_universal_cohesion(
        samples_by_source,
        encode=None,
        tokens=["message"],
    )
    v = verdicts[0]
    assert v.cohesion is None


def test_universal_cohesion_disjoint_artifacts_gives_honest_banner():
    """Disjoint-artifact timelines no longer produce the 'no shared fields' weak verdict.

    Regression test for the original bug: per-artifact bucketing made message
    appear in only one source per bucket even though it existed in all sources.
    timeline_universal_cohesion pools across artifacts, so comparable messages
    produce a non-weak (moderate or strong) banner.
    """
    # Simulate two sources with disjoint artifact types but comparable messages.
    # src_a: WEBHIST artifact; src_b: EVTX artifact.
    # Both have logon-flavoured messages that embed to the same y-axis direction.
    samples_by_source = {
        "src_a": {"message": ["user login from browser", "logon session", "logon completed"]},
        "src_b": {"message": ["login event recorded", "user logon", "session logon"]},
    }
    verdicts = timeline_universal_cohesion(
        samples_by_source,
        encode=_fake_encode_factory(),
        tokens=["message", "display_name", "tags", "timestamp_desc"],
        cohesion_threshold=0.6,
    )
    summary = timeline_cohesion_summary(verdicts, source_count=2, encode_available=True)
    # Must NOT produce the "no shared fields" weak verdict anymore.
    assert summary.level in ("moderate", "strong"), (
        f"Expected moderate/strong, got {summary.level}: {summary.message}"
    )
    assert summary.mean_cohesion is not None
    assert summary.shared_field_count >= 1
