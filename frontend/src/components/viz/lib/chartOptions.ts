/**
 * One place where a ChartConfig's optional knobs become concrete values.
 *
 * The Visualize page and the agent's chart proposal card render the same
 * `ChartConfig` through the same `vizApi` calls, but each used to apply its
 * own defaults — the page defaulted `buckets` to 60 while the card passed
 * `undefined` and let the backend decide, and the page ignored `buckets`
 * entirely for value-over-time charts. Resolving here means an agent-proposed
 * chart and a hand-built one are the same chart by construction, which is the
 * whole point of the two sharing a config shape.
 *
 * Caps mirror what the page's controls allow, so a config arriving from a URL,
 * a saved chart, or an agent proposal cannot ask for more than the analyst
 * could have asked for by hand.
 */
import { CHART_META, chartTypesFor } from "./chartMeta";
import { isTimeField } from "./timeFields";
import type { ChartConfig, ChartOptions, ChartType, Scale } from "./chartConfig";

/**
 * Preference order for "the chart type to land on for this scale".
 *
 * Not `chartTypesFor(scale)[0]`: `CHART_META`'s key order starts with `time`,
 * whose `scales` is all four, so the naive pick sends every scale to the
 * *field-free* events-over-time chart — dropping the field the analyst just
 * chose. `heatmap` is the interval answer rather than `line`/`histogram`
 * because interval fields include the string-valued `time:date` and
 * `time:year_month`, which have no numeric stats to plot.
 */
const CHART_TYPE_PREFERENCE: ChartType[] = ["bar", "heatmap", "line", "histogram", "time"];

/**
 * Chart types legal for *scale* that could also plot *field*.
 *
 * Scale alone is not enough for a virtual `time:` field. Its SQL yields
 * zero-padded strings and date strings, so `toFloat64OrNull` returns null for
 * every row and any numeric-fed mark (histogram/box/violin/ecdf) or scatter
 * renders empty — and the page's render gates are all `data && <Chart/>`, so
 * "empty" means a blank box with no spinner and no message. `time:date` and
 * `time:year_month` are `interval`, which makes `histogram` and `scatter`
 * offered by scale alone; this is what stops them being offered.
 *
 * The agent's equivalent guard is `propose_chart` raising on a `count == 0`
 * numeric field — same rule, stated as an error rather than a shrunk dropdown.
 */
export function chartTypesForField(scale: Scale, field: string | null): ChartType[] {
  const legal = chartTypesFor(scale);
  if (field == null || !isTimeField(field)) return legal;
  return legal.filter(
    (c) => CHART_META[c].dataKind !== "numeric" && CHART_META[c].dataKind !== "scatter",
  );
}

/** The chart type to select when only the scale and the field are known. */
export function defaultChartTypeForScale(scale: Scale, field: string | null = null): ChartType {
  const legal = chartTypesForField(scale, field);
  return CHART_TYPE_PREFERENCE.find((c) => legal.includes(c)) ?? legal[0];
}

export interface ResolvedChartOptions {
  topN: number;
  /** null = automatic bin count (server-side Freedman–Diaconis). */
  bins: number | null;
  buckets: number;
  limitX: number;
  limitY: number;
  sampleLimit: number;
  orientation: NonNullable<ChartOptions["orientation"]>;
  sort: NonNullable<ChartOptions["sort"]>;
  logScale: boolean;
  seriesMode: NonNullable<ChartOptions["seriesMode"]>;
  legend: boolean;
  showDensity: boolean;
  groups: number;
  showPoints: boolean;
}

