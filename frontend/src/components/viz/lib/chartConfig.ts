/**
 * ChartConfig — the single serializable description of a Visualize-page
 * chart. URL state (shareable links), saved charts (Postgres), and export
 * captions all derive from this one object, so what an analyst sees, saves,
 * shares, and exports is the same chart by construction.
 *
 * Versioned (`v: 2`): saved charts round-trip through Postgres and may be
 * loaded by a future frontend — bump the version on breaking shape changes
 * and handle old versions explicitly instead of silently misreading them.
 */
import type { CompareTimeResponse, EventFilters, HistogramResponse } from "@/api/types";
import {
  FILTER_PARAM_KEYS,
  filtersToParams,
  filtersToViewPayload,
  viewPayloadToFilters,
} from "@/lib/queryParams";
import type { Metric } from "./transforms";
import { CHART_META } from "./chartMeta";

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
  | "cumulative"
  | "calendar"
  | "change"
  | "lanes"
  | "pivot"
  | "sankey"
  | "scatter"
  | "corr"
  | "table";

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
  /** cumulative: what accumulates — resolved from field/scale when omitted. */
  quantity?: "events" | "sum" | "distinct";
  /** change: one row per value (dumbbell) or two columns (slope). Default dumbbell. */
  layout?: "dumbbell" | "slope";
  /** pivot/sankey: per-axis top-N caps. */
  limitX?: number;
  limitY?: number;
  /** scatter: server-side sample size. */
  sampleLimit?: number;
  /** box/violin: top-N cap when a grouping field (fieldY) is set. */
  groups?: number;
  /** box/violin: jittered raw-value strip overlay; line: point markers. */
  showPoints?: boolean;
  /** table: row order and direction. Default count / desc. */
  tableSortBy?: TableSortColumn;
  tableSortDir?: "asc" | "desc";
  /** table: values whose rows are highlighted — presentation only, captioned. */
  highlight?: string[];
}

export type DeriveSpec =
  | { kind: "bins"; mode: "width" | "log"; count: number }
  | { kind: "bins"; mode: "custom"; edges: number[] }
  | { kind: "timePart"; part: TimePart };
export type TimePart = "hour" | "weekday" | "day" | "week" | "month";

export type TableColumn = "count" | "share" | "first_seen" | "last_seen" | "distinct_second";
export type TableSortColumn = "value" | TableColumn;

/** Figure-specific inputs declared by the registry (`CHART_META[c].inputs`).
 * Only the keys the current figure declares are meaningful; the rest are
 * carried so switching figures and back loses nothing. */
export interface ChartInputs {
  startFilter?: EventFilters;
  endFilter?: EventFilters;
  pairing?: "nextEnd" | "firstLast";
  columns?: TableColumn[];
}

/** A mark is a *source* resolved at render time — never a stored pixel. */
export type MarkSource =
  | { kind: "events"; filters: EventFilters; label?: string }
  | { kind: "baseline"; definitionId: string }
  | { kind: "view"; viewId: string }
  | { kind: "instant"; at: string; label: string }
  | { kind: "range"; start: string; end: string; label: string };

export interface ChartConfig {
  v: 2;
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
  /** Change of scale applied before aggregation, or null for the raw value. */
  derive: DeriveSpec | null;
  inputs: ChartInputs;
  marks: MarkSource[];
}

export const DEFAULT_CHART_CONFIG: ChartConfig = {
  v: 2,
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
  derive: null,
  inputs: {},
  marks: [],
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
  "cumulative",
  "calendar",
  "change",
  "lanes",
  "pivot",
  "sankey",
  "scatter",
  "corr",
  "table",
];
const SCALES: Scale[] = ["nominal", "ordinal", "interval", "ratio"];
const METRICS: Metric[] = ["count", "delta", "rate", "ratio", "cumulative"];

