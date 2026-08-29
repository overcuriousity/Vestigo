/**
 * Chart data fetching, split from the rendering components so the module
 * exports plain functions (and so `ChartCanvas` stays fast-refresh clean).
 *
 * The return type names the discriminated aggregation payload both the live
 * canvas and the Stories snapshot renderer draw from.
 */
import { vizApi, type CompareMode } from "@/api/viz";
import { eventsApi } from "@/api/events";
import { histogramToCompare, type ChartConfig } from "@/components/viz/lib/chartConfig";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import type { ResolvedChartOptions } from "@/components/viz/lib/chartOptions";
import type {
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
  FieldCorrelationResponse,
  FieldNumericGroupedResponse,
  FieldNumericResponse,
  FieldPivotResponse,
  FieldScatterResponse,
  FieldTableResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
  HistogramResponse,
  PunchcardResponse,
} from "@/api/types";

/**
 * Fetch the aggregation a chart config asks for. Standalone (not inlined in
 * the component) so its return type names the discriminated payload both the
 * live canvas and the Stories snapshot renderer draw from.
 */
export async function fetchChartData(
  caseId: string,
  timelineId: string,
  config: ChartConfig,
  filters: EventFilters,
  opts: ResolvedChartOptions,
) {
  const dataKind = CHART_META[config.chartType].dataKind;
  const groupedOn =
    !!CHART_META[config.chartType].acceptsSecondField && !!config.fieldY;
  const compareApiSpec: CompareMode | null =
    config.compare.mode === "baseline"
      ? { mode: "baseline" }
      : config.compare.mode === "custom"
        ? { mode: "custom", filters: config.compare.filters }
        : null;
  switch (dataKind) {
    case "terms":
      if (compareApiSpec) {
        return {
          kind: "terms" as const,
          compare: true as const,
          data: (await vizApi.compare(caseId, timelineId, {
            kind: "terms",
            field: config.field!,
            primary: filters,
            comparison: compareApiSpec,
            limit: opts.topN,
            derive: config.derive,
          })) as CompareTermsResponse,
        };
      }
      return {
        kind: "terms" as const,
        compare: false as const,
        data: await vizApi.fieldTerms(
          caseId,
          timelineId,
          config.field!,
          filters,
          opts.topN,
          { derive: config.derive },
        ),
      };
    case "numeric":
      // A grouping field on box/violin switches to the grouped
      // aggregation — same rule the Visualize page applies.
      if (groupedOn) {
        return {
          kind: "numeric_grouped" as const,
          data: await vizApi.fieldNumericGrouped(
            caseId,
            timelineId,
            config.field!,
            config.fieldY!,
            filters,
            opts.groups,
            opts.bins ?? 30,
            opts.showPoints,
          ),
        };
      }
      if (compareApiSpec) {
        return {
          kind: "numeric" as const,
          compare: true as const,
          data: (await vizApi.compare(caseId, timelineId, {
            kind: "numeric",
            field: config.field!,
            primary: filters,
            comparison: compareApiSpec,
            bins: opts.bins ?? 30,
          })) as CompareNumericResponse,
        };
      }
      return {
        kind: "numeric" as const,
        compare: false as const,
        data: await vizApi.fieldNumeric(
          caseId,
          timelineId,
          config.field!,
          filters,
          opts.bins,
          opts.showPoints,
        ),
      };
    case "timeseries":
      return {
        kind: "timeseries" as const,
        data: await vizApi.fieldTimeseries(
          caseId,
          timelineId,
          config.field!,
          filters,
          opts.buckets,
          opts.topN,
          config.derive,
        ),
      };
    case "time": {
      const data = compareApiSpec
        ? ((await vizApi.compare(caseId, timelineId, {
            kind: "time",
            primary: filters,
            comparison: compareApiSpec,
            buckets: opts.buckets,
          })) as CompareTimeResponse)
        : histogramToCompare(
            await eventsApi.histogram(
              caseId,
              timelineId,
              filters,
              opts.buckets,
            ),
          );
      return { kind: "time" as const, data };
    }
    case "punchcard":
      return {
        kind: "punchcard" as const,
        data: await vizApi.punchcard(caseId, timelineId, filters),
      };
    case "pivot":
      return {
        kind: "pivot" as const,
        data: await vizApi.fieldPivot(
          caseId,
          timelineId,
          config.field!,
          config.fieldY!,
          filters,
          opts.limitX,
          opts.limitY,
          config.derive,
        ),
      };
    case "table":
      return {
        kind: "table" as const,
        data: await vizApi.fieldTable(caseId, timelineId, config.field!, filters, opts.topN, {
          secondField: config.fieldY,
          sortBy: opts.tableSortBy,
          sortDir: opts.tableSortDir,
          derive: config.derive,
        }),
      };
    case "corr":
      return {
        kind: "corr" as const,
        data: await vizApi.fieldCorrelation(
          caseId,
          timelineId,
          config.fields ?? [],
          filters,
        ),
      };
    case "scatter":
      return {
        kind: "scatter" as const,
        data: await vizApi.fieldScatter(
          caseId,
          timelineId,
          config.field!,
          config.fieldY!,
          filters,
          opts.sampleLimit,
        ),
      };
  }
}