/**
 * Hard ceiling on `options.topN`, per chart type.
 *
 * Two different things bound it. The *backend* caps `field-terms` at 500 and
 * `field-timeseries`' `series_limit` at 50, so nothing above those is even
 * fetchable — the timeseries charts sit at their endpoint's ceiling. The rest
 * is legibility: a bar axis can carry hundreds of rows in a scroll container,
 * while a pie past a few dozen slices is unreadable whatever the slider
 * permits (`pieReadability.ts`) and a waffle only has 100 cells to share out.
 *
 * A raised ceiling is also a taller chart: a horizontal bar frame grows with
 * its row count, so PNG export clamps its own resolution to what a canvas may
 * be (`lib/export.ts`) rather than failing on the charts these numbers make
 * reachable.
 *
 * These match what `agent/chart_exec.py` applies: `ANALYST_CHART_LIMITS`
 * carries the endpoint ceiling and `TERMS_TOP_N_BY_CHART` narrows it for the
 * marks bounded by legibility, so an exported chart freezes what the analyst
 * could ask for by hand rather than a smaller *or larger* answer than the
 * card it came from.
 */
export const TOPN_MAX: Record<ChartType, number> = {
  time: 50,
  bar: 500,
  pie: 50,
  waffle: 50,
  heatmap: 50,
  line: 50,
  histogram: 50,
  box: 50,
  violin: 50,
  ecdf: 50,
  punchcard: 50,
  pivot: 50,
  sankey: 50,
  scatter: 50,
  corr: 50,
};

/**
 * Floor on `options.topN`, shared by the slider and the exact-value box beside
 * it. One constant because two different minimums let the controls disagree —
 * a range input silently clamps to its own `min` while React's state holds the
 * lower number, and the thumb then reads a value the chart is not drawing.
 */
export const TOPN_MIN = 1;

/**
 * Where the *slider* stops. The slider is the fast path over the range an
 * analyst wants most of the time; the numeric input beside it is the escape
 * hatch up to `TOPN_MAX`, so raising the ceiling does not cost slider
 * precision in the common range.
 */
export const TOPN_SLIDER_MAX: Record<ChartType, number> = {
  ...TOPN_MAX,
  bar: 50,
  pie: 25,
  waffle: 25,
  heatmap: 20,
  line: 20,
};

export function topNMax(chartType: ChartType): number {
  return TOPN_MAX[chartType] ?? 50;
}

/** Default `topN` when a config does not carry one. */
const TOPN_DEFAULT = 10;

/**
 * Coerce an untrusted `topN` into `[TOPN_MIN, topNMax(chartType)]`.
 *
 * The ceiling alone is not enough: `c_opts` arrives from the URL as
 * `JSON.parse`d, unvalidated data (see `chartConfig.ts`), so a shared or
 * hand-edited link can carry `0` or `"x"`. `0` reached `/viz/field-terms` as
 * `limit=0`, which the endpoint rejects with a 422 — a permanently blank chart
 * with nothing on screen to explain it — and `"x"` did the same as `NaN`.
 */
export function clampTopN(value: unknown, chartType: ChartType): number {
  const n = typeof value === "number" ? Math.round(value) : Number.NaN;
  if (!Number.isFinite(n)) return TOPN_DEFAULT;
  return Math.max(TOPN_MIN, Math.min(n, topNMax(chartType)));
}

export function resolveChartOptions(config: ChartConfig): ResolvedChartOptions {
  const { options } = config;
  return {
    // Per chart type (see TOPN_MAX): a bar axis reaches the backend's 500,
    // while a value-over-time chart draws one line per value and stops at its
    // endpoint's series ceiling. Floored as well as capped — see `clampTopN`.
    topN: clampTopN(options.topN, config.chartType),
    bins: options.bins ?? null,
    buckets: options.buckets ?? 60,
    limitX: options.limitX ?? 10,
    limitY: options.limitY ?? 10,
    sampleLimit: options.sampleLimit ?? 5000,
    orientation: options.orientation ?? "horizontal",
    sort: options.sort ?? "count",
    logScale: options.logScale ?? false,
    seriesMode: options.seriesMode ?? "overlay",
    legend: options.legend ?? true,
    // Grouped box/violin cap mirrors the backend's VIZ_GROUPS_MAX.
    groups: Math.min(options.groups ?? 6, 8),
    showDensity: options.showDensity ?? true,
    showPoints: options.showPoints ?? false,
  };
}
