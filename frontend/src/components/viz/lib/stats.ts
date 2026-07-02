/**
 * Client-side statistics helpers for chart geometry.
 *
 * The server (`EventQueryService.field_numeric_stats`) already computes
 * count/min/max/mean/stddev/quantiles and a fixed-width histogram — these
 * helpers only do presentation-layer derivations from that response (a
 * smoothed density curve for the violin plot, box-plot whisker geometry).
 */
import type { FieldNumericBin, FieldNumericResponse } from "@/api/types";

export interface BoxPlotStats {
  min: number;
  q1: number;
  median: number;
  q3: number;
  max: number;
  /** Tukey-style whisker ends, clamped to [min, max] of the observed data. */
  whiskerLow: number;
  whiskerHigh: number;
}

/**
 * Derive box-plot five-number-summary + whiskers from a numeric field
 * response. Whiskers use the classic 1.5*IQR rule, but since the backend
 * doesn't return raw values (only quantiles + binned counts), the "low/high
 * outlier" points aren't individually plotted — only the whisker extent.
 */
export function boxPlotStats(resp: FieldNumericResponse): BoxPlotStats | null {
  const q1 = resp.quantiles["0.25"];
  const median = resp.quantiles["0.5"];
  const q3 = resp.quantiles["0.75"];
  if (
    resp.count === 0 ||
    resp.min == null ||
    resp.max == null ||
    q1 == null ||
    median == null ||
    q3 == null
  ) {
    return null;
  }
  const iqr = q3 - q1;
  const whiskerLow = Math.max(resp.min, q1 - 1.5 * iqr);
  const whiskerHigh = Math.min(resp.max, q3 + 1.5 * iqr);
  return { min: resp.min, q1, median, q3, max: resp.max, whiskerLow, whiskerHigh };
}

export interface DensityPoint {
  x: number;
  density: number;
}

/**
 * Turn the server's fixed-width bin counts into a smoothed density curve for
 * the violin plot, via a small triangular-kernel moving average over the bin
 * heights (a lightweight stand-in for full KDE — bins are already a coarse
 * discretization of the underlying values, so a heavy kernel over them adds
 * little; this just removes the blockiness).
 */
export function kdeFromBins(bins: FieldNumericBin[], windowRadius = 2): DensityPoint[] {
  if (bins.length === 0) return [];
  const counts = bins.map((b) => b.count);
  const total = counts.reduce((a, b) => a + b, 0) || 1;
  const smoothed = counts.map((_, i) => {
    let weightedSum = 0;
    let weightTotal = 0;
    for (let offset = -windowRadius; offset <= windowRadius; offset++) {
      const j = i + offset;
      if (j < 0 || j >= counts.length) continue;
      const weight = windowRadius + 1 - Math.abs(offset);
      weightedSum += counts[j] * weight;
      weightTotal += weight;
    }
    return weightedSum / weightTotal;
  });
  return bins.map((b, i) => ({
    x: (b.x0 + b.x1) / 2,
    density: smoothed[i] / total,
  }));
}

export interface EcdfPoint {
  x: number;
  p: number;
}

/** Empirical CDF step points derived from fixed-width bin counts. */
export function ecdfFromBins(bins: FieldNumericBin[]): EcdfPoint[] {
  const total = bins.reduce((sum, b) => sum + b.count, 0);
  if (total === 0) return [];
  let cumulative = 0;
  const points: EcdfPoint[] = [{ x: bins[0]?.x0 ?? 0, p: 0 }];
  for (const b of bins) {
    cumulative += b.count;
    points.push({ x: b.x1, p: cumulative / total });
  }
  return points;
}