/** Discriminated aggregation payload a chart is drawn from. */
export type ChartResult = Awaited<ReturnType<typeof fetchChartData>>;

/** The aggregation the server recorded having run, as frozen in a snapshot. */
export type FrozenChartKind =
  | "terms"
  | "numeric"
  | "timeseries"
  | "time"
  | "punchcard"
  | "pivot"
  | "corr"
  | "scatter"
  | "table";

/**
 * Rebuild a `ChartResult` from a snapshot's frozen aggregation.
 *
 * The live path does more than name the payload — for an uncompared time
 * chart it runs the raw histogram (`{buckets: [{start, count}]}`) through
 * `histogramToCompare` before any mark sees it. Handing the frozen histogram
 * straight to the mark produces a chart of `undefined` bars, silently, in
 * every export. This is the one place that reshaping happens for snapshots,
 * and the return type is the real union rather than a cast, so a future
 * divergence between the two paths is a build failure.
 *
 * Returns null when the frozen payload doesn't match any known aggregation.
 */
export function snapshotToChartResult(
  dataKind: string,
  compareMode: string,
  config: ChartConfig,
  frozen: unknown,
): ChartResult | null {
  const compare = compareMode !== "off";
  const grouped = !!CHART_META[config.chartType].acceptsSecondField && !!config.fieldY;
  switch (dataKind as FrozenChartKind) {
    case "terms":
      return compare
        ? { kind: "terms", compare: true, data: frozen as CompareTermsResponse }
        : { kind: "terms", compare: false, data: frozen as FieldTermsResponse };
    case "numeric":
      if (grouped) {
        return { kind: "numeric_grouped", data: frozen as FieldNumericGroupedResponse };
      }
      return compare
        ? { kind: "numeric", compare: true, data: frozen as CompareNumericResponse }
        : { kind: "numeric", compare: false, data: frozen as FieldNumericResponse };
    case "timeseries":
      return { kind: "timeseries", data: frozen as FieldTimeseriesResponse };
    case "time":
      return {
        kind: "time",
        data: compare
          ? (frozen as CompareTimeResponse)
          : histogramToCompare(frozen as HistogramResponse),
      };
    case "punchcard":
      return { kind: "punchcard", data: frozen as PunchcardResponse };
    case "pivot":
      return { kind: "pivot", data: frozen as FieldPivotResponse };
    case "corr":
      return { kind: "corr", data: frozen as FieldCorrelationResponse };
    case "scatter":
      return { kind: "scatter", data: frozen as FieldScatterResponse };
    case "table":
      return { kind: "table", data: frozen as FieldTableResponse };
    default:
      return null;
  }
}
