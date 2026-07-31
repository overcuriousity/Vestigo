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

  // `c_facet`/`c_facet_n` (the retired facet grid) are deliberately not read:
  // an old URL still parses, and renders the unfacetted chart.
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
  // A `facet` key from a chart saved before the facet grid was retired is
  // ignored, not rejected — the chart still loads, minus its panels.
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

/**
 * Serialize a chart's primary filter layer for storage.
 *
 * Built on `filtersToViewPayload` — the same normalization a saved View uses,
 * which is what lets the backend read both with one translator
 * (`stories/export.py::_filter_payload_to_spec`) — with two deliberate
 * differences, both scoped to charts so saved Views keep behaving exactly as
 * they do today:
 *
 * 1. **Defaults are dropped.** A View payload always writes every key, `null`
 *    or `false` or empty. A chart writes only what narrows, so "unfiltered"
 *    and "saved before filters were captured" are the same bytes (no `filters`
 *    key at all) and neither can read as the other.
 * 2. **Three agent-only members travel too.** `eventIds`, `runId` and
 *    `collapseRoutine` have no URL representation — the Explorer cannot
 *    produce them and a View deliberately does not freeze `collapseRoutine`,
 *    since it derives from live dispositions. An agent's `ChartSpec` *can*
 *    carry all three, and `_spec_filters_to_payload` writes them, so dropping
 *    them here would silently widen a chart the agent had scoped: the saved
 *    chart, the story card and the frozen export would each show more than the
 *    card the analyst clicked Save on.
 *
 * Returns `null` when nothing narrows.
 */
function chartFiltersToStored(filters: EventFilters): Record<string, unknown> | null {
  const payload = filtersToViewPayload(filters);
  const stored: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined || value === false) continue;
    if (Array.isArray(value) && value.length === 0) continue;
    if (typeof value === "object" && Object.keys(value).length === 0) continue;
    stored[key] = value;
  }
  // Deliberately not in `filtersToViewPayload`: see (2) above.
  if (filters.ids?.length) stored.eventIds = [...filters.ids];
  if (filters.anomalyRunId) stored.runId = filters.anomalyRunId;
  if (filters.collapseRoutine) stored.collapseRoutine = true;
  return Object.keys(stored).length > 0 ? stored : null;
}

/** Shape a ChartConfig for storage (saved charts): compare filters go
 * through the same View payload normalization the Views feature uses.
 *
 * *filters* is the primary layer — the Explorer filters the chart was built
 * under. They are stored as a sibling key rather than inside `ChartConfig`
 * because on the live page the URL owns them (see `parseStoredChartFilters`),
 * and are omitted entirely when nothing narrows so an unfiltered chart stores
 * exactly what it stored before this key existed. */
export function chartConfigToStored(
  config: ChartConfig,
  filters?: EventFilters,
): Record<string, unknown> {
  const storedFilters = filters ? chartFiltersToStored(filters) : null;
  return {
    ...config,
    compare:
      config.compare.mode === "custom"
        ? { mode: "custom", filters: filtersToViewPayload(config.compare.filters) }
        : config.compare,
    ...(storedFilters ? { filters: storedFilters } : {}),
  };
}

/**
 * Read a saved chart's frozen primary filters back out of its stored config.
 *
 * Separate from `parseStoredChartConfig` because the two answer different
 * questions: the chart *shape* is `ChartConfig` (also URL state, where the
 * Explorer filter params live beside it under their own keys), while these
 * filters exist only in storage — the one place the two travel together, the
 * same way `View.view_filter` does.
 *
 * The exact inverse of `chartFiltersToStored`, including the three members
 * `viewPayloadToFilters` does not know about. Reading them back matters as
 * much as writing them: `serializeEventFilterParams` does send all three to
 * the API, so a chart block that dropped them would draw wider on screen than
 * the same chart frozen into an export.
 *
 * Returns `{}` for a chart saved before filters were captured, which is what
 * makes those charts keep rendering exactly as they do today.
 */
export function parseStoredChartFilters(stored: unknown): EventFilters {
  if (!stored || typeof stored !== "object") return {};
  const raw = (stored as Record<string, unknown>).filters;
  if (!raw || typeof raw !== "object") return {};
  const payload = raw as Record<string, unknown>;
  const filters = viewPayloadToFilters(payload);
  const eventIds = payload.eventIds;
  if (Array.isArray(eventIds) && eventIds.length > 0) {
    filters.ids = eventIds.map(String);
  }
  if (typeof payload.runId === "string" && payload.runId) {
    filters.anomalyRunId = payload.runId;
  }
  if (payload.collapseRoutine === true) filters.collapseRoutine = true;
  return filters;
}
