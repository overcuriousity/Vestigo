/**
 * ChartCanvas — the shared "render a ChartConfig against a timeline" surface.
 *
 * Fetches through the same `vizApi` calls the Visualize page uses and renders
 * the same marks, keyed on the chart *type* rather than the aggregation that
 * fed it (several marks share one dataKind; switching on the fetch result is
 * what once turned a requested pie into a bar). Read-only: no click-to-filter,
 * which stays the Visualize page's affordance.
 *
 * Used by the agent's chart proposal cards and by Story chart blocks, so a
 * chart looks identical wherever it is embedded.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { vizApi, type CompareMode } from "@/api/viz";
import { eventsApi } from "@/api/events";
import { histogramToCompare, type ChartConfig } from "@/components/viz/lib/chartConfig";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import { resolveChartOptions } from "@/components/viz/lib/chartOptions";
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { WaffleChart } from "@/components/viz/charts/WaffleChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { GroupedDistribution } from "@/components/viz/charts/GroupedDistribution";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { CompareHistogram } from "@/components/viz/charts/CompareHistogram";
import { PunchCard } from "@/components/viz/charts/PunchCard";
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import { CorrMatrix } from "@/components/viz/charts/CorrMatrix";
import { ScatterStatsPanel } from "@/components/viz/ScatterStatsPanel";
import { Spinner } from "@/components/ui/Spinner";
import type {
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
} from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  config: ChartConfig;
  /** Primary-layer filters; omit for the whole timeline. */
  filters?: EventFilters;
  /** Shown when the config names no field to plot. */
  incompleteMessage?: string;
  testId?: string;
}

