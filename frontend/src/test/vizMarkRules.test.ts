/**
 * The rules behind the lecture-driven marks: waffle cell allocation, the pie
 * readability caution, and the deterministic jitter that keeps a point
 * overlay reproducible across renders (and therefore across exports).
 */
import { describe, it, expect } from "vitest";
import { allocateWaffleCells, WAFFLE_CELLS } from "@/components/viz/lib/waffle";
import { pieReadabilityWarning } from "@/components/viz/lib/pieReadability";
import { jitterOffset } from "@/components/viz/lib/jitter";
import type { FieldTermsResponse } from "@/api/types";

const rows = (...counts: number[]) =>
  counts.map((count, i) => ({ key: `k${i}`, label: `k${i}`, count }));

function terms(values: number[], otherCount = 0): FieldTermsResponse {
  return {
    field: "attr:status",
    total: values.reduce((a, b) => a + b, 0) + otherCount,
    distinct: values.length,
    other_count: otherCount,
    values: values.map((count, i) => ({ value: `v${i}`, count })),
  };
}

describe("allocateWaffleCells", () => {
  it("always allocates exactly 100 cells", () => {
    for (const counts of [[1], [50, 50], [70, 20, 10], [33, 33, 34], [1, 1, 1, 997]]) {
      const allocated = allocateWaffleCells(rows(...counts));
      expect(allocated.reduce((s, r) => s + r.cells, 0)).toBe(WAFFLE_CELLS);
    }
  });

  it("never rounds an existing category down to zero cells", () => {
    // 0.1% of the total would round to zero without the reserved cell.
    const allocated = allocateWaffleCells(rows(9990, 5, 5));
    expect(allocated).toHaveLength(3);
    expect(allocated.every((r) => r.cells >= 1)).toBe(true);
  });

  it("drops empty categories and returns nothing when the total is zero", () => {
    expect(allocateWaffleCells(rows(10, 0))).toHaveLength(1);
    expect(allocateWaffleCells(rows(0, 0))).toEqual([]);
    expect(allocateWaffleCells([])).toEqual([]);
  });

  it("gives the larger share more cells", () => {
    const [big, small] = allocateWaffleCells(rows(90, 10));
    expect(big.cells).toBeGreaterThan(small.cells);
  });
});

describe("pieReadabilityWarning", () => {
  it("stays silent for a small, clearly-separated set of slices", () => {
    expect(pieReadabilityWarning(terms([60, 30, 10]))).toBeNull();
  });

  it("warns past the comfortable slice count and names a better mark", () => {
    const warning = pieReadabilityWarning(terms([40, 25, 15, 10, 6, 4]));
    expect(warning).toMatch(/slices/);
    expect(warning).toMatch(/waffle/);
  });

  it("counts the Other slice toward the limit", () => {
    expect(pieReadabilityWarning(terms([40, 25, 15, 10], 10))).toMatch(/slices/);
  });

  it("warns when two slices are too close to tell apart by angle", () => {
    expect(pieReadabilityWarning(terms([100, 96]))).toMatch(/less than 10%/);
  });
});

describe("jitterOffset", () => {
  it("is deterministic, so a re-render (and an export) reproduces the strip", () => {
    expect(jitterOffset(7)).toBe(jitterOffset(7));
    expect(jitterOffset(7)).not.toBe(jitterOffset(8));
  });

  it("stays within [-1, 1]", () => {
    for (let i = 0; i < 500; i++) {
      expect(Math.abs(jitterOffset(i))).toBeLessThanOrEqual(1);
    }
  });
});
