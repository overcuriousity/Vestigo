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


def classify_field(token: str, values: Sequence[Any], *, always_text: bool = False) -> FieldVerdict:
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


def _batched_centroids[K](
    groups: dict[K, Sequence[Any]],
    encode: Callable[[list[str]], list[list[float]]],
    max_values: int,
) -> dict[K, np.ndarray | None]:
    """Compute one L2-normalised centroid per group with a single ``encode()`` call.

    Equivalent to calling :func:`_field_centroid` once per group, but flattens
    every group's (capped) value sample into one text list first so the whole
    batch costs one ``encode()`` round trip instead of ``len(groups)`` of them.
    This is safe to do unconditionally — which groups need centroids is decided
    entirely from the raw sample values (already known before encoding), never
    from a prior centroid's value, so no branch is skipped or added by batching.
    """
    flat: list[str] = []
    spans: dict[K, tuple[int, int]] = {}
    for key, values in groups.items():
        vals = _clean(values)[:max_values]
        start = len(flat)
        flat.extend(vals)
        spans[key] = (start, len(flat))

    if not flat:
        return dict.fromkeys(groups)

    vecs = np.asarray(encode(flat), dtype=np.float32)
    if vecs.ndim != 2 or vecs.shape[0] != len(flat):
        return dict.fromkeys(groups)

    out: dict[K, np.ndarray | None] = {}
    for key, (start, end) in spans.items():
        if end <= start:
            out[key] = None
            continue
        centroid = vecs[start:end].mean(axis=0)
        norm = np.linalg.norm(centroid)
        out[key] = centroid / norm if norm > 0 else centroid
    return out


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
    centroids = _batched_centroids({t: samples[t] for t in tokens}, encode, max_values)
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


# ---------------------------------------------------------------------------
# Cross-source cohesion (timeline-level recommendation)
# ---------------------------------------------------------------------------


@dataclass
class TimelineFieldVerdict:
    """Heuristic + cross-source classification of a field for a timeline."""

    token: str
    recommended: bool
    # "text" | "shared-cohesive" | "divergent" | "source-specific"
    # | "numeric" | "hash" | "guid" | "id" | "constant" | "empty"
    kind: str
    reason: str
    # How many of the timeline's sources contain this field.
    present_in_sources: int
    # Mean pairwise cosine between per-source value-centroids; None when
    # fewer than 2 sources have the field or encode is absent.
    cohesion: float | None


@dataclass
class TimelineFieldRecommendation:
    """Result of :func:`recommend_fields_across_sources` for one artifact."""

    recommended: list[str]
    verdicts: list[TimelineFieldVerdict]
    related_groups: list[list[str]]


@dataclass
class CohesionSummary:
    """Timeline-level cohesion verdict used by the wizard banner and analysis UI."""

    # "strong" (≥0.7) | "moderate" (≥0.5) | "weak" (<0.5) | "unavailable"
    level: str
    # Mean cohesion across all shared fields (None when unavailable).
    mean_cohesion: float | None
    # Number of fields present in ≥2 sources and text-rich.
    shared_field_count: int
    source_count: int
    message: str


def cross_source_cohesion(
    values_by_source: dict[str, Sequence[Any]],
    encode: Callable[[list[str]], list[list[float]]],
    max_values: int = 40,
) -> float | None:
    """Return the mean pairwise cosine between per-source value-centroids.

    ``values_by_source`` maps source_id → list of sampled values for one field.
    Returns ``None`` when fewer than 2 sources have usable values.
    """
    by_source = _batched_centroids(values_by_source, encode, max_values)
    centroids = [c for c in by_source.values() if c is not None]
    if len(centroids) < 2:
        return None
    # Mean pairwise cosine — centroids are already L2-normalised.
    mat = np.vstack(centroids)  # (n, d)
    sim_matrix = mat @ mat.T  # (n, n)
    n = len(centroids)
    # Pairwise upper-triangle indices only (exclude self-similarity).
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if not pairs:
        return None
    return float(np.mean([sim_matrix[i, j] for i, j in pairs]))


