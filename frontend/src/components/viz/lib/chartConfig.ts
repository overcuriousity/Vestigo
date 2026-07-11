/**
 * ChartConfig — the single serializable description of a Visualize-page
 * chart. URL state (shareable links), saved charts (Postgres), and export
 * captions all derive from this one object, so what an analyst sees, saves,
 * shares, and exports is the same chart by construction.
 *
 * Versioned (`v: 1`): saved charts round-trip through Postgres and may be
 * loaded by a future frontend — bump the version on breaking shape changes
 * and handle old versions explicitly instead of silently misreading them.
 */
import type { EventFilters } from "@/api/types";
import { filtersToParams, filtersToViewPayload, viewPayloadToFilters } from "@/lib/queryParams";
import type { Metric } from "./transforms";

export type Scale = "nominal" | "ordinal" | "interval" | "ratio";
export type ChartType =
  | "time"
  | "bar"
  | "pie"
  | "heatmap"
  | "line"
  | "histogram"
  | "box"
  | "violin"
  | "ecdf"
  | "punchcard"
  | "pivot"
  | "sankey"
  | "scatter";

export type CompareSpec =
  | { mode: "off" }
  | { mode: "baseline" }
  | { mode: "custom"; filters: EventFilters };

export interface ChartOptions {
  orientation?: "horizontal" | "vertical";
  sort?: "count" | "value";
  logScale?: boolean;
  seriesMode?: "overlay" | "stacked";
  legend?: boolean;
  topN?: number;
  bins?: number;
  buckets?: number;
  /** pivot/sankey: per-axis top-N caps. */
  limitX?: number;
  limitY?: number;
  /** scatter: server-side sample size. */
  sampleLimit?: number;
}

export interface ChartConfig {
  v: 1;
  /** Field token, or null for pure event-count charts ("time"/"punchcard"). */
  field: string | null;
  /** Second field token for two-field charts (pivot/sankey/scatter), else null. */
  fieldY: string | null;
  scale: Scale;
  chartType: ChartType;
  metric: Metric;
  compare: CompareSpec;
  options: ChartOptions;
}

export const DEFAULT_CHART_CONFIG: ChartConfig = {
  v: 1,
  field: null,
  fieldY: null,
  scale: "nominal",
  // Events-over-time is the fresh-load default: it needs no field, runs on the
  // already-optimized single-pass histogram, and never lands on an empty canvas
  // (a bar chart shows nothing until a field is picked and its live scan lands).
  chartType: "time",
  metric: "count",
  compare: { mode: "off" },
  options: {},
};

const CHART_TYPES: ChartType[] = [
  "time",
  "bar",
  "pie",
  "heatmap",
  "line",
  "histogram",
  "box",
  "violin",
  "ecdf",
  "punchcard",
  "pivot",
  "sankey",
  "scatter",
];
const SCALES: Scale[] = ["nominal", "ordinal", "interval", "ratio"];
const METRICS: Metric[] = ["count", "delta", "rate", "ratio", "cumulative"];

/**
 * Write the chart-specific state into *params* under `c_*` keys, leaving the
 * Explorer filter params (q/filters/start/...) untouched — the two live side
 * by side in the Visualize page's URL.
 */
export function chartConfigToParams(
  config: ChartConfig,
  params: URLSearchParams = new URLSearchParams(),
): URLSearchParams {
  for (const key of [...params.keys()].filter((k) => k.startsWith("c_"))) {
    params.delete(key);
  }
  params.set("c_type", config.chartType);
  params.set("c_scale", config.scale);
  if (config.field) params.set("c_field", config.field);
  if (config.fieldY) params.set("c_field_y", config.fieldY);
  if (config.metric !== "count") params.set("c_metric", config.metric);
  if (config.compare.mode !== "off") {
    params.set("c_compare", config.compare.mode);
    if (config.compare.mode === "custom") {
      params.set("c_compare_filters", JSON.stringify(filtersToViewPayload(config.compare.filters)));
    }
  }
  if (Object.keys(config.options).length > 0) {
    params.set("c_opts", JSON.stringify(config.options));
  }
  return params;
}

/**
 * Read a ChartConfig back out of URL params. Unknown/malformed values fall
 * back to defaults field-by-field rather than discarding the whole config.
 */
