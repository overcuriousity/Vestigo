import { describe, expect, it } from "vitest";
import {
  CHART_ID_PARAM,
  chartConfigToParams,
  chartConfigToStored,
  chartUrlParams,
  DEFAULT_CHART_CONFIG,
  paramsToChartConfig,
  parseStoredChartConfig,
  parseStoredChartFilters,
  unrepresentableFilterMembers,
  type ChartConfig,
} from "@/components/viz/lib/chartConfig";

const fullConfig: ChartConfig = {
  v: 1,
  field: "attr:src_ip",
  fieldY: null,
  fields: null,
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

describe("saved-chart reference", () => {
  it("is cleared by spelling a chart out", () => {
    // The rule the whole feature rests on. `?c_chart=<id>` claims "this is
    // saved chart X"; writing a config out in full is the analyst taking the
    // chart over, which makes that claim false. Because the reference lives in
    // the `c_*` namespace that `chartConfigToParams` clears, no call site has
    // to remember to drop it — and none can forget.
    const params = new URLSearchParams({ [CHART_ID_PARAM]: "chart-1", q: "logon" });
    chartConfigToParams(fullConfig, params);

    expect(params.get(CHART_ID_PARAM)).toBeNull();
    expect(paramsToChartConfig(params)).toEqual(fullConfig);
    // The filter params are not part of that namespace and survive, which is
    // what keeps a takeover from also dropping the chart's slice.
    expect(params.get("q")).toBe("logon");
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

  it("stores the filters the chart was drawn under, and reads them back", () => {
    // A saved chart is the slice it was built over: drop the filters and the
    // story block redraws the excluded data the analyst just filtered out.
    const filters = {
      q: "logon",
      exclusions: { user: ["svc_backup"] },
      tagsExclude: ["known-good"],
      start: "2026-01-01T00:00:00Z",
    };
    const stored = chartConfigToStored(fullConfig, filters);
    expect(parseStoredChartConfig(stored)).toEqual(fullConfig);
    expect(parseStoredChartFilters(stored)).toEqual(filters);
  });

  it("omits the filters key entirely for an unfiltered chart", () => {
    // "Unfiltered" and "saved before filters were captured" must be the same
    // bytes, so neither reads as the other.
    expect("filters" in chartConfigToStored(fullConfig, {})).toBe(false);
    expect("filters" in chartConfigToStored(fullConfig)).toBe(false);
    expect(parseStoredChartFilters(chartConfigToStored(fullConfig))).toEqual({});
  });

  it("reads no filters from a config saved before they were captured", () => {
    expect(parseStoredChartFilters({ v: 1, chartType: "bar" })).toEqual({});
    expect(parseStoredChartFilters(null)).toEqual({});
    expect(parseStoredChartFilters({ v: 1, filters: "nope" })).toEqual({});
  });

  it("stores narrowings the presentation helpers do not count as chips", () => {
    // Gating persistence on `hasActiveFilters` lost these two: it is a chip
    // helper, and neither renders one. A chart whose only narrowing is an
    // excluded tag must not silently redraw over the whole timeline.
    for (const filters of [{ excludeTag: "known-good" }, { annotationTagValue: "triaged" }]) {
      const stored = chartConfigToStored(fullConfig, filters);
      expect("filters" in stored).toBe(true);
      expect(parseStoredChartFilters(stored)).toEqual(filters);
    }
  });

  it("round-trips the three members only an agent chart can carry", () => {
    // `eventIds`/`runId`/`collapseRoutine` have no URL form, so the Explorer
    // never produces them — but a ChartSpec does, and the backend writes them
    // (`_spec_filters_to_payload`). Dropping either half would make the story
    // card draw wider than the frozen export of the same chart.
    const filters = {
      q: "logon",
      ids: ["evt-1", "evt-2"],
      anomalyRunId: "run-7",
      collapseRoutine: true,
    };
    const stored = chartConfigToStored(fullConfig, filters) as Record<string, unknown>;
    const payload = stored.filters as Record<string, unknown>;
    // Backend key names, not the frontend member names.
    expect(payload.eventIds).toEqual(["evt-1", "evt-2"]);
    expect(payload.runId).toBe("run-7");
    expect(payload.collapseRoutine).toBe(true);
    expect(parseStoredChartFilters(stored)).toEqual(filters);
  });

  it("writes only what narrows, so a View's null-filled defaults never appear", () => {
    const stored = chartConfigToStored(fullConfig, { q: "logon" }) as Record<string, unknown>;
    expect(stored.filters).toEqual({ q: "logon" });
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

describe("multi-field serialization", () => {
  // The facet grid was retired; a bookmark or saved chart from before that
  // must still open — as the unfacetted chart, not as an error.
  it("ignores the retired facet params instead of failing to parse the URL", () => {
    const params = new URLSearchParams({
      c_type: "bar",
      c_field: "attr:status",
      c_facet: "attr:user",
      c_facet_n: "99",
    });
    const parsed = paramsToChartConfig(params);
    expect(parsed.chartType).toBe("bar");
    expect(parsed.field).toBe("attr:status");
    expect("facet" in parsed).toBe(false);
  });

  it("ignores a retired facet key on a stored chart config", () => {
    const parsed = parseStoredChartConfig({
      v: 1,
      chartType: "histogram",
      field: "attr:bytes",
      scale: "ratio",
      facet: { field: "attr:user", limit: 6 },
    });
    expect(parsed?.chartType).toBe("histogram");
    expect(parsed && "facet" in parsed).toBe(false);
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

describe("unrepresentableFilterMembers", () => {
  it("names every narrowing the URL would drop", () => {
    expect(
      unrepresentableFilterMembers({
        ids: ["e1", "e2"],
        anomalyRunId: "run-9",
        collapseRoutine: true,
      }),
    ).toEqual(["a fixed event set", "a detector run", "routine collapse"]);
  });

  it("says nothing about filters the URL can carry", () => {
    expect(unrepresentableFilterMembers({ q: "logon", artifacts: ["auth"] })).toEqual([]);
    expect(unrepresentableFilterMembers({})).toEqual([]);
  });

  it("does not count an empty event set as a narrowing", () => {
    expect(unrepresentableFilterMembers({ ids: [], collapseRoutine: false })).toEqual([]);
  });
});

describe("chartConfigToStored filter-key hygiene", () => {
  it("never lets a stray config `filters` key masquerade as stored filters", () => {
    // Guards the day `ChartConfig` grows its own `filters`: the spread inside
    // `chartConfigToStored` would otherwise carry it into storage, and
    // `parseStoredChartFilters` would read it back as a slice nobody chose.
    const polluted = { ...DEFAULT_CHART_CONFIG, filters: { q: "not-a-slice" } } as ChartConfig;
    expect("filters" in chartConfigToStored(polluted)).toBe(false);
    expect(parseStoredChartFilters(chartConfigToStored(polluted))).toEqual({});
    // A real filter set still wins.
    expect(parseStoredChartFilters(chartConfigToStored(polluted, { q: "logon" }))).toEqual({
      q: "logon",
    });
  });
});

describe("chartUrlParams", () => {
  const config: ChartConfig = { ...DEFAULT_CHART_CONFIG, field: "hostname", scale: "nominal" };

  it("writes both namespaces this page owns", () => {
    const params = chartUrlParams(config, { q: "logon" }, new URLSearchParams());
    expect(params.get("c_field")).toBe("hostname");
    expect(params.get("q")).toBe("logon");
  });

  it("drops the chart reference and stale chart keys", () => {
    const prev = new URLSearchParams({ [CHART_ID_PARAM]: "chart-1", c_metric: "rate" });
    const params = chartUrlParams(config, {}, prev);
    expect(params.get(CHART_ID_PARAM)).toBeNull();
    expect(params.get("c_metric")).toBeNull();
  });

  it("drops a filter the new set no longer carries", () => {
    // A filter key absent from `filters` is a *cleared* filter, not a foreign
    // key — carrying it over would resurrect a narrowing that was removed.
    const prev = new URLSearchParams({ q: "logon", tagsInclude: "suspicious" });
    const params = chartUrlParams(config, { q: "logon" }, prev);
    expect(params.get("q")).toBe("logon");
    expect(params.get("tagsInclude")).toBeNull();
  });

  it("carries over every key outside both namespaces", () => {
    const prev = new URLSearchParams({ tour: "viz", c_field: "old", q: "old" });
    const params = chartUrlParams(config, {}, prev);
    expect(params.get("tour")).toBe("viz");
  });
});
