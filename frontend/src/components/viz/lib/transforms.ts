/**
 * Derived-metric transforms for comparison/time charts.
 *
 * Raw counts from the API are the forensic ground truth; everything here is
 * a pure function over them, applied client-side only. A bin whose input is
 * undefined for the metric (first bin of a delta, a zero-baseline ratio bin)
 * yields `null` — charts skip nulls rather than fabricating a 0 or ∞ — and
 * every metric carries the exact formula string charts print in their
 * caption and embed in exports.
 */

export type Metric = "count" | "delta" | "rate" | "ratio" | "cumulative";

export const METRIC_INFO: Record<
  Metric,
  {
    label: string;
    /** Exact formula, printed verbatim in captions and exports. */
    formula: string;
    /** Only defined against a comparison layer. */
    requiresCompare?: boolean;
    /** Only defined over time-bucketed series (needs bin order / interval). */
    timeBucketedOnly?: boolean;
  }
> = {
  count: { label: "Count", formula: "count[i]" },
  delta: {
    label: "Δ per bin",
    formula: "count[i] − count[i−1] (first bin undefined)",
    timeBucketedOnly: true,
  },
  rate: {
    label: "Rate (events/s)",
    formula: "count[i] / bucket_interval_seconds",
    timeBucketedOnly: true,
  },
  ratio: {
    label: "% of baseline",
    formula: "primary[i] / comparison[i] × 100 (undefined where comparison[i] = 0)",
    requiresCompare: true,
  },
  cumulative: {
    label: "Cumulative",
    formula: "Σ count[0..i]",
    timeBucketedOnly: true,
  },
};

/** Per-bin difference; the first bin has no predecessor and is `null`. */
export function delta(counts: number[]): (number | null)[] {
  return counts.map((c, i) => (i === 0 ? null : c - counts[i - 1]));
}

/** Events per second. All-`null` when the interval is unknown/non-positive. */
export function rate(counts: number[], intervalSeconds: number): (number | null)[] {
  if (!Number.isFinite(intervalSeconds) || intervalSeconds <= 0) {
    return counts.map(() => null);
  }
  return counts.map((c) => c / intervalSeconds);
}

/**
 * Per-bin `primary / comparison × 100`. `null` where the comparison bin is 0
 * — never a fake 0 or Infinity. Lengths must match (shared grid guarantees
 * this for API responses); a mismatch throws rather than silently zipping.
 */
export function ratioOfBaseline(
  primary: number[],
  comparison: number[],
): (number | null)[] {
  if (primary.length !== comparison.length) {
    throw new Error(
      `ratioOfBaseline: layer lengths differ (${primary.length} vs ${comparison.length})`,
    );
  }
  return primary.map((p, i) => (comparison[i] === 0 ? null : (p / comparison[i]) * 100));
}

/** Running sum. */
export function cumulative(counts: number[]): number[] {
  let sum = 0;
  return counts.map((c) => (sum += c));
}

/**
 * Apply *metric* to one layer's counts. `comparison` is required for
 * `ratio`, `intervalSeconds` for `rate`; both are ignored otherwise.
 */
export function applyMetric(
  metric: Metric,
  counts: number[],
  ctx: { intervalSeconds?: number; comparison?: number[] } = {},
): (number | null)[] {
  switch (metric) {
    case "count":
      return counts;
    case "delta":
      return delta(counts);
    case "rate":
      return rate(counts, ctx.intervalSeconds ?? 0);
    case "ratio":
      if (!ctx.comparison) return counts.map(() => null);
      return ratioOfBaseline(counts, ctx.comparison);
    case "cumulative":
      return cumulative(counts);
  }
}
