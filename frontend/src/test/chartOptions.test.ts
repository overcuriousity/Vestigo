/**
 * resolveChartOptions — the one place a ChartConfig's optional knobs become
 * concrete. Shared by the Visualize page and the agent's ChartProposalCard so
 * an agent-proposed chart and a hand-built one are the same chart; before it
 * existed the two applied different defaults.
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";
import {
  resolveChartOptions,
  defaultChartTypeForScale,
  chartTypesForField,
  TOPN_MAX,
  TOPN_MIN,
  TOPN_SLIDER_MAX,
} from "@/components/viz/lib/chartOptions";
import { chartTypesFor, SCALES } from "@/components/viz/lib/chartMeta";
import { TIME_FIELDS } from "@/components/viz/lib/timeFields";

const config = (patch: Partial<ChartConfig>): ChartConfig => ({
  ...DEFAULT_CHART_CONFIG,
  ...patch,
});

describe("resolveChartOptions", () => {
  it("fills every option with the analyst-facing default", () => {
    expect(resolveChartOptions(config({ chartType: "bar" }))).toEqual({
      topN: 10,
      bins: null,
      showDensity: true,
      groups: 6,
      showPoints: false,
      buckets: 60,
      quantity: "events",
      layout: "dumbbell",
      limitX: 10,
      limitY: 10,
      sampleLimit: 5000,
      orientation: "horizontal",
      sort: "count",
      logScale: false,
      seriesMode: "overlay",
      legend: true,
      tableSortBy: "count",
      tableSortDir: "desc",
      highlight: [],
    });
  });

  it("passes explicit values through", () => {
    const resolved = resolveChartOptions(
      config({ chartType: "bar", options: { topN: 25, logScale: true, sort: "value" } }),
    );
    expect(resolved.topN).toBe(25);
    expect(resolved.logScale).toBe(true);
    expect(resolved.sort).toBe("value");
  });

  it("caps topN per chart type, lower for value-over-time and pie than for a bar axis", () => {
    // A bar axis reaches the field-terms endpoint's 500; a timeseries stops at
    // the series_limit ceiling of 50; a pie is bounded by legibility, not by
    // what is fetchable.
    expect(resolveChartOptions(config({ chartType: "bar", options: { topN: 999 } })).topN).toBe(500);
    expect(resolveChartOptions(config({ chartType: "line", options: { topN: 999 } })).topN).toBe(50);
    expect(
      resolveChartOptions(config({ chartType: "heatmap", options: { topN: 999 } })).topN,
    ).toBe(50);
    expect(resolveChartOptions(config({ chartType: "pie", options: { topN: 999 } })).topN).toBe(50);
  });

  it("keeps every slider ceiling at or below the hard ceiling it escapes from", () => {
    // A slider max above TOPN_MAX would let the analyst drag to a value the
    // resolver then silently clamps — the exact failure #297 was filed for.
    for (const type of Object.keys(TOPN_MAX) as (keyof typeof TOPN_MAX)[]) {
      expect(TOPN_SLIDER_MAX[type]).toBeLessThanOrEqual(TOPN_MAX[type]);
    }
  });

  it("keeps every slider floor at the shared minimum the number box allows", () => {
    // A range input clamps to its own `min` while React state holds the lower
    // number — the slider then reads a value the chart is not drawing. One
    // constant for both controls is what stops them disagreeing.
    expect(TOPN_MIN).toBeLessThanOrEqual(Math.min(...Object.values(TOPN_SLIDER_MAX)));
  });

  it("keeps a legend explicitly turned off, rather than treating false as unset", () => {
    expect(resolveChartOptions(config({ chartType: "line", options: { legend: false } })).legend).toBe(
      false,
    );
  });

  it("floors an explicit zero rather than treating it as unset", () => {
    // Two distinct things are asserted here. `0` is falsy, so a `||` default
    // would turn it into 10 and hide the analyst's input; and `0` is also not
    // a drawable answer — it reached `/viz/field-terms` as `limit=0`, which the
    // endpoint rejects with a 422, leaving a permanently blank chart with
    // nothing on screen to explain it. It lands on the floor, not the default.
    expect(resolveChartOptions(config({ chartType: "bar", options: { topN: 0 } })).topN).toBe(
      TOPN_MIN,
    );
    expect(resolveChartOptions(config({ chartType: "bar", options: { topN: -5 } })).topN).toBe(
      TOPN_MIN,
    );
  });

  it("falls back to the default for a topN that is not a number", () => {
    // `c_opts` arrives from the URL as `JSON.parse`d, unvalidated data, so a
    // shared or hand-edited link can carry anything at all. `"x"` used to
    // resolve to `NaN` and blank the chart the same way `0` did.
    const opts = (topN: unknown) => ({ topN }) as unknown as ChartConfig["options"];
    expect(resolveChartOptions(config({ chartType: "bar", options: opts("x") })).topN).toBe(10);
    expect(resolveChartOptions(config({ chartType: "bar", options: opts(null) })).topN).toBe(10);
    expect(
      resolveChartOptions(config({ chartType: "bar", options: opts(Number.NaN) })).topN,
    ).toBe(10);
  });

  it("rounds a fractional topN, which the endpoint's integer limit requires", () => {
    expect(resolveChartOptions(config({ chartType: "bar", options: { topN: 12.6 } })).topN).toBe(13);
  });
});

describe("defaultChartTypeForScale", () => {
  it("never lands on a field-free chart, which would drop the picked field", () => {
    // The naive `chartTypesFor(s)[0]` returns "time" for every scale, because
    // CHART_META is keyed with `time` first and it is legal under all four.
    for (const scale of SCALES) {
      expect(defaultChartTypeForScale(scale)).not.toBe("time");
      expect(defaultChartTypeForScale(scale)).not.toBe("punchcard");
    }
  });

  it("picks a chart that is legal for the scale", () => {
    for (const scale of SCALES) {
      expect(chartTypesFor(scale)).toContain(defaultChartTypeForScale(scale));
    }
  });

  it("maps the scales a time field can carry", () => {
    // time:hour_of_day / day_of_week / month / ... are ordinal.
    expect(defaultChartTypeForScale("ordinal")).toBe("bar");
    expect(defaultChartTypeForScale("nominal")).toBe("bar");
    // time:date / time:year_month are interval and string-valued, so the
    // numeric marks would render empty — heatmap plots their strings.
    expect(defaultChartTypeForScale("interval")).toBe("heatmap");
    expect(defaultChartTypeForScale("ratio")).toBe("line");
  });
});

describe("chartTypesForField", () => {
  it("leaves an ordinary field's options untouched", () => {
    for (const scale of SCALES) {
      expect(chartTypesForField(scale, "attr:bytes")).toEqual(chartTypesFor(scale));
      expect(chartTypesForField(scale, null)).toEqual(chartTypesFor(scale));
    }
  });

  it("drops the numeric marks for a time field, which are string-valued", () => {
    // `time:date` is interval, so scale alone offers histogram and scatter —
    // and both would render an empty box with no spinner and no message,
    // because the numeric probe is disabled for time fields.
    expect(chartTypesFor("interval")).toContain("histogram");
    expect(chartTypesFor("interval")).toContain("scatter");
    const offered = chartTypesForField("interval", "time:date");
    expect(offered).not.toContain("histogram");
    expect(offered).not.toContain("scatter");
    expect(offered).toContain("heatmap");
  });

  it("never leaves a time field with nothing to plot", () => {
    for (const token of Object.keys(TIME_FIELDS)) {
      const scale = TIME_FIELDS[token].scale;
      expect(chartTypesForField(scale, token).length).toBeGreaterThan(0);
      // ...and the default pick is one of them.
      expect(chartTypesForField(scale, token)).toContain(defaultChartTypeForScale(scale, token));
    }
  });
});

describe("resolveChartOptions — cumulative quantity", () => {
  it("defaults from field and scale, and keeps an explicit choice", () => {
    const base = { ...DEFAULT_CHART_CONFIG, chartType: "cumulative" as const };
    expect(resolveChartOptions({ ...base, field: null }).quantity).toBe("events");
    expect(resolveChartOptions({ ...base, field: "attr:bytes", scale: "ratio" }).quantity).toBe(
      "sum",
    );
    expect(
      resolveChartOptions({ ...base, field: "attr:user", scale: "nominal" }).quantity,
    ).toBe("distinct");
    expect(
      resolveChartOptions({
        ...base,
        field: "attr:user",
        scale: "nominal",
        options: { quantity: "events" },
      }).quantity,
    ).toBe("events");
  });
});

describe("resolveChartOptions — ranked change", () => {
  it("defaults the layout to dumbbell and keeps an explicit slope", () => {
    const base = { ...DEFAULT_CHART_CONFIG, chartType: "change" as const, field: "artifact" };
    expect(resolveChartOptions(base).layout).toBe("dumbbell");
    expect(resolveChartOptions({ ...base, options: { layout: "slope" } }).layout).toBe("slope");
  });

  it("caps the per-window top-N at 100 with the slider stopping at 20", () => {
    expect(TOPN_MAX.change).toBe(100);
    expect(TOPN_SLIDER_MAX.change).toBe(20);
  });
});