export function paramsToChartConfig(params: URLSearchParams): ChartConfig {
  const config: ChartConfig = { ...DEFAULT_CHART_CONFIG, compare: { mode: "off" }, options: {} };

  const type = params.get("c_type");
  if (type && (CHART_TYPES as string[]).includes(type)) config.chartType = type as ChartType;
  const scale = params.get("c_scale");
  if (scale && (SCALES as string[]).includes(scale)) config.scale = scale as Scale;
  config.field = params.get("c_field") || null;
  config.fieldY = params.get("c_field_y") || null;
  const metric = params.get("c_metric");
  if (metric && (METRICS as string[]).includes(metric)) config.metric = metric as Metric;

  const compare = params.get("c_compare");
  if (compare === "baseline") {
    config.compare = { mode: "baseline" };
  } else if (compare === "custom") {
    try {
      const payload = JSON.parse(params.get("c_compare_filters") ?? "{}");
      config.compare = { mode: "custom", filters: viewPayloadToFilters(payload) };
    } catch {
      config.compare = { mode: "off" };
    }
  }

  const rawOpts = params.get("c_opts");
  if (rawOpts) {
    try {
      const parsed = JSON.parse(rawOpts);
      if (parsed && typeof parsed === "object") config.options = parsed as ChartOptions;
    } catch {
      // malformed options — keep defaults
    }
  }
  return config;
}

/**
 * Parse a saved chart's stored config JSON. Returns null for unsupported
 * versions or non-object payloads — the caller shows a graceful "saved with
 * an older/newer version" message instead of rendering garbage.
 */
export function parseStoredChartConfig(stored: unknown): ChartConfig | null {
  if (!stored || typeof stored !== "object") return null;
  const raw = stored as Record<string, unknown>;
  if (raw.v !== 1) return null;
  const config: ChartConfig = {
    ...DEFAULT_CHART_CONFIG,
    compare: { mode: "off" },
    options: {},
  };
  if (typeof raw.chartType === "string" && (CHART_TYPES as string[]).includes(raw.chartType)) {
    config.chartType = raw.chartType as ChartType;
  }
  if (typeof raw.scale === "string" && (SCALES as string[]).includes(raw.scale)) {
    config.scale = raw.scale as Scale;
  }
  if (typeof raw.field === "string" && raw.field) config.field = raw.field;
  // Additive v1 field — absent in older saved configs, which load as null.
  if (typeof raw.fieldY === "string" && raw.fieldY) config.fieldY = raw.fieldY;
  if (typeof raw.metric === "string" && (METRICS as string[]).includes(raw.metric)) {
    config.metric = raw.metric as Metric;
  }
  const compare = raw.compare as Record<string, unknown> | undefined;
  if (compare && compare.mode === "baseline") {
    config.compare = { mode: "baseline" };
  } else if (compare && compare.mode === "custom" && compare.filters) {
    config.compare = {
      mode: "custom",
      filters: viewPayloadToFilters(compare.filters as Record<string, unknown>),
    };
  }
  if (raw.options && typeof raw.options === "object") {
    config.options = raw.options as ChartOptions;
  }
  return config;
}

/**
 * Rebuild the URL params for a new filter set while carrying over every
 * `c_*` chart-config key from *prev*. `filtersToParams` builds a FRESH
 * URLSearchParams, so any filter write on the Visualize page (click-to-
 * filter, brush-zoom, reset range) must go through this or it silently
 * wipes the chart config out of the URL.
 */
export function filterParamsPreservingChartConfig(
  next: EventFilters,
  prev: URLSearchParams,
): URLSearchParams {
  const params = filtersToParams(next);
  for (const [k, v] of prev.entries()) {
    if (k.startsWith("c_")) params.set(k, v);
  }
  return params;
}

/** Shape a ChartConfig for storage (saved charts): compare filters go
 * through the same View payload normalization the Views feature uses. */
export function chartConfigToStored(config: ChartConfig): Record<string, unknown> {
  return {
    ...config,
    compare:
      config.compare.mode === "custom"
        ? { mode: "custom", filters: filtersToViewPayload(config.compare.filters) }
        : config.compare,
  };
}