/**
 * Names a *saved* chart in the URL instead of spelling its state out.
 *
 * The alternative — reconstructing `c_*` params from a saved chart's config —
 * cannot carry the filter members that have no URL representation (`ids`,
 * `anomalyRunId`, `collapseRoutine`), so a story block's link to an
 * agent-scoped chart would silently open the whole timeline. Addressing the
 * chart by id lets the page read both halves back out of storage, which is
 * the one place a chart and the slice it describes travel together.
 *
 * Deliberately inside the `c_*` namespace: `chartConfigToParams` clears that
 * namespace before writing, so every path that spells a chart out in full
 * drops the reference by construction, and "this is saved chart X" cannot
 * survive an edit that makes it untrue.
 */
export const CHART_ID_PARAM = "c_chart";

const DERIVE_MODES = ["width", "log"] as const;
const TIME_PARTS: TimePart[] = ["hour", "weekday", "day", "week", "month"];
const PAIRINGS = ["nextEnd", "firstLast"] as const;
const TABLE_COLUMNS: TableColumn[] = ["count", "share", "first_seen", "last_seen", "distinct_second"];

const isRecord = (x: unknown): x is Record<string, unknown> =>
  !!x && typeof x === "object" && !Array.isArray(x);
const isPosInt = (x: unknown): x is number =>
  typeof x === "number" && Number.isInteger(x) && x > 0;

/** A `DeriveSpec` or null — a malformed one is *no* derivation, never a guess. */
export function parseDeriveSpec(raw: unknown): DeriveSpec | null {
  if (!isRecord(raw)) return null;
  if (raw.kind === "bins") {
    if (raw.mode === "custom") {
      const edges = raw.edges;
      if (
        Array.isArray(edges) &&
        edges.length > 0 &&
        edges.every((e) => typeof e === "number" && Number.isFinite(e))
      ) {
        return { kind: "bins", mode: "custom", edges: [...(edges as number[])] };
      }
      return null;
    }
    if ((DERIVE_MODES as readonly unknown[]).includes(raw.mode) && isPosInt(raw.count)) {
      return { kind: "bins", mode: raw.mode as "width" | "log", count: raw.count };
    }
    return null;
  }
  if (raw.kind === "timePart" && (TIME_PARTS as unknown[]).includes(raw.part)) {
    return { kind: "timePart", part: raw.part as TimePart };
  }
  return null;
}

/** Field-by-field: a bad member is dropped, the good ones survive. */
export function parseChartInputs(raw: unknown): ChartInputs {
  const out: ChartInputs = {};
  if (!isRecord(raw)) return out;
  if (isRecord(raw.startFilter)) out.startFilter = viewPayloadToFilters(raw.startFilter);
  if (isRecord(raw.endFilter)) out.endFilter = viewPayloadToFilters(raw.endFilter);
  if ((PAIRINGS as readonly unknown[]).includes(raw.pairing)) {
    out.pairing = raw.pairing as ChartInputs["pairing"];
  }
  if (Array.isArray(raw.columns)) {
    // `[]` is the value-only table the rail's checkboxes can produce and the
    // figure draws; dropping it here snapped every box back on after each edit.
    out.columns = raw.columns.filter((c): c is TableColumn =>
      TABLE_COLUMNS.includes(c as TableColumn),
    );
  }
  return out;
}

/** Each mark is validated on its own; a malformed one is dropped. */
export function parseMarkSources(raw: unknown): MarkSource[] {
  if (!Array.isArray(raw)) return [];
  const out: MarkSource[] = [];
  for (const m of raw) {
    if (!isRecord(m)) continue;
    const label = typeof m.label === "string" ? m.label : undefined;
    switch (m.kind) {
      case "events":
        if (isRecord(m.filters)) {
          const filters = viewPayloadToFilters(m.filters);
          const ids = Array.isArray(m.filters.eventIds)
            ? m.filters.eventIds.filter((v): v is string => typeof v === "string" && v !== "")
            : [];
          if (ids.length) filters.ids = ids;
          out.push({ kind: "events", filters, ...(label !== undefined ? { label } : {}) });
        }
        break;
      case "baseline":
        if (typeof m.definitionId === "string" && m.definitionId) {
          out.push({ kind: "baseline", definitionId: m.definitionId });
        }
        break;
      case "view":
        if (typeof m.viewId === "string" && m.viewId) out.push({ kind: "view", viewId: m.viewId });
        break;
      case "instant":
        if (typeof m.at === "string" && m.at && label !== undefined) {
          out.push({ kind: "instant", at: m.at, label });
        }
        break;
      case "range":
        if (
          typeof m.start === "string" &&
          m.start &&
          typeof m.end === "string" &&
          m.end &&
          label !== undefined
        ) {
          out.push({ kind: "range", start: m.start, end: m.end, label });
        }
        break;
    }
  }
  return out;
}

