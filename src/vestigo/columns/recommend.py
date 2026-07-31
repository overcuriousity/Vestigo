"""Deterministic scoring of candidate event-grid columns (issue #213).

Pure functions over the per-source field-statistics cache
(``db/field_stats.py``) — no database handle, no model, no clock. The same
stats always produce the same columns in the same order, which is what lets
the recommendation carry a forensic reason string instead of "the computer
decided".

**What makes a good grid column is not what makes a good embedding field.**
``db/field_recommend.py`` answers a different question (which fields carry
free-text meaning worth vectorizing) and therefore rejects exactly the fields
an analyst most wants on screen: ports, status codes, event IDs, IP
addresses. Only its value-shape regexes are shared; the scoring is its own.

Five signals, combined into one score:

* **breadth** — the fraction of the timeline's sources that carry the field.
  Weighted highest, because a column that is blank for two of four sources
  makes a merged timeline look broken.
* **fill** — the fraction of events with a non-empty value, summed exactly
  across sources.
* **cardinality** — rewards fields that *group*. A field with one value
  everywhere and a field with a different value on every row are both dead
  grid space; the useful ones sit between.
* **shape** — a gate, not a score: hashes, GUIDs and values too long to fit
  in a cell are rejected outright.
* **name affinity** — a small static vocabulary of forensically meaningful
  field names. This is the deterministic stand-in for "semantic value"; it
  only re-ranks fields that already passed the statistical gates, so a corpus
  whose fields are all named ``f_17`` still gets a sensible answer.

Deliberate exclusions:

* ``timestamp`` is pinned as the first column by :func:`finalize_columns` and
  is never a candidate — it is the spine of a timeline, not a suggestion.
* ``message`` is a *filler*: eligible only to reach the minimum column count
  when the corpus has nothing better, which is the situation the default
  columns already handled badly.
* Ingestion metadata (``parser_name``, ``source_file``, hashes) is never a
  candidate. It describes how the evidence got here, not what happened.
* Attribute keys consumed by a timeline's ``field_mappings`` are skipped. The
  grid renders a dynamic column straight out of ``attributes[colId]``
  (``EventGrid.tsx``), so neither the canonical name (absent from the map)
  nor one arbitrary raw key (blank for the sources using the other spelling)
  renders correctly — recommending either would be recommending a column that
  looks broken. See ``docs/ROADMAP.md``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import cache
from typing import Any

# The value-shape regexes are the one thing the embedding recommender and this
# one legitimately agree on: a hash is opaque whatever you want the field for.
from vestigo.db.field_recommend import _GUID_RE, _HEX_RE

#: Top-level columns eligible as recommendations. A subset of the cached
#: ``_NOVELTY_CANDIDATE_TOP_LEVEL`` columns: ``parser_name`` is ingestion
#: metadata, and ``artifact_long``/``source_id`` duplicate columns already
#: reachable from the picker.
CANDIDATE_TOP_LEVEL: tuple[str, ...] = ("artifact", "timestamp_desc", "display_name")

#: Always the first column, never scored.
PINNED_COLUMN = "timestamp"

#: Last-resort filler (see the module docstring).
FILLER_COLUMN = "message"

#: Grid-internal column ids, on top of the top-level display columns.
_GRID_INTERNAL_COLUMN_IDS = frozenset({"_select", "_expand", "_annotations"})


@cache
def reserved_column_ids() -> frozenset[str]:
    """Grid column ids an attribute key must not collide with.

    The grid resolves a known id to its built-in column definition, so an
    attribute literally named ``message`` would silently render the top-level
    ``message`` instead of itself — a recommendation that shows the wrong data
    is worse than no recommendation.

    ``db.queries`` is imported lazily here for the same reason
    ``db/field_stats.py`` defers it: it is a heavy module, and this one is
    otherwise pure enough to import from a unit test or the CLI for free.
    """
    from vestigo.db.queries import TOP_LEVEL_DISPLAY_COLUMNS

    return frozenset(TOP_LEVEL_DISPLAY_COLUMNS) | _GRID_INTERNAL_COLUMN_IDS


#: Field-name fragments that carry forensic meaning, matched against the
#: word-ish segments of a key. Intentionally small and generic — this is a
#: tie-breaker between fields that already scored well, not a schema.
_MEANINGFUL_NAME_TOKENS = frozenset(
    {
        "account",
        "action",
        "app",
        "cmd",
        "command",
        "computer",
        "country",
        "dst",
        "dest",
        "destination",
        "device",
        "domain",
        "event",
        "eventid",
        "exe",
        "file",
        "host",
        "hostname",
        "http",
        "image",
        "ip",
        "level",
        "method",
        "name",
        "op",
        "operation",
        "outcome",
        "path",
        "pid",
        "port",
        "proc",
        "process",
        "proto",
        "protocol",
        "query",
        "reason",
        "request",
        "resource",
        "result",
        "rule",
        "service",
        "session",
        "severity",
        "signature",
        "src",
        "source",
        "status",
        "target",
        "type",
        "uri",
        "url",
        "user",
        "username",
        "verb",
    }
)

# Weights. Breadth dominates: the issue's primary ask is "fields present on
# multiple source artifacts are preferred".
_W_BREADTH = 3.0
_W_FILL = 2.0
_W_CARDINALITY = 2.0
_W_NAME = 1.0
_W_TOTAL = _W_BREADTH + _W_FILL + _W_CARDINALITY + _W_NAME

#: Minimum normalized score a field must reach to be offered at all. Set so a
#: field present in every source and reasonably filled clears it even with an
#: unrecognizable name, while a sparse single-source field does not.
SCORE_FLOOR = 0.35

#: Below this many non-empty values, "every value is distinct" says nothing —
#: a 12-event source has 12 distinct values for almost every field. The
#: uniqueness rejection is skipped rather than applied to noise.
_MIN_COVERAGE_FOR_UNIQUENESS = 50

#: Mean rendered length above which a value stops being a column and starts
#: being a paragraph. Grid cells are ~160px.
_MAX_MEAN_VALUE_LENGTH = 120

#: Mean length at or below which a value is comfortably scannable.
_COMFORTABLE_VALUE_LENGTH = 40

#: Enrichment-derived keys (``src_ip:geo_country``) get a small bump: someone
#: deliberately ran that enricher on this case.
_DERIVED_BONUS = 0.05

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class ColumnCandidate:
    """One scored candidate column, with the evidence behind its score."""

    token: str
    score: float
    breadth: float
    fill: float
    distinct: int
    coverage: int
    sources_present: int
    sources_total: int
    samples: tuple[str, ...]
    reason: str


def _name_segments(token: str) -> set[str]:
    """Split a field key into lowercase word-ish segments.

    Handles ``src_ip``, ``SourceIp``, ``source.ip`` and ``src_ip:geo_country``
    alike, so name affinity does not depend on a vendor's casing convention.
    """
    decamelized = _CAMEL_SPLIT_RE.sub("_", token)
    return {seg for seg in _WORD_SPLIT_RE.split(decamelized.lower()) if seg}


def _name_affinity(token: str) -> float:
    """Fraction-ish score for how forensically meaningful a field name reads."""
    segments = _name_segments(token)
    if not segments:
        return 0.0
    hits = sum(1 for seg in segments if seg in _MEANINGFUL_NAME_TOKENS)
    if hits == 0:
        return 0.0
    # One recognized segment already says a lot ("user"); a second confirms it
    # ("src_ip"). Beyond that the name is just long.
    return min(1.0, 0.7 + 0.3 * (hits - 1))


def _cardinality_score(distinct: int, coverage: int) -> float | None:
    """Score how well a field *groups*, or None when it should be rejected.

    Rejects a constant (nothing to read) and a per-row-unique value (nothing
    to compare). ``distinct`` is max-across-sources — a documented
    approximation of the union in ``db/field_stats.py`` — so the uniqueness
    test only applies once a field has enough values for the approximation to
    mean anything.
    """
    if distinct <= 1:
        return None
    ratio = min(1.0, distinct / coverage) if coverage > 0 else 1.0
    if coverage >= _MIN_COVERAGE_FOR_UNIQUENESS and ratio >= 0.95:
        return None
    # log10-scaled: 10 distinct -> 0.5, 100+ -> 1.0. More values means more to
    # tell apart, with diminishing returns.
    diversity = min(1.0, math.log10(distinct) / 2.0)
    # Full credit while values repeat often; tapering as the field approaches
    # one-value-per-row.
    uniqueness = 1.0 if ratio <= 0.3 else max(0.0, (1.0 - ratio) / 0.7)
    return 0.5 * diversity + 0.5 * uniqueness


def _shape_factor(samples: list[str]) -> float | None:
    """Multiplier for how renderable a field's values are, or None to reject.

    Rejects opaque identifiers (hashes, GUIDs) and values too long for a grid
    cell. Returns a mild bonus for short values, which is what a dense grid
    is for.
    """
    if not samples:
        # No sampled values cached (oversized values, or a truncated key list)
        # — no evidence either way, so neither reward nor punish.
        return 1.0
    n = len(samples)
    frac_hex = sum(1 for s in samples if _HEX_RE.match(s)) / n
    frac_guid = sum(1 for s in samples if _GUID_RE.match(s)) / n
    if frac_hex >= 0.8 or frac_guid >= 0.8:
        return None
    mean_len = sum(len(s) for s in samples) / n
    if mean_len > _MAX_MEAN_VALUE_LENGTH:
        return None
    return 1.1 if mean_len <= _COMFORTABLE_VALUE_LENGTH else 1.0


@cache
def _derived_suffixes() -> frozenset[str]:
    """Every registered enricher's output-field names.

    Same set ``GET /fields`` publishes as ``derived_suffixes`` — matching on it
    rather than on "contains a colon" is what keeps a vendor key like
    ``Event:System:Level`` from being mistaken for enrichment output.
    """
    from vestigo.enrichers.registry import all_enrichers

    return frozenset(field for enricher in all_enrichers() for field in enricher.output_fields)


def _is_derived_key(token: str) -> bool:
    """Whether *token* is an enrichment-derived ``parent:field`` key."""
    from vestigo.enrichers.base import FIELD_KEY_SEPARATOR

    if FIELD_KEY_SEPARATOR not in token:
        return False
    return token.rsplit(FIELD_KEY_SEPARATOR, 1)[1] in _derived_suffixes()


def _entry_samples(entry: dict[str, Any]) -> list[str]:
    """Pull sample values out of a cached field entry.

    Attribute entries carry ``samples``; top-level entries only carry the
    top-N ``values`` list. Either is a fine basis for shape analysis.
    """
    samples = [str(v) for v in entry.get("samples") or [] if v not in (None, "")]
    if samples:
        return samples
    return [str(v) for v, _ in (entry.get("values") or []) if v not in (None, "")]


def _describe(
    *, sources_present: int, sources_total: int, fill: float, distinct: int, derived: bool
) -> str:
    """Build the human-readable reason shown next to a recommended column."""
    parts = [f"in {sources_present}/{sources_total} source{'s' if sources_total != 1 else ''}"]
    parts.append(f"{round(fill * 100)}% filled")
    parts.append(f"{distinct} distinct value{'s' if distinct != 1 else ''}")
    if derived:
        parts.append("enrichment output")
    return " · ".join(parts)


def score_columns(
    stats: dict[str, tuple[int, dict[str, Any]]],
    field_mappings: dict[str, list[str]] | None = None,
    *,
    max_candidates: int = 25,
) -> list[ColumnCandidate]:
    """Rank candidate grid columns for a timeline.

    Args:
        stats: ``{source_id: (events_total, payload)}`` exactly as
            ``db/field_stats.ensure_source_field_stats`` returns it.
        field_mappings: The timeline's canonical field mappings, if any. Raw
            keys consumed by a mapping are excluded (see the module
            docstring).
        max_candidates: Cap on the returned list, highest score first.

    Returns:
        Candidates above :data:`SCORE_FLOOR`, best first, ties broken by token
        name so the order is reproducible.
    """
    sources_total = len(stats)
    total_events = sum(t for t, _ in stats.values())
    if sources_total == 0 or total_events == 0:
        return []

    mapped_raws = {raw for raws in (field_mappings or {}).values() for raw in raws}
    excluded_attrs = reserved_column_ids() | mapped_raws | set(field_mappings or {})

    # Merge per-source entries into one view per token. Coverage sums exactly;
    # distinct is max-across-sources (db/field_stats.py's documented
    # approximation); samples are pooled. Ineligible tokens are dropped here
    # rather than after merging, so an attribute that shares a name with a
    # top-level column can never contaminate that column's statistics.
    merged: dict[str, dict[str, Any]] = {}
    for _events_total, payload in stats.values():
        sections: tuple[tuple[str, frozenset[str] | None], ...] = (
            ("top_level", frozenset(CANDIDATE_TOP_LEVEL)),
            ("attributes", None),
        )
        for section, allowed in sections:
            entries: dict[str, Any] = payload.get(section) or {}
            for token, entry in entries.items():
                if allowed is not None and token not in allowed:
                    continue
                if allowed is None and token in excluded_attrs:
                    continue
                coverage = int(entry.get("coverage", 0))
                if coverage <= 0:
                    continue
                acc = merged.setdefault(
                    token,
                    {
                        "coverage": 0,
                        "distinct": 0,
                        "sources": 0,
                        "samples": [],
                        "top_level": section == "top_level",
                    },
                )
                acc["coverage"] += coverage
                acc["distinct"] = max(acc["distinct"], int(entry.get("distinct", 0)))
                acc["sources"] += 1
                acc["samples"].extend(_entry_samples(entry)[:5])

    candidates: list[ColumnCandidate] = []
    for token, acc in merged.items():
        coverage = int(acc["coverage"])
        distinct = int(acc["distinct"])
        cardinality = _cardinality_score(distinct, coverage)
        if cardinality is None:
            continue
        shape = _shape_factor(acc["samples"])
        if shape is None:
            continue

        breadth = acc["sources"] / sources_total
        fill = min(1.0, coverage / total_events)
        derived = not acc["top_level"] and _is_derived_key(token)
        weighted = (
            _W_BREADTH * breadth
            + _W_FILL * fill
            + _W_CARDINALITY * cardinality
            + _W_NAME * _name_affinity(token)
        ) / _W_TOTAL
        score = weighted * shape + (_DERIVED_BONUS if derived else 0.0)

        candidates.append(
            ColumnCandidate(
                token=token,
                score=round(score, 6),
                breadth=round(breadth, 4),
                fill=round(fill, 4),
                distinct=distinct,
                coverage=coverage,
                sources_present=int(acc["sources"]),
                sources_total=sources_total,
                samples=tuple(acc["samples"][:3]),
                reason=_describe(
                    sources_present=int(acc["sources"]),
                    sources_total=sources_total,
                    fill=fill,
                    distinct=distinct,
                    derived=derived,
                ),
            )
        )

    ranked = [c for c in candidates if c.score >= SCORE_FLOOR]
    ranked.sort(key=lambda c: (-c.score, c.token))
    return ranked[:max_candidates]


def pick_columns(candidates: list[ColumnCandidate], *, k_min: int = 3, k_max: int = 5) -> list[str]:
    """Choose the recommended tokens from *candidates*, best first.

    Returns an empty list when fewer than *k_min* columns can be justified —
    the caller records that as "insufficient" and the explorer keeps its
    built-in defaults, which is the honest outcome for a corpus with nothing
    to recommend.
    """
    picked = [c.token for c in candidates[:k_max]]
    if len(picked) < k_min and FILLER_COLUMN not in picked:
        # Nothing structured enough to fill the grid — fall back to the long
        # message column rather than shipping a two-column timeline.
        picked.append(FILLER_COLUMN)
    if len(picked) < k_min:
        return []
    return picked


def finalize_columns(
    tokens: list[str], candidates: list[ColumnCandidate]
) -> tuple[list[str], dict[str, str]]:
    """Assemble the persisted column list and its per-column reasons.

    Pins :data:`PINNED_COLUMN` first, drops duplicates while preserving order,
    and attaches each column's evidence string. Returns ``([], {})`` when
    *tokens* is empty, so an "insufficient" result stays distinguishable from
    "timestamp only".
    """
    if not tokens:
        return [], {}
    reasons_by_token = {c.token: c.reason for c in candidates}
    columns = [PINNED_COLUMN]
    for token in tokens:
        if token not in columns:
            columns.append(token)
    reasons = {
        token: reasons_by_token.get(
            token,
            "fallback column — the timeline had nothing more structured to show",
        )
        for token in columns
        if token != PINNED_COLUMN
    }
    return columns, reasons
