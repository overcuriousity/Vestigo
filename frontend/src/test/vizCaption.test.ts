import { describe, expect, it } from "vitest";
import { buildCaptionLines, describeFilters } from "@/components/viz/lib/caption";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";

const base = {
  caseId: "c1",
  timelineId: "t1",
  chartLabel: "Time histogram (events over time)",
  filters: {},
  facts: {},
};

describe("describeFilters", () => {
  it("renders a compact human-readable summary, never JSON", () => {
    expect(
      describeFilters({
        q: "dos",
        filters: { "attr:src_ip": ["203.0.113.7"] },
        exclusions: { artifact: ["noise"] },
        tagsInclude: ["suspicious"],
      }),
    ).toBe('search "dos" · tag=suspicious · attr:src_ip=203.0.113.7 · artifact≠noise');
  });
  it("says 'no filters' for an empty set", () => {
    expect(describeFilters({})).toBe("no filters");
  });
});

describe("buildCaptionLines", () => {
  it("includes both layer summaries with totals when compare is on", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "time",
      compare: { mode: "baseline" },
    };
    const lines = buildCaptionLines({
      ...base,
      config,
      filters: { q: "dos" },
      facts: { primaryTotal: 41201, comparisonTotal: 1203554, intervalSeconds: 300 },
    });
    expect(lines).toContain('primary: search "dos" — 41,201 events');
    expect(lines).toContain(
      "comparison: all timeline events (same time range) — 1,203,554 events",
    );
    expect(lines).toContain("5 min buckets, UTC");
  });

  it("warns about top-N capping with the Other count", () => {
    const config: ChartConfig = { ...DEFAULT_CHART_CONFIG, field: "attr:src_ip" };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Bar",
      config,
      facts: { distinct: 3441, shownValues: 12, otherCount: 900 },
    });
    expect(lines).toContain(
      'showing top 12 of 3,441 distinct values (capped; 900 events in "Other")',
    );
  });

  it("states the metric formula and undefined-bin caveats", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "time",
      metric: "delta",
    };
    const lines = buildCaptionLines({ ...base, config, facts: {} });
    expect(lines).toContain("first bin omitted (Δ undefined)");
    expect(lines.some((l) => l.startsWith("metric: Δ per bin ="))).toBe(true);
  });

  it("no capping warning when everything is shown", () => {
    const config: ChartConfig = { ...DEFAULT_CHART_CONFIG, field: "artifact" };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Bar",
      config,
      facts: { distinct: 5, shownValues: 5 },
    });
    expect(lines.some((l) => l.includes("capped"))).toBe(false);
  });

  it("punchcard header line states day×hour and UTC", () => {
    const config: ChartConfig = { ...DEFAULT_CHART_CONFIG, chartType: "punchcard" };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Punch card (day × hour)",
      config,
      facts: { primaryTotal: 100 },
    });
    expect(
      lines.some((l) => l.includes("day-of-week × hour-of-day, UTC")),
    ).toBe(true);
  });

  it("pivot caption names both fields and per-axis capping", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "pivot",
      field: "attr:username",
      fieldY: "attr:workstation",
    };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Heatmap (field × field)",
      config,
      facts: { xDistinct: 40, xShown: 10, yDistinct: 5, yShown: 5 },
    });
    expect(lines.some((l) => l.includes("attr:username × attr:workstation"))).toBe(true);
    expect(lines).toContain('x-axis: top 10 of 40 distinct values (rest in "Other")');
    expect(lines.some((l) => l.startsWith("y-axis:"))).toBe(false);
  });

  it("scatter caption states the sample truthfully", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "scatter",
      field: "attr:bytes",
      fieldY: "attr:latency",
    };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Scatter (numeric × numeric)",
      config,
      facts: { sampledPoints: 5000, totalPoints: 120000 },
    });
    expect(lines).toContain(
      "showing 5,000 of 120,000 points (uniform random sample; axes span full data)",
    );
  });

  it("no sample line when every point is drawn", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "scatter",
      field: "attr:bytes",
      fieldY: "attr:latency",
    };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Scatter (numeric × numeric)",
      config,
      facts: { sampledPoints: 800, totalPoints: 800 },
    });
    expect(lines.some((l) => l.includes("random sample"))).toBe(false);
  });

  it("histogram caption states the bin rule and the skewness reading", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
    };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Histogram",
      config,
      facts: { binCount: 42, valueMin: 0, valueMax: 100, binRule: "fd", skewness: 1.7 },
    });
    expect(lines).toContain(
      "42 fixed-width bins over [0, 100] (Freedman–Diaconis automatic width)",
    );
    expect(lines).toContain(
      "skewness g₁ = 1.70 — right-skewed (long upper tail; mode < median < mean)",
    );
  });

  it("symmetric skewness reads as approximately symmetric", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
    };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Histogram",
      config,
      facts: { binCount: 30, valueMin: 0, valueMax: 10, binRule: "manual", skewness: -0.1 },
    });
    expect(lines).toContain("30 fixed-width bins over [0, 10] (manual)");
    expect(lines).toContain("skewness g₁ = -0.10 — approximately symmetric");
  });
});

describe("lecture-driven caption lines", () => {
  const base = {
    caseId: "c1",
    timelineId: "t1",
    chartLabel: "Chart",
    filters: {},
  };

  it("states the grouped-distribution omission without inventing an Other group", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Box plot",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "box",
        field: "attr:latency",
        fieldY: "attr:user",
        scale: "ratio",
      },
      facts: {
        groupField: "attr:user",
        groupsShown: 2,
        groupsOmitted: 3,
        groupOmittedCount: 41,
      },
    });
    const line = lines.find((l) => l.startsWith("grouped by"))!;
    expect(line).toContain("2 groups shown");
    expect(line).toContain("3 smaller groups omitted (41 events)");
    expect(line).toContain('not merged into an "Other" group');
  });

  it("states the point-overlay sample honestly", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Violin plot",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "violin", field: "attr:latency", scale: "ratio" },
      facts: { overlayShown: 1000, overlayTotal: 52341 },
    });
    expect(lines).toContain(
      "point overlay: showing 1,000 of 52,341 values (uniform random sample)",
    );
  });

  it("records the correlation matrix's pairwise-complete counts and the causation caveat", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Correlation matrix",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "corr", scale: "ratio" },
      facts: {
        corrFields: ["attr:bytes", "attr:latency"],
        corrPairs: 1,
        corrMinPairN: 900,
        corrMaxPairN: 900,
        corrDropped: ["attr:retries"],
      },
    });
    expect(lines.some((l) => l.includes("1 field pairs over 2 fields"))).toBe(true);
    expect(lines.some((l) => l.includes("900 events with both values (pairwise-complete)"))).toBe(
      true,
    );
    expect(lines.some((l) => l.includes("no numeric values under these filters"))).toBe(true);
    expect(lines.some((l) => l.includes("correlation is not causation"))).toBe(true);
  });

  it("states what a facet grid leaves out", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Bar",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "bar", field: "attr:status" },
      facts: { facetField: "attr:user", facetPanels: 6, facetOmittedValues: 14, facetOmittedCount: 220 },
    });
    const line = lines.find((l) => l.startsWith("split into"))!;
    expect(line).toContain("6 panels by attr:user");
    expect(line).toContain("14 further values (220 events)");
  });

  it("carries the pie readability caution into the export", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Pie / Donut",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "pie", field: "attr:status" },
      facts: { readabilityWarning: "6 slices — past about 4, judging angles gets unreliable." },
    });
    expect(lines.some((l) => l.startsWith("readability: 6 slices"))).toBe(true);
  });
});
