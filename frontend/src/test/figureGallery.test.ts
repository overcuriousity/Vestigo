import { describe, expect, it } from "vitest";
import { galleryEntries } from "@/components/viz/lib/figureGallery";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import { fieldTokenLabel } from "@/components/viz/lib/fieldDisplay";

describe("galleryEntries", () => {
  it("lists every figure, in registry order, exactly once", () => {
    const entries = galleryEntries("nominal", "artifact");
    expect(entries.map((e) => e.chartType)).toEqual(Object.keys(CHART_META));
  });

  it("lights the figures legal for the scale and greys the rest with the scales they need", () => {
    const byType = Object.fromEntries(
      galleryEntries("nominal", "artifact").map((e) => [e.chartType, e]),
    );
    expect(byType.bar.legal).toBe(true);
    expect(byType.bar.reason).toBeNull();
    expect(byType.histogram.legal).toBe(false);
    expect(byType.histogram.reason).toBe("Histogram needs Number or time or Measure.");
    expect(byType.time.legal).toBe(true);
  });

  it("names the time-field guard when a numeric mark cannot plot a time part", () => {
    const byType = Object.fromEntries(
      galleryEntries("interval", "time:date").map((e) => [e.chartType, e]),
    );
    expect(byType.histogram.legal).toBe(false);
    expect(byType.histogram.reason).toBe(
      `Histogram can't plot ${fieldTokenLabel("time:date")} — a time part has no numeric values.`,
    );
    expect(byType.heatmap.legal).toBe(true);
  });
});
