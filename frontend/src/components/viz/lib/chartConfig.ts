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
import type { CompareTimeResponse, EventFilters, HistogramResponse } from "@/api/types";
import { filtersToParams, filtersToViewPayload, viewPayloadToFilters } from "@/lib/queryParams";
import type { Metric } from "./transforms";

export type Scale = "nominal" | "ordinal" | "interval" | "ratio";
export type ChartType =
  | "time"
  | "bar"
  | "pie"
  | "waffle"
  | "heatmap"
  | "line"
  | "histogram"
  | "box"
  | "violin"
  | "ecdf"
  | "punchcard"
  | "pivot"
  | "sankey"
  | "scatter"
  | "corr";

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
  /** Histogram bin count; omitted = automatic Freedman–Diaconis width. */
  bins?: number;
  /** Histogram: smoothed density (KDE) curve overlay. Default on. */
  showDensity?: boolean;
  buckets?: number;
  /** pivot/sankey: per-axis top-N caps. */
  limitX?: number;
  limitY?: number;
  /** scatter: server-side sample size. */
  sampleLimit?: number;
  /** box/violin: top-N cap when a grouping field (fieldY) is set. */
  groups?: number;
  /** box/violin: jittered raw-value strip overlay; line: point markers. */
  showPoints?: boolean;
}

/** Small multiples: one panel per top value of a categorical field. */
export interface FacetSpec {
  field: string;
  /** Panels to draw (top values by event count). Clamped to 2–12. */
  limit: number;
}

export interface ChartConfig {
  v: 1;
  /** Field token, or null for pure event-count charts ("time"/"punchcard"). */
  field: string | null;
  /** Second field token for two-field charts (pivot/sankey/scatter), or the
   * optional categorical grouping field for box/violin; else null. */
  fieldY: string | null;
  /** Field list for the correlation matrix (2–8 numeric tokens), else null.
   * JSON-encoded in the URL rather than comma-joined: attribute tokens are
   * user data and may legitimately contain a comma. */
  fields: string[] | null;
  scale: Scale;
  chartType: ChartType;
  metric: Metric;
  compare: CompareSpec;
  /** Facet grid, or null. Mutually exclusive with a comparison layer: one
   * splits the data into panels, the other overlays two layers in one. */
  facet: FacetSpec | null;
  options: ChartOptions;
}

export const DEFAULT_CHART_CONFIG: ChartConfig = {
  v: 1,
  field: null,
  fieldY: null,
  fields: null,
  scale: "nominal",
  // Events-over-time is the fresh-load default: it needs no field, runs on the
  // already-optimized single-pass histogram, and never lands on an empty canvas
  // (a bar chart shows nothing until a field is picked and its live scan lands).
  chartType: "time",
  metric: "count",
  compare: { mode: "off" },
  facet: null,
  options: {},
};

const CHART_TYPES: ChartType[] = [
  "time",
  "bar",
  "pie",
  "waffle",
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
  "corr",
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
  if (config.fields?.length) params.set("c_fields", JSON.stringify(config.fields));
  if (config.metric !== "count") params.set("c_metric", config.metric);
  if (config.compare.mode !== "off") {
    params.set("c_compare", config.compare.mode);
    if (config.compare.mode === "custom") {
      params.set("c_compare_filters", JSON.stringify(filtersToViewPayload(config.compare.filters)));
    }
  }
  if (config.facet) {
    params.set("c_facet", config.facet.field);
    params.set("c_facet_n", String(config.facet.limit));
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
  const rawFields = params.get("c_fields");
  if (rawFields) {
    try {
      const parsed = JSON.parse(rawFields);
      if (Array.isArray(parsed) && parsed.every((f) => typeof f === "string")) {
        config.fields = parsed;
      }
    } catch {
      // malformed field list — chart falls back to no selection
    }
  }
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

  const facetField = params.get("c_facet");
  if (facetField) {
    const limit = Number(params.get("c_facet_n"));
    config.facet = {
      field: facetField,
      limit: Number.isFinite(limit) ? Math.min(12, Math.max(2, limit)) : 6,
    };
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
  if (Array.isArray(raw.fields) && raw.fields.every((f) => typeof f === "string")) {
    config.fields = raw.fields as string[];
  }
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
  const facet = raw.facet as Record<string, unknown> | undefined;
  if (facet && typeof facet.field === "string" && facet.field) {
    const limit = Number(facet.limit);
    config.facet = {
      field: facet.field,
      limit: Number.isFinite(limit) ? Math.min(12, Math.max(2, limit)) : 6,
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

/** Adapt the single-layer histogram response to the compare shape so one
 * chart component (CompareHistogram) renders both the compare-off and
 * compare-on cases — shared by the Visualize page and `ChartProposalCard`
 * (the agent's `propose_chart` "time"/"compare_time" kinds). */
export function histogramToCompare(h: HistogramResponse): CompareTimeResponse {
  return {
    kind: "time",
    interval_seconds: h.interval_seconds,
    min: h.min,
    max: h.max,
    buckets: h.buckets.map((b) => ({ start: b.start, primary: b.count, comparison: 0 })),
    primary_total: h.buckets.reduce((sum, b) => sum + b.count, 0),
    comparison_total: 0,
  };
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