def recommend_fields_across_sources(
    field_samples_by_source: dict[str, dict[str, Sequence[Any]]],
    *,
    source_count: int,
    always_tokens: Sequence[str] = ("message",),
    encode: Callable[[list[str]], list[list[float]]] | None = None,
    sim_threshold: float = 0.6,
    cohesion_threshold: float = 0.6,
    max_values_for_embedding: int = 40,
) -> TimelineFieldRecommendation:
    """Timeline-level field recommendation with cross-source cohesion scoring.

    ``field_samples_by_source`` maps ``source_id → token → [sampled values]``.
    ``source_count`` is the total number of sources in the timeline (some may
    contribute no samples for a given artifact, so this can exceed the number
    of keys in ``field_samples_by_source``).

    Single-source timelines degrade to the per-source heuristic (no cohesion
    penalty for source-specific fields).  ``encode`` absence further degrades
    to heuristic-only (no Stage 2 pairing, no cohesion computation).

    Selection rules for multi-source timelines (source_count >= 2):
    - ``message`` → always on.
    - text-rich **and** present in ≥2 sources **and** cohesion ≥ threshold → on,
      kind ``"shared-cohesive"``.
    - text-rich **and** present in ≥2 sources **and** cohesion < threshold → off,
      reason ``"diverges across sources — would track source identity"``.
    - text-rich **but** present in only 1 source → off (kind ``"source-specific"``).
    - low-signal (id/hash/guid/numeric/constant/empty) → off regardless.
    """
    always = set(always_tokens)
    is_multi_source = source_count >= 2

    # Pool values from all sources per token.
    pooled: dict[str, list[Any]] = {}
    source_values: dict[str, dict[str, list[Any]]] = {}  # token → source_id → values
    for src_id, token_map in field_samples_by_source.items():
        for token, values in token_map.items():
            pooled.setdefault(token, []).extend(values)
            source_values.setdefault(token, {}).setdefault(src_id, []).extend(values)

    all_tokens = list(pooled.keys())

    verdicts: list[TimelineFieldVerdict] = []
    for token in all_tokens:
        values = pooled[token]
        present = len(source_values.get(token, {}))

        # Cohesion (Stage 2, only when encode is available and ≥2 sources).
        cohesion: float | None = None
        if encode is not None and present >= 2:
            cohesion = cross_source_cohesion(source_values[token], encode, max_values_for_embedding)

        if token in always:
            verdicts.append(
                TimelineFieldVerdict(
                    token=token,
                    recommended=True,
                    kind="text",
                    reason="primary embedding field",
                    present_in_sources=present,
                    cohesion=cohesion,
                )
            )
            continue

        # Stage 1: value-heuristic verdict on pooled values.
        base = classify_field(token, values)

        if not base.recommended:
            # Low-signal regardless of sharing.
            verdicts.append(
                TimelineFieldVerdict(
                    token=token,
                    recommended=False,
                    kind=base.kind,
                    reason=base.reason,
                    present_in_sources=present,
                    cohesion=cohesion,
                )
            )
            continue

        # Text-rich field — apply cross-source rules for multi-source timelines.
        if is_multi_source:
            if present < 2:
                verdicts.append(
                    TimelineFieldVerdict(
                        token=token,
                        recommended=False,
                        kind="source-specific",
                        reason="source-specific — skews cross-source comparison",
                        present_in_sources=present,
                        cohesion=cohesion,
                    )
                )
            elif cohesion is not None and cohesion < cohesion_threshold:
                verdicts.append(
                    TimelineFieldVerdict(
                        token=token,
                        recommended=False,
                        kind="divergent",
                        reason="diverges across sources — would track source identity",
                        present_in_sources=present,
                        cohesion=cohesion,
                    )
                )
            else:
                # Shared and cohesive (or cohesion unavailable — give benefit of doubt).
                verdicts.append(
                    TimelineFieldVerdict(
                        token=token,
                        recommended=True,
                        kind="shared-cohesive",
                        reason="shared across sources with cohesive content",
                        present_in_sources=present,
                        cohesion=cohesion,
                    )
                )
        else:
            # Single-source: plain text verdict.
            verdicts.append(
                TimelineFieldVerdict(
                    token=token,
                    recommended=True,
                    kind=base.kind,
                    reason=base.reason,
                    present_in_sources=present,
                    cohesion=cohesion,
                )
            )

    recommended = [v.token for v in verdicts if v.recommended]

    # Stage 2: group related fields using pooled values.
    related_groups: list[list[str]] = []
    if encode is not None and len(recommended) >= 2:
        related_groups = _group_related_fields(
            {t: pooled[t] for t in recommended},
            encode=encode,
            sim_threshold=sim_threshold,
            max_values=max_values_for_embedding,
        )

    return TimelineFieldRecommendation(
        recommended=recommended,
        verdicts=verdicts,
        related_groups=related_groups,
    )