/** Filters inside inputs/marks travel in the View payload shape, like `compare`. */
function inputsToPayload(inputs: ChartInputs): Record<string, unknown> {
  const out: Record<string, unknown> = { ...inputs };
  if (inputs.startFilter) out.startFilter = filtersToViewPayload(inputs.startFilter);
  if (inputs.endFilter) out.endFilter = filtersToViewPayload(inputs.endFilter);
  return out;
}
export function marksToPayload(marks: MarkSource[]): unknown[] {
  return marks.map((m) =>
    m.kind === "events"
      ? {
          ...m,
          filters: {
            ...filtersToViewPayload(m.filters),
            // A mark's event ids are its provenance, so unlike the Explorer's
            // session-only `ids` they travel with the chart.
            ...(m.filters.ids?.length ? { eventIds: m.filters.ids } : {}),
          },
        }
      : m,
  );
}

/**
 * Bring any stored/URL config object to the v2 key set. v1 lacked `derive`,
 * `inputs` and `marks`; every other key kept its meaning, so the upgrade is
 * lossless and a v2 object passes through untouched. Unknown versions are
 * returned as-is for the caller to refuse.
 */
export function upgradeChartConfig(raw: Record<string, unknown>): Record<string, unknown> {
  if (raw.v === 1) return { ...raw, v: 2, derive: null, inputs: {}, marks: [] };
  return raw;
}

/**
 * Write the chart-specific state into *params* under `c_*` keys, leaving the
 * Explorer filter params (q/filters/start/...) untouched — the two live side
 * by side in the Visualize page's URL.
 *
 * Clears every pre-existing `c_*` key first, :data:`CHART_ID_PARAM` included —
 * see there for why that is the point rather than a side effect.
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
  if (config.derive) params.set("c_derive", JSON.stringify(config.derive));
  if (Object.keys(config.inputs).length > 0) {
    params.set("c_inputs", JSON.stringify(inputsToPayload(config.inputs)));
  }
  if (config.marks.length > 0) params.set("c_marks", JSON.stringify(marksToPayload(config.marks)));
  return params;
}

/**
 * Build the Visualize page's whole query string: this chart, these filters,
 * and everything in *prev* that belongs to neither.
 *
 * Both halves are rewritten wholesale — that is the point, since after an
 * analyst's edit the params are the only record of the chart and a
 * half-updated URL would describe a chart nobody chose. But "wholesale" has to
 * mean the two namespaces this function owns (`c_*` and `FILTER_PARAM_KEYS`)
 * and not the whole query string: the previous implementation mutated a copy
 * of the URL and so preserved unrelated keys by construction, and rebuilding
 * from scratch silently dropped them instead. Nothing else writes this page's
 * URL today, which is exactly why the loss would go unnoticed until something
 * did.
 */
export function chartUrlParams(
  config: ChartConfig,
  filters: EventFilters,
  prev: URLSearchParams,
): URLSearchParams {
  const params = chartConfigToParams(config, filtersToParams(filters));
  for (const [key, value] of prev.entries()) {
    // Ours, and already written above in their current form. A filter key
    // absent from `filters` is a *cleared* filter, so carrying it over would
    // resurrect a narrowing the analyst just removed.
    if (key.startsWith("c_") || FILTER_PARAM_KEYS.has(key)) continue;
    params.append(key, value);
  }
  return params;
}

/**
 * Drop what the figure cannot honour, so nothing downstream can assert it.
 *
 * A `compare` layer on a single-layer figure and a `derive` on a figure whose
 * registry entry admits none are both invisible in the rail and unreachable to
 * clear there, but the caption reads the *config*, not the request — which is
 * how a pie came to print "comparison: all timeline events" under one layer it
 * never fetched, and a cumulative step to claim a binning it never sent. Run
 * on every way a config enters the app (a URL, a saved chart, a Story
 * snapshot), so the rail is not the only thing keeping the two honest.
 */
