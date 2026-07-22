import { describe, expect, it } from "vitest";
import {
  chartConfigToParams,
  chartConfigToStored,
  DEFAULT_CHART_CONFIG,
  filterParamsPreservingChartConfig,
  paramsToChartConfig,
  parseStoredChartConfig,
  type ChartConfig,
} from "@/components/viz/lib/chartConfig";

const fullConfig: ChartConfig = {
  v: 1,
  field: "attr:src_ip",
  fieldY: null,
  fields: null,
  facet: null,
  scale: "nominal",
  chartType: "time",
  metric: "ratio",
  compare: { mode: "custom", filters: { q: "error", filters: { artifact: ["apache"] } } },
  options: { orientation: "vertical", logScale: true, buckets: 90 },
};

describe("URL round-trip", () => {
  it("round-trips a full config exactly", () => {
    const params = chartConfigToParams(fullConfig);
    expect(paramsToChartConfig(params)).toEqual(fullConfig);
  });

  it("round-trips the default config", () => {
    const params = chartConfigToParams(DEFAULT_CHART_CONFIG);
    expect(paramsToChartConfig(params)).toEqual(DEFAULT_CHART_CONFIG);
  });

  it("leaves non-chart params (Explorer filters) untouched", () => {
    const params = new URLSearchParams({ q: "dos", start: "2024-01-01T00:00:00Z" });
    chartConfigToParams(fullConfig, params);
    expect(params.get("q")).toBe("dos");
    expect(params.get("start")).toBe("2024-01-01T00:00:00Z");
  });

  it("clears stale c_* keys when writing a smaller config", () => {
    const params = chartConfigToParams(fullConfig);
    chartConfigToParams(DEFAULT_CHART_CONFIG, params);
    expect(params.get("c_compare")).toBeNull();
    expect(params.get("c_compare_filters")).toBeNull();
    expect(params.get("c_opts")).toBeNull();
  });

  it("falls back per-field on unknown values instead of discarding everything", () => {
    const params = new URLSearchParams({
      c_type: "sparkline-3d",
      c_scale: "ratio",
      c_metric: "nonsense",
      c_opts: "{not json",
    });
    const config = paramsToChartConfig(params);
    expect(config.chartType).toBe(DEFAULT_CHART_CONFIG.chartType);
    expect(config.scale).toBe("ratio");
    expect(config.metric).toBe("count");
    expect(config.options).toEqual({});
  });

  it("malformed custom-compare filters degrade to compare off", () => {
    const params = new URLSearchParams({ c_compare: "custom", c_compare_filters: "{broken" });
    expect(paramsToChartConfig(params).compare).toEqual({ mode: "off" });
  });

  it("round-trips a two-field chart (c_field_y)", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "pivot",
      field: "attr:username",
      fieldY: "attr:workstation",
      options: { limitX: 8, limitY: 12 },
    };
    const params = chartConfigToParams(config);
    expect(params.get("c_field_y")).toBe("attr:workstation");
    expect(paramsToChartConfig(params)).toEqual(config);
  });

  it("clears a stale c_field_y when the next config has none", () => {
    const params = chartConfigToParams({
      ...DEFAULT_CHART_CONFIG,
      chartType: "sankey",
      field: "artifact",
      fieldY: "attr:status",
    });
    chartConfigToParams(DEFAULT_CHART_CONFIG, params);
    expect(params.get("c_field_y")).toBeNull();
  });
});

describe("filterParamsPreservingChartConfig", () => {
  it("writes the new filters while carrying over every c_* key", () => {
    const prev = chartConfigToParams(fullConfig, new URLSearchParams({ q: "old" }));
    const next = filterParamsPreservingChartConfig(
      { q: "dos", start: "2024-01-01T00:00:00Z", end: "2024-01-02T00:00:00Z" },
      prev,
    );
    // Filters replaced wholesale…
    expect(next.get("q")).toBe("dos");
    expect(next.get("start")).toBe("2024-01-01T00:00:00Z");
    // …chart config untouched (round-trips to the same object).
    expect(paramsToChartConfig(next)).toEqual(fullConfig);
  });

  it("drops removed filters instead of inheriting them", () => {
    const prev = chartConfigToParams(fullConfig, new URLSearchParams({ q: "old" }));
    const next = filterParamsPreservingChartConfig({}, prev);
    expect(next.get("q")).toBeNull();
    expect(paramsToChartConfig(next)).toEqual(fullConfig);
  });
});

describe("stored (saved chart) round-trip", () => {
  it("round-trips a full config through the stored shape", () => {
    expect(parseStoredChartConfig(chartConfigToStored(fullConfig))).toEqual(fullConfig);
  });

  it("rejects unsupported versions", () => {
    expect(parseStoredChartConfig({ ...chartConfigToStored(fullConfig), v: 2 })).toBeNull();
  });

  it("rejects non-object payloads", () => {
    expect(parseStoredChartConfig(null)).toBeNull();
    expect(parseStoredChartConfig("v1")).toBeNull();
  });

  it("baseline compare survives the round-trip", () => {
    const config: ChartConfig = { ...DEFAULT_CHART_CONFIG, compare: { mode: "baseline" } };
    expect(parseStoredChartConfig(chartConfigToStored(config))).toEqual(config);
  });

  it("round-trips a two-field chart through the stored shape", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "scatter",
      scale: "ratio",
      field: "attr:bytes",
      fieldY: "attr:latency",
      options: { sampleLimit: 10000 },
    };
    expect(parseStoredChartConfig(chartConfigToStored(config))).toEqual(config);
  });

  it("loads pre-fieldY v1 configs with fieldY null (additive field)", () => {
    const stored = chartConfigToStored(fullConfig) as Record<string, unknown>;
    delete stored.fieldY;
    expect(parseStoredChartConfig(stored)).toEqual({ ...fullConfig, fieldY: null });
  });

  it("falls back to the default chart type for unknown stored types", () => {
    // An OLD frontend loading a NEWER config (unknown chartType) must degrade
    // gracefully, not error — this locks the forward-compat behavior in.
    const stored = { ...chartConfigToStored(fullConfig), chartType: "hologram" };
    const parsed = parseStoredChartConfig(stored);
    expect(parsed).not.toBeNull();
    expect(parsed?.chartType).toBe(DEFAULT_CHART_CONFIG.chartType);
  });
});

describe("facet and multi-field serialization", () => {
  it("round-trips a facet spec through the URL", () => {
    const config: ChartConfig = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
      facet: { field: "attr:status", limit: 4 },
    };
    const params = chartConfigToParams(config);
    expect(params.get("c_facet")).toBe("attr:status");
    expect(paramsToChartConfig(params).facet).toEqual({ field: "attr:status", limit: 4 });
  });

  it("clamps a facet panel count arriving from a hand-edited URL", () => {
    const params = new URLSearchParams({ c_type: "bar", c_facet: "attr:user", c_facet_n: "99" });
    expect(paramsToChartConfig(params).facet).toEqual({ field: "attr:user", limit: 12 });
  });

  it("round-trips a correlation field list, commas and all", () => {
    const fields = ["attr:bytes", "attr:weird,name", "attr:latency"];
    const params = chartConfigToParams({
      ...DEFAULT_CHART_CONFIG,
      chartType: "corr",
      scale: "ratio",
      fields,
    });
    expect(paramsToChartConfig(params).fields).toEqual(fields);
  });

  it("ignores a malformed field list instead of throwing", () => {
    const params = new URLSearchParams({ c_type: "corr", c_fields: "{not json" });
    expect(paramsToChartConfig(params).fields).toBeNull();
  });
});
