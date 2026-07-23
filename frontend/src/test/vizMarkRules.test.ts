/**
 * The rules behind the lecture-driven marks: waffle cell allocation, the pie
 * readability caution, and the deterministic jitter that keeps a point
 * overlay reproducible across renders (and therefore across exports).
 */
import { describe, it, expect } from "vitest";
import { allocateWaffleCells, WAFFLE_CELLS } from "@/components/viz/lib/waffle";
import { OTHER_KEY, OTHER_LABEL } from "@/components/viz/lib/colors";
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

  it("keeps the 100-cell invariant when there are more categories than cells", () => {
    // One cell per category is the floor, so 150 categories cannot all be
    // drawn — the tail folds into Other instead of overflowing the grid.
    const counts = Array.from({ length: 150 }, (_, i) => 150 - i);
    const allocated = allocateWaffleCells(rows(...counts));
    expect(allocated.reduce((s, r) => s + r.cells, 0)).toBe(WAFFLE_CELLS);
    expect(allocated.every((r) => r.cells >= 1)).toBe(true);
    expect(allocated.length).toBeLessThanOrEqual(WAFFLE_CELLS);
    // Nothing is lost: the folded categories' events live in Other.
    const total = counts.reduce((a, b) => a + b, 0);
    expect(allocated.reduce((s, r) => s + r.count, 0)).toBe(total);
  });

  it("gives exactly one cell each at the capacity boundary", () => {
    // WAFFLE_CELLS categories reserve every cell up front, leaving none to
    // distribute — each must end with exactly one, still summing to 100.
    const counts = Array.from({ length: WAFFLE_CELLS }, (_, i) => WAFFLE_CELLS - i);
    const allocated = allocateWaffleCells(rows(...counts));
    expect(allocated).toHaveLength(WAFFLE_CELLS);
    expect(allocated.every((r) => r.cells === 1)).toBe(true);
    expect(allocated.reduce((s, r) => s + r.cells, 0)).toBe(WAFFLE_CELLS);
  });

  it("merges the overflow into an existing Other row rather than adding a second", () => {
    const counts = Array.from({ length: 150 }, () => 10);
    // The largest row IS the Other bucket, so it survives the cut and the
    // folded tail must merge into it instead of creating a duplicate.
    const input = rows(...counts).map((r, i) =>
      i === 0 ? { ...r, key: OTHER_KEY, label: OTHER_LABEL, count: 9999 } : r,
    );
    const allocated = allocateWaffleCells(input);
    expect(allocated.filter((r) => r.key === OTHER_KEY)).toHaveLength(1);
    expect(allocated.find((r) => r.key === OTHER_KEY)!.count).toBeGreaterThan(9999);
    expect(allocated.reduce((s, r) => s + r.cells, 0)).toBe(WAFFLE_CELLS);
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

  // A strip feeds this consecutive indices, so neighbours must scatter rather
  // than drift together — the failure mode of the smooth sin-based hash this
  // replaced, which banded visibly instead of jittering.
  it("decorrelates neighbouring indices", () => {
    const xs: number[] = [];
    for (let i = 0; i < 200; i++) xs.push(jitterOffset(i));
    const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
    // Lag-1 autocorrelation of an independent sequence sits near zero.
    let cov = 0;
    let varr = 0;
    for (let i = 0; i < xs.length - 1; i++) cov += (xs[i] - mean) * (xs[i + 1] - mean);
    for (const x of xs) varr += (x - mean) ** 2;
    expect(Math.abs(cov / varr)).toBeLessThan(0.2);
    // ...and spread over the full range rather than clustering in one band.
    const octants = new Set(xs.map((x) => Math.floor(((x + 1) / 2) * 8)));
    expect(octants.size).toBe(8);
  });
});