export function normalizeChartConfig(config: ChartConfig): ChartConfig {
  const meta = CHART_META[config.chartType];
  const dropCompare = config.compare.mode !== "off" && !meta.supportsCompare;
  const dropDerive =
    config.derive != null && !meta.derives.includes(config.derive.kind);
  if (!dropCompare && !dropDerive) return config;
  return {
    ...config,
    ...(dropCompare ? { compare: { mode: "off" as const } } : {}),
    ...(dropDerive ? { derive: null } : {}),
  };
}

/**
 * Read a ChartConfig back out of URL params. Unknown/malformed values fall
 * back to defaults field-by-field rather than discarding the whole config.
 */
export function paramsToChartConfig(params: URLSearchParams): ChartConfig {
  const config: ChartConfig = {
    ...DEFAULT_CHART_CONFIG,
    compare: { mode: "off" },
    options: {},
    inputs: {},
    marks: [],
  };

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
  const json = (key: string): unknown => {
    const raw = params.get(key);
    if (!raw) return undefined;
    try {
      return JSON.parse(raw);
    } catch {
      return undefined;
    }
  };
  config.derive = parseDeriveSpec(json("c_derive"));
  config.inputs = parseChartInputs(json("c_inputs"));
  config.marks = parseMarkSources(json("c_marks"));
  return normalizeChartConfig(config);
}

/**
 * Parse a saved chart's stored config JSON. Returns null for unsupported
 * versions or non-object payloads — the caller shows a graceful "saved with
 * an older/newer version" message instead of rendering garbage.
 */
export function parseStoredChartConfig(stored: unknown): ChartConfig | null {
  if (!stored || typeof stored !== "object") return null;
  const raw = upgradeChartConfig(stored as Record<string, unknown>);
  if (raw.v !== 2) return null;
  const config: ChartConfig = {
    ...DEFAULT_CHART_CONFIG,
    compare: { mode: "off" },
    options: {},
    inputs: {},
    marks: [],
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
  config.derive = parseDeriveSpec(raw.derive);
  config.inputs = parseChartInputs(raw.inputs);
  config.marks = parseMarkSources(raw.marks);
  return normalizeChartConfig(config);
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
  const stored: Record<string, unknown> = {
    ...config,
    compare:
      config.compare.mode === "custom"
        ? { mode: "custom", filters: filtersToViewPayload(config.compare.filters) }
        : config.compare,
    inputs: inputsToPayload(config.inputs),
    marks: marksToPayload(config.marks),
  };
  // `filters` is *this function's* key, never `ChartConfig`'s. Cleared before
  // writing rather than merely overwritten: a future `ChartConfig.filters`
  // would otherwise ride the spread above into storage on every save that
  // passes no filters, and `parseStoredChartFilters` would read it back as a
  // slice the analyst never chose.
  delete stored.filters;
  if (storedFilters) stored.filters = storedFilters;
  return stored;
}

/**
 * Filter members that survive storage but have no `c_*`/filter-param form.
 *
 * `filtersToParams` cannot express any of them, so writing a chart out as URL
 * params drops them — which silently *widens* the chart, since each one only
 * ever narrows. The Visualize page uses this to say so out loud when the
 * analyst's own edit takes a saved chart over (see `takeOver` there), rather
 * than leaving a chart scoped to 40 events looking like one that was always
 * drawn over the whole timeline.
 */
const URL_UNREPRESENTABLE_FILTERS: { key: keyof EventFilters; label: string }[] = [
  { key: "ids", label: "a fixed event set" },
  { key: "anomalyRunId", label: "a detector run" },
  { key: "collapseRoutine", label: "routine collapse" },
];

/** Human-readable labels for the narrowings *filters* would lose in the URL. */
export function unrepresentableFilterMembers(filters: EventFilters): string[] {
  return URL_UNREPRESENTABLE_FILTERS.filter(({ key }) => {
    const value = filters[key];
    return Array.isArray(value) ? value.length > 0 : !!value;
  }).map(({ label }) => label);
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
