import { describe, expect, it } from "vitest";
import {
  chartConfigToParams,
  chartConfigToStored,
  DEFAULT_CHART_CONFIG,
  paramsToChartConfig,
  parseStoredChartConfig,
  type ChartConfig,
} from "@/components/viz/lib/chartConfig";

const fullConfig: ChartConfig = {
  v: 1,
  field: "attr:src_ip",
  scale: "nominal",
  chartType: "time",
  metric: "ratio",
  compare: { mode: "custom", filters: { q: "error", filters: { artifact: "apache" } } },
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
});
