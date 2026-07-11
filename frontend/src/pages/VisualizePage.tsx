/**
 * VisualizePage — full statistical visualization workbench.
 *
 * Inherits the Explorer's current filters/time-range from the URL (same
 * `paramsToFilters` the Explorer itself reads), so a chart here always
 * matches whatever the analyst was just looking at in the grid. The analyst
 * picks a field, declares its scale of measurement, and gets the chart
 * types appropriate to that scale — each backed by one of the `vizApi`
 * aggregations.
 *
 * All chart state (type, field, scale, metric, comparison layer, options)
 * lives in the URL as a serialized `ChartConfig` (`c_*` params, see
 * `viz/lib/chartConfig.ts`) alongside the filter params — a Visualize URL is
 * a complete, shareable description of the chart.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, HelpCircle, Lightbulb, RotateCcw, X } from "lucide-react";
import { vizApi, type CompareMode } from "@/api/viz";
import { eventsApi } from "@/api/events";
import { timelinesApi } from "@/api/timelines";
import { filtersToParams, paramsToFilters } from "@/lib/queryParams";
import { applyFieldEntries } from "@/lib/fieldFilters";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { ExportControls } from "@/components/viz/ExportControls";
import { CompareFilterEditor } from "@/components/viz/CompareFilterEditor";
import { SavedChartsRail } from "@/components/viz/SavedChartsRail";
import { ChartActionPopover } from "@/components/viz/ChartActionPopover";
import type { ChartValueClick } from "@/components/viz/lib/interaction";
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";
import { CompareHistogram } from "@/components/viz/charts/CompareHistogram";
import { PunchCard } from "@/components/viz/charts/PunchCard";
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import {
  chartConfigToParams,
  filterParamsPreservingChartConfig,
  paramsToChartConfig,
  type ChartConfig,
  type ChartType,
  type Scale,
} from "@/components/viz/lib/chartConfig";
import { METRIC_INFO, type Metric } from "@/components/viz/lib/transforms";
import { CHART_META, chartTypesFor, SCALES } from "@/components/viz/lib/chartMeta";
import { buildCaptionLines, type CaptionFacts } from "@/components/viz/lib/caption";
import { CHART_PRESETS } from "@/components/viz/lib/presets";
import { ChartCaption } from "@/components/viz/primitives/ChartCaption";
import type {
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
  HistogramResponse,
} from "@/api/types";

const SCALE_INFO: Record<Scale, { label: string; hint: string }> = {
  nominal: {
    label: "Nominal",
    hint: "Unordered categories — e.g. HTTP method, source IP, artifact type. Identity only; order carries no meaning.",
  },
  ordinal: {
    label: "Ordinal",
    hint: "Ordered categories — e.g. log level (debug < info < warning < error). Order matters, but not the distance between steps.",
  },
  interval: {
    label: "Interval",
    hint: "Numeric with meaningful differences but no true zero — e.g. a timestamp. Differences are meaningful; ratios are not.",
  },
  ratio: {
    label: "Ratio",
    hint: "Numeric with a true zero — e.g. bytes transferred, response time, request count. Differences and ratios are both meaningful.",
  },
};

const METRICS: Metric[] = ["count", "delta", "rate", "ratio", "cumulative"];

/** Why Compare is disabled for a chart type — shown instead of hiding the
 * control (see chartMeta: pie/box/violin/ecdf have no honest two-layer
 * encoding; the newer kinds simply have no compare aggregation yet). */
function compareUnavailableReason(chartType: ChartType): string {
  if (chartType === "punchcard" || chartType === "pivot" || chartType === "sankey" || chartType === "scatter") {
    return "Compare isn't supported for this chart type yet.";
  }
  return "This chart type has no honest two-layer encoding — overlaid layers would misrepresent one of them. Use Bar, Histogram, or the Time histogram to compare.";
}

/** Adapt the single-layer histogram response to the compare shape so one
 * chart component renders both the compare-off and compare-on cases. */
