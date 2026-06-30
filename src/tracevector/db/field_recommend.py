"""Heuristic + embedding-based field recommendation for the embedding wizard.

Two-stage ("hybrid") strategy:

1. **Value heuristics** (cheap, deterministic, no model) classify each candidate
   field as semantically *rich* — free-text / natural-language content worth
   embedding — or *low-signal* — opaque IDs, hashes, GUIDs, pure numbers, or
   near-constant enums that only add noise to a vector.
2. **Embedding relatedness** runs only on the rich survivors: a per-field
   centroid is computed from a sample of its values and fields whose centroids
   are close in cosine space are grouped into *related field groups* (pairs and
   larger).  This surfaces fields that carry overlapping meaning (e.g.
   ``message`` + ``display_name`` or ``src_ip`` + ``dst_ip``) so the analyst can
   decide whether the redundancy is worth embedding twice.

Stage 1 is pure and unit-testable.  Stage 2 is skipped when no ``encode``
callable is supplied, giving a heuristic-only fallback that never loads a model.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# A run of >=16 hex chars (sha1/sha256/md5, opaque handles).
_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
# Canonical GUID/UUID, with or without braces.
_GUID_RE = re.compile(
    r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$"
)
# Integer or decimal, optionally signed, with ``.`` or ``,`` decimal mark.
_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


@dataclass
class FieldVerdict:
    """Heuristic classification of a single candidate field."""

    token: str
    recommended: bool
    # "text" | "numeric" | "hash" | "guid" | "id" | "constant" | "empty"
    kind: str
    reason: str


@dataclass
class FieldRecommendation:
    """Result of :func:`recommend_fields` for one artifact."""

    recommended: list[str]
    verdicts: list[FieldVerdict]
    related_groups: list[list[str]]


def _fraction(values: Sequence[str], pred: Callable[[str], bool]) -> float:
    return sum(1 for v in values if pred(v)) / len(values) if values else 0.0


def _clean(values: Sequence[Any]) -> list[str]:
    """Return non-empty, stripped string forms of ``values``."""
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def classify_field(
    token: str, values: Sequence[Any], *, always_text: bool = False
) -> FieldVerdict:
    """Classify a field by sampling its values.

    ``always_text`` forces a positive verdict (used for ``message``, which is the
    primary embedding target and is always recommended regardless of content).
    """
    if always_text:
        return FieldVerdict(token, True, "text", "primary embedding field")

    strs = _clean(values)
    if len(strs) < 2:
        return FieldVerdict(token, False, "empty", "too few non-empty values")

    n = len(strs)
    distinct = len(set(strs))
    if distinct <= 1:
        return FieldVerdict(token, False, "constant", "single constant value")

    distinct_ratio = distinct / n
    frac_guid = _fraction(strs, lambda s: bool(_GUID_RE.match(s)))
    frac_numeric = _fraction(strs, lambda s: bool(_NUMERIC_RE.match(s)))
    frac_hex = _fraction(strs, lambda s: bool(_HEX_RE.match(s)))
    frac_multitoken = _fraction(strs, lambda s: " " in s)
    avg_tokens = sum(len(s.split()) for s in strs) / n

    if frac_guid >= 0.8:
        return FieldVerdict(token, False, "guid", "mostly GUID/UUID values")
    if frac_numeric >= 0.9:
        return FieldVerdict(token, False, "numeric", "mostly numeric values")
    if frac_hex >= 0.8:
        return FieldVerdict(token, False, "hash", "mostly hex/hash values")
    # High-cardinality single-token strings look like unique identifiers
    # (session keys, handles) rather than language worth embedding.
    if distinct_ratio > 0.95 and avg_tokens < 1.5 and frac_multitoken < 0.1:
        return FieldVerdict(token, False, "id", "high-cardinality identifier")

    return FieldVerdict(token, True, "text", "free-text content")


def _field_centroid(
    values: Sequence[Any],
    encode: Callable[[list[str]], list[list[float]]],
    max_values: int,
) -> np.ndarray | None:
    """Return the L2-normalised mean embedding of up to ``max_values`` samples."""
    vals = _clean(values)[:max_values]
    if not vals:
        return None
    vecs = np.asarray(encode(vals), dtype=np.float32)
    if vecs.ndim != 2 or vecs.shape[0] == 0:
        return None
    centroid = vecs.mean(axis=0)
    norm = np.linalg.norm(centroid)
    return centroid / norm if norm > 0 else centroid


def _group_related_fields(
    samples: dict[str, Sequence[Any]],
    *,
    encode: Callable[[list[str]], list[list[float]]],
    sim_threshold: float,
    max_values: int,
) -> list[list[str]]:
    """Group tokens whose value-centroids are within ``sim_threshold`` cosine.

    Uses union-find over the pairwise-similar edges so transitively related
    fields land in one group.  Singletons are dropped; groups preserve the input
    token order for stable output.
    """
    tokens = list(samples.keys())
    centroids = {
        t: _field_centroid(samples[t], encode, max_values) for t in tokens
    }
    usable = [t for t in tokens if centroids[t] is not None]
    if len(usable) < 2:
        return []

    parent = {t: t for t in usable}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for i, a in enumerate(usable):
        for b in usable[i + 1 :]:
            sim = float(np.dot(centroids[a], centroids[b]))  # unit vectors
            if sim >= sim_threshold:
                union(a, b)

    groups: dict[str, list[str]] = {}
    for t in usable:
        groups.setdefault(find(t), []).append(t)
    # Preserve input order within and across groups; keep only real groups.
    ordered = sorted(
        (g for g in groups.values() if len(g) >= 2),
        key=lambda g: tokens.index(g[0]),
    )
    return [sorted(g, key=tokens.index) for g in ordered]


def recommend_fields(
    field_samples: dict[str, Sequence[Any]],
    *,
    always_tokens: Sequence[str] = ("message",),
    encode: Callable[[list[str]], list[list[float]]] | None = None,
    sim_threshold: float = 0.6,
    max_values_for_embedding: int = 40,
) -> FieldRecommendation:
    """Apply the hybrid heuristic→pairs strategy to one artifact's fields.

    ``field_samples`` maps a field token (``"message"`` or ``"attr:<key>"``) to a
    sample of that field's raw values.  When ``encode`` is omitted only the
    heuristic stage runs and ``related_groups`` is empty.
    """
    always = set(always_tokens)
    verdicts = [
        classify_field(token, values, always_text=token in always)
        for token, values in field_samples.items()
    ]
    recommended = [v.token for v in verdicts if v.recommended]

    related_groups: list[list[str]] = []
    if encode is not None and len(recommended) >= 2:
        related_groups = _group_related_fields(
            {t: field_samples[t] for t in recommended},
            encode=encode,
            sim_threshold=sim_threshold,
            max_values=max_values_for_embedding,
        )

    return FieldRecommendation(
        recommended=recommended,
        verdicts=verdicts,
        related_groups=related_groups,
    )