export function ChartCanvas({
  caseId,
  timelineId,
  config,
  filters: filtersProp,
  incompleteMessage = "This chart is missing a field, so there is nothing to plot.",
  testId = "chart-canvas",
}: Props) {
  const filters = useMemo<EventFilters>(() => filtersProp ?? {}, [filtersProp]);
  const dataKind = CHART_META[config.chartType].dataKind;
  const groupedOn = !!CHART_META[config.chartType].acceptsSecondField && !!config.fieldY;
  const compareOn = config.compare.mode !== "off";
  const compareApiSpec: CompareMode | null =
    config.compare.mode === "baseline"
      ? { mode: "baseline" }
      : config.compare.mode === "custom"
        ? { mode: "custom", filters: config.compare.filters }
        : null;
  // Same resolver the Visualize page uses, so an embedded chart and the chart
  // the analyst gets from "Open in Visualize" are drawn from identical values.
  const opts = useMemo(() => resolveChartOptions(config), [config]);

  // Every kind but time/punchcard needs a field, and pivot/scatter need two.
  // Callers normally validate before rendering (`propose_chart` rejects an
  // incomplete spec; a saved chart was legal when saved), but an un-run query
  // renders as neither loading nor error — i.e. a silently blank chart box —
  // so say so explicitly rather than leave the analyst looking at nothing.
  const specComplete =
    dataKind === "time" || dataKind === "punchcard"
      ? true
      : dataKind === "corr"
        ? (config.fields?.length ?? 0) >= 2
        : dataKind === "pivot" || dataKind === "scatter"
          ? !!(config.field && config.fieldY)
          : !!config.field;

  const chartQuery = useQuery({
    queryKey: ["chart-canvas", caseId, timelineId, config, filters],
    queryFn: async () => {
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
              })) as CompareTermsResponse,
            };
          }
          return {
            kind: "terms" as const,
            compare: false as const,
            data: await vizApi.fieldTerms(caseId, timelineId, config.field!, filters, opts.topN),
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
                await eventsApi.histogram(caseId, timelineId, filters, opts.buckets),
              );
          return { kind: "time" as const, data };
        }
        case "punchcard":
          return { kind: "punchcard" as const, data: await vizApi.punchcard(caseId, timelineId, filters) };
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
            ),
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
    },
    enabled: specComplete,
  });


  return (
  <div
    data-testid={testId}
    className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2"
  >
    {!specComplete && (
      <p className="py-2 text-[var(--color-fg-muted)]">
        {incompleteMessage}
      </p>
    )}
    {chartQuery.isLoading && (
      <div className="flex items-center justify-center py-6">
        <Spinner size={16} />
      </div>
    )}
    {chartQuery.isError && (
      <p className="py-2 text-[var(--color-danger)]">
        Couldn't load this chart:{" "}
        {chartQuery.error instanceof Error ? chartQuery.error.message : "unknown error"}
      </p>
    )}
    {/* Keyed on the chart *type*, not the aggregation that fed it: several
        marks share one dataKind (pie and bar both read terms; box, violin
        and ecdf all read numeric), so switching on the fetch result is
        what silently turned a requested pie into a bar. Mirrors the
        Visualize page's canvas one-for-one, minus click-to-filter — the
        card is a read-only sandbox and filtering is the page's affordance. */}
    {chartQuery.data?.kind === "terms" && config.chartType === "bar" && (
      <BarChart
        terms={chartQuery.data.compare ? undefined : chartQuery.data.data}
        compare={chartQuery.data.compare ? chartQuery.data.data : undefined}
        orientation={opts.orientation}
        sort={opts.sort}
        logScale={opts.logScale}
      />
    )}
    {chartQuery.data?.kind === "terms" &&
      config.chartType === "pie" &&
      !chartQuery.data.compare && <PieChart terms={chartQuery.data.data} />}
    {chartQuery.data?.kind === "terms" &&
      config.chartType === "waffle" &&
      !chartQuery.data.compare && <WaffleChart terms={chartQuery.data.data} />}
    {chartQuery.data?.kind === "numeric" && config.chartType === "histogram" && (
      <NumericHistogram
        stats={chartQuery.data.compare ? undefined : chartQuery.data.data}
        compare={chartQuery.data.compare ? chartQuery.data.data : undefined}
        logScale={opts.logScale}
        showDensity={opts.showDensity}
        showMarkers
      />
    )}
    {chartQuery.data?.kind === "numeric_grouped" &&
      (config.chartType === "box" || config.chartType === "violin") && (
        <GroupedDistribution
          data={chartQuery.data.data}
          mark={config.chartType}
          showPoints={opts.showPoints}
        />
      )}
    {chartQuery.data?.kind === "numeric" &&
      !chartQuery.data.compare &&
      config.chartType === "box" && (
        <BoxPlot stats={chartQuery.data.data} showPoints={opts.showPoints} />
      )}
    {chartQuery.data?.kind === "numeric" &&
      !chartQuery.data.compare &&
      config.chartType === "violin" && (
        <ViolinPlot stats={chartQuery.data.data} showPoints={opts.showPoints} />
      )}
    {chartQuery.data?.kind === "numeric" &&
      !chartQuery.data.compare &&
      config.chartType === "ecdf" && <EcdfChart stats={chartQuery.data.data} />}
    {chartQuery.data?.kind === "timeseries" && config.chartType === "line" && (
      <LineChart
        data={chartQuery.data.data}
        seriesMode={opts.seriesMode}
        showPoints={config.options.showPoints ?? true}
        showLegend={opts.legend}
      />
    )}
    {chartQuery.data?.kind === "timeseries" && config.chartType === "heatmap" && (
      <Heatmap data={chartQuery.data.data} />
    )}
    {chartQuery.data?.kind === "time" && (
      <CompareHistogram
        data={chartQuery.data.data}
        metric={config.metric}
        hasComparison={compareOn}
      />
    )}
    {chartQuery.data?.kind === "punchcard" && <PunchCard data={chartQuery.data.data} />}
    {chartQuery.data?.kind === "pivot" && config.chartType === "pivot" && (
      <PivotHeatmap data={chartQuery.data.data} />
    )}
    {chartQuery.data?.kind === "pivot" && config.chartType === "sankey" && (
      <SankeyFlow data={chartQuery.data.data} />
    )}
    {chartQuery.data?.kind === "corr" && <CorrMatrix data={chartQuery.data.data} />}
    {chartQuery.data?.kind === "scatter" && (
      <>
        <ScatterChart data={chartQuery.data.data} />
        {chartQuery.data.data.stats && (
          <ScatterStatsPanel stats={chartQuery.data.data.stats} />
        )}
      </>
    )}
  </div>
  );
}
