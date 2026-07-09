"""ClickHouse-backed statistical anomaly detection.

Two AMiner-inspired detectors operate directly on parsed event fields stored in
ClickHouse.  No embeddings or Qdrant are required — both detectors run against
already-ingested data.

**value_novelty** (``detector="value_novelty"``)
    Score rare or first-seen field values by surprise: ``-log(count/total)``.

    Two modes:

    * *self-baseline* — the full timeline is its own reference.  Values
      appearing ≤ ``rarity_floor`` times are flagged.  Works immediately after
      ingestion; no baseline window required.
    * *temporal* — analyst supplies an ``AnalysisWindows`` (a baseline range
      plus 1..N labeled suspect windows). Values absent from the baseline
      window but present in a suspect window are flagged as "first seen after
      the incident start," one finding per suspect window they appear in,
      scored against that window's own event count. Events outside every
      window are ignored; ``rarity_floor`` is ignored.

**frequency** (``detector="frequency"``)
    Detect event-count spikes and silences in time windows per ``series_field``
    value using z-score.  Buckets are computed with the same
    ``duration / bucket_count`` interval formula as the histogram endpoint, but
    over the *unfiltered* timeline for the given ``case_id``/``source_ids`` —
    it does not honor the events-view's ``q``/``artifact``/``tag``/time-range
    filters the way ``QueryService.histogram`` does, so anomaly windows can be
    computed over a different span than what a filtered histogram displays.
    Score = |z|; windows above ``z_threshold`` are returned.

    Two sub-modes mirror value_novelty:

    * *z-score* — baseline = all buckets in the timeline; flag any window
      beyond ``z_threshold`` standard deviations from the series mean.
    * *temporal-z-score* — an ``AnalysisWindows`` supplies the baseline range
      and 1..N suspect windows. The bucket interval is derived from the
      baseline window (so it keeps the full ``bucket_count`` resolution) and
      the same epoch-aligned interval buckets every suspect window. Mean/std
      are learned from the baseline window's zero-filled, fully-contained
      buckets only; each suspect window's fully-contained buckets are scored
      against that fixed baseline. Buckets cut by a window edge are excluded,
      and a suspect window with no full bucket is warned about rather than
      scored on a partial bucket.

**value_combo** (``detector="value_combo"``)
    The multi-field extension of ``value_novelty`` (AMiner
    ``NewMatchPathValueComboDetector``): instead of scoring a single field's
    values, score *combinations* of two or more fields (``GROUP BY`` over the
    field expressions). A combination rare or absent in the baseline is
    flagged even when each field's individual values are common — an
    ``(action, hour)`` pair like ``(login_ok, 03:00)`` can be novel while
    ``login_ok`` and ``03:00`` are each unremarkable. Same two modes and same
    surprise score as ``value_novelty``; auto mode picks a single tuple from
    the two highest-coverage recommended fields (no pair enumeration).

**numeric_range** (``detector="numeric_range"``)
    For fields whose values parse as numbers (syntactic type detection via
    ``toFloat64OrNull`` — never by field meaning), learn a baseline band and
    flag detect-window values outside it. AMiner ``ValueRangeDetector``. Two
    modes: *self-baseline* (``method="iqr"``) uses a Tukey fence
    ``[q1 − 1.5·IQR, q3 + 1.5·IQR]`` over the whole corpus — an exact min/max
    over the corpus flags nothing by construction; *temporal*
    (``method="temporal-range"``) learns exact min/max from the baseline
    window and flags detect-window values outside it. Findings group by
    distinct violating value; score = distance outside the band ÷ band width.

**charset** (``detector="charset"``)
    Per field, learn a reference character set over *distinct values* and flag
    values containing characters outside it (null bytes, unicode homoglyphs,
    injection metacharacters — purely syntactic, never by meaning). AMiner
    ``CharsetDetector``. Two modes: *self-baseline* (``method="rare-chars"``)
    treats characters appearing in ≤ ``rarity_floor`` distinct values as rare
    and flags values containing them (the whole-corpus charset trivially
    contains everything — same degeneracy the numeric_range detector's IQR
    fence works around); *temporal* (``method="temporal-charset"``) learns the
    baseline-window charset and flags detect-window values with never-seen
    characters. Score = Σ per novel char ``-log(n_vals_with_char / n_vals)``
    (temporal: ``log(n_vals + 1)`` per char) — value_novelty's surprise family.

**entropy** (``detector="entropy"``)
    Per field, Shannon character entropy (bits) of each *distinct value*
    compared against the field's baseline entropy distribution via a Tukey
    fence — above-band values look random (DGA domains, encoded payloads),
    below-band values look degenerate (padding, character stuffing). AMiner
    ``EntropyDetector``, purely syntactic. Two modes: *self-baseline*
    (``method="iqr"``, fence over the whole corpus — quantiles are not
    degenerate over their own population, unlike min/max) and *temporal*
    (``method="temporal-iqr"``, fence learned from the baseline window,
    detection restricted to the detect window). Entropies are per distinct
    value (frequency-independent); values shorter than
    ``_MIN_ENTROPY_VALUE_LEN`` codepoints are excluded throughout. Score =
    distance outside the band ÷ band width, like numeric_range.

**proportion_shift** (``detector="proportion_shift"``)
    Per (field, value), test whether the value's *share* of events differs
    significantly between the baseline window and each suspect window — a 2×2
    G-test (log-likelihood ratio, Dunning 1993) per (value, suspect window),
    p-values from the exact df=1 chi² survival function, Benjamini–Hochberg
    FDR across every test in the run, plus an effect-size floor (the share
    must change by at least ``min_ratio``× in either direction). Two-sided:
    findings carry ``direction`` ("up"/"down"), and a value that vanishes
    from a suspect window entirely is a maximal "down" (its rate ratio uses
    Haldane–Anscombe +0.5 smoothing; the test itself always uses raw counts).
    Temporal-only (``method="g-test"``): a share can only "shift" between two
    populations, so there is no self-baseline mode. First-seen values
    (``baseline_cnt = 0``) are excluded by construction — temporal
    value_novelty owns those. Score = the G statistic.

**interval_periodicity** (``detector="interval_periodicity"``)
    Per (field, value), test whether the value's *arrival cadence* changed
    between the baseline window and each suspect window. Adapted from
    AMiner's ``PathValueTimeIntervalDetector``, merged with the roadmap's D6
    (per-value silence) — the periodicity angle is exactly what
    proportion_shift's whole-window share test cannot see. Inter-arrival
    deltas are computed strictly *within* each window (the lag partition is
    ``(value, window)``, so boundary-straddling deltas cannot exist). Two
    directions, disjoint by construction on the baseline delta
    coefficient-of-variation (CV = stddev/mean):

    * *cadence break* (``direction`` = "missed"/"accelerated") — a value
      that arrived regularly in the baseline (CV ≤ regular ceiling, enough
      intervals) whose arrival *rate* differs in a suspect window: agent
      killed, heartbeat suppressed, or a periodic job running hot. Tested
      with a two-sample Poisson-rate likelihood-ratio G (window durations as
      exposures, df = 1 chi² p-value). For a genuinely periodic value the
      count variance is sub-Poisson, so the p-value is conservative — a
      flagged deficit is at least as significant under the periodic model.
      ``count = 0`` is the maximal "missed" — the per-value silence case —
      and its representative event is the value's last baseline occurrence.
    * *new regularity* (``direction`` = "new_regularity", beaconing) — a
      value that was bursty (CV ≥ irregular floor) or sparse in the baseline
      whose suspect-window arrivals are too evenly spaced to be random:
      Greenwood's spacing statistic ``G = Σ(δ/S)²`` over the window's active
      span ``S``, normal approximation, left-tail p ("more regular than a
      Poisson process"). Effect floors demand a genuinely tight cadence
      (window CV ceiling) covering a real fraction of the window (span
      floor), so a short dense burst never reads as beaconing.

    The CV band between the regular ceiling and the irregular floor is a
    deliberate dead band — values there are ambiguous and get no test. All
    tests in a run share one Benjamini–Hochberg FDR pool. Temporal-only
    (``method="cadence"``): cadence can only change between two populations.
    First-seen values (``baseline_cnt = 0``) are excluded by construction —
    temporal value_novelty owns those. Score = ``-log10(p)`` (unlike
    proportion_shift's raw G: two different statistics must rank on a common
    scale).

**timestamp_order** (``detector="timestamp_order"``)
    Flag events whose parsed timestamp jumps *backwards* relative to the
    previous record in the source file (record order = ``byte_offset``, then
    ``line_number``).  Log-tampering / clock-manipulation indicator, adapted
    from AMiner's ``TimestampsUnsortedDetector``.  Mode-less
    (``method="sequential"``): there is no baseline/detect split — the
    violation is purely positional.  Each event is compared to its immediate
    predecessor (``lagInFrame``), not to a running maximum, so a single
    future-dated outlier flags two boundaries instead of cascading over every
    later event.  ``min_skew_seconds`` suppresses sub-second logger jitter.
    Score = backwards jump in seconds.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from tracesignal.db._buckets import bucket_interval_seconds, query_timestamp_range
from tracesignal.db._columns import resolve_column_token
from tracesignal.db._dt import (
    TS_NOT_SENTINEL_SQL,
    ensure_utc,
    is_null_ts_sentinel,
    to_clickhouse_utc,
)
from tracesignal.db._scan import HEAVY_SCAN_SETTINGS
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_mappings import mapping_coalesce_expr, resolve_mapping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default fields scanned by value_novelty when no list is supplied (fallback only).
_DEFAULT_NOVELTY_FIELDS = ["timestamp_desc"]

# Pipeline-synthesized fields (added by normalization, not present in the raw
# log data). Never auto-recommended for novelty scanning — rare values here
# reflect ingestion metadata, not analyst-relevant log content. Still valid
# tokens for explicit `fields=` selections and the viz field picker.
_SYNTHETIC_FIELDS = {"artifact", "display_name", "parser_name", "parser_version", "source_file"}

# Top-level columns considered by the field recommender (excludes free-text
# and identifier-like columns that are not useful for novelty detection).
_NOVELTY_CANDIDATE_TOP_LEVEL = [
    "artifact",
    "timestamp_desc",
    "display_name",
    "parser_name",
]

# Maximum number of attribute keys the recommender will evaluate.
_RECOMMENDER_MAX_ATTR_KEYS = 50

# Cap on how many auto-selected fields find_value_novelty scans per call.
# Each field is a separate sequential full-partition-scan query (no batching
# across fields today), so an uncapped recommended set (up to ~54 fields —
# _NOVELTY_CANDIDATE_TOP_LEVEL plus _RECOMMENDER_MAX_ATTR_KEYS) could turn
# one panel-open into dozens of serial ClickHouse round-trips. Capped to the
# highest-coverage recommended fields, since recommend_novelty_fields already
# sorts by (recommended, -coverage).
_MAX_AUTO_SCAN_FIELDS = 15

# Of the auto-scan cap, the number of slots reserved for identifier-kind fields
# (URLs, hashes, user agents) in the charset/entropy detectors so a source with
# many categorical columns can't crowd them out — they are those detectors'
# primary target. Categoricals take the rest; each kind backfills the other's
# unused slots. Kept in sync with the frontend picker (detector-shared.tsx).
_AUTO_IDENTIFIER_RESERVE = 5

# Minimum baseline numeric samples before the range detector trusts a field's
# learned band — below this, the min/max or quartiles are too noisy to score.
_MIN_RANGE_BASELINE = 20

# Fraction of a field's non-empty values that must parse as numbers for it to
# be offered as a range-detector candidate (syntactic type detection only).
_MIN_NUMERIC_RATIO = 0.9

# Minimum distinct baseline values before the charset detector trusts a
# field's learned character set — below this, "never seen" is noise.
_MIN_CHARSET_BASELINE = 20

# Skip fields whose reference character set exceeds this (free text in large
# scripts, e.g. CJK) — a huge alphabet makes "novel character" meaningless and
# the reference-set query parameter unreasonably large.
_MAX_CHARSET_SIZE = 5000

# Minimum distinct baseline values before the entropy detector trusts a
# field's entropy distribution — quartiles over fewer points are noise.
_MIN_ENTROPY_BASELINE = 20

# Values shorter than this (in codepoints) are excluded from entropy scoring
# entirely (baseline and detect): character entropy of a 3-char string is
# degenerate and would swamp the band with false lows.
_MIN_ENTROPY_VALUE_LEN = 6

# Minimum buckets in a frequency series for z-scoring to be meaningful.
_MIN_FREQUENCY_BUCKETS = 3

# Floor applied to the leave-one-out std in self-baseline mode so that a
# near-constant rest-of-series doesn't divide by ~0 when scoring the
# excluded point (half an event-count unit — small enough to still flag any
# real deviation, large enough to avoid blowing up the z-score to inf/NaN).
_MIN_FREQUENCY_STD = 0.5

# Suspect windows with fewer events than this get a warning attached to the
# result: per-window surprise denominators over tiny samples are unstable.
# Warning only — findings are never silently suppressed.
_MIN_WINDOW_EVENTS = 50

# Separators used to flatten a value_combo finding's field/value tuples into
# the single (field, value) key the detector allowlist stores. The fields are
# comma-joined (tokens never contain commas); the values are joined with the
# ASCII unit separator, which cannot appear in parsed log values in practice
# and keeps the key reversible. Mirrored in the finding's details
# ("allowlist_field"/"allowlist_value") so clients never re-derive it.
COMBO_FIELD_SEP = ","
COMBO_VALUE_SEP = "\x1f"

# ---------------------------------------------------------------------------
# Analysis windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeWindow:
    """A labeled half-open time range ``[start, end)``, UTC."""

    label: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class AnalysisWindows:
    """The temporal detectors' window contract: one baseline, 1..N suspects.

    The baseline window is the known-normal reference period; suspect windows
    are the ranges under investigation. Windows are half-open ``[start, end)``
    and need not be adjacent — events outside every window are ignored by the
    detectors entirely. Baseline and suspect windows must be disjoint (the
    API validates this); suspect windows may overlap each other.
    """

    baseline: TimeWindow
    suspects: tuple[TimeWindow, ...]

    def payload(self) -> dict[str, Any]:
        """Serializable snapshot (DetectorRun params / result echo shape)."""
        return {
            "baseline": {
                "start": ensure_utc(self.baseline.start).isoformat(),
                "end": ensure_utc(self.baseline.end).isoformat(),
            },
            "suspect_windows": [
                {
                    "label": w.label,
                    "start": ensure_utc(w.start).isoformat(),
                    "end": ensure_utc(w.end).isoformat(),
                }
                for w in self.suspects
            ],
        }

    def config_hash(self) -> str:
        """SHA-256 over the canonical window payload — the run's window identity."""
        import hashlib
        import json

        canonical = json.dumps(
            self.payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def windows_from_split(
    baseline_end: datetime, min_ts: datetime, max_ts: datetime
) -> AnalysisWindows:
    """Build the legacy split-point window pair from a single ``baseline_end``.

    Preserves the original temporal contract — baseline = everything before
    the split, detect = everything after, adjacent and exhaustive — as one
    ``AnalysisWindows`` value, so detector internals have exactly one
    temporal code path. Using the real ``min_ts``/``max_ts`` (instead of
    ±infinity) keeps the year-2299 no-timestamp sentinel out of the detect
    window by construction; the max is padded by 1 ms because windows are
    half-open and ``max_ts`` itself must stay inside.
    """
    baseline_end = ensure_utc(baseline_end)
    return AnalysisWindows(
        baseline=TimeWindow("baseline", ensure_utc(min_ts), baseline_end),
        suspects=(
            TimeWindow("detect", baseline_end, ensure_utc(max_ts) + timedelta(milliseconds=1)),
        ),
    )


def _window_preds(windows: AnalysisWindows, params: dict[str, Any]) -> tuple[str, list[str]]:
    """Bind window bounds into *params* and return SQL predicates.

    Returns ``(baseline_pred, [suspect_pred, ...])`` — each a self-contained
    parenthesized ``timestamp`` range test. Parameter names are fixed
    (``b0``/``b1``, ``w{i}s``/``w{i}e``), so one params dict must not be
    reused for two different window sets.
    """
    params["b0"] = to_clickhouse_utc(windows.baseline.start)
    params["b1"] = to_clickhouse_utc(windows.baseline.end)
    baseline_pred = "(timestamp >= {b0:String} AND timestamp < {b1:String})"
    suspect_preds = []
    for i, w in enumerate(windows.suspects):
        params[f"w{i}s"] = to_clickhouse_utc(w.start)
        params[f"w{i}e"] = to_clickhouse_utc(w.end)
        suspect_preds.append(f"(timestamp >= {{w{i}s:String}} AND timestamp < {{w{i}e:String}})")
    return baseline_pred, suspect_preds


def _suspect_multiif(suspect_preds: list[str]) -> str:
    """A ``multiIf`` expression mapping timestamp → suspect-window index (or -1)."""
    branches = ", ".join(f"{pred}, {i}" for i, pred in enumerate(suspect_preds))
    return f"multiIf({branches}, -1)"


def _full_bucket_starts(window: TimeWindow, interval: int) -> list[datetime]:
    """Epoch-aligned bucket starts whose bucket lies fully inside *window*.

    ``toStartOfInterval`` aligns buckets to the Unix epoch, so a window edge
    can cut a bucket; partial buckets are excluded from frequency statistics
    entirely (a half-covered bucket would read as a fake spike or drop). May
    be empty when the window is shorter than one interval — callers must
    warn, never score a partial bucket.
    """
    start_epoch = ensure_utc(window.start).timestamp()
    end_epoch = ensure_utc(window.end).timestamp()
    first = math.ceil(start_epoch / interval) * interval
    out: list[datetime] = []
    b = first
    while b + interval <= end_epoch:
        out.append(datetime.fromtimestamp(b, tz=UTC))
        b += interval
    return out


def _window_size_warnings(windows: AnalysisWindows, suspect_totals: list[int]) -> list[str]:
    """Warnings for suspect windows too small for stable surprise scores."""
    return [
        f"Suspect window {w.label!r} has only {total} events — "
        f"surprise scores over samples below {_MIN_WINDOW_EVENTS} are unstable"
        for w, total in zip(windows.suspects, suspect_totals, strict=False)
        if total < _MIN_WINDOW_EVENTS
    ]


# ---------------------------------------------------------------------------
# Proportion-shift statistics (pure math — no scipy; airgapped-by-default)
# ---------------------------------------------------------------------------


def _g_statistic(a: int, b: int, c: int, d: int) -> float:
    """2×2 log-likelihood-ratio (G) statistic (Dunning 1993).

    Contingency table rows (baseline, window) × cols (value, other):
    ``a`` = baseline count of the value, ``b`` = rest of the baseline,
    ``c`` = suspect-window count, ``d`` = rest of the window. Zero cells
    contribute nothing (lim x→0 of x·log(x/e) = 0); the clamp absorbs
    floating-point noise on near-null tables.
    """
    n = a + b + c + d
    if n == 0:
        return 0.0
    g = 0.0
    for obs, row, col in (
        (a, a + b, a + c),
        (b, a + b, b + d),
        (c, c + d, a + c),
        (d, c + d, b + d),
    ):
        if obs > 0:
            g += obs * math.log(obs * n / (row * col))
    return max(2.0 * g, 0.0)


def _chi2_sf_df1(g: float) -> float:
    """P(χ²₁ ≥ g) — exact for one degree of freedom via ``erfc(√(g/2))``.

    The df=1 chi² survival function has this closed form, so no scipy
    dependency is needed for the G-test's p-value.
    """
    return math.erfc(math.sqrt(g / 2.0)) if g > 0 else 1.0


def _bh_qvalues(pvals: list[float]) -> list[float]:
    """Benjamini–Hochberg adjusted p-values (step-up), in input order.

    ``q[i] = min over ranks ≥ rank(i) of (p·m/rank)`` — the standard
    monotone-enforced adjustment; a finding is significant at FDR level q*
    iff its q-value ≤ q*.
    """
    m = len(pvals)
    if m == 0:
        return []
    p = np.asarray(pvals, dtype=np.float64)
    order = np.argsort(p)  # ascending
    q = np.empty(m, dtype=np.float64)
    running = 1.0
    for rank in range(m, 0, -1):  # walk from the largest p down
        idx = order[rank - 1]
        running = min(running, p[idx] * m / rank)
        q[idx] = running
    return [float(v) for v in q]


def _poisson_rate_g(a: int, d_a: float, c: int, d_c: float) -> float:
    """Two-sample Poisson-rate log-likelihood-ratio statistic (df = 1).

    ``a`` events over exposure ``d_a`` (baseline duration, seconds) vs.
    ``c`` events over exposure ``d_c``. Under H0 (one common rate) the
    expected counts split the total proportionally to exposure; the LR
    statistic is asymptotically chi²₁, so :func:`_chi2_sf_df1` applies.
    Zero cells contribute nothing (lim x→0 of x·log(x/e) = 0).
    """
    n = a + c
    if n == 0 or d_a <= 0 or d_c <= 0:
        return 0.0
    e_a = n * d_a / (d_a + d_c)
    e_c = n * d_c / (d_a + d_c)
    g = 0.0
    if a > 0:
        g += a * math.log(a / e_a)
    if c > 0:
        g += c * math.log(c / e_c)
    return max(2.0 * g, 0.0)


def _greenwood_p(g: float, n_spacings: int) -> tuple[float, float]:
    """Left-tail (too-regular) p for Greenwood's spacing statistic.

    ``g = Σ(δᵢ/S)²`` over ``n_spacings`` inter-arrival deltas normalized by
    their active span ``S``. Under H0 — arrivals from a homogeneous Poisson
    process, i.e. given the first/last arrival the interior points are
    uniform on the span — ``E[G] = 2/(N+1)`` and
    ``Var[G] = 4(N−1)/((N+1)²(N+2)(N+3))``. Returns ``(z, Φ(z))``; a small
    left-tail p means the spacings are more even than randomness allows
    (beaconing). The normal approximation is rough for small N — callers
    gate on a minimum interval count. Burstiness (right tail) is the
    frequency detector's territory, so it is deliberately not scored here.
    """
    n = n_spacings
    if n < 2:
        return 0.0, 1.0
    mean = 2.0 / (n + 1)
    var = 4.0 * (n - 1) / ((n + 1) ** 2 * (n + 2) * (n + 3))
    z = (g - mean) / math.sqrt(var)
    # Φ(z) via erfc — same no-scipy approach as _chi2_sf_df1.
    p = 0.5 * math.erfc(-z / math.sqrt(2.0))
    return z, p


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ValueFinding:
    """One rare/novel value returned by the value-novelty detector."""

    field: str
    value: str
    count: int
    # -log(count / total_events); higher = rarer.
    score: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class FreqFinding:
    """One anomalous time window returned by the frequency detector."""

    series_field: str
    series_value: str
    window_start: str
    window_end: str
    observed: int
    expected: float
    z_score: float
    # |z_score|; used for ranking.
    score: float
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class RangeFinding:
    """One out-of-range numeric value from the numeric-range detector."""

    field: str
    value: float
    count: int
    # excess distance beyond the band, normalized by band width.
    score: float
    direction: str  # "below" | "above"
    lower: float
    upper: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class CharsetFinding:
    """One value containing never-seen characters from the charset detector."""

    field: str
    value: str
    # Characters in the value that are outside the field's reference charset.
    novel_chars: list[str]
    count: int
    # Sum of per-novel-char surprise; higher = more/rarer novel characters.
    score: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class EntropyFinding:
    """One entropy-outlier value from the entropy detector."""

    field: str
    value: str
    # Shannon character entropy of the value, in bits.
    entropy: float
    count: int
    # excess distance beyond the entropy band, normalized by band width.
    score: float
    direction: str  # "below" | "above"
    lower: float
    upper: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class ShiftFinding:
    """One value whose share of events shifted between baseline and a suspect window."""

    field: str
    value: str
    # Occurrences in the suspect window; 0 = vanished (present only in baseline).
    count: int
    baseline_count: int
    # baseline_count / baseline window's event total.
    baseline_rate: float
    # count / suspect window's event total (0.5-smoothed only when count == 0).
    window_rate: float
    # window_rate / baseline_rate.
    rate_ratio: float
    direction: str  # "up" | "down"
    g_statistic: float
    p_value: float
    # Benjamini–Hochberg adjusted p-value across every test in this run.
    q_value: float
    # = g_statistic; used for ranking.
    score: float
    # First occurrence in the suspect window; None when vanished.
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class IntervalFinding:
    """One value whose arrival cadence changed between baseline and a suspect window."""

    field: str
    value: str
    # "missed" | "accelerated" (cadence break) | "new_regularity" (beaconing).
    direction: str
    # Occurrences in the suspect window; 0 = fully silent (maximal "missed").
    count: int
    baseline_count: int
    # Median inter-arrival delta (seconds) inside each window; None when the
    # window holds fewer than 2 occurrences of the value.
    baseline_median_interval: float | None
    window_median_interval: float | None
    # stddev/mean of the inter-arrival deltas; None when undefined (< 2 deltas).
    baseline_cv: float | None
    window_cv: float | None
    # Poisson-rate G (cadence break) or Greenwood G (new regularity).
    statistic: float
    p_value: float
    # Benjamini–Hochberg adjusted p-value across every test in this run.
    q_value: float
    # -log10(p_value); used for ranking across the two test families.
    score: float
    # First occurrence in the suspect window; None when fully silent.
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class NumericFieldInfo:
    """A numeric-parseable field candidate for the range detector."""

    token: str
    distinct: int
    coverage: float
    # fraction of non-empty values that parse as a number (0–1).
    numeric_ratio: float
    recommended: bool


@dataclass
class ComboFinding:
    """One rare/novel field *combination* from the value-combo detector."""

    fields: list[str]
    values: list[str]
    count: int
    # -log(count / total_events); higher = rarer.
    score: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class OrderFinding:
    """One out-of-order timestamp returned by the timestamp-order detector."""

    source_id: str
    event_id: str
    # Violating event's timestamp (ISO, UTC).
    timestamp: str
    # Previous record's timestamp in file/record order (ISO, UTC).
    prev_timestamp: str
    # prev_timestamp - timestamp, in seconds (always > 0 for a violation).
    skew_seconds: float
    byte_offset: int
    line_number: int
    # = skew_seconds; used for ranking.
    score: float
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class StatAnomalyResult:
    """Return value of statistical anomaly detection."""

    status: str  # "ok" | "no_data" | "insufficient_data"
    # "value_novelty" | "value_combo" | "frequency" | "timestamp_order" | "numeric_range"
    #  | "charset" | "entropy" | "proportion_shift" | "interval_periodicity"
    detector: str
    # "self-baseline" | "temporal" | "z-score" | "temporal-z-score" | "sequential"
    #  | "iqr" | "temporal-range" | "rare-chars" | "temporal-charset" | "temporal-iqr"
    #  | "g-test" | "cadence"
    method: str
    baseline_size: int  # total events (value_novelty) or event-count used for z-score
    results: list[
        ValueFinding
        | FreqFinding
        | OrderFinding
        | ComboFinding
        | RangeFinding
        | CharsetFinding
        | EntropyFinding
        | ShiftFinding
        | IntervalFinding
    ] = field(default_factory=list)
    # Effective |z| cutoff used by the frequency detector; None for value_novelty.
    z_threshold: float | None = None
    # Non-fatal caveats about the run (tiny suspect windows, unscoreable
    # windows, …). Surfaced verbatim to the analyst — findings are never
    # silently suppressed for statistical-quality reasons.
    warnings: list[str] = field(default_factory=list)
    # Serialized AnalysisWindows.payload() snapshot for temporal runs driven
    # by explicit windows; None for self-baseline runs.
    windows: dict[str, Any] | None = None


@dataclass
class NoveltyFieldInfo:
    """Field recommendation produced by :meth:`recommend_novelty_fields`.

    Used by the API to populate the field picker and frequency GROUP BY dropdown,
    and internally as the smart default for :meth:`find_value_novelty`.
    """

    token: str  # e.g. "artifact", "attr:status_code"
    distinct: int  # uniqExact() count
    coverage: float  # fraction of events with a non-empty value (0–1)
    kind: str  # "constant" | "identifier" | "categorical" | "sparse"
    recommended: bool  # True → include in default scan


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _col_expr(
    field_token: str,
    params: dict[str, Any],
    field_mappings: dict[str, list[str]] | None = None,
    prefix: str = "fk",
) -> str:
    """Return a ClickHouse SQL expression for a field token.

    Top-level columns (``"artifact"``, ``"display_name"``, …) are returned as-is,
    case/whitespace-insensitively, sharing the same allowlist `queries.py`
    uses for the events-view filter — a field like `parser_version` must
    resolve to the real column in both places, or a detector can silently
    score against an always-empty attribute lookup instead of the values the
    events view shows for that field. Attribute keys prefixed with
    ``"attr:"`` (``"attr:user_agent"``) or any other non-top-level token are
    returned as ``attributes[{prefix:String}]`` with the key injected into
    *params*. Single-field call sites use a fresh *params* dict per token, so
    the default ``"fk"`` name is safe; multi-field queries (value_combo) pass
    a distinct *prefix* per field (``fk0``, ``fk1``, …) to share one dict.

    ``field_mappings`` (issue #10): a token naming a canonical mapped field
    resolves to a coalesce over its raw attribute keys (parameter names
    ``{prefix}_m0..{prefix}_mN`` — same params-dict-uniqueness assumption).
    """
    mapped_raws = resolve_mapping(field_token, field_mappings)
    if mapped_raws:
        return mapping_coalesce_expr(mapped_raws, params, prefix)
    column, attr_key = resolve_column_token(field_token)
    if column is not None:
        return column
    params[prefix] = attr_key
    return f"attributes[{{{prefix}:String}}]"


def _apply_allowlist(findings: list[Any], allowlist: set[tuple[str, str]] | None) -> list[Any]:
    """Drop findings whose (allowlist_field, allowlist_value) key is allowlisted.

    Every value-shaped finding carries its own allowlist key in ``details``
    (``allowlist_field``/``allowlist_value``) so the suppression key is
    computed exactly once, here, and clients creating allowlist entries from
    a finding never re-derive it. Value-level, unlike the legacy per-event
    ``normal`` annotation: the same value is suppressed on every event.
    """
    if not allowlist:
        return findings
    return [
        f
        for f in findings
        if (f.details.get("allowlist_field"), f.details.get("allowlist_value")) not in allowlist
    ]


def _freq_finding(
    series_field: str,
    series_value: Any,
    bucket_dt: datetime,
    interval: int,
    cnt: int,
    mean_val: float,
    z: float,
    method: str,
    suspect_window: TimeWindow | None = None,
) -> FreqFinding:
    """Build a `FreqFinding` for one anomalous bucket.

    *suspect_window* attributes the bucket to a named suspect window in
    temporal mode (None in self-baseline mode).
    """
    window_end_dt = bucket_dt + timedelta(seconds=interval)
    details: dict[str, Any] = {
        "detector": "frequency",
        "method": method,
        "series_field": series_field,
        "series_value": str(series_value),
        "window_start": bucket_dt.isoformat(),
        "window_end": window_end_dt.isoformat(),
        "observed": cnt,
        "expected": round(mean_val, 2),
        "z_score": round(z, 4),
        "interval_seconds": interval,
        # A frequency allowlist entry keyed on the series value suppresses the
        # whole series ("known-noisy service") — the field is the series field.
        "allowlist_field": series_field,
        "allowlist_value": str(series_value),
    }
    if suspect_window is not None:
        details.update(
            {
                "suspect_window_label": suspect_window.label,
                "suspect_window_start": ensure_utc(suspect_window.start).isoformat(),
                "suspect_window_end": ensure_utc(suspect_window.end).isoformat(),
            }
        )
    return FreqFinding(
        series_field=series_field,
        series_value=str(series_value),
        window_start=bucket_dt.isoformat(),
        window_end=window_end_dt.isoformat(),
        observed=cnt,
        expected=round(mean_val, 2),
        z_score=round(z, 4),
        score=round(abs(z), 4),
        event_id=None,
        event=None,
        details=details,
    )


def _stub_event(evt_id: str | None, case_id: str, first_seen: str | None) -> dict[str, Any] | None:
    """Minimal full-shape event stub for a finding's representative event.

    The scan queries only aggregate ``argMin(event_id, ...)`` — fat columns
    (message, attributes) are deliberately not read there. Findings that
    survive ranking get their stub replaced by a fully hydrated event via
    ``StatisticalAnomalyService._hydrate_finding_events``; this stub is the
    fallback shape when hydration misses (e.g. a concurrent source delete).
    """
    if not evt_id:
        return None
    return {
        "event_id": evt_id,
        "case_id": case_id,
        "source_id": "",
        "message": "",
        "timestamp": first_seen,
        "timestamp_desc": None,
        "artifact": None,
        "artifact_long": None,
        "display_name": None,
        "tags": [],
        "attributes": {},
        "content_hash": "",
        "file_hash": "",
        "parser_name": "",
        "parser_version": "",
        "source_file": "",
        "byte_offset": None,
        "line_number": None,
        "embedding_model": None,
        "embedding_config_hash": None,
        "ingest_time": None,
    }


def _interval_window_block(row: tuple, w: int) -> dict[str, Any]:
    """Parse one window's aggregate block from an interval-periodicity scan row.

    Row layout: ``val`` at index 0, then 10 columns per window ``w``:
    ``n, k, mean, std, med, sum2, first, last, first_evt, last_evt``.
    ClickHouse returns NaN (not NULL) for ``avgIf``/``stddevSampIf``/
    ``quantileIf`` over an empty set and the type default (1970 epoch, empty
    id) for ``minIf``/``argMinIf`` — every derived number here is therefore
    gated on the interval count ``k`` (or ``n`` for first/last) instead of
    trusting the raw value. ``cv`` (stddev/mean) needs ≥ 2 deltas and a
    positive mean; ``span`` is the value's active first→last extent in
    seconds and needs ≥ 2 occurrences.
    """

    def _f(v: Any) -> float | None:
        if v is None:
            return None
        f = float(v)
        return None if math.isnan(f) else f

    o = 1 + w * 10
    n = int(row[o])
    k = int(row[o + 1])
    mean = _f(row[o + 2]) if k >= 1 else None
    std = _f(row[o + 3]) if k >= 2 else None
    med = _f(row[o + 4]) if k >= 1 else None
    sum2 = _f(row[o + 5]) or 0.0
    first = row[o + 6] if n >= 1 else None
    last = row[o + 7] if n >= 1 else None
    cv = round(std / mean, 4) if std is not None and mean is not None and mean > 0 else None
    span = None
    if n >= 2 and first is not None and last is not None:
        span = (ensure_utc(last) - ensure_utc(first)).total_seconds()
    return {
        "n": n,
        "k": k,
        "mean": mean,
        "std": std,
        "med": round(med, 4) if med is not None else None,
        "sum2": sum2,
        "first": first,
        "last": last,
        "first_evt": row[o + 8] if n >= 1 else None,
        "last_evt": row[o + 9] if n >= 1 else None,
        "cv": cv,
        "span": span,
    }


def _present_ts(value: Any) -> str | None:
    """Serialize a first-seen/anchor timestamp for API payloads.

    Maps falsy values and the no-timestamp storage sentinel (see
    `db/_dt.py`) to ``None`` so findings never surface the fake 2299 date —
    a group whose representative rows are all undated has no meaningful
    first-seen time.
    """
    if not value or is_null_ts_sentinel(value):
        return None
    return ensure_utc(value).isoformat()


# ---------------------------------------------------------------------------
# Field classification helper
# ---------------------------------------------------------------------------


def _classify_field(distinct: int, non_empty_count: int, total: int = 0) -> tuple[str, bool]:
    """Return (kind, recommended) for a field based on cardinality metrics.

    Parameters
    ----------
    distinct:
        ``uniqExact()`` — number of distinct non-empty values.
    non_empty_count:
        ``countIf(col != '')`` — events with a non-empty value.
    total:
        Total event count (used for sparse threshold).  When 0, sparse check is
        skipped.

    Classification rules (no content assumptions — purely numeric):

    * ``constant``   : distinct ≤ 1                           → not recommended
    * ``sparse``     : non_empty / total < 5 %                → not recommended
    * ``identifier`` : distinct / non_empty_count ≥ 0.9       → not recommended
      (hashes, UUIDs, free-text where nearly every value is unique)
    * ``categorical``: otherwise                               → recommended
    """
    if distinct <= 1:
        return "constant", False
    if total > 0 and non_empty_count / total < 0.05:
        return "sparse", False
    if non_empty_count > 0 and distinct / non_empty_count >= 0.9:
        return "identifier", False
    return "categorical", True


def _select_auto_scan_tokens(cats: list[str], ids: list[str]) -> list[str]:
    """Blend categorical and identifier field tokens under the auto-scan cap.

    ``cats`` and ``ids`` are each already ordered best-first. Identifier fields
    get up to ``_AUTO_IDENTIFIER_RESERVE`` reserved slots so they can't be
    crowded out by a wide categorical set; categoricals fill the remainder, and
    each kind backfills any slots the other leaves unused. Result length is at
    most ``_MAX_AUTO_SCAN_FIELDS``. Mirrored by the frontend picker's
    ``selectAutoScanTokens`` (detector-shared.tsx) so the "auto" preview matches
    what actually runs.
    """
    reserve = min(len(ids), _AUTO_IDENTIFIER_RESERVE)
    picked = cats[: _MAX_AUTO_SCAN_FIELDS - reserve]
    picked += ids[: _MAX_AUTO_SCAN_FIELDS - len(picked)]
    if len(picked) < _MAX_AUTO_SCAN_FIELDS:
        # Identifiers left slack (fewer than reserved) — backfill with any
        # remaining categoricals.
        picked += [t for t in cats if t not in picked][: _MAX_AUTO_SCAN_FIELDS - len(picked)]
    return picked


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StatisticalAnomalyService:
    """Statistical anomaly detection over ClickHouse event fields.

    Operates purely on ingested events — no embeddings required.
    """

    def __init__(self, clickhouse: ClickHouseStore | None = None) -> None:
        self.ch = clickhouse or ClickHouseStore()

    # ------------------------------------------------------------------
    # Field recommendation
    # ------------------------------------------------------------------

    def _count_events(self, case_id: str, source_ids: list[str]) -> int:
        """Return the total event count for a case/source scope."""
        self.ch.init_schema()
        db = self.ch.database
        total_res = self.ch.client.query(
            f"SELECT count() FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)",
            parameters={"cid": case_id, "src": source_ids},
        )
        return int(total_res.result_rows[0][0]) if total_res.result_rows else 0

    def _hydrate_finding_events(
        self,
        case_id: str,
        source_ids: list[str],
        findings: list[Any],
    ) -> None:
        """Replace findings' stub events with fully hydrated rows, in one batch.

        Called on the final (post-suppression, post-limit) finding slice, so
        at most ``limit`` events are fetched — the detector scans themselves
        only aggregate ``argMin(event_id, ...)`` and never read the fat
        message/attributes columns. Findings whose id is missing from the
        result keep their :func:`_stub_event` fallback.
        """
        ids = [f.event_id for f in findings if f.event_id]
        if not ids:
            return
        by_id = self.ch.get_events_by_ids(case_id, source_ids, ids)
        for f in findings:
            if f.event_id and f.event_id in by_id:
                f.event = by_id[f.event_id]

    def _finalize_findings(
        self,
        findings: list[Any],
        *,
        detector: str,
        method: str,
        total_events: int,
        evaluated_fields: int,
        exclude_event_ids: set[str] | None,
        limit: int,
        case_id: str,
        source_ids: list[str],
        allowlist: set[tuple[str, str]] | None = None,
        warnings: list[str] | None = None,
        windows: AnalysisWindows | None = None,
    ) -> StatAnomalyResult:
        """Rank, suppress, cap and hydrate a per-field detector's findings.

        Shared tail for the field-scanning detectors: when no field yielded a
        usable baseline the status is ``insufficient_data``; otherwise
        allowlisted values and excluded (user-marked-normal) events are
        dropped, findings are sorted by score descending, capped to
        ``limit``, and their representative events are hydrated in one batch
        before returning an ``ok`` result.
        """
        windows_payload = windows.payload() if windows is not None else None
        if evaluated_fields == 0:
            return StatAnomalyResult(
                status="insufficient_data",
                detector=detector,
                method=method,
                baseline_size=total_events,
                warnings=warnings or [],
                windows=windows_payload,
            )
        findings = _apply_allowlist(findings, allowlist)
        if exclude_event_ids:
            findings = [
                f for f in findings if not f.event_id or f.event_id not in exclude_event_ids
            ]
        findings.sort(key=lambda f: f.score, reverse=True)
        results = findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector=detector,
            method=method,
            baseline_size=total_events,
            results=results,
            warnings=warnings or [],
            windows=windows_payload,
        )

    def field_inventory(
        self,
        case_id: str,
        source_ids: list[str],
        total: int | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> tuple[list[tuple[str, int, int]], int]:
        """Return ``((token, distinct, non_empty_count), ...), total`` for candidate fields.

        The raw, unclassified field enumeration shared by
        :py:meth:`recommend_novelty_fields` and the Visualization page's
        field picker: a curated list of categorical top-level columns
        (``artifact``, ``timestamp_desc``, ``display_name``, ``parser_name``)
        plus every attribute key found in the events table (as ``attr:<key>``
        tokens). Order: top-level columns in candidate order, then attribute
        keys by non-empty count descending — callers apply their own ranking.

        *total*, the event count callers use as the coverage denominator, is
        queried internally when omitted — pass it when the caller already has
        it to avoid a redundant identical round-trip.
        """
        self.ch.init_schema()
        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        if total is None:
            total = self._count_events(case_id, source_ids)
        if total == 0:
            return [], 0

        inventory: list[tuple[str, int, int]] = []

        # -- Top-level columns (batched in a single aggregation) ---------------
        agg_parts = []
        for col in _NOVELTY_CANDIDATE_TOP_LEVEL:
            agg_parts.append(f"uniqExact({col}) AS {col}_dist, countIf({col} != '') AS {col}_cov")
        top_sql = (
            f"SELECT {', '.join(agg_parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" {HEAVY_SCAN_SETTINGS}"
        )
        top_res = self.ch.client.query(top_sql, parameters=params)
        if top_res.result_rows:
            row = top_res.result_rows[0]
            for i, col in enumerate(_NOVELTY_CANDIDATE_TOP_LEVEL):
                inventory.append((col, int(row[i * 2]), int(row[i * 2 + 1])))

        # -- Attribute keys (ARRAY JOIN to enumerate + aggregate in one pass) --
        #
        # Memory-safety is deliberate here: the paired keys/values ARRAY JOIN
        # avoids re-materializing the whole map per expanded row (the former
        # ``attributes[key]`` lookup kept the full map column alive in every
        # expanded row and OOM-killed the server on wide sources), the
        # ``val != ''`` pre-filter shrinks the expansion before GROUP BY
        # (empties count as absent everywhere in novelty scoring anyway), and
        # ``uniq`` (approximate, ~1% error) replaces ``uniqExact`` — the
        # cardinality classification thresholds don't need exactness, and
        # exact per-key hash sets over near-unique values are the other
        # memory blowup. HEAVY_SCAN_SETTINGS (external GROUP BY spill + a
        # query memory cap) bounds the worst case instead of trusting the
        # server-wide limit.
        attr_sql = f"""
            SELECT
                key,
                uniq(val)  AS dist,
                count()    AS cov_count
            FROM {db}.events
            ARRAY JOIN mapKeys(attributes) AS key, mapValues(attributes) AS val
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND val != ''
            GROUP BY key
            ORDER BY cov_count DESC
            LIMIT {{max_keys:UInt32}}
            {HEAVY_SCAN_SETTINGS}
        """
        attr_res = self.ch.client.query(
            attr_sql, parameters={**params, "max_keys": _RECOMMENDER_MAX_ATTR_KEYS}
        )
        mapped_raws = {r for raws in (field_mappings or {}).values() for r in raws}
        for key, dist, cov_count in attr_res.result_rows:
            if key in mapped_raws:
                continue  # replaced by the canonical entry below
            inventory.append((f"attr:{key}", int(dist), int(cov_count)))

        if field_mappings:
            inventory.extend(self.canonical_inventory(case_id, source_ids, field_mappings))

        return inventory, total

    def canonical_inventory(
        self,
        case_id: str,
        source_ids: list[str],
        field_mappings: dict[str, list[str]],
    ) -> list[tuple[str, int, int]]:
        """Exact ``(canonical, distinct, coverage)`` aggregates for mapped fields.

        Canonical mapped fields (issue #10) aggregate over the coalesce
        expression, all canonicals batched into one round-trip — summing the
        per-raw-key numbers would double-count events that carry several of
        the raw keys. This is the one inventory piece that stays a live query
        even when the rest comes from the per-source stats cache
        (``db/field_stats.py``): per-source counts cannot dedupe those events.
        """
        self.ch.init_schema()
        db = self.ch.database
        m_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        m_parts = []
        canonicals = list(field_mappings.items())
        for i, (_, raws) in enumerate(canonicals):
            expr = mapping_coalesce_expr(raws, m_params, f"inv{i}")
            m_parts.append(f"uniqExact({expr}) AS d{i}, countIf({expr} != '') AS c{i}")
        m_sql = (
            f"SELECT {', '.join(m_parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" {HEAVY_SCAN_SETTINGS}"
        )
        m_res = self.ch.client.query(m_sql, parameters=m_params)
        out: list[tuple[str, int, int]] = []
        if m_res.result_rows:
            row = m_res.result_rows[0]
            for i, (canonical, _) in enumerate(canonicals):
                out.append((canonical, int(row[i * 2]), int(row[i * 2 + 1])))
        return out

    def recommend_novelty_fields(
        self,
        case_id: str,
        source_ids: list[str],
        total: int | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
    ) -> list[NoveltyFieldInfo]:
        """Return a ranked, annotated list of candidate fields for value_novelty.

        Fields are classified by cardinality *without* any content heuristics so
        the result is valid for any timeseries type (nginx, Windows events, …):

        * ``constant``   — distinct ≤ 1 value; no signal.
        * ``identifier`` — near-unique (distinct / non-empty ≥ 0.9); hashes, IDs,
          free-text messages.  Not useful for grouping.
        * ``sparse``     — non-empty coverage < 5 % of events.  Low signal.
        * ``categorical``— recommended; moderate cardinality with decent coverage.

        The candidate set comes from :py:meth:`field_inventory`; this method
        only layers the novelty classification and ranking on top.

        *total*, the event count used as the coverage denominator, is queried
        internally when omitted — pass it when the caller already has it
        (e.g. ``find_value_novelty``'s auto-field-selection path) to avoid a
        redundant identical round-trip.

        *inventory* lets callers supply a pre-merged candidate list (the
        per-source stats cache, ``db/field_stats.py``) instead of the live
        :py:meth:`field_inventory` scan; *total* must be passed with it.
        """
        if inventory is None:
            inventory, total = self.field_inventory(case_id, source_ids, total, field_mappings)
        elif total is None:
            raise ValueError("total is required when inventory is supplied")
        if total == 0:
            return []

        findings: list[NoveltyFieldInfo] = []
        for token, dist, cov_count in inventory:
            kind, recommended = _classify_field(dist, cov_count, total)
            if token in _SYNTHETIC_FIELDS:
                recommended = False
            findings.append(
                NoveltyFieldInfo(
                    token=token,
                    distinct=dist,
                    coverage=round(cov_count / total, 4),
                    kind=kind,
                    recommended=recommended,
                )
            )

        # Sort: recommended first, then by coverage descending.
        findings.sort(key=lambda f: (not f.recommended, -f.coverage))
        return findings

    # ------------------------------------------------------------------
    # Timeline range helper
    # ------------------------------------------------------------------

    def get_timeline_range(
        self,
        case_id: str,
        source_ids: list[str],
    ) -> tuple[datetime | None, datetime | None]:
        """Return the (min, max) UTC timestamp of the timeline, or (None, None)."""
        self.ch.init_schema()
        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        return query_timestamp_range(
            self.ch.client,
            db,
            "case_id = {cid:String} AND has({src:Array(String)}, source_id)",
            params,
        )

    def get_timeline_midpoint(
        self,
        case_id: str,
        source_ids: list[str],
    ) -> datetime | None:
        """Return the midpoint timestamp of the timeline, or None if no events."""
        min_dt, max_dt = self.get_timeline_range(case_id, source_ids)
        if min_dt is None or max_dt is None:
            return None
        return min_dt + (max_dt - min_dt) / 2

    # ------------------------------------------------------------------
    # Value / combo novelty
    # ------------------------------------------------------------------

    def _window_totals(
        self, case_id: str, source_ids: list[str], windows: AnalysisWindows
    ) -> tuple[int, list[int]]:
        """Return ``(baseline_total, [suspect_total, ...])`` event counts.

        One round-trip; these are the per-window surprise denominators (the
        old whole-corpus denominator overstated rarity whenever the windows
        covered only part of the timeline).
        """
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        bp, sps = _window_preds(windows, params)
        parts = [f"countIf({bp}) AS bl_total"] + [
            f"countIf({sp}) AS w{i}_total" for i, sp in enumerate(sps)
        ]
        sql = (
            f"SELECT {', '.join(parts)}"
            f" FROM {self.ch.database}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" AND {TS_NOT_SENTINEL_SQL}"
        )
        rows = self.ch.client.query(sql, parameters=params).result_rows
        if not rows:
            return 0, [0] * len(windows.suspects)
        row = rows[0]
        return int(row[0]), [int(v) for v in row[1:]]

    def find_value_novelty(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        rarity_floor: int = 3,
        windows: AnalysisWindows | None = None,
        per_field_limit: int = 25,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        inventory_total: int | None = None,
    ) -> StatAnomalyResult:
        """Return rare or first-seen values per field, ranked by surprise score.

        *fields* is a list of field tokens (``"artifact"``, ``"timestamp_desc"``,
        ``"attr:user_agent"``).  When ``None``, the cardinality-based recommender
        selects useful attribute fields automatically (falls back to
        ``_DEFAULT_NOVELTY_FIELDS`` only if recommendation yields nothing).

        *inventory* / *inventory_total* let async callers pass a pre-merged
        field candidate list from the per-source stats cache
        (``db/field_stats.py::merged_inventory``) so the ``fields is None``
        path skips the live :py:meth:`field_inventory` scan — the map-scanning
        query family that can be very expensive on wide sources. The cache
        approximates merged ``distinct`` as max-across-sources, which is fine
        for the recommender's coarse cardinality classification. Ignored when
        *fields* is given.

        In *self-baseline* mode values appearing ≤ *rarity_floor* times are
        flagged, scored ``-log(count / total_events)``.  In *temporal* mode
        (*windows* provided) any value absent from the baseline window but
        present in a suspect window is flagged, once per suspect window it
        appears in, scored against that window's own event count
        (``-log(w_cnt / window_total)``); events outside every window are
        ignored. *allowlist* suppresses findings whose (field, value) an
        analyst declared never-anomalous.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "self-baseline" if windows is None else "temporal"

        # Total event count for surprise score denominator.
        total_events = self._count_events(case_id, source_ids)

        if fields is not None:
            scan_fields = fields
        else:
            # Auto-discover useful fields for this specific timeseries. Pass
            # the total we already have — recommend_novelty_fields would
            # otherwise re-run the exact same count() query. A supplied cache
            # inventory keeps its own total: coverage counts in it were
            # computed against the cache's event totals, not _count_events.
            rec = self.recommend_novelty_fields(
                case_id,
                source_ids,
                total=inventory_total if inventory is not None else total_events,
                field_mappings=field_mappings,
                inventory=inventory,
            )
            scan_fields = [f.token for f in rec if f.recommended] or _DEFAULT_NOVELTY_FIELDS
            # Each field below is a separate sequential ClickHouse round-trip
            # (no cross-field batching); cap how many an auto-selected set can
            # trigger per call. recommend_novelty_fields already sorts
            # recommended fields by coverage descending, so this keeps the
            # most useful ones.
            scan_fields = scan_fields[:_MAX_AUTO_SCAN_FIELDS]

        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="value_novelty",
                method=method,
                baseline_size=0,
            )

        # Per-window event totals (surprise denominators) — temporal only.
        baseline_size = total_events
        suspect_totals: list[int] = []
        run_warnings: list[str] = []
        if windows is not None:
            baseline_size, suspect_totals = self._window_totals(case_id, source_ids, windows)
            run_warnings = _window_size_warnings(windows, suspect_totals)

        all_findings: list[ValueFinding] = []

        for field_token in scan_fields:
            params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, params, field_mappings)

            if windows is None:
                # Self-baseline: flag values with count ≤ rarity_floor.
                params["floor"] = rarity_floor
                params["lim"] = per_field_limit
                sql = f"""
                    SELECT
                        {col} AS val,
                        count() AS cnt,
                        min(timestamp) AS first_seen,
                        toString(argMin(event_id, timestamp)) AS evt_id
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {col} != ''
                    GROUP BY val
                    HAVING cnt <= {{floor:UInt32}}
                    ORDER BY cnt ASC, first_seen ASC
                    LIMIT {{lim:UInt32}}
                    {HEAVY_SCAN_SETTINGS}
                """
            else:
                # Temporal: flag values absent from the baseline window but
                # present in a suspect window — one countIf/minIf/argMinIf
                # block per suspect window, so a single scan yields per-window
                # counts and representatives. The WHERE union restricts the
                # scan to the windows (events between/outside windows are
                # ignored by construction).
                bp, sps = _window_preds(windows, params)
                params["lim"] = per_field_limit
                w_blocks = ",\n                        ".join(
                    f"countIf({sp}) AS w{i}_cnt,"
                    f" minIf(timestamp, {sp}) AS w{i}_first,"
                    f" toString(argMinIf(event_id, timestamp, {sp})) AS w{i}_evt"
                    for i, sp in enumerate(sps)
                )
                w_sum = " + ".join(f"w{i}_cnt" for i in range(len(sps)))
                union_pred = " OR ".join([bp, *sps])
                sql = f"""
                    SELECT
                        {col} AS val,
                        countIf({bp}) AS baseline_cnt,
                        {w_blocks}
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {col} != ''
                      AND {TS_NOT_SENTINEL_SQL}
                      AND ({union_pred})
                    GROUP BY val
                    HAVING baseline_cnt = 0 AND ({w_sum}) > 0
                    ORDER BY ({w_sum}) ASC
                    LIMIT {{lim:UInt32}}
                    {HEAVY_SCAN_SETTINGS}
                """

            rows = self.ch.client.query(sql, parameters=params).result_rows

            for row in rows:
                if windows is None:
                    val, cnt, first_seen, evt_id = row
                    if not val:
                        continue
                    all_findings.append(
                        self._novelty_finding(
                            case_id,
                            field_token,
                            str(val),
                            int(cnt),
                            total_events,
                            first_seen,
                            evt_id,
                            method,
                        )
                    )
                else:
                    val = row[0]
                    if not val:
                        continue
                    # One finding per (value, suspect window with cnt > 0):
                    # window attribution is part of the claim being made.
                    for i, w in enumerate(windows.suspects):
                        w_cnt = int(row[2 + i * 3])
                        if w_cnt <= 0:
                            continue
                        first_seen, evt_id = row[3 + i * 3], row[4 + i * 3]
                        finding = self._novelty_finding(
                            case_id,
                            field_token,
                            str(val),
                            w_cnt,
                            suspect_totals[i] if i < len(suspect_totals) else 0,
                            first_seen,
                            evt_id,
                            method,
                        )
                        finding.details.update(
                            {
                                "baseline_size": baseline_size,
                                "window_label": w.label,
                                "window_start": ensure_utc(w.start).isoformat(),
                                "window_end": ensure_utc(w.end).isoformat(),
                                "window_total_events": suspect_totals[i]
                                if i < len(suspect_totals)
                                else 0,
                            }
                        )
                        all_findings.append(finding)

        # Value-level allowlist first, then the legacy per-event suppression.
        all_findings = _apply_allowlist(all_findings, allowlist)
        if exclude_event_ids:
            all_findings = [
                f for f in all_findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        # Sort by surprise descending (rarest first), apply global limit,
        # then hydrate only the surviving findings' representative events.
        all_findings.sort(key=lambda f: f.score, reverse=True)
        results = all_findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector="value_novelty",
            method=method,
            baseline_size=baseline_size,
            results=results,
            warnings=run_warnings,
            windows=windows.payload() if windows is not None else None,
        )

    def _novelty_finding(
        self,
        case_id: str,
        field_token: str,
        val: str,
        count: int,
        denominator: int,
        first_seen: Any,
        evt_id: Any,
        method: str,
    ) -> ValueFinding:
        """Build one value_novelty finding with its surprise score.

        *denominator* is the corpus size in self-baseline mode and the
        suspect window's own event count in temporal mode.
        """
        # `+ 0.0` normalizes IEEE -0.0 (count == denominator ⇒ -log(1)) to +0.0.
        score = (-math.log(count / denominator) if count > 0 and denominator > 0 else 0.0) + 0.0
        # min(timestamp)/minIf(...) return a native DateTime (not a
        # ClickHouse-formatted string) so we can attach an explicit UTC
        # offset before serializing — a bare "YYYY-MM-DD HH:MM:SS" string is
        # ambiguous to JS's Date parser (browsers treat it as local time),
        # which silently shifted the histogram markers and event-grid anomaly
        # matching by the browser's UTC offset.
        first_seen_str = _present_ts(first_seen)
        evt_id_str = str(evt_id) if evt_id else None
        return ValueFinding(
            field=field_token,
            value=val,
            count=count,
            score=round(score, 4),
            first_seen=first_seen_str,
            event_id=evt_id_str,
            event=_stub_event(evt_id_str, case_id, first_seen_str),
            details={
                "detector": "value_novelty",
                "method": method,
                "field": field_token,
                "value": val,
                "count": count,
                "total_events": denominator,
                "surprise": round(score, 4),
                "allowlist_field": field_token,
                "allowlist_value": val,
            },
        )

    def find_value_combos(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        rarity_floor: int = 3,
        windows: AnalysisWindows | None = None,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> StatAnomalyResult:
        """Return rare or first-seen *combinations* of field values.

        The multi-field extension of :meth:`find_value_novelty`: instead of
        grouping by a single field, group by two or more field expressions
        together (``GROUP BY expr0, expr1, ...``) and score each surviving
        combination by the same surprise formula.

        *fields* must name at least two field tokens. When ``None``, the top
        two highest-coverage recommended fields are used as a single tuple —
        no pairwise enumeration (that would be one ClickHouse round-trip per
        pair and a result set no analyst can triage).

        Modes mirror :meth:`find_value_novelty`: *self-baseline* flags
        combinations appearing ≤ *rarity_floor* times in the whole corpus;
        *temporal* (*windows* provided) flags combinations absent from the
        baseline window but present in a suspect window, once per suspect
        window, scored against that window's event count. The allowlist key
        for a combo is the ``COMBO_FIELD_SEP``-joined field tokens and
        ``COMBO_VALUE_SEP``-joined values (see the finding's
        ``allowlist_field``/``allowlist_value`` details).

        Raises:
            ValueError: if fewer than two fields are resolved.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "self-baseline" if windows is None else "temporal"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="value_combo",
                method=method,
                baseline_size=0,
            )

        # Field resolution (auto mode) issues live queries — only after the
        # empty-corpus short-circuit above.
        if fields is not None:
            combo_fields = fields
        else:
            rec = self.recommend_novelty_fields(
                case_id, source_ids, total=total_events, field_mappings=field_mappings
            )
            combo_fields = [f.token for f in rec if f.recommended][:2]

        if len(combo_fields) < 2:
            if fields is not None:
                raise ValueError("value_combo requires at least two fields")
            # Auto mode couldn't find two useful fields to combine.
            return StatAnomalyResult(
                status="insufficient_data",
                detector="value_combo",
                method=method,
                baseline_size=total_events,
            )

        # Build one expression per field into a single shared params dict —
        # distinct prefixes (fk0, fk1, …) keep the attribute-key bind params
        # from colliding (see _col_expr's `prefix`).
        params: dict[str, Any] = {**base_params}
        exprs = [
            _col_expr(tok, params, field_mappings, prefix=f"fk{i}")
            for i, tok in enumerate(combo_fields)
        ]
        val_cols = ", ".join(f"{expr} AS v{i}" for i, expr in enumerate(exprs))
        group_by = ", ".join(f"v{i}" for i in range(len(exprs)))
        non_empty = " AND ".join(f"{expr} != ''" for expr in exprs)

        # Per-window event totals (surprise denominators) — temporal only.
        baseline_size = total_events
        suspect_totals: list[int] = []
        run_warnings: list[str] = []
        if windows is not None:
            baseline_size, suspect_totals = self._window_totals(case_id, source_ids, windows)
            run_warnings = _window_size_warnings(windows, suspect_totals)

        if windows is None:
            params["floor"] = rarity_floor
            params["lim"] = limit
            sql = f"""
                SELECT
                    {val_cols},
                    count() AS cnt,
                    min(timestamp) AS first_seen,
                    toString(argMin(event_id, timestamp)) AS evt_id
                FROM {db}.events
                WHERE case_id = {{cid:String}}
                  AND has({{src:Array(String)}}, source_id)
                  AND {non_empty}
                GROUP BY {group_by}
                HAVING cnt <= {{floor:UInt32}}
                ORDER BY cnt ASC, first_seen ASC
                LIMIT {{lim:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """
        else:
            bp, sps = _window_preds(windows, params)
            params["lim"] = limit
            w_blocks = ",\n                    ".join(
                f"countIf({sp}) AS w{i}_cnt,"
                f" minIf(timestamp, {sp}) AS w{i}_first,"
                f" toString(argMinIf(event_id, timestamp, {sp})) AS w{i}_evt"
                for i, sp in enumerate(sps)
            )
            w_sum = " + ".join(f"w{i}_cnt" for i in range(len(sps)))
            union_pred = " OR ".join([bp, *sps])
            sql = f"""
                SELECT
                    {val_cols},
                    countIf({bp}) AS baseline_cnt,
                    {w_blocks}
                FROM {db}.events
                WHERE case_id = {{cid:String}}
                  AND has({{src:Array(String)}}, source_id)
                  AND {non_empty}
                  AND {TS_NOT_SENTINEL_SQL}
                  AND ({union_pred})
                GROUP BY {group_by}
                HAVING baseline_cnt = 0 AND ({w_sum}) > 0
                ORDER BY ({w_sum}) ASC
                LIMIT {{lim:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """

        rows = self.ch.client.query(sql, parameters=params).result_rows
        n_fields = len(exprs)

        def _combo_finding(
            values: list[str], cnt: int, denominator: int, first_seen: Any, evt_id: Any
        ) -> ComboFinding:
            score = (-math.log(cnt / denominator) if cnt > 0 and denominator > 0 else 0.0) + 0.0
            first_seen_str = _present_ts(first_seen)
            evt_id_str = str(evt_id) if evt_id else None
            return ComboFinding(
                fields=list(combo_fields),
                values=values,
                count=cnt,
                score=round(score, 4),
                first_seen=first_seen_str,
                event_id=evt_id_str,
                event=_stub_event(evt_id_str, case_id, first_seen_str),
                details={
                    "detector": "value_combo",
                    "method": method,
                    "fields": combo_fields,
                    "values": values,
                    "count": cnt,
                    "total_events": denominator,
                    "surprise": round(score, 4),
                    "allowlist_field": COMBO_FIELD_SEP.join(combo_fields),
                    "allowlist_value": COMBO_VALUE_SEP.join(values),
                },
            )

        all_findings: list[ComboFinding] = []
        for row in rows:
            values = [str(v) for v in row[:n_fields]]
            if any(v == "" for v in values):
                continue
            if windows is None:
                cnt = int(row[n_fields])
                first_seen, evt_id = row[n_fields + 1 : n_fields + 3]
                all_findings.append(_combo_finding(values, cnt, total_events, first_seen, evt_id))
            else:
                # row: values..., baseline_cnt, then (cnt, first, evt) per window.
                for i, w in enumerate(windows.suspects):
                    w_cnt = int(row[n_fields + 1 + i * 3])
                    if w_cnt <= 0:
                        continue
                    first_seen = row[n_fields + 2 + i * 3]
                    evt_id = row[n_fields + 3 + i * 3]
                    w_total = suspect_totals[i] if i < len(suspect_totals) else 0
                    finding = _combo_finding(values, w_cnt, w_total, first_seen, evt_id)
                    finding.details.update(
                        {
                            "baseline_size": baseline_size,
                            "window_label": w.label,
                            "window_start": ensure_utc(w.start).isoformat(),
                            "window_end": ensure_utc(w.end).isoformat(),
                            "window_total_events": w_total,
                        }
                    )
                    all_findings.append(finding)

        all_findings = _apply_allowlist(all_findings, allowlist)
        if exclude_event_ids:
            all_findings = [
                f for f in all_findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        all_findings.sort(key=lambda f: f.score, reverse=True)
        results = all_findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector="value_combo",
            method=method,
            baseline_size=baseline_size,
            results=results,
            warnings=run_warnings,
            windows=windows.payload() if windows is not None else None,
        )

    # ------------------------------------------------------------------
    # Numeric range violations
    # ------------------------------------------------------------------

    def recommend_numeric_fields(
        self,
        case_id: str,
        source_ids: list[str],
        total: int | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        min_ratio: float = _MIN_NUMERIC_RATIO,
    ) -> list[NumericFieldInfo]:
        """Return fields whose values parse as numbers, for the range detector.

        Candidates come from the field inventory (top-level columns + attribute
        keys, or a supplied *inventory* — the per-source stats cache); the ones
        with non-trivial coverage and cardinality are probed with a single
        batched query computing, per field, the fraction of non-empty values
        that ``toFloat64OrNull`` parses. Fields at or above *min_ratio* are
        marked recommended. Type detection is purely syntactic — a field of
        HTTP status codes qualifies, but so would any numeric-looking id, which
        is why the UI leans on temporal mode for those.
        """
        if inventory is None:
            inventory, total = self.field_inventory(case_id, source_ids, total, field_mappings)
        elif total is None:
            raise ValueError("total is required when inventory is supplied")
        if not total:
            return []

        # Keep fields with ≥5% coverage and more than one distinct value, cap 15.
        candidates = [
            (tok, dist, cov) for tok, dist, cov in inventory if cov / total >= 0.05 and dist > 1
        ][:_MAX_AUTO_SCAN_FIELDS]
        if not candidates:
            return []

        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        parts = []
        exprs = []
        for i, (tok, _, _) in enumerate(candidates):
            expr = _col_expr(tok, params, field_mappings, prefix=f"nf{i}")
            exprs.append(expr)
            parts.append(
                f"countIf(toFloat64OrNull({expr}) IS NOT NULL) AS num{i}, "
                f"countIf({expr} != '') AS ne{i}"
            )
        probe_sql = (
            f"SELECT {', '.join(parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" {HEAVY_SCAN_SETTINGS}"
        )
        row = self.ch.client.query(probe_sql, parameters=params).result_rows
        out: list[NumericFieldInfo] = []
        if row:
            r = row[0]
            for i, (tok, dist, cov) in enumerate(candidates):
                num = int(r[i * 2])
                ne = int(r[i * 2 + 1])
                ratio = num / ne if ne else 0.0
                out.append(
                    NumericFieldInfo(
                        token=tok,
                        distinct=dist,
                        coverage=round(cov / total, 4),
                        numeric_ratio=round(ratio, 4),
                        recommended=ratio >= min_ratio,
                    )
                )
        out.sort(key=lambda f: (not f.recommended, -f.numeric_ratio, -f.coverage))
        return out

    def find_range_violations(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        per_field_limit: int = 25,
        windows: AnalysisWindows | None = None,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> StatAnomalyResult:
        """Return numeric values falling outside a field's learned range.

        For each numeric field, learn a baseline band and flag values outside
        it. *self-baseline* (``method="iqr"``) uses the Tukey fence
        ``[q1 − 1.5·IQR, q3 + 1.5·IQR]`` over the whole corpus; *temporal*
        (``method="temporal-range"``, *windows* provided) learns exact
        min/max from the baseline window and flags values inside the suspect
        windows that fall outside it — findings carry which suspect window
        the value appeared in, and events outside every window are ignored.

        When *fields* is ``None`` the numeric-field recommender selects
        candidates automatically. A field with fewer than ``_MIN_RANGE_BASELINE``
        numeric baseline samples is skipped; when every scanned field is skipped
        the status is ``insufficient_data``.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "iqr" if windows is None else "temporal-range"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="numeric_range",
                method=method,
                baseline_size=0,
            )

        if fields is not None:
            scan_fields = fields
        else:
            rec = self.recommend_numeric_fields(
                case_id, source_ids, total=total_events, field_mappings=field_mappings
            )
            scan_fields = [f.token for f in rec if f.recommended][:_MAX_AUTO_SCAN_FIELDS]

        all_findings: list[RangeFinding] = []
        evaluated_fields = 0

        for field_token in scan_fields:
            # --- Learn the band from the baseline. ---
            stat_params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, stat_params, field_mappings)
            num_src = (
                f"SELECT toFloat64OrNull({col}) AS num, timestamp"
                f" FROM {db}.events"
                f" WHERE case_id = {{cid:String}}"
                f" AND has({{src:Array(String)}}, source_id)"
                f" AND {col} != ''"
            )
            if windows is None:
                stat_sql = (
                    f"SELECT quantile(0.25)(num) AS q1, quantile(0.75)(num) AS q3, count() AS n"
                    f" FROM ({num_src}) WHERE num IS NOT NULL {HEAVY_SCAN_SETTINGS}"
                )
            else:
                stat_bp, _ = _window_preds(windows, stat_params)
                stat_sql = (
                    f"SELECT min(num) AS lo, max(num) AS hi, count() AS n"
                    f" FROM ({num_src}) WHERE num IS NOT NULL AND {stat_bp}"
                    f" {HEAVY_SCAN_SETTINGS}"
                )
            srows = self.ch.client.query(stat_sql, parameters=stat_params).result_rows
            if not srows or srows[0][2] is None:
                continue
            a, b, n = srows[0]
            if int(n) < _MIN_RANGE_BASELINE or a is None or b is None:
                continue

            if windows is None:
                q1, q3 = float(a), float(b)
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                band_extra: dict[str, Any] = {
                    "q1": round(q1, 4),
                    "q3": round(q3, 4),
                    "iqr": round(iqr, 4),
                }
            else:
                lower, upper = float(a), float(b)
                band_extra = {"baseline_min": round(lower, 4), "baseline_max": round(upper, 4)}

            # A degenerate (zero-width) band would divide the score by zero and
            # flag every off-band value with infinite severity — floor the width.
            width = max(upper - lower, 1e-9)
            evaluated_fields += 1

            # --- Flag values outside the band. ---
            viol_params: dict[str, Any] = {**base_params}
            vcol = _col_expr(field_token, viol_params, field_mappings)
            viol_params["lo"] = lower
            viol_params["hi"] = upper
            viol_params["plim"] = per_field_limit
            win_idx_col = ""
            win_idx_group = ""
            detect_clause = ""
            if windows is not None:
                _, viol_sps = _window_preds(windows, viol_params)
                # Restrict the scan to the suspect-window union (events outside
                # every window are ignored), tag each row with its window index
                # for attribution, and exclude sentinel/undated rows — the
                # year-2299 sentinel would otherwise land in any open-ended
                # window. Matches the sentinel guard value_combo/frequency apply.
                win_idx_col = f", {_suspect_multiif(viol_sps)} AS win_idx"
                win_idx_group = ", win_idx"
                detect_clause = f" AND ({' OR '.join(viol_sps)}) AND {TS_NOT_SENTINEL_SQL}"
            viol_sql = f"""
                SELECT
                    num AS val,
                    count() AS cnt,
                    min(timestamp) AS first_seen,
                    toString(argMin(event_id, timestamp)) AS evt_id{win_idx_group}
                FROM (
                    SELECT toFloat64OrNull({vcol}) AS num, timestamp, event_id{win_idx_col}
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {vcol} != ''{detect_clause}
                )
                WHERE num IS NOT NULL AND (num < {{lo:Float64}} OR num > {{hi:Float64}})
                GROUP BY val{win_idx_group}
                ORDER BY greatest({{lo:Float64}} - val, val - {{hi:Float64}}) DESC, first_seen ASC
                LIMIT {{plim:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """
            vrows = self.ch.client.query(viol_sql, parameters=viol_params).result_rows

            for vrow in vrows:
                if windows is None:
                    val, cnt, first_seen, evt_id = vrow
                    window: TimeWindow | None = None
                else:
                    val, cnt, first_seen, evt_id, win_idx = vrow
                    wi = int(win_idx)
                    window = windows.suspects[wi] if 0 <= wi < len(windows.suspects) else None
                if val is None:
                    continue
                val_f = float(val)
                direction = "below" if val_f < lower else "above"
                excess = (lower - val_f) if val_f < lower else (val_f - upper)
                score = round(excess / width, 4)
                first_seen_str = _present_ts(first_seen)
                evt_id_str = str(evt_id) if evt_id else None
                mini_event = _stub_event(evt_id_str, case_id, first_seen_str)

                details: dict[str, Any] = {
                    "detector": "numeric_range",
                    "method": method,
                    "field": field_token,
                    "value": val_f,
                    "count": int(cnt),
                    "lower": round(lower, 4),
                    "upper": round(upper, 4),
                    "direction": direction,
                    "excess": round(excess, 4),
                    "baseline_n": int(n),
                    "allowlist_field": field_token,
                    "allowlist_value": str(val_f),
                    **band_extra,
                }
                if window is not None:
                    details.update(
                        {
                            "window_label": window.label,
                            "window_start": ensure_utc(window.start).isoformat(),
                            "window_end": ensure_utc(window.end).isoformat(),
                        }
                    )
                all_findings.append(
                    RangeFinding(
                        field=field_token,
                        value=val_f,
                        count=int(cnt),
                        score=score,
                        direction=direction,
                        lower=round(lower, 4),
                        upper=round(upper, 4),
                        first_seen=first_seen_str,
                        event_id=evt_id_str,
                        event=mini_event,
                        details=details,
                    )
                )

        if evaluated_fields == 0:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="numeric_range",
                method=method,
                baseline_size=total_events,
                windows=windows.payload() if windows is not None else None,
            )

        all_findings = _apply_allowlist(all_findings, allowlist)
        if exclude_event_ids:
            all_findings = [
                f for f in all_findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        all_findings.sort(key=lambda f: f.score, reverse=True)
        results = all_findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector="numeric_range",
            method=method,
            baseline_size=total_events,
            results=results,
            windows=windows.payload() if windows is not None else None,
        )

    # ------------------------------------------------------------------
    # Charset novelty
    # ------------------------------------------------------------------

    def _auto_string_fields(
        self,
        case_id: str,
        source_ids: list[str],
        total_events: int,
        field_mappings: dict[str, list[str]] | None,
        inventory: list[tuple[str, int, int]] | None,
        inventory_total: int | None,
    ) -> list[str]:
        """Auto-select string fields for the charset/entropy detectors.

        Unlike value_novelty's default (categorical only), identifier-kind
        fields are kept: near-unique values (URLs, filenames, user agents,
        hashes) are exactly where injected metacharacters and random-looking
        strings appear. Constant and sparse fields stay excluded, as do
        pipeline-synthesized fields.

        ``recommend_novelty_fields`` sorts categorical (recommended) fields
        ahead of identifier fields, so a naive ``[:N]`` slice would starve the
        identifier fields — the detectors' *primary* target — on wide sources
        with many categorical columns. A quota reserves up to
        ``_AUTO_IDENTIFIER_RESERVE`` of the cap for identifier fields; the rest
        goes to categoricals, and either kind backfills any slack the other
        leaves. The frontend picker mirrors this rule (see selectAutoScanTokens
        in detector-shared.tsx) so its "auto" preview matches what runs.
        """
        rec = self.recommend_novelty_fields(
            case_id,
            source_ids,
            total=inventory_total if inventory is not None else total_events,
            field_mappings=field_mappings,
            inventory=inventory,
        )
        cats = [
            f.token for f in rec if f.kind == "categorical" and f.token not in _SYNTHETIC_FIELDS
        ]
        ids = [f.token for f in rec if f.kind == "identifier" and f.token not in _SYNTHETIC_FIELDS]
        return _select_auto_scan_tokens(cats, ids)

    def find_charset_novelty(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        per_field_limit: int = 25,
        rarity_floor: int = 3,
        windows: AnalysisWindows | None = None,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        inventory_total: int | None = None,
    ) -> StatAnomalyResult:
        """Return values containing characters outside a field's reference charset.

        Per field, learn a reference character set and flag values in the
        detect scope containing characters outside it (null bytes, unicode
        homoglyphs, injection metacharacters — detected purely syntactically).
        AMiner ``CharsetDetector``. Character sets are computed over *distinct
        values* (not rows), so a character carried by one hot value counts
        once.

        Two modes:

        * *self-baseline* (``method="rare-chars"``) — the whole-corpus charset
          trivially contains every character, so instead a character appearing
          in ≤ ``rarity_floor`` distinct values is *rare*; the reference set is
          all non-rare characters and any value containing a rare character is
          flagged (mirrors the IQR fallback the numeric_range detector uses
          for the same degeneracy).
        * *temporal* (``method="temporal-charset"``, *windows* provided) —
          reference set = every character seen in baseline-window values;
          suspect-window values containing characters absent from it are
          flagged, attributed to the suspect window they appear in. Events
          outside every window are ignored.

        Score = Σ over the value's novel characters of ``-log(n_vals_with_char
        / n_distinct_values)`` (self mode; temporal uses ``log(n_distinct + 1)``
        per never-seen character) — same surprise family as value_novelty.

        Fields with fewer than ``_MIN_CHARSET_BASELINE`` distinct baseline
        values or a reference charset larger than ``_MAX_CHARSET_SIZE`` are
        skipped; when every scanned field skips the status is
        ``insufficient_data``.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "rare-chars" if windows is None else "temporal-charset"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="charset",
                method=method,
                baseline_size=0,
            )

        if fields is not None:
            scan_fields = fields
        else:
            scan_fields = self._auto_string_fields(
                case_id, source_ids, total_events, field_mappings, inventory, inventory_total
            )

        all_findings: list[CharsetFinding] = []
        evaluated_fields = 0

        for field_token in scan_fields:
            # --- Learn the reference charset. ---
            char_counts: dict[str, int] = {}
            if windows is None:
                # Per-character distinct-value counts over the whole corpus,
                # plus the total distinct-value count in the same scan: a window
                # `count() OVER ()` over the DISTINCT subquery yields n_vals on
                # every row, so we avoid a second whole-corpus uniqExact scan of
                # the identical column/predicate.
                cc_params: dict[str, Any] = {**base_params}
                col = _col_expr(field_token, cc_params, field_mappings)
                cc_sql = f"""
                    SELECT c, count() AS n_vals_with_c, any(total) AS n_vals
                    FROM (
                        SELECT
                            count() OVER () AS total,
                            arrayDistinct(extractAll(val, '(?s).')) AS chars
                        FROM (
                            SELECT DISTINCT {col} AS val
                            FROM {db}.events
                            WHERE case_id = {{cid:String}}
                              AND has({{src:Array(String)}}, source_id)
                              AND {col} != ''
                        )
                    )
                    ARRAY JOIN chars AS c
                    GROUP BY c
                    {HEAVY_SCAN_SETTINGS}
                """
                cc_rows = self.ch.client.query(cc_sql, parameters=cc_params).result_rows
                char_counts = {str(c): int(nv) for c, nv, _ in cc_rows}
                n_vals = int(cc_rows[0][2]) if cc_rows else 0
                reference = [c for c, nv in char_counts.items() if nv > rarity_floor]
                # Skip decision is about the field's *whole* alphabet, not just
                # its non-rare subset: on a huge-alphabet field (CJK prose,
                # base64 blobs) most characters are rare, so `reference` stays
                # small while the real alphabet is enormous — "novel character"
                # is meaningless there. Measure `char_counts`, which holds every
                # character seen (temporal mode's `reference` already is the
                # full baseline alphabet).
                alphabet_size = len(char_counts)
            else:
                # Charset of the baseline window (a bounded range now, so the
                # year-2299 sentinel can never fall inside it).
                bs_params: dict[str, Any] = {**base_params}
                col = _col_expr(field_token, bs_params, field_mappings)
                bs_bp, _ = _window_preds(windows, bs_params)
                bs_sql = f"""
                    SELECT
                        groupUniqArrayArray(arrayDistinct(extractAll(val, '(?s).'))) AS charset,
                        count() AS n_vals
                    FROM (
                        SELECT DISTINCT {col} AS val
                        FROM {db}.events
                        WHERE case_id = {{cid:String}}
                          AND has({{src:Array(String)}}, source_id)
                          AND {col} != ''
                          AND {bs_bp}
                    )
                    {HEAVY_SCAN_SETTINGS}
                """
                bs_rows = self.ch.client.query(bs_sql, parameters=bs_params).result_rows
                if not bs_rows:
                    continue
                charset_arr, n_vals = bs_rows[0]
                n_vals = int(n_vals)
                reference = [str(c) for c in (charset_arr or [])]
                alphabet_size = len(reference)

            if n_vals < _MIN_CHARSET_BASELINE or alphabet_size > _MAX_CHARSET_SIZE:
                continue
            evaluated_fields += 1

            # --- Flag values containing characters outside the reference set. ---
            viol_params: dict[str, Any] = {**base_params}
            vcol = _col_expr(field_token, viol_params, field_mappings)
            viol_params["base"] = reference
            viol_params["plim"] = per_field_limit
            win_idx_sel = ""
            win_idx_group = ""
            detect_clause = ""
            if windows is not None:
                _, viol_sps = _window_preds(windows, viol_params)
                # Restrict the scan to the suspect-window union, tag rows with
                # their window index for attribution, and exclude sentinel/
                # undated rows (the year-2299 sentinel would land in any
                # open-ended range test).
                win_idx_sel = f", {_suspect_multiif(viol_sps)} AS win_idx"
                win_idx_group = ", win_idx"
                detect_clause = f" AND ({' OR '.join(viol_sps)}) AND {TS_NOT_SENTINEL_SQL}"
            viol_sql = f"""
                SELECT val, novel, cnt, first_seen, evt_id{win_idx_group}
                FROM (
                    SELECT
                        val,
                        arrayFilter(
                            c -> NOT has({{base:Array(String)}}, c),
                            arrayDistinct(extractAll(val, '(?s).'))
                        ) AS novel,
                        cnt, first_seen, evt_id{win_idx_group}
                    FROM (
                        SELECT
                            {vcol} AS val,
                            count() AS cnt,
                            min(timestamp) AS first_seen,
                            toString(argMin(event_id, timestamp)) AS evt_id{win_idx_sel}
                        FROM {db}.events
                        WHERE case_id = {{cid:String}}
                          AND has({{src:Array(String)}}, source_id)
                          AND {vcol} != ''{detect_clause}
                        GROUP BY val{win_idx_group}
                    )
                )
                WHERE length(novel) > 0
                ORDER BY length(novel) DESC, cnt ASC
                LIMIT {{plim:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """
            vrows = self.ch.client.query(viol_sql, parameters=viol_params).result_rows

            for vrow in vrows:
                if windows is None:
                    val, novel, cnt, first_seen, evt_id = vrow
                    window: TimeWindow | None = None
                else:
                    val, novel, cnt, first_seen, evt_id, win_idx = vrow
                    wi = int(win_idx)
                    window = windows.suspects[wi] if 0 <= wi < len(windows.suspects) else None
                novel_chars = [str(c) for c in (novel or [])]
                if not val or not novel_chars:
                    continue
                score = 0.0
                for c in novel_chars:
                    nv_c = char_counts.get(c, 0)
                    if nv_c > 0 and n_vals > 0:
                        score += -math.log(nv_c / n_vals)
                    else:
                        # Never seen in the baseline: +1-smoothed surprise.
                        score += math.log(n_vals + 1)
                first_seen_str = _present_ts(first_seen)
                evt_id_str = str(evt_id) if evt_id else None
                mini_event = _stub_event(evt_id_str, case_id, first_seen_str)

                details: dict[str, Any] = {
                    "detector": "charset",
                    "method": method,
                    "field": field_token,
                    "value": str(val),
                    "novel_chars": novel_chars,
                    "codepoints": [f"U+{ord(c):04X}" for c in novel_chars if len(c) == 1],
                    "count": int(cnt),
                    "baseline_distinct_values": n_vals,
                    "allowlist_field": field_token,
                    "allowlist_value": str(val),
                }
                if windows is None:
                    details["rarity_floor"] = rarity_floor
                    details["char_value_counts"] = {c: char_counts.get(c, 0) for c in novel_chars}
                if window is not None:
                    details.update(
                        {
                            "window_label": window.label,
                            "window_start": ensure_utc(window.start).isoformat(),
                            "window_end": ensure_utc(window.end).isoformat(),
                        }
                    )
                all_findings.append(
                    CharsetFinding(
                        field=field_token,
                        value=str(val),
                        novel_chars=novel_chars,
                        count=int(cnt),
                        score=round(score, 4),
                        first_seen=first_seen_str,
                        event_id=evt_id_str,
                        event=mini_event,
                        details=details,
                    )
                )

        return self._finalize_findings(
            all_findings,
            detector="charset",
            method=method,
            total_events=total_events,
            evaluated_fields=evaluated_fields,
            exclude_event_ids=exclude_event_ids,
            limit=limit,
            case_id=case_id,
            source_ids=source_ids,
            allowlist=allowlist,
            windows=windows,
        )

    # ------------------------------------------------------------------
    # Value entropy outliers
    # ------------------------------------------------------------------

    def find_entropy_outliers(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        per_field_limit: int = 25,
        windows: AnalysisWindows | None = None,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        inventory_total: int | None = None,
    ) -> StatAnomalyResult:
        """Return values whose Shannon character entropy falls outside a learned band.

        Per field, compute the character entropy (bits) of each *distinct
        value* and compare it against the field's baseline entropy
        distribution via a Tukey fence ``[q1 − 1.5·IQR, q3 + 1.5·IQR]``.
        Values above the band look random (DGA domains, encoded payloads,
        keys); values below it look degenerate (padding, repeated-character
        stuffing). AMiner ``EntropyDetector`` — entropy is a property of the
        characters, never of what the value means.

        Two modes: *self-baseline* (``method="iqr"``) computes the fence over
        the whole corpus's per-distinct-value entropies (unlike an exact
        min/max, quartiles are not degenerate over their own population);
        *temporal* (``method="temporal-iqr"``, *windows* provided) learns the
        fence from the baseline window and flags only suspect-window values,
        attributed to the window they appear in; events outside every window
        are ignored.

        Entropies are weighted per distinct value, not per row — one hot
        value repeated millions of times cannot drag the band toward itself.
        Values shorter than ``_MIN_ENTROPY_VALUE_LEN`` codepoints are excluded
        from baseline and detection alike. A field with fewer than
        ``_MIN_ENTROPY_BASELINE`` qualifying baseline values is skipped; when
        every scanned field skips the status is ``insufficient_data``.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "iqr" if windows is None else "temporal-iqr"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="entropy",
                method=method,
                baseline_size=0,
            )

        if fields is not None:
            scan_fields = fields
        else:
            scan_fields = self._auto_string_fields(
                case_id, source_ids, total_events, field_mappings, inventory, inventory_total
            )

        # Shannon character entropy in bits (log2) per distinct value, via
        # ClickHouse's built-in `entropy` aggregate over the value's characters
        # ARRAY JOIN-ed out one row each. This is linear in the value length;
        # the earlier `arrayMap(c -> countEqual(chars, c), ...)` form rescanned
        # the whole char array once per distinct character (quadratic).
        all_findings: list[EntropyFinding] = []
        evaluated_fields = 0

        for field_token in scan_fields:
            # --- Learn the entropy band from the baseline. ---
            stat_params: dict[str, Any] = {**base_params, "minlen": _MIN_ENTROPY_VALUE_LEN}
            col = _col_expr(field_token, stat_params, field_mappings)
            baseline_clause = ""
            if windows is not None:
                stat_bp, _ = _window_preds(windows, stat_params)
                # The baseline window is a bounded range, so the year-2299
                # sentinel can never fall inside it.
                baseline_clause = f" AND {stat_bp}"
            stat_sql = f"""
                SELECT quantile(0.25)(ent) AS q1, quantile(0.75)(ent) AS q3, count() AS n
                FROM (
                    SELECT entropy(c) AS ent
                    FROM (
                        SELECT val, arrayJoin(extractAll(val, '(?s).')) AS c
                        FROM (
                            SELECT DISTINCT {col} AS val
                            FROM {db}.events
                            WHERE case_id = {{cid:String}}
                              AND has({{src:Array(String)}}, source_id)
                              AND {col} != ''
                              AND lengthUTF8({col}) >= {{minlen:UInt32}}{baseline_clause}
                        )
                    )
                    GROUP BY val
                )
                {HEAVY_SCAN_SETTINGS}
            """
            srows = self.ch.client.query(stat_sql, parameters=stat_params).result_rows
            if not srows or srows[0][2] is None:
                continue
            q1, q3, n = srows[0]
            if int(n) < _MIN_ENTROPY_BASELINE or q1 is None or q3 is None:
                continue

            q1, q3 = float(q1), float(q3)
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            # Degenerate (zero-width) band → same width floor as numeric_range.
            width = max(upper - lower, 1e-9)
            evaluated_fields += 1

            # --- Flag values whose entropy falls outside the band. ---
            viol_params: dict[str, Any] = {**base_params, "minlen": _MIN_ENTROPY_VALUE_LEN}
            vcol = _col_expr(field_token, viol_params, field_mappings)
            viol_params["lo"] = lower
            viol_params["hi"] = upper
            viol_params["plim"] = per_field_limit
            win_idx_sel = ""
            win_idx_group = ""
            detect_clause = ""
            if windows is not None:
                _, viol_sps = _window_preds(windows, viol_params)
                # Restrict the scan to the suspect-window union, tag each row
                # with its window index for attribution, and exclude sentinel/
                # undated rows (year-2299 sentinel would land in any open range).
                win_idx_sel = f", {_suspect_multiif(viol_sps)} AS win_idx"
                win_idx_group = ", win_idx"
                detect_clause = f" AND ({' OR '.join(viol_sps)}) AND {TS_NOT_SENTINEL_SQL}"
            viol_sql = f"""
                SELECT val, ent, cnt, first_seen, evt_id{win_idx_group}
                FROM (
                    SELECT
                        val,
                        entropy(c) AS ent,
                        any(cnt) AS cnt,
                        any(first_seen) AS first_seen,
                        any(evt_id) AS evt_id{win_idx_group}
                    FROM (
                        SELECT val, cnt, first_seen, evt_id{win_idx_group},
                               arrayJoin(extractAll(val, '(?s).')) AS c
                        FROM (
                            SELECT
                                {vcol} AS val,
                                count() AS cnt,
                                min(timestamp) AS first_seen,
                                toString(argMin(event_id, timestamp)) AS evt_id{win_idx_sel}
                            FROM {db}.events
                            WHERE case_id = {{cid:String}}
                              AND has({{src:Array(String)}}, source_id)
                              AND {vcol} != ''
                              AND lengthUTF8({vcol}) >= {{minlen:UInt32}}{detect_clause}
                            GROUP BY val{win_idx_group}
                        )
                    )
                    GROUP BY val{win_idx_group}
                )
                WHERE ent < {{lo:Float64}} OR ent > {{hi:Float64}}
                ORDER BY greatest({{lo:Float64}} - ent, ent - {{hi:Float64}}) DESC, first_seen ASC
                LIMIT {{plim:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """
            vrows = self.ch.client.query(viol_sql, parameters=viol_params).result_rows

            for vrow in vrows:
                if windows is None:
                    val, ent, cnt, first_seen, evt_id = vrow
                    window: TimeWindow | None = None
                else:
                    val, ent, cnt, first_seen, evt_id, win_idx = vrow
                    wi = int(win_idx)
                    window = windows.suspects[wi] if 0 <= wi < len(windows.suspects) else None
                if not val or ent is None:
                    continue
                ent_f = float(ent)
                direction = "below" if ent_f < lower else "above"
                excess = (lower - ent_f) if ent_f < lower else (ent_f - upper)
                score = round(excess / width, 4)
                first_seen_str = _present_ts(first_seen)
                evt_id_str = str(evt_id) if evt_id else None
                mini_event = _stub_event(evt_id_str, case_id, first_seen_str)

                details: dict[str, Any] = {
                    "detector": "entropy",
                    "method": method,
                    "field": field_token,
                    "value": str(val),
                    "entropy": round(ent_f, 4),
                    "count": int(cnt),
                    "lower": round(lower, 4),
                    "upper": round(upper, 4),
                    "direction": direction,
                    "excess": round(excess, 4),
                    "q1": round(q1, 4),
                    "q3": round(q3, 4),
                    "iqr": round(iqr, 4),
                    "baseline_n": int(n),
                    "allowlist_field": field_token,
                    "allowlist_value": str(val),
                }
                if window is not None:
                    details.update(
                        {
                            "window_label": window.label,
                            "window_start": ensure_utc(window.start).isoformat(),
                            "window_end": ensure_utc(window.end).isoformat(),
                        }
                    )
                all_findings.append(
                    EntropyFinding(
                        field=field_token,
                        value=str(val),
                        entropy=round(ent_f, 4),
                        count=int(cnt),
                        score=score,
                        direction=direction,
                        lower=round(lower, 4),
                        upper=round(upper, 4),
                        first_seen=first_seen_str,
                        event_id=evt_id_str,
                        event=mini_event,
                        details=details,
                    )
                )

        return self._finalize_findings(
            all_findings,
            detector="entropy",
            method=method,
            total_events=total_events,
            evaluated_fields=evaluated_fields,
            allowlist=allowlist,
            windows=windows,
            exclude_event_ids=exclude_event_ids,
            limit=limit,
            case_id=case_id,
            source_ids=source_ids,
        )

    # ------------------------------------------------------------------
    # Proportion shift (G-test)
    # ------------------------------------------------------------------

    def find_proportion_shifts(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        windows: AnalysisWindows | None = None,
        fdr_q: float = 0.05,
        min_ratio: float = 2.0,
        max_candidates_per_field: int = 2000,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        inventory_total: int | None = None,
    ) -> StatAnomalyResult:
        """Return values whose share of events shifted between baseline and suspect windows.

        Per (field, value, suspect window), a 2×2 G-test (log-likelihood
        ratio) compares the value's share of the baseline window's events
        against its share of the suspect window's events. All tests in the
        run — every field × candidate value × suspect window — share one
        Benjamini–Hochberg FDR pool; a finding needs ``q_value ≤ fdr_q``
        *and* a share change of at least *min_ratio*× in either direction
        (``direction`` = "up"/"down"). A value present in the baseline but
        absent from a suspect window is a maximal "down" (rate ratio via
        Haldane–Anscombe +0.5 smoothing; the test itself uses raw counts, and
        its representative event is the value's last baseline occurrence).

        Temporal-only: without *windows* the result is ``insufficient_data``
        — a share can only shift between two populations. First-seen values
        (``baseline_cnt = 0``) are excluded by construction; temporal
        value_novelty owns those. The per-field candidate set is capped at
        *max_candidates_per_field* rows (highest total volume first); hitting
        the cap understates the FDR pool for that field, which is surfaced as
        a warning rather than silently accepted. Score = the G statistic.
        """
        method = "g-test"
        if windows is None:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="proportion_shift",
                method=method,
                baseline_size=0,
                warnings=[
                    "proportion_shift is temporal-only — select or create a baseline "
                    "definition (baseline + suspect windows)."
                ],
            )

        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="proportion_shift",
                method=method,
                baseline_size=0,
                windows=windows.payload(),
            )

        baseline_size, suspect_totals = self._window_totals(case_id, source_ids, windows)
        run_warnings = _window_size_warnings(windows, suspect_totals)
        if baseline_size == 0:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="proportion_shift",
                method=method,
                baseline_size=0,
                warnings=[*run_warnings, "The baseline window contains no events."],
                windows=windows.payload(),
            )

        if fields is not None:
            scan_fields = fields
        else:
            # Same auto-selection as value_novelty: categorical fields only —
            # near-unique identifier fields have no repeating shares to test.
            rec = self.recommend_novelty_fields(
                case_id,
                source_ids,
                total=inventory_total if inventory is not None else total_events,
                field_mappings=field_mappings,
                inventory=inventory,
            )
            scan_fields = [f.token for f in rec if f.recommended] or _DEFAULT_NOVELTY_FIELDS
            scan_fields = scan_fields[:_MAX_AUTO_SCAN_FIELDS]

        # Phase 1: fetch per-(field, value) window counts from ClickHouse.
        # Candidates ordered by total volume descending — a power-based,
        # direction-neutral ordering, so the cap drops only the low-volume
        # values that lacked the power to reach significance anyway.
        candidates: list[tuple[str, str, int, Any, Any, list[tuple[int, Any, Any]]]] = []
        evaluated_fields = 0
        for field_token in scan_fields:
            params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, params, field_mappings)
            bp, sps = _window_preds(windows, params)
            params["cap"] = max_candidates_per_field
            w_blocks = ",\n                    ".join(
                f"countIf({sp}) AS w{i}_cnt,"
                f" minIf(timestamp, {sp}) AS w{i}_first,"
                f" toString(argMinIf(event_id, timestamp, {sp})) AS w{i}_evt"
                for i, sp in enumerate(sps)
            )
            w_sum = " + ".join(f"w{i}_cnt" for i in range(len(sps)))
            union_pred = " OR ".join([bp, *sps])
            # `bp` in the WHERE union keeps baseline-only (vanished) values in
            # the result; `HAVING baseline_cnt >= 1` is the only prune — it is
            # definitional (first-seen belongs to value_novelty), so the BH
            # test count stays honest.
            sql = f"""
                SELECT
                    {col} AS val,
                    countIf({bp}) AS baseline_cnt,
                    maxIf(timestamp, {bp}) AS bl_last,
                    toString(argMaxIf(event_id, timestamp, {bp})) AS bl_evt,
                    {w_blocks}
                FROM {db}.events
                WHERE case_id = {{cid:String}}
                  AND has({{src:Array(String)}}, source_id)
                  AND {col} != ''
                  AND {TS_NOT_SENTINEL_SQL}
                  AND ({union_pred})
                GROUP BY val
                HAVING baseline_cnt >= 1
                ORDER BY (baseline_cnt + {w_sum}) DESC, val ASC
                LIMIT {{cap:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """
            rows = self.ch.client.query(sql, parameters=params).result_rows
            if not rows:
                continue
            evaluated_fields += 1
            if len(rows) >= max_candidates_per_field:
                run_warnings.append(
                    f"Field {field_token!r} hit the {max_candidates_per_field}-value "
                    f"candidate cap — the FDR correction covers only the "
                    f"{max_candidates_per_field} highest-volume values; treat marginal "
                    f"q-values for this field as exploratory."
                )
            for row in rows:
                val = row[0]
                if not val:
                    continue
                per_window = [
                    (int(row[4 + i * 3]), row[5 + i * 3], row[6 + i * 3]) for i in range(len(sps))
                ]
                candidates.append((field_token, str(val), int(row[1]), row[2], row[3], per_window))

        # Phase 2: one G-test per (candidate value, non-empty suspect window),
        # all pooled into a single BH-FDR correction for the run.
        tests: list[tuple[int, int, float, float]] = []  # (cand_idx, win_idx, g, p)
        for ci, (_, _, a, _, _, per_window) in enumerate(candidates):
            for wi, (c, _, _) in enumerate(per_window):
                n_w = suspect_totals[wi] if wi < len(suspect_totals) else 0
                if n_w <= 0:
                    continue
                g = _g_statistic(a, baseline_size - a, c, n_w - c)
                tests.append((ci, wi, g, _chi2_sf_df1(g)))
        qvals = _bh_qvalues([t[3] for t in tests])
        m_tests = len(tests)

        # Phase 3: gate on FDR + effect floor and build findings.
        findings: list[ShiftFinding] = []
        for (ci, wi, g, p), q in zip(tests, qvals, strict=True):
            if q > fdr_q:
                continue
            field_token, val, a, bl_last, bl_evt, per_window = candidates[ci]
            c, w_first, w_evt = per_window[wi]
            n_w = suspect_totals[wi]
            rate_bl = a / baseline_size
            # Haldane–Anscombe +0.5 smoothing for the *ratio only* when the
            # value vanished — the test above always used the raw counts.
            rate_w = c / n_w if c > 0 else 0.5 / n_w
            ratio = rate_w / rate_bl
            if 1.0 / min_ratio < ratio < min_ratio:
                continue
            direction = "up" if rate_w > rate_bl else "down"
            window = windows.suspects[wi]
            first_seen_str = _present_ts(w_first) if c > 0 else None
            evt_id = w_evt if c > 0 else bl_evt
            evt_id_str = str(evt_id) if evt_id else None
            details: dict[str, Any] = {
                "detector": "proportion_shift",
                "method": method,
                "field": field_token,
                "value": val,
                "baseline_count": a,
                "baseline_total": baseline_size,
                "count": c,
                "window_total_events": n_w,
                "baseline_rate": round(rate_bl, 6),
                "window_rate": round(rate_w, 6),
                "rate_ratio": round(ratio, 4),
                "direction": direction,
                "g_statistic": round(g, 4),
                "p_value": round(p, 6),
                "q_value": round(q, 6),
                "m_tests": m_tests,
                "q_threshold": fdr_q,
                "min_ratio": min_ratio,
                "window_label": window.label,
                "window_start": ensure_utc(window.start).isoformat(),
                "window_end": ensure_utc(window.end).isoformat(),
                "baseline_size": baseline_size,
                "allowlist_field": field_token,
                "allowlist_value": val,
            }
            if c == 0:
                details["last_seen_baseline"] = _present_ts(bl_last)
            findings.append(
                ShiftFinding(
                    field=field_token,
                    value=val,
                    count=c,
                    baseline_count=a,
                    baseline_rate=round(rate_bl, 6),
                    window_rate=round(rate_w, 6),
                    rate_ratio=round(ratio, 4),
                    direction=direction,
                    g_statistic=round(g, 4),
                    p_value=round(p, 6),
                    q_value=round(q, 6),
                    score=round(g, 4),
                    first_seen=first_seen_str,
                    event_id=evt_id_str,
                    event=_stub_event(evt_id_str, case_id, first_seen_str),
                    details=details,
                )
            )

        return self._finalize_findings(
            findings,
            detector="proportion_shift",
            method=method,
            total_events=baseline_size,
            evaluated_fields=evaluated_fields,
            exclude_event_ids=exclude_event_ids,
            limit=limit,
            case_id=case_id,
            source_ids=source_ids,
            allowlist=allowlist,
            warnings=run_warnings,
            windows=windows,
        )

    # ------------------------------------------------------------------
    # Interval periodicity (cadence)
    # ------------------------------------------------------------------

    def find_interval_periodicity(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        windows: AnalysisWindows | None = None,
        fdr_q: float = 0.05,
        min_rate_ratio: float = 2.0,
        min_baseline_intervals: int = 5,
        cv_regular_max: float = 0.5,
        cv_irregular_min: float = 0.8,
        beacon_min_intervals: int = 10,
        beacon_cv_max: float = 0.3,
        beacon_min_span: float = 0.5,
        max_candidates_per_field: int = 2000,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        inventory_total: int | None = None,
    ) -> StatAnomalyResult:
        """Return values whose arrival cadence changed between baseline and suspect windows.

        Per (field, value), inter-arrival deltas are computed strictly within
        each window (lag partitioned by ``(value, window)``, so a delta can
        never straddle a window boundary). Two disjoint directions gated on
        the *baseline* delta CV (stddev/mean):

        * cadence break (baseline-regular values, CV ≤ *cv_regular_max* with
          ≥ *min_baseline_intervals* deltas): a two-sample Poisson-rate
          likelihood-ratio test of the value's arrival rate, window durations
          as exposures. ``count = 0`` is the maximal "missed" — per-value
          silence — and its representative event is the last baseline
          occurrence. Conservative under true periodicity (sub-Poisson count
          variance). Effect floor: the rate must change ≥ *min_rate_ratio*×.
        * new regularity / beaconing (baseline-bursty values, CV ≥
          *cv_irregular_min*, or sparse baselines below the interval
          minimum): Greenwood's spacing statistic over the suspect window's
          deltas, left tail ("too even to be random"), needing
          ≥ *beacon_min_intervals* deltas. Effect floors: window CV ≤
          *beacon_cv_max* and active span ≥ *beacon_min_span* of the window.

        The CV dead band between the two gates gets no test. All tests share
        one Benjamini–Hochberg pool (``q_value ≤ fdr_q``). Temporal-only
        (``method="cadence"``); first-seen values are excluded by
        construction (``HAVING baseline_cnt >= 1``) — temporal value_novelty
        owns those. The per-field candidate set is capped at
        *max_candidates_per_field* (highest total volume first) with the
        same warning semantics as proportion_shift. Score = ``-log10(p)``.
        """
        method = "cadence"
        if windows is None:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="interval_periodicity",
                method=method,
                baseline_size=0,
                warnings=[
                    "interval_periodicity is temporal-only — select or create a baseline "
                    "definition (baseline + suspect windows)."
                ],
            )

        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="interval_periodicity",
                method=method,
                baseline_size=0,
                windows=windows.payload(),
            )

        baseline_size, suspect_totals = self._window_totals(case_id, source_ids, windows)
        run_warnings = _window_size_warnings(windows, suspect_totals)
        if baseline_size == 0:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="interval_periodicity",
                method=method,
                baseline_size=0,
                warnings=[*run_warnings, "The baseline window contains no events."],
                windows=windows.payload(),
            )

        if fields is not None:
            scan_fields = fields
        else:
            # Same auto-selection as proportion_shift: categorical fields only —
            # near-unique identifier fields have no repeating arrivals to time.
            rec = self.recommend_novelty_fields(
                case_id,
                source_ids,
                total=inventory_total if inventory is not None else total_events,
                field_mappings=field_mappings,
                inventory=inventory,
            )
            scan_fields = [f.token for f in rec if f.recommended] or _DEFAULT_NOVELTY_FIELDS
            scan_fields = scan_fields[:_MAX_AUTO_SCAN_FIELDS]

        # Window durations in seconds — the Poisson-rate test's exposures.
        d_b = (windows.baseline.end - windows.baseline.start).total_seconds()
        d_ws = [(w.end - w.start).total_seconds() for w in windows.suspects]
        n_wins = 1 + len(windows.suspects)

        # Phase 1: per (field, value), per-window delta aggregates from
        # ClickHouse. The lag partition is (value, window index) — the window
        # index is materialized via arrayJoin over the window predicates
        # (overlapping suspect windows duplicate the row per window, which is
        # exactly right: each window's cadence is judged independently), so a
        # delta can never span two windows. Candidates ordered by total
        # volume descending, same power-based cap rationale as
        # proportion_shift.
        candidates: list[tuple[str, str, list[dict[str, Any]]]] = []
        evaluated_fields = 0
        for field_token in scan_fields:
            params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, params, field_mappings)
            bp, sps = _window_preds(windows, params)
            params["cap"] = max_candidates_per_field
            win_exprs = ", ".join(
                [f"if({bp}, 0, -1)"] + [f"if({sp}, {i + 1}, -1)" for i, sp in enumerate(sps)]
            )
            w_blocks = ",\n                    ".join(
                f"countIf(win = {w}) AS w{w}_n,"
                f" countIf(win = {w} AND isNotNull(delta)) AS w{w}_k,"
                f" avgIf(delta, win = {w} AND isNotNull(delta)) AS w{w}_mean,"
                f" stddevSampIf(delta, win = {w} AND isNotNull(delta)) AS w{w}_std,"
                f" quantileIf(0.5)(delta, win = {w} AND isNotNull(delta)) AS w{w}_med,"
                f" sumIf(delta * delta, win = {w} AND isNotNull(delta)) AS w{w}_sum2,"
                f" minIf(ts, win = {w}) AS w{w}_first,"
                f" maxIf(ts, win = {w}) AS w{w}_last,"
                f" toString(argMinIf(event_id, ts, win = {w})) AS w{w}_first_evt,"
                f" toString(argMaxIf(event_id, ts, win = {w})) AS w{w}_last_evt"
                for w in range(n_wins)
            )
            n_sum = " + ".join(f"w{w}_n" for w in range(n_wins))
            union_pred = " OR ".join([bp, *sps])
            # `bp` in the WHERE union keeps baseline-only (silent) values in
            # the result; `HAVING w0_n >= 1` is the only prune — definitional
            # (first-seen belongs to value_novelty), so the BH pool stays
            # honest.
            sql = f"""
                SELECT
                    val,
                    {w_blocks}
                FROM (
                    SELECT
                        val, ts, event_id, win,
                        -- toNullable: lagInFrame on the non-Nullable column
                        -- would yield 1970-01-01 (a huge fake delta) for each
                        -- partition's first row instead of NULL.
                        dateDiff('millisecond', lagInFrame(toNullable(ts)) OVER w, ts) / 1000.0
                            AS delta
                    FROM (
                        SELECT
                            {col} AS val,
                            timestamp AS ts,
                            event_id,
                            arrayJoin(arrayFilter(x -> x >= 0, [{win_exprs}])) AS win
                        FROM {db}.events
                        WHERE case_id = {{cid:String}}
                          AND has({{src:Array(String)}}, source_id)
                          AND {col} != ''
                          AND {TS_NOT_SENTINEL_SQL}
                          AND ({union_pred})
                    )
                    WINDOW w AS (
                        PARTITION BY val, win
                        ORDER BY ts, event_id
                        ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING
                    )
                )
                GROUP BY val
                HAVING w0_n >= 1
                ORDER BY ({n_sum}) DESC, val ASC
                LIMIT {{cap:UInt32}}
                {HEAVY_SCAN_SETTINGS}
            """
            rows = self.ch.client.query(sql, parameters=params).result_rows
            if not rows:
                continue
            evaluated_fields += 1
            if len(rows) >= max_candidates_per_field:
                run_warnings.append(
                    f"Field {field_token!r} hit the {max_candidates_per_field}-value "
                    f"candidate cap — the FDR correction covers only the "
                    f"{max_candidates_per_field} highest-volume values; treat marginal "
                    f"q-values for this field as exploratory."
                )
            for row in rows:
                val = row[0]
                if not val:
                    continue
                blocks = [_interval_window_block(row, w) for w in range(n_wins)]
                candidates.append((field_token, str(val), blocks))

        # Phase 2: gate each candidate into at most one direction per suspect
        # window, all tests pooled into a single BH-FDR correction.
        # test = (kind, cand_idx, win_idx, statistic, p, extra)
        tests: list[tuple[str, int, int, float, float, dict[str, Any]]] = []
        for ci, (_, _, blocks) in enumerate(candidates):
            bl = blocks[0]
            cv_b = bl["cv"]
            regular = (
                bl["k"] >= min_baseline_intervals
                and cv_b is not None
                and cv_b <= cv_regular_max
                and (bl["mean"] or 0) > 0
            )
            bursty_or_sparse = (cv_b is not None and cv_b >= cv_irregular_min) or bl[
                "k"
            ] < min_baseline_intervals
            for wi in range(1, n_wins):
                wb = blocks[wi]
                d_w = d_ws[wi - 1]
                if regular:
                    g = _poisson_rate_g(bl["n"], d_b, wb["n"], d_w)
                    tests.append(("cadence_break", ci, wi, g, _chi2_sf_df1(g), {}))
                elif bursty_or_sparse and wb["k"] >= beacon_min_intervals:
                    span = wb["span"]
                    if span is None or span <= 0:
                        continue
                    g_w = wb["sum2"] / (span * span)
                    z, p = _greenwood_p(g_w, wb["k"])
                    tests.append(("beacon", ci, wi, g_w, p, {"z": z, "span": span}))
        qvals = _bh_qvalues([t[4] for t in tests])
        m_tests = len(tests)

        # Phase 3: gate on FDR + per-direction effect floors, build findings.
        findings: list[IntervalFinding] = []
        for (kind, ci, wi, stat, p, extra), q in zip(tests, qvals, strict=True):
            if q > fdr_q:
                continue
            field_token, val, blocks = candidates[ci]
            bl, wb = blocks[0], blocks[wi]
            d_w = d_ws[wi - 1]
            window = windows.suspects[wi - 1]
            details: dict[str, Any] = {
                "detector": "interval_periodicity",
                "method": method,
                "field": field_token,
                "value": val,
                "count": wb["n"],
                "baseline_count": bl["n"],
                "baseline_intervals": bl["k"],
                "window_intervals": wb["k"],
                "baseline_median_interval": bl["med"],
                "window_median_interval": wb["med"],
                "baseline_cv": bl["cv"],
                "window_cv": wb["cv"],
                "baseline_duration_s": round(d_b, 3),
                "window_duration_s": round(d_w, 3),
                "p_value": round(p, 6),
                "q_value": round(q, 6),
                "m_tests": m_tests,
                "q_threshold": fdr_q,
                "window_label": window.label,
                "window_start": ensure_utc(window.start).isoformat(),
                "window_end": ensure_utc(window.end).isoformat(),
                "baseline_size": baseline_size,
                "allowlist_field": field_token,
                "allowlist_value": val,
            }
            if kind == "cadence_break":
                # Haldane–Anscombe +0.5 smoothing when the value went fully
                # silent — the test above always used the raw counts.
                rate_b = bl["n"] / d_b
                rate_w = (wb["n"] if wb["n"] > 0 else 0.5) / d_w
                ratio = rate_w / rate_b
                if 1.0 / min_rate_ratio < ratio < min_rate_ratio:
                    continue
                direction = "accelerated" if ratio > 1.0 else "missed"
                med_b = bl["med"]
                details.update(
                    {
                        "direction": direction,
                        "rate_ratio": round(ratio, 4),
                        "min_rate_ratio": min_rate_ratio,
                        "expected_count": round(d_w / med_b, 1) if med_b else None,
                        "g_statistic": round(stat, 4),
                    }
                )
                if wb["n"] == 0:
                    details["last_seen_baseline"] = _present_ts(bl["last"])
            else:  # beacon
                cv_w = wb["cv"]
                span_fraction = extra["span"] / d_w if d_w > 0 else 0.0
                if cv_w is None or cv_w > beacon_cv_max or span_fraction < beacon_min_span:
                    continue
                direction = "new_regularity"
                details.update(
                    {
                        "direction": direction,
                        "greenwood_g": round(stat, 6),
                        "greenwood_z": round(extra["z"], 4),
                        "span_seconds": round(extra["span"], 3),
                        "span_fraction": round(span_fraction, 4),
                        "beacon_cv_max": beacon_cv_max,
                        "beacon_min_span": beacon_min_span,
                    }
                )
            score = round(-math.log10(max(p, 1e-300)), 4)
            silent = wb["n"] == 0
            first_seen_str = None if silent else _present_ts(wb["first"])
            evt_id = bl["last_evt"] if silent else wb["first_evt"]
            evt_id_str = str(evt_id) if evt_id else None
            findings.append(
                IntervalFinding(
                    field=field_token,
                    value=val,
                    direction=direction,
                    count=wb["n"],
                    baseline_count=bl["n"],
                    baseline_median_interval=bl["med"],
                    window_median_interval=wb["med"],
                    baseline_cv=bl["cv"],
                    window_cv=wb["cv"],
                    statistic=round(stat, 6),
                    p_value=round(p, 6),
                    q_value=round(q, 6),
                    score=score,
                    first_seen=first_seen_str,
                    event_id=evt_id_str,
                    event=_stub_event(evt_id_str, case_id, first_seen_str),
                    details=details,
                )
            )

        return self._finalize_findings(
            findings,
            detector="interval_periodicity",
            method=method,
            total_events=baseline_size,
            evaluated_fields=evaluated_fields,
            exclude_event_ids=exclude_event_ids,
            limit=limit,
            case_id=case_id,
            source_ids=source_ids,
            allowlist=allowlist,
            warnings=run_warnings,
            windows=windows,
        )

    # ------------------------------------------------------------------
    # Frequency / volume anomalies
    # ------------------------------------------------------------------

    def find_frequency_anomalies(
        self,
        case_id: str,
        source_ids: list[str],
        series_field: str = "artifact",
        limit: int = 20,
        bucket_count: int = 60,
        z_threshold: float = 2.5,
        windows: AnalysisWindows | None = None,
        exclude_event_ids: set[str] | None = None,
        allowlist: set[tuple[str, str]] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> StatAnomalyResult:
        """Return time windows with anomalous event-count frequency.

        Event counts per ``series_field`` value are windowed into time
        buckets.  For each series with at least ``_MIN_FREQUENCY_BUCKETS``
        buckets, z-scores are computed and windows with |z| ≥ *z_threshold*
        are returned ranked by |z| descending.

        *self-baseline* (``method="z-score"``): the whole timeline is bucketed
        into *bucket_count* buckets and each bucket is scored leave-one-out
        against the rest of its series.

        *temporal* (``method="temporal-z-score"``, *windows* provided): the
        bucket interval is derived from the **baseline** window (the reference
        distribution, so it gets the full *bucket_count* resolution), and the
        same epoch-aligned interval buckets every suspect window. Mean/std are
        learned from the baseline window's buckets only (zero-filled — a silent
        bucket contributes a real 0 to the baseline), and each suspect window's
        buckets are scored against that fixed baseline. Only buckets lying
        **fully** inside a window are used; a window edge cutting a bucket
        would read as a fake spike/drop, so partial buckets are excluded, and a
        suspect window with no full bucket yields a warning rather than a bogus
        single-bucket z-score.
        """
        self.ch.init_schema()
        db = self.ch.database
        field_params: dict[str, Any] = {}
        col = _col_expr(series_field, field_params, field_mappings)
        src_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "z-score" if windows is None else "temporal-z-score"

        if windows is not None:
            return self._frequency_windowed(
                case_id,
                source_ids,
                series_field,
                col,
                field_params,
                windows,
                limit,
                bucket_count,
                z_threshold,
                exclude_event_ids,
                allowlist,
            )

        # --- Self-baseline: whole-timeline buckets, leave-one-out z-score. ---
        min_ts, max_ts = query_timestamp_range(
            self.ch.client,
            db,
            "case_id = {cid:String} AND has({src:Array(String)}, source_id)",
            src_params,
        )
        if min_ts is None or max_ts is None:
            return StatAnomalyResult(
                status="no_data",
                detector="frequency",
                method=method,
                baseline_size=0,
                z_threshold=z_threshold,
            )

        interval = bucket_interval_seconds(min_ts, max_ts, bucket_count)

        params: dict[str, Any] = {**src_params, **field_params, "iv": interval}
        bucket_sql = f"""
            SELECT
                toStartOfInterval(timestamp, INTERVAL {{iv:UInt32}} second) AS bucket,
                {col} AS series_val,
                count() AS cnt
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {TS_NOT_SENTINEL_SQL}
              AND {col} != ''
            GROUP BY bucket, series_val
            ORDER BY bucket
            {HEAVY_SCAN_SETTINGS}
        """
        brows = self.ch.client.query(bucket_sql, parameters=params).result_rows

        if not brows:
            return StatAnomalyResult(
                status="no_data",
                detector="frequency",
                method=method,
                baseline_size=0,
                z_threshold=z_threshold,
            )

        series: dict[str, list[tuple[Any, int]]] = defaultdict(list)
        for brow in brows:
            bucket, sv, cnt = brow
            if sv:
                series[sv].append((bucket, int(cnt)))

        baseline_size = 0
        findings: list[FreqFinding] = []
        evaluated_series = 0

        for sv, pts in series.items():
            pts_aware = [(ensure_utc(b), c) for b, c in pts]
            if len(pts_aware) < _MIN_FREQUENCY_BUCKETS:
                continue
            baseline_size += sum(c for _, c in pts_aware)
            # Leave-one-out mean/std: score each bucket against the rest of
            # the series, not against itself. Otherwise a single dominant
            # spike inflates its own baseline and can suppress detection
            # of the very spike being scored.
            counts = np.array([c for _, c in pts_aware], dtype=np.float64)
            n = len(counts)
            total = float(counts.sum())
            total_sq = float(np.square(counts).sum())
            evaluated_series += 1
            for (bucket_dt, cnt), c in zip(pts_aware, counts, strict=False):
                n_loo = n - 1
                mean_val = (total - c) / n_loo
                var_loo = (total_sq - c * c - n_loo * mean_val * mean_val) / (n_loo - 1)
                std_val = max(math.sqrt(max(var_loo, 0.0)), _MIN_FREQUENCY_STD)
                z = (cnt - mean_val) / std_val
                if abs(z) >= z_threshold:
                    findings.append(
                        _freq_finding(
                            series_field, sv, bucket_dt, interval, int(cnt), mean_val, z, method
                        )
                    )

        return self._finalize_freq(
            findings,
            case_id,
            source_ids,
            col,
            db,
            field_params,
            interval,
            baseline_size,
            evaluated_series,
            z_threshold,
            exclude_event_ids,
            allowlist,
            limit=limit,
            warnings=[],
            windows=None,
        )

    def _frequency_windowed(
        self,
        case_id: str,
        source_ids: list[str],
        series_field: str,
        col: str,
        field_params: dict[str, Any],
        windows: AnalysisWindows,
        limit: int,
        bucket_count: int,
        z_threshold: float,
        exclude_event_ids: set[str] | None,
        allowlist: set[tuple[str, str]] | None,
    ) -> StatAnomalyResult:
        """Temporal frequency detection over an explicit baseline + suspect windows.

        See :meth:`find_frequency_anomalies` for the semantics; this is the
        ``windows is not None`` branch, kept separate because its bucket math
        (baseline-derived interval, full-bucket-only, zero-filled baseline)
        shares nothing with the self-baseline leave-one-out path.
        """
        db = self.ch.database
        method = "temporal-z-score"
        warnings: list[str] = []

        # Interval from the baseline window: it is the reference distribution,
        # so it gets the full bucket_count resolution; the same epoch-aligned
        # interval then buckets every suspect window for comparability.
        interval = bucket_interval_seconds(
            windows.baseline.start, windows.baseline.end, bucket_count
        )

        baseline_full = _full_bucket_starts(windows.baseline, interval)
        if len(baseline_full) < _MIN_FREQUENCY_BUCKETS:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="frequency",
                method=method,
                baseline_size=0,
                results=[],
                z_threshold=z_threshold,
                warnings=[
                    "Baseline window spans fewer than "
                    f"{_MIN_FREQUENCY_BUCKETS} full {interval}s buckets — widen it to "
                    "build a frequency distribution"
                ],
                windows=windows.payload(),
            )

        suspect_full: list[list[datetime]] = []
        for w in windows.suspects:
            full = _full_bucket_starts(w, interval)
            suspect_full.append(full)
            if not full:
                warnings.append(
                    f"Suspect window {w.label!r} is shorter than the {interval}s bucket "
                    "interval (derived from the baseline) — not scored; shrink the "
                    "baseline or widen the window"
                )

        # One bucket scan over the union of all windows.
        params: dict[str, Any] = {**{"cid": case_id, "src": source_ids}, **field_params}
        _, sps = _window_preds(windows, params)
        params["iv"] = interval
        union_pred = " OR ".join(["(timestamp >= {b0:String} AND timestamp < {b1:String})", *sps])
        bucket_sql = f"""
            SELECT
                toStartOfInterval(timestamp, INTERVAL {{iv:UInt32}} second) AS bucket,
                {col} AS series_val,
                count() AS cnt
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {TS_NOT_SENTINEL_SQL}
              AND {col} != ''
              AND ({union_pred})
            GROUP BY bucket, series_val
            {HEAVY_SCAN_SETTINGS}
        """
        brows = self.ch.client.query(bucket_sql, parameters=params).result_rows
        if not brows:
            return StatAnomalyResult(
                status="no_data",
                detector="frequency",
                method=method,
                baseline_size=0,
                results=[],
                z_threshold=z_threshold,
                warnings=warnings,
                windows=windows.payload(),
            )

        # series_val → {bucket_start_iso: cnt}
        counts_by_series: dict[str, dict[str, int]] = defaultdict(dict)
        for bucket, sv, cnt in brows:
            if sv:
                counts_by_series[str(sv)][ensure_utc(bucket).isoformat()] = int(cnt)

        baseline_iso = [b.isoformat() for b in baseline_full]
        baseline_size = 0
        findings: list[FreqFinding] = []
        evaluated_series = 0

        for sv, cmap in counts_by_series.items():
            # Zero-fill the baseline: a bucket with no events for this series is
            # a real 0 in the distribution, not a missing sample. A silence
            # inflates the mean if dropped.
            bl_counts = np.array([cmap.get(b, 0) for b in baseline_iso], dtype=np.float64)
            baseline_size += int(bl_counts.sum())
            mean_val = float(bl_counts.mean())
            # Floor std at _MIN_FREQUENCY_STD: covers a series absent from the
            # baseline (all-zero → mean 0) and a constant-baseline series
            # alike, so a spike against either is still scored instead of
            # dividing by ~0 or being silently skipped.
            std_val = max(float(bl_counts.std(ddof=1)), _MIN_FREQUENCY_STD)
            evaluated_series += 1

            # Overlapping suspect windows (allowed) share epoch-aligned buckets;
            # a bucket scored once per window would emit duplicate findings for
            # the same (series, bucket). Dedupe on bucket start — the first
            # suspect window covering it wins the attribution.
            scored_buckets: set[str] = set()
            for w, full in zip(windows.suspects, suspect_full, strict=False):
                for b in full:
                    b_iso = b.isoformat()
                    if b_iso in scored_buckets:
                        continue
                    scored_buckets.add(b_iso)
                    cnt = cmap.get(b_iso, 0)
                    z = (cnt - mean_val) / std_val
                    if abs(z) >= z_threshold:
                        findings.append(
                            _freq_finding(
                                series_field,
                                sv,
                                b,
                                interval,
                                cnt,
                                mean_val,
                                z,
                                method,
                                suspect_window=w,
                            )
                        )

        return self._finalize_freq(
            findings,
            case_id,
            source_ids,
            col,
            db,
            field_params,
            interval,
            baseline_size,
            evaluated_series,
            z_threshold,
            exclude_event_ids,
            allowlist,
            limit=limit,
            warnings=warnings,
            windows=windows,
        )

    def _finalize_freq(
        self,
        findings: list[FreqFinding],
        case_id: str,
        source_ids: list[str],
        col: str,
        db: str,
        field_params: dict[str, Any],
        interval: int,
        baseline_size: int,
        evaluated_series: int,
        z_threshold: float,
        exclude_event_ids: set[str] | None,
        allowlist: set[tuple[str, str]] | None,
        *,
        limit: int,
        warnings: list[str],
        windows: AnalysisWindows | None,
    ) -> StatAnomalyResult:
        """Hydrate, suppress, rank and cap frequency findings; build the result."""
        windows_payload = windows.payload() if windows is not None else None
        if not findings:
            return StatAnomalyResult(
                status="ok" if evaluated_series > 0 else "insufficient_data",
                detector="frequency",
                method="z-score" if windows is None else "temporal-z-score",
                baseline_size=baseline_size,
                results=[],
                z_threshold=z_threshold,
                warnings=warnings,
                windows=windows_payload,
            )

        # Rank first, then hydrate only a bounded candidate slice. Temporal
        # mode can emit a finding per suspect-window bucket (every silent
        # bucket is a valid drop vs the baseline), so the raw list can be
        # thousands long — hydrating all of them builds a `vals`/`buckets`
        # query whose HTTP params overflow ClickHouse's field-length limit
        # ("Field value too long"). The allowlist filter needs no event id
        # (it keys on series value in details), so apply it pre-hydration;
        # the candidate buffer above `limit` leaves room for the
        # normal-annotation `exclude_event_ids` pass to drop a few without
        # shrinking the page below `limit`.
        findings = _apply_allowlist(findings, allowlist)
        findings.sort(key=lambda f: f.score, reverse=True)
        candidates = findings[: max(limit * 3, 100)]
        candidates = self._hydrate_freq_findings(
            candidates, case_id, source_ids, col, db, field_params, interval
        )
        if exclude_event_ids:
            candidates = [
                f for f in candidates if not f.event_id or f.event_id not in exclude_event_ids
            ]

        return StatAnomalyResult(
            status="ok",
            detector="frequency",
            method="z-score" if windows is None else "temporal-z-score",
            baseline_size=baseline_size,
            results=candidates[:limit],
            z_threshold=z_threshold,
            warnings=warnings,
            windows=windows_payload,
        )

    def _hydrate_freq_findings(
        self,
        findings: list[FreqFinding],
        case_id: str,
        source_ids: list[str],
        col: str,
        db: str,
        field_params: dict[str, Any],
        interval: int,
    ) -> list[FreqFinding]:
        """Fetch one representative event per (series_value, window) pair.

        A single grouped query replaces one query per finding: every
        (bucket, series value) pair here was already aggregated together in
        ``find_frequency_anomalies``'s bucket scan, so ``argMin(..., timestamp)``
        recovers the earliest event of each pair in one round-trip.
        """
        if not findings:
            return findings

        series_values = sorted({f.series_value for f in findings})
        # Keep bucket bounds timezone-aware UTC: clickhouse_connect binds a
        # *naive* DateTime param in the client process's local timezone, so
        # stripping tzinfo here made every `IN {buckets}` comparison miss by
        # the client's UTC offset (representative events silently never
        # hydrated on any non-UTC host). Events are stored/compared in UTC.
        buckets = sorted({ensure_utc(datetime.fromisoformat(f.window_start)) for f in findings})
        params: dict[str, Any] = {
            **field_params,
            "cid": case_id,
            "src": source_ids,
            "vals": series_values,
            "buckets": buckets,
            "iv": interval,
        }
        # Phase 1 aggregates only argMin(event_id, ...) per (bucket, series)
        # pair — the former all-column argMin dragged message/attributes for
        # every matching row through the aggregation. The winners' full rows
        # are then fetched in one bounded get_events_by_ids batch.
        #
        # `timestamp` is DateTime64(3), and toStartOfInterval over a DateTime64
        # returns DateTime64 on ClickHouse builds that preserve the argument's
        # precision — comparing that against an Array(DateTime) literal then
        # raises TYPE_MISMATCH (code 53), 500-ing the whole frequency detector
        # (value_novelty is unaffected — it never runs this hydration query,
        # which is why only frequency broke). Bucket boundaries are always
        # whole seconds (interval >= 1s), so wrapping in toDateTime() is
        # lossless and pins both sides to DateTime regardless of CH version.
        bucket_expr = "toDateTime(toStartOfInterval(timestamp, INTERVAL {iv:UInt32} second))"
        sql = f"""
            SELECT
                {bucket_expr} AS bucket,
                {col} AS series_val,
                toString(argMin(event_id, timestamp)) AS evt_id
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {col} IN {{vals:Array(String)}}
              AND {bucket_expr} IN {{buckets:Array(DateTime)}}
            GROUP BY bucket, series_val
            {HEAVY_SCAN_SETTINGS}
        """
        rows = self.ch.client.query(sql, parameters=params).result_rows

        id_by_key: dict[tuple[str, str], str] = {
            (str(series_val), ensure_utc(bucket).isoformat()): str(evt_id)
            for bucket, series_val, evt_id in rows
            if evt_id
        }
        events_by_id = self.ch.get_events_by_ids(
            case_id, source_ids, sorted(set(id_by_key.values()))
        )

        hydrated: list[FreqFinding] = []
        for f in findings:
            evt_id = id_by_key.get((f.series_value, f.window_start))
            evt = events_by_id.get(evt_id) if evt_id else None
            if evt is not None:
                hydrated.append(replace(f, event_id=str(evt.get("event_id", "")), event=evt))
            elif evt_id:
                hydrated.append(
                    replace(f, event_id=evt_id, event=_stub_event(evt_id, case_id, None))
                )
            else:
                hydrated.append(f)
        return hydrated

    # ------------------------------------------------------------------
    # Timestamp-order violations
    # ------------------------------------------------------------------

    def find_order_violations(
        self,
        case_id: str,
        source_ids: list[str],
        min_skew_seconds: float = 1.0,
        limit: int = 50,
        exclude_event_ids: set[str] | None = None,
    ) -> StatAnomalyResult:
        """Return events whose timestamp jumps backwards in record order.

        Record order within a source is ``byte_offset`` (the byte position of
        the raw record in the source file — monotonic per file), then
        ``line_number`` and ``event_id`` as deterministic tie-breaks. Each
        event's timestamp is compared to its immediate predecessor
        (``lagInFrame`` over a per-``source_id`` window); a backwards jump of
        at least *min_skew_seconds* is a violation.

        This detector is mode-less (``method="sequential"``) — there is no
        baseline/detect split. NULL timestamps are excluded (they carry no
        order signal).
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "sequential"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="timestamp_order",
                method=method,
                baseline_size=0,
            )

        # Inner subquery: per source, lag the timestamp over record order and
        # compute the backwards skew (0 when in order). `dateDiff('millisecond')`
        # / 1000 keeps sub-second precision that dateDiff('second') would lose.
        inner = f"""
            SELECT
                source_id,
                toString(event_id) AS event_id,
                timestamp,
                -- toNullable: on the non-Nullable timestamp column lagInFrame
                -- returns the type default (1970-01-01), not NULL, for each
                -- source's first row — which would make the IS NOT NULL
                -- first-row guard below always-true.
                lagInFrame(toNullable(timestamp)) OVER w AS prev_ts,
                byte_offset,
                line_number,
                message,
                if(prev_ts IS NOT NULL AND timestamp < prev_ts,
                   dateDiff('millisecond', timestamp, prev_ts) / 1000.0, 0.) AS skew
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {TS_NOT_SENTINEL_SQL}
            WINDOW w AS (
                PARTITION BY source_id
                ORDER BY byte_offset, line_number, event_id
                ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING
            )
        """

        # Per-source summary: violation count + worst skew, for the UI's
        # per-source grouping header and the overall status.
        summary_params = {**base_params, "skew": float(min_skew_seconds)}
        summary_sql = f"""
            SELECT
                source_id,
                countIf(skew >= {{skew:Float64}}) AS n_viol,
                maxIf(skew, skew >= {{skew:Float64}}) AS max_skew
            FROM ({inner})
            GROUP BY source_id
            {HEAVY_SCAN_SETTINGS}
        """
        summary_rows = self.ch.client.query(summary_sql, parameters=summary_params).result_rows
        source_summary: dict[str, tuple[int, float]] = {
            str(r[0]): (int(r[1]), float(r[2]) if r[2] is not None else 0.0) for r in summary_rows
        }
        total_violations = sum(n for n, _ in source_summary.values())

        if total_violations == 0:
            return StatAnomalyResult(
                status="ok",
                detector="timestamp_order",
                method=method,
                baseline_size=total_events,
                results=[],
            )

        # Detail: the worst violations across all sources.
        detail_params = {**base_params, "skew": float(min_skew_seconds), "lim": limit}
        detail_sql = f"""
            SELECT source_id, event_id, timestamp, prev_ts, skew, byte_offset, line_number, message
            FROM ({inner})
            WHERE skew >= {{skew:Float64}}
            ORDER BY skew DESC, source_id, byte_offset
            LIMIT {{lim:UInt32}}
            {HEAVY_SCAN_SETTINGS}
        """
        rows = self.ch.client.query(detail_sql, parameters=detail_params).result_rows

        findings: list[OrderFinding] = []
        for row in rows:
            source_id, event_id, ts, prev_ts, skew, byte_offset, line_number, msg = row
            if not event_id:
                continue
            ts_str = _present_ts(ts)
            prev_str = ensure_utc(prev_ts).isoformat() if prev_ts else None
            n_viol, max_skew = source_summary.get(str(source_id), (0, 0.0))
            mini_event: dict[str, Any] = {
                "event_id": str(event_id),
                "case_id": case_id,
                "source_id": str(source_id),
                "message": str(msg) if msg else "",
                "timestamp": ts_str,
                "timestamp_desc": None,
                "artifact": None,
                "artifact_long": None,
                "display_name": None,
                "tags": [],
                "attributes": {},
                "content_hash": "",
                "file_hash": "",
                "parser_name": "",
                "parser_version": "",
                "source_file": "",
                "byte_offset": int(byte_offset),
                "line_number": int(line_number),
                "embedding_model": None,
                "embedding_config_hash": None,
                "ingest_time": None,
            }
            details: dict[str, Any] = {
                "detector": "timestamp_order",
                "method": method,
                "source_id": str(source_id),
                "prev_timestamp": prev_str,
                "skew_seconds": round(float(skew), 3),
                "byte_offset": int(byte_offset),
                "line_number": int(line_number),
                "min_skew_seconds": float(min_skew_seconds),
                "source_total_violations": n_viol,
                "source_max_skew": round(max_skew, 3),
            }
            findings.append(
                OrderFinding(
                    source_id=str(source_id),
                    event_id=str(event_id),
                    timestamp=ts_str or "",
                    prev_timestamp=prev_str or "",
                    skew_seconds=round(float(skew), 3),
                    byte_offset=int(byte_offset),
                    line_number=int(line_number),
                    score=round(float(skew), 3),
                    event=mini_event,
                    details=details,
                )
            )

        # Suppress findings whose representative event was marked normal
        # (before the limit is applied — filtering after would shrink the page
        # instead of backfilling from the next-worst violation).
        if exclude_event_ids:
            findings = [
                f for f in findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        return StatAnomalyResult(
            status="ok",
            detector="timestamp_order",
            method=method,
            baseline_size=total_events,
            results=findings[:limit],
        )
