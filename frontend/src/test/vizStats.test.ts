import { describe, it, expect } from "vitest";
import { boxPlotStats, kdeFromBins, ecdfFromBins } from "@/components/viz/lib/stats";
import type { FieldNumericResponse } from "@/api/types";

function makeStats(overrides: Partial<FieldNumericResponse> = {}): FieldNumericResponse {
  return {
    field: "attr:bytes_sent",
    count: 100,
    min: 0,
    max: 100,
    mean: 50,
    stddev: 20,
    quantiles: { "0.25": 25, "0.5": 50, "0.75": 75 },
    bins: [
      { x0: 0, x1: 25, count: 25 },
      { x0: 25, x1: 50, count: 25 },
      { x0: 50, x1: 75, count: 25 },
      { x0: 75, x1: 100, count: 25 },
    ],
    ...overrides,
  };
}

describe("boxPlotStats", () => {
  it("derives the five-number summary and 1.5*IQR whiskers", () => {
    const box = boxPlotStats(makeStats());
    expect(box).not.toBeNull();
    expect(box!.q1).toBe(25);
    expect(box!.median).toBe(50);
    expect(box!.q3).toBe(75);
    // IQR = 50, whiskers = q1 - 75 / q3 + 75, clamped to [min, max]
    expect(box!.whiskerLow).toBe(0);
    expect(box!.whiskerHigh).toBe(100);
  });

  it("returns null when count is 0 (non-numeric field)", () => {
    expect(boxPlotStats(makeStats({ count: 0 }))).toBeNull();
  });

  it("returns null when quantiles are missing", () => {
    expect(boxPlotStats(makeStats({ quantiles: {} }))).toBeNull();
  });

  it("clamps whiskers to the observed min/max, never extending past them", () => {
    const box = boxPlotStats(
      makeStats({
        min: 20,
        max: 80,
        quantiles: { "0.25": 40, "0.5": 50, "0.75": 60 },
      }),
    );
    // IQR = 20, 1.5*IQR = 30 -> raw whiskers [10, 90], clamped to [20, 80]
    expect(box!.whiskerLow).toBe(20);
    expect(box!.whiskerHigh).toBe(80);
  });
});

describe("kdeFromBins", () => {
  it("returns one density point per bin, centered on the bin midpoint", () => {
    const density = kdeFromBins(makeStats().bins);
    expect(density).toHaveLength(4);
    expect(density[0].x).toBe(12.5);
    expect(density[3].x).toBe(87.5);
  });

  it("density values sum to ~1 across all bins (normalized)", () => {
    const density = kdeFromBins(makeStats().bins);
    const total = density.reduce((s, d) => s + d.density, 0);
    expect(total).toBeCloseTo(1, 5);
  });

  it("returns an empty array for no bins", () => {
    expect(kdeFromBins([])).toEqual([]);
  });
});

describe("ecdfFromBins", () => {
  it("is monotonically non-decreasing and ends at 1", () => {
    const points = ecdfFromBins(makeStats().bins);
    for (let i = 1; i < points.length; i++) {
      expect(points[i].p).toBeGreaterThanOrEqual(points[i - 1].p);
    }
    expect(points[points.length - 1].p).toBeCloseTo(1);
  });

  it("returns an empty array when there are no observations", () => {
    expect(ecdfFromBins([{ x0: 0, x1: 1, count: 0 }])).toEqual([]);
  });
});