function histogramToCompare(h: HistogramResponse): CompareTimeResponse {
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

export function VisualizePage() {
  const { caseId, timelineId } = useParams<{ caseId: string; timelineId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => paramsToFilters(searchParams), [searchParams]);
  const config = useMemo(() => paramsToChartConfig(searchParams), [searchParams]);

  const updateConfig = useCallback(
    (patch: Partial<ChartConfig>) => {
      setSearchParams(
        (prev) => {
          const next = { ...paramsToChartConfig(prev), ...patch };
          return chartConfigToParams(next, new URLSearchParams(prev));
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // The one place a filter change is written from this page — the helper
  // carries the `c_*` chart-config keys over, since `filtersToParams`
  // builds a fresh URLSearchParams (see its doc comment).
  const updateFilters = useCallback(
    (next: EventFilters) => {
      setSearchParams((prev) => filterParamsPreservingChartConfig(next, prev));
    },
    [setSearchParams],
  );

  // Click-to-filter: charts report the clicked mark's field=value pair(s);
  // the popover offers filter in / filter out / open in Explorer.
  const [pendingClick, setPendingClick] = useState<ChartValueClick | null>(null);
  const handleChartValueClick = useCallback((click: ChartValueClick) => {
    setPendingClick(click);
  }, []);

  const { field, fieldY, scale, chartType, metric } = config;
  const dataKind = CHART_META[chartType].dataKind;
  const requiresSecondField = !!CHART_META[chartType].requiresSecondField;
  // "time" and "punchcard" chart the whole event count — no field involved.
  const fieldFree = dataKind === "time" || dataKind === "punchcard";
  const compareOn = config.compare.mode !== "off";
  const compareSupported = !!CHART_META[chartType].supportsCompare;
  const compareApiSpec: CompareMode | null =
    config.compare.mode === "baseline"
      ? { mode: "baseline" }
      : config.compare.mode === "custom"
        ? { mode: "custom", filters: config.compare.filters }
        : null;

  const topN = Math.min(config.options.topN ?? 10, dataKind === "timeseries" ? 20 : 50);
  const bins = config.options.bins ?? 30;
  const buckets = config.options.buckets ?? 60;
  const limitX = config.options.limitX ?? 10;
  const limitY = config.options.limitY ?? 10;
  const sampleLimit = config.options.sampleLimit ?? 5000;

  const svgRef = useRef<SVGSVGElement | null>(null);
  // Preset strip: open by default on a fresh page (no chart state in the
  // URL yet); a URL that already describes a chart skips the guidance.
  const [presetsOpen, setPresetsOpen] = useState(() => !searchParams.has("c_type"));

  const applyPreset = (preset: (typeof CHART_PRESETS)[number]) => {
    updateConfig(preset.config);
    setPresetsOpen(false);
  };

  const timelineQuery = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const fieldsQuery = useQuery({
    queryKey: ["viz-fields", caseId, timelineId],
    queryFn: () => vizApi.fields(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  // Default to the first field once the list loads — the backend sorts by
  // coverage descending, so this is the highest-coverage field.
  useEffect(() => {
    if (field == null && fieldsQuery.data?.fields.length) {
      updateConfig({ field: fieldsQuery.data.fields[0].token });
    }
  }, [field, fieldsQuery.data, updateConfig]);

  // Probe numeric-ness only when actually needed: once per field change (to
  // auto-suggest a scale) and while a numeric chart type is displayed (as its
  // data source). `autoProbedField` gates the auto-suggest to once per field
  // — the analyst's manual scale choice is never overridden afterwards.
  const autoProbedField = useRef<string | null>(field);
  const numericQuery = useQuery({
    queryKey: ["viz-field-numeric", caseId, timelineId, field, filters, bins],
    queryFn: () => vizApi.fieldNumeric(caseId!, timelineId!, field!, filters, bins),
    // Run only when a numeric chart actually needs the data, or when a
    // *field-dependent* chart needs its one-time scale probe. The field-free
    // charts (time, punchcard) never need it, and the two-field charts have
    // their own endpoints and keep their chart type — skipping the probe
    // there avoids the field_numeric_stats double-scan.
    enabled:
      !!(caseId && timelineId && field) &&
      (dataKind === "numeric" ||
        (!fieldFree && !requiresSecondField && field !== autoProbedField.current)),
  });

  useEffect(() => {
    if (!field || field === autoProbedField.current) return;
    if (numericQuery.data == null) return;
    autoProbedField.current = field;
    // Don't yank the analyst off the field-independent charts (time,
    // punchcard) or a deliberately-picked two-field chart.
    if (fieldFree || requiresSecondField) return;
    const isNumeric = numericQuery.data.count > 0;
    updateConfig({
      scale: isNumeric ? "ratio" : "nominal",
      chartType: isNumeric ? "histogram" : "bar",
    });
  }, [field, numericQuery.data, fieldFree, requiresSecondField, updateConfig]);

  // Keep chartType valid when the analyst switches scale — clamped at event
  // time rather than in an effect, so there is never a render with an
  // inconsistent scale/chartType pair.
  const handleScaleChange = (s: Scale) => {
    if (!CHART_META[chartType].scales.includes(s)) {
      updateConfig({ scale: s, chartType: chartTypesFor(s)[0] });
    } else {
      updateConfig({ scale: s });
    }
  };

  // Metric gating: % of baseline needs a comparison layer; delta/rate/
  // cumulative need time-bucketed bins. Clamp the active metric the same way
  // so a chart-type/compare change never leaves an impossible combination.
  const metricAvailable = useCallback(
    (m: Metric): boolean => {
      const info = METRIC_INFO[m];
      if (info.requiresCompare && !compareOn) return false;
      if (info.timeBucketedOnly && dataKind !== "time") return false;
      return m === "count" || dataKind === "time";
    },
    [compareOn, dataKind],
  );
  useEffect(() => {
    if (!metricAvailable(metric)) updateConfig({ metric: "count" });
  }, [metric, metricAvailable, updateConfig]);

  const compareTermsOn = compareOn && chartType === "bar" && compareApiSpec != null;
  const termsQuery = useQuery({
    queryKey: ["viz-field-terms", caseId, timelineId, field, filters, topN],
    queryFn: () => vizApi.fieldTerms(caseId!, timelineId!, field!, filters, topN),
    enabled: !!(caseId && timelineId && field) && dataKind === "terms" && !compareTermsOn,
  });

  const compareTermsQuery = useQuery({
    queryKey: ["viz-compare-terms", caseId, timelineId, field, filters, config.compare, topN],
    queryFn: async () =>
      (await vizApi.compare(caseId!, timelineId!, {
        kind: "terms",
        field: field!,
        primary: filters,
        comparison: compareApiSpec!,
        limit: topN,
      })) as CompareTermsResponse,
    enabled: !!(caseId && timelineId && field) && compareTermsOn,
  });

  const compareNumericOn = compareOn && chartType === "histogram" && compareApiSpec != null;
  const compareNumericQuery = useQuery({
    queryKey: ["viz-compare-numeric", caseId, timelineId, field, filters, config.compare, bins],
    queryFn: async () =>
      (await vizApi.compare(caseId!, timelineId!, {
        kind: "numeric",
        field: field!,
        primary: filters,
        comparison: compareApiSpec!,
        bins,
      })) as CompareNumericResponse,
    enabled: !!(caseId && timelineId && field) && compareNumericOn,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["viz-field-timeseries", caseId, timelineId, field, filters, topN],
    queryFn: () => vizApi.fieldTimeseries(caseId!, timelineId!, field!, filters, 60, topN),
    enabled: !!(caseId && timelineId && field) && dataKind === "timeseries",
  });

  // Events-over-time: one shared-grid compare call when a comparison layer
  // is on, otherwise the Explorer's own histogram adapted to the same shape.
  const timeQuery = useQuery({
    queryKey: ["viz-time", caseId, timelineId, filters, config.compare, buckets],
    queryFn: async (): Promise<CompareTimeResponse> => {
      if (compareApiSpec) {
        return (await vizApi.compare(caseId!, timelineId!, {
          kind: "time",
          primary: filters,
          comparison: compareApiSpec,
          buckets,
        })) as CompareTimeResponse;
      }
      return histogramToCompare(await eventsApi.histogram(caseId!, timelineId!, filters, buckets));
    },
    enabled: !!(caseId && timelineId) && dataKind === "time",
  });

  const punchcardQuery = useQuery({
    queryKey: ["viz-punchcard", caseId, timelineId, filters],
    queryFn: () => vizApi.punchcard(caseId!, timelineId!, filters),
    enabled: !!(caseId && timelineId) && dataKind === "punchcard",
  });

  // Shared by the pivot heatmap AND the sankey (same aggregation, two marks)
  // — switching between those chart types refetches nothing.
  const pivotQuery = useQuery({
    queryKey: ["viz-field-pivot", caseId, timelineId, field, fieldY, filters, limitX, limitY],
    queryFn: () => vizApi.fieldPivot(caseId!, timelineId!, field!, fieldY!, filters, limitX, limitY),
    enabled: !!(caseId && timelineId && field && fieldY) && dataKind === "pivot",
  });

  const scatterQuery = useQuery({
    queryKey: ["viz-field-scatter", caseId, timelineId, field, fieldY, filters, sampleLimit],
    queryFn: () => vizApi.fieldScatter(caseId!, timelineId!, field!, fieldY!, filters, sampleLimit),
    enabled: !!(caseId && timelineId && field && fieldY) && dataKind === "scatter",
  });

  const availableChartTypes = chartTypesFor(scale);

  // Data-derived caption facts for the active query — totals, grid width,
  // and top-N capping feed the truthful caption/export lines.
  const facts: CaptionFacts = {};
  if (dataKind === "time" && timeQuery.data) {
    facts.primaryTotal = timeQuery.data.primary_total;
    if (compareOn) facts.comparisonTotal = timeQuery.data.comparison_total;
    facts.intervalSeconds = timeQuery.data.interval_seconds;
  } else if (dataKind === "terms") {
    if (compareTermsOn && compareTermsQuery.data) {
      facts.primaryTotal = compareTermsQuery.data.primary_total;
      facts.comparisonTotal = compareTermsQuery.data.comparison_total;
      facts.distinct = compareTermsQuery.data.distinct;
      facts.shownValues = compareTermsQuery.data.values.length;
      facts.otherCount = compareTermsQuery.data.primary_other;
    } else if (termsQuery.data) {
      facts.primaryTotal = termsQuery.data.total;
      facts.distinct = termsQuery.data.distinct;
      facts.shownValues = termsQuery.data.values.length;
      facts.otherCount = termsQuery.data.other_count;
    }
  } else if (dataKind === "numeric") {
    if (compareNumericOn && compareNumericQuery.data) {
      facts.primaryTotal = compareNumericQuery.data.primary_total;
      facts.comparisonTotal = compareNumericQuery.data.comparison_total;
      facts.binCount = compareNumericQuery.data.bins.length;
      facts.valueMin = compareNumericQuery.data.min;
      facts.valueMax = compareNumericQuery.data.max;
    } else if (numericQuery.data) {
      facts.primaryTotal = numericQuery.data.count;
      facts.binCount = numericQuery.data.bins.length;
      facts.valueMin = numericQuery.data.min;
      facts.valueMax = numericQuery.data.max;
    }
  } else if (dataKind === "timeseries" && timeseriesQuery.data) {
    facts.shownValues = timeseriesQuery.data.series.length;
    facts.intervalSeconds = timeseriesQuery.data.interval_seconds;
  } else if (dataKind === "punchcard" && punchcardQuery.data) {
    facts.primaryTotal = punchcardQuery.data.total;
  } else if (dataKind === "pivot" && pivotQuery.data) {
    facts.primaryTotal = pivotQuery.data.total;
    facts.xDistinct = pivotQuery.data.x_distinct;
    facts.xShown = pivotQuery.data.x_values.length;
    facts.yDistinct = pivotQuery.data.y_distinct;
    facts.yShown = pivotQuery.data.y_values.length;
  } else if (dataKind === "scatter" && scatterQuery.data) {
    facts.primaryTotal = scatterQuery.data.total;
    facts.sampledPoints = scatterQuery.data.sampled;
    facts.totalPoints = scatterQuery.data.total;
  }

  const captionLines = buildCaptionLines({
    caseId,
    timelineId,
    chartLabel: CHART_META[chartType].label,
    config,
    filters,
    facts,
  });

  const loading =
    (dataKind === "time" && timeQuery.isLoading) ||
    (dataKind === "terms" && (compareTermsOn ? compareTermsQuery.isLoading : termsQuery.isLoading)) ||
    (dataKind === "numeric" &&
      (compareNumericOn ? compareNumericQuery.isLoading : numericQuery.isLoading)) ||
    (dataKind === "timeseries" && timeseriesQuery.isLoading) ||
    (dataKind === "punchcard" && punchcardQuery.isLoading) ||
    (dataKind === "pivot" && pivotQuery.isLoading) ||
    (dataKind === "scatter" && scatterQuery.isLoading);

  return (
    <div className="flex h-full overflow-hidden">
      {/* Control rail */}
      <div className="flex w-72 shrink-0 flex-col gap-4 overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-bg-surface)] p-3">
        <div>
          {caseId && timelineId && (
            <Link
              to={`/cases/${caseId}/timelines/${timelineId}?${searchParams.toString()}`}
              className="flex items-center gap-1 text-xs text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
            >
              <ArrowLeft size={12} /> Back to Explorer
            </Link>
          )}
          <h2 className="mt-1 text-sm font-semibold text-[var(--color-fg-primary)]">
            Visualize {timelineQuery.data ? `— ${timelineQuery.data.name}` : ""}
          </h2>
          <button
            onClick={() => setPresetsOpen((v) => !v)}
            className="mt-1 flex items-center gap-1 text-xs text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
          >
            <Lightbulb size={12} /> Presets
          </button>
        </div>

        {/* Field picker */}
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            {requiresSecondField ? "Field (X)" : "Field"}
          </label>
          {fieldFree ? (
            <div className="rounded border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-fg-muted)]">
              — event count —
            </div>
          ) : (
            <Select value={field ?? undefined} onValueChange={(v) => updateConfig({ field: v })}>
              <SelectTrigger className="text-sm">
                <SelectValue placeholder="Choose a field…" />
              </SelectTrigger>
              <SelectContent>
                {(fieldsQuery.data?.fields ?? []).map((f) => (
                  <SelectItem key={f.token} value={f.token}>
                    {f.token}{" "}
                    <span className="text-[var(--color-fg-muted)]">
                      ({f.distinct} distinct)
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>

        {/* Second field picker — pivot/sankey/scatter chart both axes */}
        {requiresSecondField && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Field (Y)
            </label>
            <Select
              value={fieldY ?? undefined}
              onValueChange={(v) => updateConfig({ fieldY: v })}
            >
              <SelectTrigger className="text-sm">
                <SelectValue placeholder="Choose a second field…" />
              </SelectTrigger>
              <SelectContent>
                {(fieldsQuery.data?.fields ?? [])
                  .filter((f) => f.token !== field)
                  .map((f) => (
                    <SelectItem key={f.token} value={f.token}>
                      {f.token}{" "}
                      <span className="text-[var(--color-fg-muted)]">
                        ({f.distinct} distinct)
                      </span>
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
        )}

        {/* Scale of measurement */}
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Scale of measurement
          </label>
          <div className="space-y-1">
            {SCALES.map((s) => (
              <label
                key={s}
                className={`flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm ${
                  scale === s ? "bg-[var(--color-accent-dim)]" : "hover:bg-[var(--color-bg-hover)]"
                }`}
              >
                <input
                  type="radio"
                  name="scale"
                  checked={scale === s}
                  onChange={() => handleScaleChange(s)}
                  className="accent-[var(--color-accent)]"
                />
                {SCALE_INFO[s].label}
                <Tooltip content={SCALE_INFO[s].hint} side="right">
                  <HelpCircle size={12} className="text-[var(--color-fg-muted)]" />
                </Tooltip>
              </label>
            ))}
          </div>
        </div>

        {/* Chart type */}
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Chart type
          </label>
          <Select
            value={chartType}
            onValueChange={(v) => updateConfig({ chartType: v as ChartType })}
          >
            <SelectTrigger className="text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {availableChartTypes.map((c) => (
                <SelectItem key={c} value={c}>
                  {CHART_META[c].label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* Compare — time histogram, bar (grouped), numeric histogram (overlay).
            Always rendered; disabled (with the reason) for chart types without
            an honest two-layer encoding, instead of silently disappearing. */}
        <div>
          <label className="mb-1 flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Compare
            <Tooltip
              content={
                compareSupported
                  ? "Adds a second layer evaluated on the same time grid: the whole timeline (baseline) or a second filter set. Both layers always share the time range and bucket width, so they are directly comparable."
                  : compareUnavailableReason(chartType)
              }
              side="right"
            >
              <HelpCircle size={12} className="text-[var(--color-fg-muted)]" />
            </Tooltip>
          </label>
          <div className="space-y-1">
            {(
              [
                { mode: "off", label: "Off" },
                { mode: "baseline", label: "Baseline (all events)" },
                { mode: "custom", label: "Custom filters" },
              ] as const
            ).map((opt) => (
              <label
                key={opt.mode}
                className={`flex items-center gap-2 rounded px-2 py-1.5 text-sm ${
                  !compareSupported
                    ? "cursor-not-allowed opacity-50"
                    : config.compare.mode === opt.mode
                      ? "cursor-pointer bg-[var(--color-accent-dim)]"
                      : "cursor-pointer hover:bg-[var(--color-bg-hover)]"
                }`}
              >
                <input
                  type="radio"
                  name="compare"
                  disabled={!compareSupported}
                  checked={compareSupported && config.compare.mode === opt.mode}
                  onChange={() =>
                    updateConfig({
                      compare:
                        opt.mode === "custom"
                          ? { mode: "custom", filters: {} }
                          : { mode: opt.mode },
                    })
                  }
                  className="accent-[var(--color-accent)]"
                />
                {opt.label}
              </label>
            ))}
          </div>
          {!compareSupported && (
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
              {compareUnavailableReason(chartType)}
            </p>
          )}
          {compareSupported && config.compare.mode === "custom" && (
            <div className="mt-2 rounded border border-[var(--color-border)] p-2">
              <CompareFilterEditor
                filters={config.compare.filters}
                onChange={(f) => updateConfig({ compare: { mode: "custom", filters: f } })}
                fields={fieldsQuery.data?.fields ?? []}
              />
            </div>
          )}
        </div>

        {/* Metric */}
        {dataKind === "time" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Metric
            </label>
            <Select
              value={metric}
              onValueChange={(v) => updateConfig({ metric: v as Metric })}
            >
              <SelectTrigger className="text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {METRICS.filter(metricAvailable).map((m) => (
                  <SelectItem key={m} value={m}>
                    {METRIC_INFO[m].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {metric !== "count" && (
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                {METRIC_INFO[metric].formula}
              </p>
            )}
            {!compareOn && (
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                Turn on Compare to unlock “% of baseline”.
              </p>
            )}
          </div>
        )}

        {/* Per-chart options */}
        {(chartType === "bar" ||
          chartType === "histogram" ||
          chartType === "time" ||
          chartType === "line") && (
          <details className="rounded border border-[var(--color-border)]">
            <summary className="cursor-pointer px-2 py-1.5 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Options
            </summary>
            <div className="space-y-3 px-2 pb-2 pt-1">
              {chartType === "bar" && (
                <>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                      Orientation
                    </label>
                    <Select
                      value={config.options.orientation ?? "horizontal"}
                      onValueChange={(v) =>
                        updateConfig({
                          options: {
                            ...config.options,
                            orientation: v as "horizontal" | "vertical",
                          },
                        })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="horizontal">Horizontal</SelectItem>
                        <SelectItem value="vertical">Vertical</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                      Sort
                    </label>
                    <Select
                      value={config.options.sort ?? "count"}
                      onValueChange={(v) =>
                        updateConfig({
                          options: { ...config.options, sort: v as "count" | "value" },
                        })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="count">By count (descending)</SelectItem>
                        <SelectItem value="value">By value (A→Z)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </>
              )}
              {(chartType === "bar" || chartType === "histogram") && (
                <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                  <input
                    type="checkbox"
                    checked={config.options.logScale ?? false}
                    onChange={(e) =>
                      updateConfig({
                        options: { ...config.options, logScale: e.target.checked },
                      })
                    }
                    className="accent-[var(--color-accent)]"
                  />
                  Log-scale count axis
                </label>
              )}
              {chartType === "time" && (
                <div>
                  <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                    Buckets: {buckets}
                  </label>
                  <input
                    type="range"
                    min={10}
                    max={200}
                    step={10}
                    value={buckets}
                    onChange={(e) =>
                      updateConfig({
                        options: { ...config.options, buckets: Number(e.target.value) },
                      })
                    }
                    className="w-full accent-[var(--color-accent)]"
                  />
                </div>
              )}
              {chartType === "line" && (
                <>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                      Series mode
                    </label>
                    <Select
                      value={config.options.seriesMode ?? "overlay"}
                      onValueChange={(v) =>
                        updateConfig({
                          options: {
                            ...config.options,
                            seriesMode: v as "overlay" | "stacked",
                          },
                        })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="overlay">Overlay (lines)</SelectItem>
                        <SelectItem value="stacked">Stacked (areas)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                    <input
                      type="checkbox"
                      checked={config.options.legend ?? true}
                      onChange={(e) =>
                        updateConfig({
                          options: { ...config.options, legend: e.target.checked },
                        })
                      }
                      className="accent-[var(--color-accent)]"
                    />
                    Show legend
                  </label>
                </>
              )}
            </div>
          </details>
        )}

        {/* Options */}
        {dataKind === "numeric" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Bins: {bins}
            </label>
            <input
              type="range"
              min={5}
              max={100}
              step={5}
              value={bins}
              onChange={(e) =>
                updateConfig({ options: { ...config.options, bins: Number(e.target.value) } })
              }
              className="w-full accent-[var(--color-accent)]"
            />
          </div>
        )}
        {(dataKind === "terms" || dataKind === "timeseries") && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Top values: {topN}
            </label>
            <input
              type="range"
              min={3}
              max={dataKind === "timeseries" ? 20 : 50}
              step={1}
              value={topN}
              onChange={(e) =>
                updateConfig({ options: { ...config.options, topN: Number(e.target.value) } })
              }
              className="w-full accent-[var(--color-accent)]"
            />
          </div>
        )}
        {dataKind === "pivot" && (
          <>
            <div>
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                Top X values: {limitX}
              </label>
              <input
                type="range"
                min={3}
                max={50}
                step={1}
                value={limitX}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, limitX: Number(e.target.value) } })
                }
                className="w-full accent-[var(--color-accent)]"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                Top Y values: {limitY}
              </label>
              <input
                type="range"
                min={3}
                max={50}
                step={1}
                value={limitY}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, limitY: Number(e.target.value) } })
                }
                className="w-full accent-[var(--color-accent)]"
              />
            </div>
          </>
        )}
        {dataKind === "scatter" && (
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                Sample size
              </label>
              <Select
                value={String(sampleLimit)}
                onValueChange={(v) =>
                  updateConfig({ options: { ...config.options, sampleLimit: Number(v) } })
                }
              >
                <SelectTrigger className="text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1000">1,000 points</SelectItem>
                  <SelectItem value="5000">5,000 points</SelectItem>
                  <SelectItem value="10000">10,000 points</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
              <input
                type="checkbox"
                checked={config.options.logScale ?? false}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, logScale: e.target.checked } })
                }
                className="accent-[var(--color-accent)]"
              />
              Log-scale axes (positive values only)
            </label>
          </div>
        )}

        <div className="mt-auto space-y-3 border-t border-[var(--color-border)] pt-3">
          {caseId && timelineId && (
            <SavedChartsRail
              caseId={caseId}
              timelineId={timelineId}
              currentConfig={config}
              onLoad={(loaded) => updateConfig(loaded)}
            />
          )}
          <ExportControls
            svgRef={svgRef}
            filename={`${
              dataKind === "time"
                ? "events_over_time"
                : dataKind === "punchcard"
                  ? "activity_punchcard"
                  : requiresSecondField && field && fieldY
                    ? `${field}_x_${fieldY}`
                    : (field ?? "visualization")
            }_${chartType}`}
            captionLines={captionLines}
          />
        </div>
      </div>

      {/* Canvas */}
      <div className="flex-1 overflow-auto p-4">
        {(filters.start || filters.end) && (
          <div className="mb-2 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
            <span>
              Time range: {filters.start ?? "…"} → {filters.end ?? "…"}
            </span>
            <button
              type="button"
              onClick={() => updateFilters({ ...filters, start: undefined, end: undefined })}
              className="flex items-center gap-1 rounded border border-[var(--color-border)] px-1.5 py-0.5 hover:bg-[var(--color-bg-hover)]"
              title="Clear the start/end range (set by brush-zoom or inherited from the Explorer)"
            >
              <RotateCcw size={11} /> Reset range
            </button>
          </div>
        )}
        {presetsOpen && (
          <div className="mb-4 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                What do you want to find out?
              </span>
              <button
                onClick={() => setPresetsOpen(false)}
                className="text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
                aria-label="Close presets"
              >
                <X size={14} />
              </button>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {CHART_PRESETS.map((p) => (
                <button
                  key={p.id}
                  onClick={() => applyPreset(p)}
                  className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2.5 text-left hover:border-[var(--color-accent)]"
                >
                  <div className="text-sm font-medium text-[var(--color-fg-primary)]">
                    {p.label}
                  </div>
                  <div className="mt-1 text-xs text-[var(--color-fg-muted)]">{p.question}</div>
                </button>
              ))}
            </div>
          </div>
        )}
        {!fieldFree && !field ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-[var(--color-fg-muted)]">
            {fieldsQuery.isLoading ? (
              <>
                <Spinner size={20} />
                <span className="text-xs">Scanning fields — can take a while on large timelines…</span>
              </>
            ) : (
              "Choose a field to visualize."
            )}
          </div>
        ) : requiresSecondField && !fieldY ? (
          <div className="flex h-full items-center justify-center text-sm text-[var(--color-fg-muted)]">
            Choose a second field (Y) to chart {CHART_META[chartType].label.toLowerCase()}.
          </div>
        ) : loading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner size={24} />
          </div>
        ) : (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
            {chartType === "time" && timeQuery.data && (
              <CompareHistogram
                data={timeQuery.data}
                metric={metric}
                hasComparison={compareOn}
                svgRef={svgRef}
                onRangeSelect={(start, end) => updateFilters({ ...filters, start, end })}
              />
            )}
            {chartType === "bar" && (compareTermsOn ? compareTermsQuery.data : termsQuery.data) && (
              <BarChart
                terms={compareTermsOn ? undefined : termsQuery.data}
                compare={compareTermsOn ? compareTermsQuery.data : undefined}
                orientation={config.options.orientation ?? "horizontal"}
                sort={config.options.sort ?? "count"}
                logScale={config.options.logScale ?? false}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "pie" && termsQuery.data && (
              <PieChart terms={termsQuery.data} svgRef={svgRef} onValueClick={handleChartValueClick} />
            )}
            {chartType === "heatmap" && timeseriesQuery.data && (
              <Heatmap data={timeseriesQuery.data} svgRef={svgRef} onValueClick={handleChartValueClick} />
            )}
            {chartType === "line" && timeseriesQuery.data && (
              <LineChart
                data={timeseriesQuery.data}
                seriesMode={config.options.seriesMode ?? "overlay"}
                showLegend={config.options.legend ?? true}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "histogram" &&
              (compareNumericOn ? compareNumericQuery.data : numericQuery.data) && (
                <NumericHistogram
                  stats={compareNumericOn ? undefined : numericQuery.data}
                  compare={compareNumericOn ? compareNumericQuery.data : undefined}
                  logScale={config.options.logScale ?? false}
                  svgRef={svgRef}
                />
              )}
            {chartType === "box" && numericQuery.data && (
              <BoxPlot stats={numericQuery.data} svgRef={svgRef} />
            )}
            {chartType === "violin" && numericQuery.data && (
              <ViolinPlot stats={numericQuery.data} svgRef={svgRef} />
            )}
            {chartType === "ecdf" && numericQuery.data && (
              <EcdfChart stats={numericQuery.data} svgRef={svgRef} />
            )}
            {chartType === "punchcard" && punchcardQuery.data && (
              <PunchCard data={punchcardQuery.data} svgRef={svgRef} />
            )}
            {chartType === "pivot" && pivotQuery.data && (
              <PivotHeatmap
                data={pivotQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "sankey" && pivotQuery.data && (
              <SankeyFlow
                data={pivotQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "scatter" && scatterQuery.data && (
              <ScatterChart
                data={scatterQuery.data}
                logScale={config.options.logScale ?? false}
                svgRef={svgRef}
              />
            )}
            <ChartCaption lines={captionLines} />
          </div>
        )}
      </div>

      {pendingClick && caseId && timelineId && (
        <ChartActionPopover
          click={pendingClick}
          explorerHref={`/cases/${caseId}/timelines/${timelineId}?${filtersToParams(
            applyFieldEntries(filters, pendingClick.entries, true),
          ).toString()}`}
          onFilter={(include) => {
            updateFilters(applyFieldEntries(filters, pendingClick.entries, include));
            setPendingClick(null);
          }}
          onClose={() => setPendingClick(null)}
        />
      )}
    </div>
  );
}
