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
      "showing 5,000 of 120,000 points (uniform sample, stable across reruns; axes span full data)",
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

  it("does not credit Freedman–Diaconis for a fallback or a clamped count", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
    };
    const fallback = buildCaptionLines({
      ...base,
      chartLabel: "Histogram",
      config,
      facts: { binCount: 30, valueMin: 0, valueMax: 100, binRule: "fd_fallback" },
    });
    expect(fallback).toContain(
      "30 fixed-width bins over [0, 100] (no interquartile spread — the automatic rule is undefined; fixed default)",
    );
    expect(fallback.join(" ")).not.toContain("Freedman–Diaconis automatic width");

    const clamped = buildCaptionLines({
      ...base,
      chartLabel: "Histogram",
      config,
      facts: { binCount: 60, valueMin: 0, valueMax: 100, binRule: "fd", binCountClamped: true },
    });
    expect(clamped).toContain(
      "60 fixed-width bins over [0, 100] (Freedman–Diaconis, clamped to the allowed bin range)",
    );
  });

  it("spells out that grouped violin widths compare shape, not group size", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Violin plot",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "violin",
        field: "attr:latency",
        fieldY: "attr:user",
        scale: "ratio",
      },
      facts: {
        groupField: "attr:user",
        groupsShown: 3,
        groupedViolin: true,
      },
    });
    const line = lines.find((l) => l.startsWith("violin widths"))!;
    expect(line).toContain("relative frequency");
    expect(line).toContain("not its size");
  });

  it("cautions when the grouping field looks like an identifier", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Box plot",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "box",
        field: "attr:latency",
        fieldY: "attr:event_id",
        scale: "ratio",
      },
      facts: { groupField: "attr:event_id", groupsShown: 8, groupDistinct: 4210 },
    });
    expect(lines.some((l) => l.includes("usually an identifier"))).toBe(true);
  });

  it("never presents an untested normality default as a recommendation", () => {
    const stats = {
      n: 10,
      basis: "full" as const,
      pearson: { r: 0.5, p: 0.1 },
      spearman: { rho: 0.4, p: 0.2 },
      kendall: null,
      regression: null,
      shapiro: { x: null, y: null, basis: "sample" as const, n: 0 },
      recommendation: "spearman" as const,
      recommendation_basis: "default" as const,
    };
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Scatter",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "scatter",
        field: "attr:bytes",
        fieldY: "attr:latency",
        scale: "ratio",
      },
      facts: { scatterStats: stats },
    });
    expect(lines.some((l) => l.includes("normality could not be tested here"))).toBe(true);
    expect(lines.join(" ")).not.toContain("recommended coefficient");
  });

  // The test's power grows with n, so at the API's sample ceiling it rejects
  // deviations too small to change which coefficient to quote. The caption is
  // the export text — without this it reads as a finding about the data.
  it("qualifies a Shapiro–Wilk verdict drawn from a large sample", () => {
    const stats = (n: number) => ({
      n: 50_000,
      basis: "full" as const,
      pearson: { r: 0.5, p: 1e-9 },
      spearman: { rho: 0.4, p: 1e-8 },
      kendall: null,
      regression: null,
      shapiro: {
        x: { w: 0.98, p: 0.001 },
        y: { w: 0.99, p: 0.2 },
        basis: "sample" as const,
        n,
      },
      recommendation: "spearman" as const,
      recommendation_basis: "shapiro" as const,
    });
    const captionFor = (n: number) =>
      buildCaptionLines({
        ...base,
        chartLabel: "Scatter",
        config: {
          ...DEFAULT_CHART_CONFIG,
          chartType: "scatter",
          field: "attr:bytes",
          fieldY: "attr:latency",
          scale: "ratio",
        },
        facts: { scatterStats: stats(n) },
      })
        .find((l) => l.startsWith("recommended coefficient"))!;

    expect(captionFor(5000)).toContain("flags even slight departures from normality");
    // A small sample carries no such caveat — the verdict stands on its own.
    expect(captionFor(120)).not.toContain("flags even slight departures");
  });

  it("states the point-overlay sample honestly", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Violin plot",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "violin", field: "attr:latency", scale: "ratio" },
      facts: { overlayShown: 1000, overlayTotal: 52341 },
    });
    expect(lines).toContain(
      "point overlay: showing 1,000 of 52,341 values (uniform sample, stable across reruns)",
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

describe("buildCaptionLines — derivations", () => {
  it("names the derivation, its edges and the unbinnable count", () => {
    const lines = buildCaptionLines({
      caseId: "c",
      timelineId: "t",
      chartLabel: "Bar",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "bar",
        field: "attr:bytes",
        scale: "ratio",
        derive: { kind: "bins", mode: "log", count: 3 },
      },
      filters: {},
      facts: {
        derive: { kind: "bins", labels: [], edges: [10, 100], negative_bin: true },
        distinct: 3,
        shownValues: 3,
        otherCount: 0,
      },
    });
    expect(lines).toContain("field: attr:bytes (ratio → ordered categories) — Bar");
    expect(lines).toContain(
      "derived: grouped into 3 log-spaced ranges (edges: 10 · 100); values ≤ 0 in their own range — values that do not parse as numbers are not counted",
    );
  });

  it("says a calendar part is UTC and drops what does not parse", () => {
    const lines = buildCaptionLines({
      caseId: "c",
      timelineId: "t",
      chartLabel: "Bar",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "bar",
        field: "attr:logon_at",
        scale: "interval",
        derive: { kind: "timePart", part: "weekday" },
      },
      filters: {},
      facts: {},
    });
    expect(lines).toContain(
      "derived: calendar part: day of week (UTC) — values that do not parse as timestamps are not counted; parts are UTC",
    );
  });
});