def timeline_universal_cohesion(
    samples_by_source: dict[str, dict[str, Sequence[Any]]],
    *,
    encode: Callable[[list[str]], list[list[float]]] | None,
    tokens: Sequence[str] = ("message", "display_name", "tags", "timestamp_desc"),
    cohesion_threshold: float = 0.6,
    max_values: int = 40,
) -> list[TimelineFieldVerdict]:
    """Compute cross-source cohesion on universal top-level fields.

    Unlike :func:`recommend_fields_across_sources` — which buckets by artifact
    and therefore only sees a field as "shared" when the *same* artifact type
    appears in ≥2 sources — this function pools each source's values **across
    all its artifacts** for a fixed set of Timesketch-normalised fields
    (``message``, ``display_name``, ``tags``, ``timestamp_desc``).  This gives an
    honest measure of whether the fields that exist in *every* source carry
    comparable content, regardless of whether the sources share artifact types.

    Returns one :class:`TimelineFieldVerdict` per token in ``tokens``.
    ``encode=None`` produces verdicts with ``cohesion=None`` (heuristic-only
    path — the caller should still call :func:`timeline_cohesion_summary` to
    propagate the right "unavailable" level).
    """
    verdicts: list[TimelineFieldVerdict] = []
    for token in tokens:
        values_by_source: dict[str, list[Any]] = {
            src: list(samples.get(token, [])) for src, samples in samples_by_source.items()
        }
        # Count sources with at least one non-empty value.
        present_in_sources = sum(1 for vals in values_by_source.values() if _clean(vals))

        cohesion: float | None = None
        if encode is not None and present_in_sources >= 2:
            cohesion = cross_source_cohesion(values_by_source, encode, max_values)

        if present_in_sources < 2 or cohesion is None:
            # Not shared or not computable — omit from the "shared" pool.
            # Use kind "source-specific" so timeline_cohesion_summary skips it.
            verdicts.append(
                TimelineFieldVerdict(
                    token=token,
                    recommended=False,
                    kind="source-specific",
                    reason="not present in ≥2 sources or cohesion not computable",
                    present_in_sources=present_in_sources,
                    cohesion=cohesion,
                )
            )
        elif cohesion >= cohesion_threshold:
            verdicts.append(
                TimelineFieldVerdict(
                    token=token,
                    recommended=True,
                    kind="shared-cohesive",
                    reason=f"shared across sources with cohesive content ({cohesion:.2f})",
                    present_in_sources=present_in_sources,
                    cohesion=cohesion,
                )
            )
        else:
            verdicts.append(
                TimelineFieldVerdict(
                    token=token,
                    recommended=False,
                    kind="divergent",
                    reason=f"diverges across sources ({cohesion:.2f} < {cohesion_threshold})",
                    present_in_sources=present_in_sources,
                    cohesion=cohesion,
                )
            )
    return verdicts


def timeline_cohesion_summary(
    verdicts: list[TimelineFieldVerdict],
    *,
    source_count: int,
    encode_available: bool,
) -> CohesionSummary:
    """Aggregate per-field verdicts into a timeline-level cohesion verdict.

    Used by the embedding wizard banner and the analysis UI to explain whether
    cross-source outlier detection is expected to be meaningful.
    """
    if not encode_available:
        return CohesionSummary(
            level="unavailable",
            mean_cohesion=None,
            shared_field_count=0,
            source_count=source_count,
            message=(
                "Embedding model unavailable — field cohesion could not be computed. "
                "Recommendations are heuristic-only."
            ),
        )

    if source_count < 2:
        return CohesionSummary(
            level="unavailable",
            mean_cohesion=None,
            shared_field_count=0,
            source_count=source_count,
            message="Single-source timeline — cross-source cohesion does not apply.",
        )

    shared = [v for v in verdicts if v.present_in_sources >= 2 and v.cohesion is not None]
    if not shared:
        return CohesionSummary(
            level="weak",
            mean_cohesion=None,
            shared_field_count=0,
            source_count=source_count,
            message=(
                "No shared fields with computable cohesion. "
                "Similarity search across sources may reflect source format rather than content. "
                "Consider limiting search to events within a single source."
            ),
        )

    mean_c = float(np.mean([v.cohesion for v in shared]))  # type: ignore[arg-type]
    shared_count = len(shared)

    if mean_c >= 0.7:
        level = "strong"
        msg = (
            f"{shared_count} shared field{'s' if shared_count != 1 else ''} with "
            f"strong cohesion ({mean_c:.2f}). "
            "Cross-source outlier detection should be meaningful."
        )
    elif mean_c >= 0.5:
        level = "moderate"
        msg = (
            f"{shared_count} shared field{'s' if shared_count != 1 else ''} with "
            f"moderate cohesion ({mean_c:.2f}). "
            "Similarity search results may reflect some source-format variation. "
            "Consider deselecting divergent fields in the embedding wizard."
        )
    else:
        level = "weak"
        msg = (
            f"{shared_count} shared field{'s' if shared_count != 1 else ''} with "
            f"weak cohesion ({mean_c:.2f}). "
            "Similarity scores across sources may reflect log format rather than content. "
            "Deselect divergent fields in the embedding wizard for better results."
        )

    return CohesionSummary(
        level=level,
        mean_cohesion=mean_c,
        shared_field_count=shared_count,
        source_count=source_count,
        message=msg,
    )
