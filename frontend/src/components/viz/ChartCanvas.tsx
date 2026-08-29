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
import { busyMessage, busyRetry } from "@/lib/queryClient";
import type { ChartConfig } from "@/components/viz/lib/chartConfig";
import { fetchChartData, type ChartResult } from "@/components/viz/chartFetch";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import {
  resolveChartOptions,
  type ResolvedChartOptions,
} from "@/components/viz/lib/chartOptions";
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
import { CumulativeStep } from "@/components/viz/charts/CumulativeStep";
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import { CorrMatrix } from "@/components/viz/charts/CorrMatrix";
import { TableFigure, TableHtml } from "@/components/viz/charts/TableFigure";
import { ScatterStatsPanel } from "@/components/viz/ScatterStatsPanel";
import { Spinner } from "@/components/ui/Spinner";
import type { EventFilters, ResolvedMark } from "@/api/types";
import { useResolvedMarks } from "@/components/viz/useResolvedMarks";

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
  const compareOn = config.compare.mode !== "off";
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

  const marksQuery = useResolvedMarks(caseId, timelineId, config);
  const chartQuery = useQuery({
    queryKey: ["chart-canvas", caseId, timelineId, config, filters],
    queryFn: () => fetchChartData(caseId, timelineId, config, filters, opts),
    enabled: specComplete,
    ...busyRetry,
  });
  const waiting = busyMessage(chartQuery.failureReason);

  return (
    <div
      data-testid={testId}
      className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2"
    >
      {!specComplete && (
        <p className="py-2 text-[var(--color-fg-muted)]">{incompleteMessage}</p>
      )}
      {chartQuery.isLoading && (
        <div className="flex items-center justify-center gap-2 py-6">
          <Spinner size={16} />
          {waiting && <span className="text-xs text-[var(--color-fg-muted)]">{waiting}</span>}
        </div>
      )}
      {chartQuery.isError && (
        <p className="py-2 text-[var(--color-danger)]">
          Couldn't load this chart:{" "}
          {chartQuery.error instanceof Error
            ? chartQuery.error.message
            : "unknown error"}
        </p>
      )}
      {chartQuery.data && (
        <div className="relative">
          {/* A busy lane (#300) while marks are already drawn. `isLoading` is
              false once the key has data, so the spinner branch above never
              runs for a refetch — without this the chart would sit stale and
              silent for the whole retry window and only then fail. Gated on
              `isFetching`, which stays true across the retry delay. */}
          {waiting && chartQuery.isFetching && (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
              <span className="rounded bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] shadow">
                {waiting}
              </span>
            </div>
          )}
          <ChartMarks
            config={config}
            data={chartQuery.data}
            opts={opts}
            compareOn={compareOn}
            marks={marksQuery.data?.marks}
          />
        </div>
      )}
    </div>
  );
}

/**
 * The mark dispatch, drawn from whatever produced the data — a live query or
 * a frozen export snapshot — so an exported report and the live page render
 * identically.
 */
export type { ChartResult };

export function ChartMarks({
  config,
  data,
  opts,
  compareOn,
  tableAs = "svg",
  marks,
}: {
  config: ChartConfig;
  data: ChartResult;
  opts: ResolvedChartOptions;
  compareOn: boolean;
  /** Resolved marks for the time-axis figures; the snapshot passes its frozen ones. */
  marks?: ResolvedMark[];
  /** The table figure is an <svg> on the page (so it exports like every other
   * figure) and a real <table> in a Story snapshot and the HTML export. */
  tableAs?: "svg" | "html";
}) {
  return (
    <>
      {/* Keyed on the chart *type*, not the aggregation that fed it: several
          marks share one dataKind (pie and bar both read terms; box, violin
          and ecdf all read numeric), so switching on the fetch result is
          what silently turned a requested pie into a bar. Mirrors the
          Visualize page's canvas one-for-one, minus click-to-filter — the
          card is a read-only sandbox and filtering is the page's affordance. */}
      {data.kind === "terms" && config.chartType === "bar" && (
        <BarChart
          terms={data.compare ? undefined : data.data}
          compare={data.compare ? data.data : undefined}
          orientation={opts.orientation}
          sort={opts.sort}
          logScale={opts.logScale}
        />
      )}
      {data.kind === "terms" && config.chartType === "pie" && !data.compare && (
        <PieChart terms={data.data} />
      )}
      {data.kind === "terms" &&
        config.chartType === "waffle" &&
        !data.compare && <WaffleChart terms={data.data} />}
      {data.kind === "numeric" && config.chartType === "histogram" && (
        <NumericHistogram
          stats={data.compare ? undefined : data.data}
          compare={data.compare ? data.data : undefined}
          logScale={opts.logScale}
          showDensity={opts.showDensity}
          showMarkers
        />
      )}
      {data.kind === "numeric_grouped" &&
        (config.chartType === "box" || config.chartType === "violin") && (
          <GroupedDistribution
            data={data.data}
            mark={config.chartType}
            showPoints={opts.showPoints}
          />
        )}
      {data.kind === "numeric" &&
        !data.compare &&
        config.chartType === "box" && (
          <BoxPlot stats={data.data} showPoints={opts.showPoints} />
        )}
      {data.kind === "numeric" &&
        !data.compare &&
        config.chartType === "violin" && (
          <ViolinPlot stats={data.data} showPoints={opts.showPoints} />
        )}
      {data.kind === "numeric" &&
        !data.compare &&
        config.chartType === "ecdf" && <EcdfChart stats={data.data} />}
      {data.kind === "timeseries" && config.chartType === "line" && (
        <LineChart
          data={data.data}
          seriesMode={opts.seriesMode}
          showPoints={config.options.showPoints ?? true}
          showLegend={opts.legend}
          marks={marks}
        />
      )}
      {data.kind === "timeseries" && config.chartType === "heatmap" && (
        <Heatmap data={data.data} />
      )}
      {data.kind === "time" && (
        <CompareHistogram
          data={data.data}
          metric={config.metric}
          hasComparison={compareOn}
          marks={marks}
        />
      )}
      {data.kind === "cumulative" && <CumulativeStep data={data.data} marks={marks} />}
      {data.kind === "punchcard" && <PunchCard data={data.data} />}
      {data.kind === "pivot" && config.chartType === "pivot" && (
        <PivotHeatmap data={data.data} />
      )}
      {data.kind === "pivot" && config.chartType === "sankey" && (
        <SankeyFlow data={data.data} />
      )}
      {data.kind === "table" &&
        (tableAs === "html" ? (
          <TableHtml data={data.data} config={config} highlight={opts.highlight} />
        ) : (
          <TableFigure data={data.data} config={config} highlight={opts.highlight} />
        ))}
      {data.kind === "corr" && <CorrMatrix data={data.data} />}
      {data.kind === "scatter" && (
        <>
          <ScatterChart data={data.data} />
          {data.data.stats && <ScatterStatsPanel stats={data.data.stats} />}
        </>
      )}
    </>
  );
}
