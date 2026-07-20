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
import { CHART_META } from "./chartMeta";
import type { ChartConfig, ChartOptions } from "./chartConfig";

export interface ResolvedChartOptions {
  topN: number;
  bins: number;
  buckets: number;
  limitX: number;
  limitY: number;
  sampleLimit: number;
  orientation: NonNullable<ChartOptions["orientation"]>;
  sort: NonNullable<ChartOptions["sort"]>;
  logScale: boolean;
  seriesMode: NonNullable<ChartOptions["seriesMode"]>;
  legend: boolean;
}

export function resolveChartOptions(config: ChartConfig): ResolvedChartOptions {
  const { options } = config;
  const dataKind = CHART_META[config.chartType].dataKind;
  return {
    // Value-over-time charts draw one line per value, so they cap lower than
    // a bar chart's axis does.
    topN: Math.min(options.topN ?? 10, dataKind === "timeseries" ? 20 : 50),
    bins: options.bins ?? 30,
    buckets: options.buckets ?? 60,
    limitX: options.limitX ?? 10,
    limitY: options.limitY ?? 10,
    sampleLimit: options.sampleLimit ?? 5000,
    orientation: options.orientation ?? "horizontal",
    sort: options.sort ?? "count",
    logScale: options.logScale ?? false,
    seriesMode: options.seriesMode ?? "overlay",
    legend: options.legend ?? true,
  };
}