describe("buildCaptionLines — table", () => {
  it("names the sort, the share denominator, the remainder and the highlighted rows", () => {
    const lines = buildCaptionLines({
      caseId: "c",
      timelineId: "t",
      chartLabel: "Table (values with counts)",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "table",
        field: "attr:user",
        scale: "nominal",
        options: { tableSortBy: "last_seen", tableSortDir: "asc", highlight: ["alice", "bob"] },
      },
      filters: {},
      facts: {
        tableTotal: 11,
        distinct: 4,
        shownValues: 2,
        tableSort: { by: "last_seen", dir: "asc" },
        tableHighlight: ["alice", "bob"],
        tableRemainder: { count: 3, distinctValues: 2 },
      },
    });
    expect(lines).toContain("sorted by last seen (ascending)");
    expect(lines).toContain("share = count / 11 events with a non-empty attr:user");
    expect(lines).toContain(
      "showing top 2 of 4 distinct values; 3 events across 2 more values in the remainder row",
    );
    expect(lines).toContain("highlighted rows: alice · bob — presentation only");
    expect(lines.some((l) => l.includes("(capped"))).toBe(false);
  });
});

describe("buildCaptionLines — marks", () => {
  it("appends one line per mark source with provenance", () => {
    const lines = buildCaptionLines({
      caseId: "c",
      timelineId: "t",
      chartLabel: "Events over time",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "time",
        marks: [{ kind: "instant", at: "2026-07-20T09:41:00Z", label: "first" }],
      },
      filters: {},
      facts: {
        marks: {
          cap: 50,
          marks: [
            {
              kind: "instant",
              at: "2026-07-20T09:41:00+00:00",
              label: "first",
              source: 0,
              provenance: { kind: "analyst" },
            },
          ],
          sources: [
            { index: 0, kind: "instant", label: "first", count: 1, shown: 1, overflow: false, undated: 0 },
          ],
        },
      },
    });
    expect(lines).toContain('mark #1: "first" at 2026-07-20 09:41:00Z — analyst-placed');
  });
});

