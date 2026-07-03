import { describe, expect, it } from "vitest";
import {
  applyMetric,
  cumulative,
  delta,
  METRIC_INFO,
  rate,
  ratioOfBaseline,
} from "@/components/viz/lib/transforms";

describe("delta", () => {
  it("first bin is null, rest are pairwise differences", () => {
    expect(delta([5, 8, 3, 3])).toEqual([null, 3, -5, 0]);
  });
  it("empty input → empty output", () => {
    expect(delta([])).toEqual([]);
  });
  it("single bin → only the undefined first bin", () => {
    expect(delta([7])).toEqual([null]);
  });
});

describe("rate", () => {
  it("divides each count by the bucket interval", () => {
    expect(rate([60, 120], 60)).toEqual([1, 2]);
  });
  it("zero/negative/NaN interval yields all-null, not Infinity", () => {
    expect(rate([60], 0)).toEqual([null]);
    expect(rate([60], -5)).toEqual([null]);
    expect(rate([60], Number.NaN)).toEqual([null]);
  });
});

describe("ratioOfBaseline", () => {
  it("returns per-bin percentage of the comparison layer", () => {
    expect(ratioOfBaseline([5, 50], [10, 100])).toEqual([50, 50]);
  });
  it("zero-baseline bins are null — never 0 or Infinity", () => {
    expect(ratioOfBaseline([5, 0], [0, 0])).toEqual([null, null]);
  });
  it("throws on mismatched layer lengths instead of silently zipping", () => {
    expect(() => ratioOfBaseline([1, 2], [1])).toThrow(/lengths differ/);
  });
  it("empty layers → empty result", () => {
    expect(ratioOfBaseline([], [])).toEqual([]);
  });
});

describe("cumulative", () => {
  it("running sum", () => {
    expect(cumulative([1, 2, 3])).toEqual([1, 3, 6]);
  });
  it("empty input → empty output", () => {
    expect(cumulative([])).toEqual([]);
  });
});

describe("applyMetric", () => {
  it("count is the identity", () => {
    expect(applyMetric("count", [1, 2])).toEqual([1, 2]);
  });
  it("ratio without a comparison layer yields all-null", () => {
    expect(applyMetric("ratio", [1, 2])).toEqual([null, null]);
  });
  it("ratio with comparison delegates to ratioOfBaseline", () => {
    expect(applyMetric("ratio", [1, 2], { comparison: [2, 0] })).toEqual([50, null]);
  });
  it("rate uses the provided interval", () => {
    expect(applyMetric("rate", [30], { intervalSeconds: 30 })).toEqual([1]);
  });
});

describe("METRIC_INFO", () => {
  it("every metric carries a non-empty formula for captions/exports", () => {
    for (const info of Object.values(METRIC_INFO)) {
      expect(info.formula.length).toBeGreaterThan(0);
    }
  });
  it("ratio requires a comparison layer; delta/rate/cumulative are time-bucketed", () => {
    expect(METRIC_INFO.ratio.requiresCompare).toBe(true);
    expect(METRIC_INFO.delta.timeBucketedOnly).toBe(true);
    expect(METRIC_INFO.rate.timeBucketedOnly).toBe(true);
    expect(METRIC_INFO.cumulative.timeBucketedOnly).toBe(true);
  });
});
