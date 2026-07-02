import { describe, it, expect } from "vitest";
import {
  CATEGORICAL_SLOTS,
  seriesColorVar,
  sequentialColor,
  buildSeriesColorMap,
  OTHER_LABEL,
  OTHER_COLOR,
} from "@/components/viz/lib/colors";

describe("seriesColorVar", () => {
  it("assigns fixed-order slots for the first 8 indices", () => {
    expect(seriesColorVar(0)).toBe("var(--viz-series-1)");
    expect(seriesColorVar(7)).toBe("var(--viz-series-8)");
  });

  it("wraps past the 8-slot palette rather than generating a new hue", () => {
    expect(seriesColorVar(8)).toBe(seriesColorVar(0));
    expect(seriesColorVar(CATEGORICAL_SLOTS)).toBe("var(--viz-series-1)");
  });
});

describe("sequentialColor", () => {
  it("clamps t to [0, 1]", () => {
    expect(sequentialColor(-1)).toBe(sequentialColor(0));
    expect(sequentialColor(2)).toBe(sequentialColor(1));
  });

  it("maps low t to the lightest step and high t to the darkest", () => {
    expect(sequentialColor(0)).toBe("var(--viz-sequential-100)");
    expect(sequentialColor(0.99)).toBe("var(--viz-sequential-700)");
  });
});

describe("buildSeriesColorMap", () => {
  it("assigns colors in the given order, stable across re-calls with the same order", () => {
    const values = ["GET", "POST", "DELETE"];
    const map1 = buildSeriesColorMap(values);
    const map2 = buildSeriesColorMap(values);
    for (const v of values) {
      expect(map1.get(v)).toBe(map2.get(v));
    }
    expect(map1.get("GET")).toBe("var(--viz-series-1)");
    expect(map1.get("POST")).toBe("var(--viz-series-2)");
  });

  it("always maps OTHER_LABEL to the fixed neutral color, not a categorical slot", () => {
    const map = buildSeriesColorMap(["a", "b", OTHER_LABEL]);
    expect(map.get(OTHER_LABEL)).toBe(OTHER_COLOR);
    // "a"/"b" still get the first two categorical slots — Other doesn't
    // consume a slot in the sequence.
    expect(map.get("a")).toBe("var(--viz-series-1)");
    expect(map.get("b")).toBe("var(--viz-series-2)");
  });
});
