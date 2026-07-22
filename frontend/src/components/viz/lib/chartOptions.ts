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

export function resolveChartOptions(config: ChartConfig): ResolvedChartOptions {
  const { options } = config;
  const dataKind = CHART_META[config.chartType].dataKind;
  return {
    // Value-over-time charts draw one line per value, so they cap lower than
    // a bar chart's axis does.
    topN: Math.min(options.topN ?? 10, dataKind === "timeseries" ? 20 : 50),
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