describe("buildCaptionLines — cumulative and calendar", () => {
  it("names the quantity, the field, the bucket width and what was not summed", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Cumulative step (running total over time)",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "cumulative",
        field: "attr:bytes",
        scale: "ratio",
      },
      facts: {
        intervalSeconds: 3600,
        cumulative: { quantity: "sum", field: "attr:bytes", total: 40, events: 7, unparsed: 1 },
      },
    });
    expect(lines).toContain(
      "cumulative sum of attr:bytes (measure) over time — Cumulative step (running total over time)",
    );
    expect(lines).toContain("1 h buckets, UTC");
    expect(lines).toContain(
      "final value 40 over 7 events; 1 event with no numeric attr:bytes value not summed",
    );
  });

  it("says what a distinct count and a plain event count accumulate", () => {
    const distinct = buildCaptionLines({
      ...base,
      chartLabel: "Cumulative step",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "cumulative",
        field: "attr:user",
        scale: "nominal",
      },
      facts: {
        cumulative: { quantity: "distinct", field: "attr:user", total: 4, events: 7, unparsed: 1 },
      },
    });
    expect(distinct).toContain("distinct values of attr:user seen so far — Cumulative step");
    expect(distinct).toContain(
      "4 distinct values over 7 events; 1 event with an empty attr:user not counted",
    );
    const events = buildCaptionLines({
      ...base,
      chartLabel: "Cumulative step",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "cumulative" },
      facts: { cumulative: { quantity: "events", field: null, total: 7, events: 7, unparsed: 0 } },
    });
    expect(events).toContain("cumulative event count over time — Cumulative step");
    expect(events).toContain("final value 7 over 7 events");
  });

  it("states the calendar's UTC day boundary, its span and the week cap", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Calendar heatmap (events per day)",
      config: { ...DEFAULT_CHART_CONFIG, chartType: "calendar", field: "attr:user" },
      facts: {
        calendar: {
          field: "attr:user",
          start: "2025-07-21",
          end: "2026-07-22",
          weeks: 53,
          weeksTotal: 61,
          truncated: true,
          dropped: 9,
          total: 400,
        },
      },
    });
    expect(lines).toContain(
      "events with a attr:user value per day, UTC — Calendar heatmap (events per day)",
    );
    expect(lines).toContain("53 weeks, 2025-07-21 → 2026-07-22, day boundaries UTC");
    expect(lines).toContain("latest 53 of 61 weeks drawn; 9 earlier events not drawn");
  });
});

describe("buildCaptionLines — ranked change", () => {
  const change = {
    topN: 10,
    unionSize: 14,
    rowsShown: 14,
    truncated: false,
    omitted: 0,
    newCount: 2,
    vanishedCount: 1,
  };

  it("names share-of-window, both totals, the per-window top-N, the union and new/vanished", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Ranked change (share of window, two windows)",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "change",
        field: "attr:user",
        compare: { mode: "baseline" },
      },
      facts: { primaryTotal: 20, comparisonTotal: 1000, change },
    });
    expect(lines).toContain(
      "share-of-window change of attr:user between two windows — Ranked change (share of window, two windows)",
    );
    expect(lines).toContain("comparison: all timeline events (same time range) — 1,000 events");
    expect(lines).toContain(
      "ranked by |Δ share of window|, not by count; top 10 values per window, 14 in the union — 2 new, 1 vanished",
    );
    expect(lines.some((l) => l?.startsWith("union capped"))).toBe(false);
  });

  it("discloses the union cap and what it dropped", () => {
    const lines = buildCaptionLines({
      ...base,
      chartLabel: "Ranked change",
      config: {
        ...DEFAULT_CHART_CONFIG,
        chartType: "change",
        field: "attr:user",
        compare: { mode: "custom", filters: { q: "phase-a" } },
      },
      facts: {
        primaryTotal: 20,
        comparisonTotal: 10,
        change: { ...change, unionSize: 260, rowsShown: 200, truncated: true, omitted: 60, newCount: 0, vanishedCount: 0 },
      },
    });
    expect(lines).toContain(
      "ranked by |Δ share of window|, not by count; top 10 values per window, 260 in the union",
    );
    expect(lines).toContain(
      "union capped at 200 of 260 values; the 60 with the smallest change not drawn",
    );
  });
});
