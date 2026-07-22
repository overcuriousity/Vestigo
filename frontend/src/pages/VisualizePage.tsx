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
import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowLeft, HelpCircle, Lightbulb, Repeat, RotateCcw, X } from "lucide-react";
import { vizApi, type CompareMode } from "@/api/viz";
import { eventsApi } from "@/api/events";
import { timelinesApi } from "@/api/timelines";
import { dispositionsApi } from "@/api/dispositions";
import { filtersToParams, paramsToFilters } from "@/lib/queryParams";
import {
  resolveCollapseRoutine,
  routineSignature,
  type RoutineOverride,
} from "@/lib/routineCollapse";
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
import { FacetGrid } from "@/components/viz/FacetGrid";
import { ChartActionPopover } from "@/components/viz/ChartActionPopover";
import type { ChartValueClick } from "@/components/viz/lib/interaction";
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { WaffleChart } from "@/components/viz/charts/WaffleChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { GroupedDistribution } from "@/components/viz/charts/GroupedDistribution";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";
import { CompareHistogram } from "@/components/viz/charts/CompareHistogram";
import { PunchCard } from "@/components/viz/charts/PunchCard";
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import { CorrMatrix, type CorrMethod } from "@/components/viz/charts/CorrMatrix";
import {
  chartConfigToParams,
  filterParamsPreservingChartConfig,
  histogramToCompare,
  paramsToChartConfig,
  type ChartConfig,
  type ChartType,
  type Scale,
} from "@/components/viz/lib/chartConfig";
import { METRIC_INFO, type Metric } from "@/components/viz/lib/transforms";
import { CHART_META, SCALES } from "@/components/viz/lib/chartMeta";
import {
  resolveChartOptions,
  defaultChartTypeForScale,
  chartTypesForField,
} from "@/components/viz/lib/chartOptions";
import { fieldTokenLabel } from "@/components/viz/lib/fieldDisplay";
import { isTimeField, TIME_FIELDS } from "@/components/viz/lib/timeFields";
import { buildCaptionLines, type CaptionFacts } from "@/components/viz/lib/caption";
import { CHART_PRESETS } from "@/components/viz/lib/presets";
import { pieReadabilityWarning } from "@/components/viz/lib/pieReadability";
import { ChartCaption } from "@/components/viz/primitives/ChartCaption";
import { ExplainerPopover } from "@/components/viz/primitives/ExplainerPopover";
import { CHART_HOW_TO_READ } from "@/components/viz/lib/explainers";
import { NumericStatStrip } from "@/components/viz/NumericStatStrip";
import { ScatterStatsPanel } from "@/components/viz/ScatterStatsPanel";
import type {
  CompareNumericResponse,
  FieldNumericResponse,
  FieldTermsResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
  VizFieldInfo,
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

/** Radix Select forbids an empty-string item value, so "no grouping" needs a
 * sentinel that cannot collide with a real field token. */
const CLEAR_GROUP = "__viz_no_group__";

/** One field picker option: display name plus a muted qualifier.
 *
 * The qualifier is driven off `isTimeField`, not off a null `distinct`: a
 * virtual field has no measured distinct count, and "time field" tells the
 * analyst more about why than an empty parenthetical would. Ordinary fields
 * guard on null anyway, so an absent count renders nothing rather than
 * "(null distinct)". */
function fieldOptionText(f: VizFieldInfo) {
  return (
    <>
      {fieldTokenLabel(f.token)}{" "}
      <span className="text-[var(--color-fg-muted)]">
        {isTimeField(f.token)
          ? "(time field)"
          : f.distinct != null
            ? `(${f.distinct} distinct)`
            : null}
      </span>
    </>
  );
}

/** Why Compare is disabled for a chart type — shown instead of hiding the
 * control (see chartMeta: pie/box/violin/ecdf have no honest two-layer
 * encoding; the newer kinds simply have no compare aggregation yet). */
function compareUnavailableReason(chartType: ChartType): string {
  if (chartType === "punchcard" || chartType === "pivot" || chartType === "sankey" || chartType === "scatter") {
    return "Compare isn't supported for this chart type yet.";
  }
  return "This chart type has no honest two-layer encoding — overlaid layers would misrepresent one of them. Use Bar, Histogram, or the Time histogram to compare.";
}

export function VisualizePage() {
  const { caseId, timelineId } = useParams<{ caseId: string; timelineId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const urlFilters = useMemo(() => paramsToFilters(searchParams), [searchParams]);
  const config = useMemo(() => paramsToChartConfig(searchParams), [searchParams]);

  // Routine collapse, derived exactly as on ExplorerPage (#147): a mute is a
  // filter and the charts must aggregate the set the grid displays.
  // `collapseRoutine` is deliberately never URL-serialized, so it cannot
  // arrive via `paramsToFilters` — the disposition set in Postgres is the
  // single source of truth, and deriving from it means a shared URL shows a
  // teammate the same collapsed charts. lib/routineCollapse.ts owns the
  // precedence and why the reveal override self-expires.
  const dispositionsQuery = useQuery({
    queryKey: ["dispositions", caseId, timelineId],
    queryFn: () => dispositionsApi.list(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });
  const routineSig = useMemo(
    () => routineSignature(dispositionsQuery.data?.dispositions ?? []),
    [dispositionsQuery.data],
  );
  const hasRoutineDispositions = routineSig !== "";
  const [routineOverride, setRoutineOverride] = useState<RoutineOverride>(null);
  const collapseRoutine = resolveCollapseRoutine(routineSig, routineOverride);
  // Every chart query waits for the disposition set: an uncollapsed first
  // fetch would render (then refetch and recompute) the muted superset on
  // every page load with mutes — the #147 flash, one page over. One small
  // Postgres query before first paint, usually already warm from Explorer.
  const scopeReady = !!(caseId && timelineId) && dispositionsQuery.isSuccess;
  const filters = useMemo(
    () => (collapseRoutine ? { ...urlFilters, collapseRoutine: true } : urlFilters),
    [urlFilters, collapseRoutine],
  );

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
  // box/violin take an OPTIONAL second field: a categorical grouping
  // variable turning one distribution into one per group.
  const acceptsSecondField = !!CHART_META[chartType].acceptsSecondField;
  // The correlation matrix charts a LIST of fields instead of field/fieldY.
  const multiField = !!CHART_META[chartType].multiField;
  const supportsFacet = !!CHART_META[chartType].supportsFacet;
  // Facet and compare are mutually exclusive (one splits into panels, the
  // other overlays layers), so a chart type that lost facet support drops
  // the spec rather than rendering a grid it cannot draw.
  const facet = supportsFacet ? config.facet : null;
  const selectedFields = config.fields ?? [];
  const groupedOn = acceptsSecondField && !!fieldY;
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

  // Shared with the agent's ChartProposalCard so a proposed chart and a
  // hand-built one resolve their defaults identically.
  const resolved = useMemo(() => resolveChartOptions(config), [config]);
  const { topN, bins, buckets, limitX, limitY, sampleLimit, groups, showPoints } = resolved;

  const svgRef = useRef<SVGSVGElement | null>(null);
  // Preset strip: open by default on a fresh page (no chart state in the
  // URL yet); a URL that already describes a chart skips the guidance.
  // Which coefficient fills the matrix cells. Purely a client-side read of
  // the same response — both coefficients always ship, so switching never
  // refetches (same reasoning as pivot↔sankey).
  const [corrMethod, setCorrMethod] = useState<CorrMethod>("pearson");
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
  // A virtual `time:` field's scale is known statically, and its SQL yields
  // zero-padded strings — `field_numeric_stats` would scan the timeline only
  // to report `count: 0` and land the analyst on nominal/bar, contradicting
  // TIME_FIELDS. Never probe one.
  const fieldIsTime = field != null && isTimeField(field);
  // A pairing the rail never offers but a saved chart or URL can still carry:
  // a numeric-fed mark over a `time:` field, whose SQL yields strings. Tested
  // on the data kind rather than on `chartTypesForField`, so a URL with an
  // inconsistent scale/chartType pair still falls through to its own handling
  // instead of collecting this (wrong) explanation.
  const chartTypeUnplottable = fieldIsTime && (dataKind === "numeric" || dataKind === "scatter");
  const numericQuery = useQuery({
    queryKey: ["viz-field-numeric", caseId, timelineId, field, filters, bins, showPoints],
    queryFn: () =>
      vizApi.fieldNumeric(caseId!, timelineId!, field!, filters, bins, showPoints),
    // Run only when a numeric chart actually needs the data, or when a
    // *field-dependent* chart needs its one-time scale probe. The field-free
    // charts (time, punchcard) never need it, and the two-field charts have
    // their own endpoints and keep their chart type — skipping the probe
    // there avoids the field_numeric_stats double-scan.
    //
    // `!fieldIsTime` is a top-level conjunct, not part of the probe disjunct:
    // gating only the probe would still fire the scan whenever a numeric
    // chart type happened to be selected.
    enabled:
      scopeReady &&
      !!field &&
      !fieldIsTime &&
      !multiField &&
      !groupedOn &&
      (dataKind === "numeric" ||
        (!fieldFree && !requiresSecondField && field !== autoProbedField.current)),
  });

  // Scale suggestion for a virtual time field — the statically-known answer,
  // no round-trip. Must run before the numeric-probe effect below so the
  // shared `autoProbedField` ref is spent first; React runs effects in
  // declaration order.
  useEffect(() => {
    if (!field || !fieldIsTime || field === autoProbedField.current) return;
    // Advance the ref even when the early-return below fires: it means "this
    // field's one-shot suggestion is spent", not "we fetched something".
    autoProbedField.current = field;
    if (fieldFree || requiresSecondField || multiField) return;
    const scale = TIME_FIELDS[field].scale;
    updateConfig({ scale, chartType: defaultChartTypeForScale(scale, field) });
  }, [field, fieldIsTime, fieldFree, requiresSecondField, multiField, updateConfig]);

  useEffect(() => {
    if (!field || field === autoProbedField.current) return;
    // Inert for time fields anyway (the query is disabled, so `data` stays
    // undefined) — stated explicitly so the intent survives a refactor.
    if (fieldIsTime) return;
    if (numericQuery.data == null) return;
    autoProbedField.current = field;
    // Don't yank the analyst off the field-independent charts (time,
    // punchcard) or a deliberately-picked two-field chart.
    if (fieldFree || requiresSecondField || multiField) return;
    const isNumeric = numericQuery.data.count > 0;
    updateConfig({
      scale: isNumeric ? "ratio" : "nominal",
      chartType: isNumeric ? "histogram" : "bar",
    });
  }, [
    field,
    fieldIsTime,
    numericQuery.data,
    fieldFree,
    requiresSecondField,
    multiField,
    updateConfig,
  ]);

  // Keep chartType valid when the analyst switches scale — clamped at event
  // time rather than in an effect, so there is never a render with an
  // inconsistent scale/chartType pair.
  const handleScaleChange = (s: Scale) => {
    // Also re-picks when the type is legal for the new scale but not for the
    // field — a `time:` field cannot feed a numeric mark at any scale.
    if (!chartTypesForField(s, field).includes(chartType)) {
      updateConfig({ scale: s, chartType: defaultChartTypeForScale(s, field) });
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
    enabled: scopeReady && !!field && dataKind === "terms" && !compareTermsOn,
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
    enabled: scopeReady && !!field && compareTermsOn,
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
        // The comparison aggregation negotiates shared bin edges between the
        // two layers and has no auto path — fall back to the manual default.
        bins: bins ?? 30,
      })) as CompareNumericResponse,
    enabled: scopeReady && !!field && compareNumericOn,
  });

  // Grouped box/violin: one distribution per top-N value of the grouping
  // field, all binned over the same global range (server-side) so the
  // silhouettes are comparable.
  const groupedQuery = useQuery({
    queryKey: [
      "viz-field-numeric-grouped",
      caseId,
      timelineId,
      field,
      fieldY,
      filters,
      groups,
      bins,
      showPoints,
    ],
    queryFn: () =>
      vizApi.fieldNumericGrouped(
        caseId!,
        timelineId!,
        field!,
        fieldY!,
        filters,
        groups,
        bins ?? 30,
        showPoints,
      ),
    enabled: scopeReady && !!field && !!fieldY && groupedOn,
  });

  const correlationQuery = useQuery({
    queryKey: ["viz-field-correlation", caseId, timelineId, selectedFields, filters],
    queryFn: () => vizApi.fieldCorrelation(caseId!, timelineId!, selectedFields, filters),
    enabled: scopeReady && multiField && selectedFields.length >= 2,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["viz-field-timeseries", caseId, timelineId, field, filters, buckets, topN],
    queryFn: () => vizApi.fieldTimeseries(caseId!, timelineId!, field!, filters, buckets, topN),
    enabled: scopeReady && !!field && dataKind === "timeseries",
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
    enabled: scopeReady && dataKind === "time",
  });

  const punchcardQuery = useQuery({
    queryKey: ["viz-punchcard", caseId, timelineId, filters],
    queryFn: () => vizApi.punchcard(caseId!, timelineId!, filters),
    enabled: scopeReady && dataKind === "punchcard",
  });

  // Shared by the pivot heatmap AND the sankey (same aggregation, two marks)
  // — switching between those chart types refetches nothing.
  const pivotQuery = useQuery({
    queryKey: ["viz-field-pivot", caseId, timelineId, field, fieldY, filters, limitX, limitY],
    queryFn: () => vizApi.fieldPivot(caseId!, timelineId!, field!, fieldY!, filters, limitX, limitY),
    enabled: scopeReady && !!(field && fieldY) && dataKind === "pivot",
  });

  const scatterQuery = useQuery({
    queryKey: ["viz-field-scatter", caseId, timelineId, field, fieldY, filters, sampleLimit],
    queryFn: () => vizApi.fieldScatter(caseId!, timelineId!, field!, fieldY!, filters, sampleLimit),
    enabled: scopeReady && !!(field && fieldY) && dataKind === "scatter",
  });

  const availableChartTypes = chartTypesForField(scale, field);

  // ── facetting (small multiples) ───────────────────────────────────────
  // Client-orchestrated: one terms query names the panels, then each panel
  // re-runs the SAME endpoint with an added equality filter. No new server
  // aggregation, and each panel's data is the honest answer to "this chart,
  // restricted to this value" — which is exactly what the grid claims.
  const facetValuesQuery = useQuery({
    queryKey: ["viz-facet-values", caseId, timelineId, facet?.field, filters, facet?.limit],
    queryFn: () =>
      vizApi.fieldTerms(caseId!, timelineId!, facet!.field, filters, facet!.limit),
    enabled: scopeReady && !!facet,
  });
  const facetValues = facetValuesQuery.data?.values ?? [];

  const facetPanelQueries = useQueries({
    queries: facetValues.map((v) => {
      const panelFilters = applyFieldEntries(filters, [[facet!.field, v.value]], true);
      return {
        queryKey: [
          "viz-facet-panel",
          caseId,
          timelineId,
          chartType,
          field,
          panelFilters,
          topN,
          bins,
          buckets,
          showPoints,
        ],
        queryFn: async () => {
          switch (dataKind) {
            case "terms":
              return vizApi.fieldTerms(caseId!, timelineId!, field!, panelFilters, topN);
            case "numeric":
              return vizApi.fieldNumeric(
                caseId!,
                timelineId!,
                field!,
                panelFilters,
                bins,
                showPoints,
              );
            default:
              return histogramToCompare(
                await eventsApi.histogram(caseId!, timelineId!, panelFilters, buckets),
              );
          }
        },
        enabled: scopeReady && !!facet && (fieldFree || !!field),
      };
    }),
  });

  // Small multiples are only comparable if every panel shares a scale —
  // otherwise two bars of equal height mean different counts, which is the
  // one failure a facet grid must not have. Computed across the loaded
  // panels and passed down; each panel still draws its own axis.
  const facetPanelData = facetPanelQueries.map((q) => q.data);
  const facetCountMax = facet
    ? Math.max(
        1,
        ...facetPanelData.flatMap((d) => {
          if (d == null) return [];
          if ("values" in d) return d.values.map((v) => v.count);
          if ("bins" in d && Array.isArray(d.bins)) {
            return (d.bins as { count?: number; primary?: number }[]).map(
              (b) => b.count ?? b.primary ?? 0,
            );
          }
          if ("buckets" in d) {
            return (d.buckets as { primary: number }[]).map((b) => b.primary);
          }
          return [];
        }),
      )
    : undefined;
  const facetValueDomain: [number, number] | undefined = (() => {
    if (!facet || dataKind !== "numeric") return undefined;
    const mins = facetPanelData.flatMap((d) =>
      d != null && "min" in d && typeof d.min === "number" ? [d.min] : [],
    );
    const maxes = facetPanelData.flatMap((d) =>
      d != null && "max" in d && typeof d.max === "number" ? [d.max] : [],
    );
    return mins.length && maxes.length
      ? [Math.min(...mins), Math.max(...maxes)]
      : undefined;
  })();

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
      facts.overlayShown = numericQuery.data.points?.shown;
      facts.overlayTotal = numericQuery.data.points?.total;
      facts.primaryTotal = numericQuery.data.count;
      facts.binCount = numericQuery.data.bins.length;
      facts.valueMin = numericQuery.data.min;
      facts.valueMax = numericQuery.data.max;
      facts.binRule = numericQuery.data.bin_rule;
      facts.skewness = numericQuery.data.skewness;
    }
    if (groupedOn && groupedQuery.data) {
      facts.primaryTotal = groupedQuery.data.total;
      facts.groupField = groupedQuery.data.group_field;
      facts.groupsShown = groupedQuery.data.groups.length;
      facts.groupsOmitted = groupedQuery.data.omitted_groups;
      facts.groupOmittedCount = groupedQuery.data.omitted_count;
      facts.valueMin = groupedQuery.data.min;
      facts.valueMax = groupedQuery.data.max;
      facts.binCount = undefined;
      facts.binRule = undefined;
      facts.skewness = undefined;
      facts.overlayShown = groupedQuery.data.points?.shown;
      facts.overlayTotal = groupedQuery.data.points?.total;
    }
  } else if (dataKind === "timeseries" && timeseriesQuery.data) {
    facts.shownValues = timeseriesQuery.data.series.length;
    facts.intervalSeconds = timeseriesQuery.data.interval_seconds;
  } else if (dataKind === "punchcard" && punchcardQuery.data) {
    facts.primaryTotal = punchcardQuery.data.total;
  } else if (dataKind === "pivot" && pivotQuery.data) {
    facts.primaryTotal = pivotQuery.data.total;
    // A bounded `time:` axis reports its domain size, not a measured distinct
    // count, and was charted whole — there is no "rest in Other" to caption.
    // Left undefined rather than relying on `distinct > shown` happening to be
    // false, so the caption cannot claim truncation that did not occur.
    facts.xDistinct = pivotQuery.data.x_bounded ? undefined : pivotQuery.data.x_distinct;
    facts.xShown = pivotQuery.data.x_values.length;
    facts.yDistinct = pivotQuery.data.y_bounded ? undefined : pivotQuery.data.y_distinct;
    facts.yShown = pivotQuery.data.y_values.length;
  } else if (dataKind === "corr" && correlationQuery.data) {
    facts.primaryTotal = correlationQuery.data.total;
    facts.corrFields = correlationQuery.data.fields;
    facts.corrPairs = correlationQuery.data.pairs.length;
    facts.corrDropped = correlationQuery.data.dropped_fields.map((d) => d.field);
    facts.corrMinPairN = correlationQuery.data.pairs.length
      ? Math.min(...correlationQuery.data.pairs.map((p) => p.n))
      : undefined;
    facts.corrMaxPairN = correlationQuery.data.pairs.length
      ? Math.max(...correlationQuery.data.pairs.map((p) => p.n))
      : undefined;
  } else if (dataKind === "scatter" && scatterQuery.data) {
    facts.primaryTotal = scatterQuery.data.total;
    facts.sampledPoints = scatterQuery.data.sampled;
    facts.totalPoints = scatterQuery.data.total;
    facts.scatterStats = scatterQuery.data.stats;
  }

  if (facet) {
    facts.facetField = facet.field;
    facts.facetPanels = facetValues.length;
    facts.facetOmittedValues = Math.max(
      0,
      (facetValuesQuery.data?.distinct ?? 0) - facetValues.length,
    );
    facts.facetOmittedCount = facetValuesQuery.data?.other_count;
  }

  // Advisory only — the pie still renders; the same rule runs in
  // `propose_chart`, so an agent proposal carries the identical caution.
  const pieWarning =
    chartType === "pie" && termsQuery.data ? pieReadabilityWarning(termsQuery.data) : null;
  if (pieWarning) facts.readabilityWarning = pieWarning;

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
    (dataKind === "scatter" && scatterQuery.isLoading) ||
    (dataKind === "corr" && correlationQuery.isLoading);

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

        {/* Field picker — hidden for the correlation matrix, which charts a
            list of fields instead (its own picker is below). */}
        <div className={multiField ? "hidden" : undefined}>
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
                    {fieldOptionText(f)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>

        {/* Field list — the correlation matrix charts 2–8 fields at once */}
        {multiField && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Fields to correlate ({selectedFields.length}/8){" "}
              <ExplainerPopover id="correlationMatrix" />
            </label>
            <div className="mb-1 flex flex-wrap gap-1">
              {selectedFields.map((token) => (
                <button
                  key={token}
                  type="button"
                  onClick={() =>
                    updateConfig({ fields: selectedFields.filter((f) => f !== token) })
                  }
                  className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
                  title="Remove from the matrix"
                >
                  {fieldTokenLabel(token)} <X size={10} />
                </button>
              ))}
              {selectedFields.length === 0 && (
                <span className="text-xs text-[var(--color-fg-muted)]">
                  Pick at least two numeric fields.
                </span>
              )}
            </div>
            <div className="mb-1 flex gap-1">
              {(["pearson", "spearman"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setCorrMethod(m)}
                  className={`flex-1 rounded border px-2 py-1 text-xs ${
                    corrMethod === m
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-fg-primary)]"
                      : "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
                  }`}
                >
                  {m === "pearson" ? "Pearson r" : "Spearman ρ"}
                </button>
              ))}
              <ExplainerPopover id={corrMethod === "pearson" ? "pearson" : "spearman"} />
            </div>
            <Select
              value={undefined}
              onValueChange={(v) =>
                updateConfig({ fields: [...selectedFields, v].slice(0, 8) })
              }
            >
              <SelectTrigger className="text-sm">
                <SelectValue placeholder="Add a field…" />
              </SelectTrigger>
              <SelectContent>
                {(fieldsQuery.data?.fields ?? [])
                  .filter((f) => !selectedFields.includes(f.token) && !isTimeField(f.token))
                  .map((f) => (
                    <SelectItem key={f.token} value={f.token}>
                      {fieldOptionText(f)}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
        )}

        {/* Second field picker — pivot/sankey/scatter chart both axes;
            box/violin use it optionally as a grouping variable */}
        {(requiresSecondField || acceptsSecondField) && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              {acceptsSecondField ? "Group by (optional)" : "Field (Y)"}
            </label>
            <Select
              value={fieldY ?? undefined}
              onValueChange={(v) => updateConfig({ fieldY: v === CLEAR_GROUP ? null : v })}
            >
              <SelectTrigger className="text-sm">
                <SelectValue
                  placeholder={
                    acceptsSecondField ? "No grouping" : "Choose a second field…"
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {acceptsSecondField && (
                  <SelectItem value={CLEAR_GROUP}>No grouping</SelectItem>
                )}
                {(fieldsQuery.data?.fields ?? [])
                  .filter((f) => f.token !== field)
                  .map((f) => (
                    <SelectItem key={f.token} value={f.token}>
                      {fieldOptionText(f)}
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
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            {CHART_HOW_TO_READ[chartType]}
          </p>
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
                      // Comparison and facetting are mutually exclusive —
                      // turning one on clears the other, in both directions.
                      ...(opt.mode !== "off" ? { facet: null } : {}),
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
          {compareSupported && facet && (
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
              Turning on a comparison layer clears the panel split — one overlays two
              layers in a single chart, the other splits the data across charts.
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
                  <label className="flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                    <input
                      type="checkbox"
                      checked={config.options.showPoints ?? true}
                      onChange={(e) =>
                        updateConfig({
                          options: { ...config.options, showPoints: e.target.checked },
                        })
                      }
                      className="accent-[var(--color-accent)]"
                    />
                    Mark measured points
                  </label>
                </>
              )}
            </div>
          </details>
        )}

        {/* Facet — small multiples */}
        {supportsFacet && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Split into panels by (optional)
            </label>
            <Select
              value={facet?.field ?? undefined}
              onValueChange={(v) =>
                updateConfig(
                  v === CLEAR_GROUP
                    ? { facet: null }
                    : // A facet grid and a comparison layer are mutually
                      // exclusive, so picking one clears the other.
                      { facet: { field: v, limit: facet?.limit ?? 6 }, compare: { mode: "off" } },
                )
              }
            >
              <SelectTrigger className="text-sm">
                <SelectValue placeholder="No panels" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={CLEAR_GROUP}>No panels</SelectItem>
                {(fieldsQuery.data?.fields ?? [])
                  .filter((f) => f.token !== field)
                  .map((f) => (
                    <SelectItem key={f.token} value={f.token}>
                      {fieldOptionText(f)}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            {facet && (
              <div className="mt-1">
                <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                  Panels: {facet.limit}
                </label>
                <input
                  type="range"
                  min={2}
                  max={12}
                  step={1}
                  value={facet.limit}
                  onChange={(e) =>
                    updateConfig({ facet: { ...facet, limit: Number(e.target.value) } })
                  }
                  className="w-full accent-[var(--color-accent)]"
                />
              </div>
            )}
          </div>
        )}

        {/* Options */}
        {dataKind === "numeric" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Bins: {bins ?? `auto (${numericQuery.data?.bins.length ?? "…"})`}{" "}
              <ExplainerPopover id="fdRule" />
            </label>
            <label className="mb-1 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
              <input
                type="checkbox"
                checked={bins == null}
                onChange={(e) =>
                  updateConfig({
                    options: {
                      ...config.options,
                      bins: e.target.checked ? undefined : (numericQuery.data?.bins.length ?? 30),
                    },
                  })
                }
                className="accent-[var(--color-accent)]"
              />
              Automatic bin width (Freedman–Diaconis)
            </label>
            {(chartType === "box" || chartType === "violin") && (
              <>
                <label className="mb-1 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                  <input
                    type="checkbox"
                    checked={showPoints}
                    onChange={(e) =>
                      updateConfig({
                        options: { ...config.options, showPoints: e.target.checked },
                      })
                    }
                    className="accent-[var(--color-accent)]"
                  />
                  Overlay data points <ExplainerPopover id="sampledPoints" />
                </label>
                {groupedOn && (
                  <div className="mb-1">
                    <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                      Groups: {groups}
                    </label>
                    <input
                      type="range"
                      min={2}
                      max={8}
                      step={1}
                      value={groups}
                      onChange={(e) =>
                        updateConfig({
                          options: { ...config.options, groups: Number(e.target.value) },
                        })
                      }
                      className="w-full accent-[var(--color-accent)]"
                    />
                  </div>
                )}
              </>
            )}
            {chartType === "histogram" && (
              <label className="mb-1 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                <input
                  type="checkbox"
                  checked={resolved.showDensity}
                  onChange={(e) =>
                    updateConfig({
                      options: { ...config.options, showDensity: e.target.checked },
                    })
                  }
                  className="accent-[var(--color-accent)]"
                />
                Density curve (KDE) <ExplainerPopover id="kde" />
              </label>
            )}
            {bins != null && (
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
            )}
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
        {/* Nothing hidden silently: whenever routine dispositions shape the
            charts (or have been revealed), say so — the grid's collapsed-count
            stat, one page over. Renders only when the set is non-empty, same
            as the Explorer's toggle. */}
        {hasRoutineDispositions && (
          <div className="mb-2 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
            <span>
              {collapseRoutine
                ? "Routine events collapsed (muted templates and patterns marked routine) — charts match the Explorer grid"
                : "Routine events shown — charts include events muted in the Explorer"}
            </span>
            <Tooltip
              content={
                collapseRoutine
                  ? "Temporarily show routine events — the next mute re-applies collapse"
                  : "Collapse routine events again"
              }
            >
              <button
                type="button"
                onClick={() =>
                  setRoutineOverride({ value: !collapseRoutine, signature: routineSig })
                }
                className={`flex items-center gap-1 rounded border px-1.5 py-0.5 hover:bg-[var(--color-bg-hover)] ${
                  collapseRoutine
                    ? "border-[var(--color-border)]"
                    : "border-[var(--color-accent)] text-[var(--color-accent)]"
                }`}
              >
                <Repeat size={11} /> {collapseRoutine ? "Show routine events" : "Collapse routine"}
              </button>
            </Tooltip>
          </div>
        )}
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
        {multiField && selectedFields.length < 2 ? (
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--color-fg-muted)]">
            Pick at least two numeric fields to correlate.
          </div>
        ) : !fieldFree && !multiField && !field ? (
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
        ) : chartTypeUnplottable ? (
          // The rail cannot offer this pairing, but a saved chart or a URL can
          // still carry one. Without this branch the numeric probe stays
          // disabled, `numericQuery.data` never arrives, and every render gate
          // below is `data && <Chart/>` — a blank canvas with no spinner and
          // no explanation.
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--color-fg-muted)]">
            {fieldTokenLabel(field!)} has no numeric values, so{" "}
            {CHART_META[chartType].label.toLowerCase()} would render empty. Pick a categorical
            chart type — bar, pie or heatmap.
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
                orientation={resolved.orientation}
                sort={resolved.sort}
                logScale={resolved.logScale}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "pie" && termsQuery.data && (
              <>
                {pieWarning && (
                  <div className="mb-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-subtle)] px-3 py-2 text-xs text-[var(--color-fg-secondary)]">
                    <strong className="text-[var(--color-fg-primary)]">Readability:</strong>{" "}
                    {pieWarning}{" "}
                    <button
                      type="button"
                      className="underline hover:text-[var(--color-accent)]"
                      onClick={() => updateConfig({ chartType: "waffle" })}
                    >
                      Switch to waffle
                    </button>
                  </div>
                )}
                <PieChart terms={termsQuery.data} svgRef={svgRef} onValueClick={handleChartValueClick} />
              </>
            )}
            {chartType === "waffle" && termsQuery.data && (
              <WaffleChart
                terms={termsQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "heatmap" && timeseriesQuery.data && (
              <Heatmap data={timeseriesQuery.data} svgRef={svgRef} onValueClick={handleChartValueClick} />
            )}
            {chartType === "line" && timeseriesQuery.data && (
              <LineChart
                data={timeseriesQuery.data}
                seriesMode={resolved.seriesMode}
                // Line markers default ON (Tufte: show where data actually
                // is); the shared resolver defaults showPoints off because
                // box/violin overlays cost an extra scan, so read the raw
                // option here instead of the resolved one.
                showPoints={config.options.showPoints ?? true}
                showLegend={resolved.legend}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "histogram" &&
              (compareNumericOn ? compareNumericQuery.data : numericQuery.data) && (
                <NumericHistogram
                  stats={compareNumericOn ? undefined : numericQuery.data}
                  compare={compareNumericOn ? compareNumericQuery.data : undefined}
                  logScale={resolved.logScale}
                  showDensity={resolved.showDensity}
                  showMarkers
                  svgRef={svgRef}
                />
              )}
            {chartType === "histogram" && !compareNumericOn && numericQuery.data && (
              <NumericStatStrip stats={numericQuery.data} />
            )}
            {groupedOn && (chartType === "box" || chartType === "violin") && groupedQuery.data && (
              <GroupedDistribution
                data={groupedQuery.data}
                mark={chartType}
                showPoints={showPoints}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {(chartType === "box" || chartType === "violin") && (
              <div className="mb-1 flex flex-wrap items-center gap-3 text-xs text-[var(--color-fg-muted)]">
                <span className="flex items-center gap-1">
                  Median <ExplainerPopover id="median" />
                </span>
                <span className="flex items-center gap-1">
                  Quartiles <ExplainerPopover id="quartiles" />
                </span>
                <span className="flex items-center gap-1">
                  IQR <ExplainerPopover id="iqr" />
                </span>
                {chartType === "box" ? (
                  <span className="flex items-center gap-1">
                    Whiskers <ExplainerPopover id="whiskers" />
                  </span>
                ) : (
                  <span className="flex items-center gap-1">
                    Density shape <ExplainerPopover id="kde" />
                  </span>
                )}
              </div>
            )}
            {!groupedOn && chartType === "box" && numericQuery.data && (
              <BoxPlot stats={numericQuery.data} showPoints={showPoints} svgRef={svgRef} />
            )}
            {!groupedOn && chartType === "violin" && numericQuery.data && (
              <ViolinPlot stats={numericQuery.data} showPoints={showPoints} svgRef={svgRef} />
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
            {facet ? (
              <FacetGrid
                field={facet.field}
                omittedValues={Math.max(
                  0,
                  (facetValuesQuery.data?.distinct ?? 0) - facetValues.length,
                )}
                omittedCount={facetValuesQuery.data?.other_count}
                panels={facetValues.map((v, i) => {
                  const panel = facetPanelQueries[i];
                  const data = panel?.data;
                  return {
                    value: v.value,
                    count: v.count,
                    isLoading: !!panel?.isLoading,
                    chart:
                      data == null ? null : dataKind === "terms" ? (
                        chartType === "pie" ? (
                          <PieChart terms={data as FieldTermsResponse} height={180} />
                        ) : chartType === "waffle" ? (
                          <WaffleChart terms={data as FieldTermsResponse} height={180} />
                        ) : (
                          <BarChart
                            terms={data as FieldTermsResponse}
                            height={180}
                            countMax={facetCountMax}
                            orientation={resolved.orientation}
                            sort={resolved.sort}
                            logScale={resolved.logScale}
                          />
                        )
                      ) : dataKind === "numeric" ? (
                        chartType === "box" ? (
                          <BoxPlot
                            stats={data as FieldNumericResponse}
                            height={180}
                            showPoints={showPoints}
                            domain={facetValueDomain}
                          />
                        ) : chartType === "violin" ? (
                          <ViolinPlot
                            stats={data as FieldNumericResponse}
                            height={180}
                            showPoints={showPoints}
                            domain={facetValueDomain}
                          />
                        ) : chartType === "ecdf" ? (
                          <EcdfChart stats={data as FieldNumericResponse} height={180} />
                        ) : (
                          <NumericHistogram
                            stats={data as FieldNumericResponse}
                            height={180}
                            logScale={resolved.logScale}
                            showDensity={resolved.showDensity}
                            showMarkers
                            countMax={facetCountMax}
                          />
                        )
                      ) : (
                        <CompareHistogram
                          data={data as CompareTimeResponse}
                          height={180}
                          metric={metric}
                          hasComparison={false}
                        />
                      ),
                  };
                })}
              />
            ) : null}
            {!facet && multiField && correlationQuery.data && (
              <CorrMatrix
                data={correlationQuery.data}
                method={corrMethod}
                svgRef={svgRef}
                onPairClick={(x, y) =>
                  updateConfig({ chartType: "scatter", field: x, fieldY: y, scale: "ratio" })
                }
              />
            )}
            {chartType === "scatter" && scatterQuery.data && (
              <>
                <ScatterChart
                  data={scatterQuery.data}
                  logScale={resolved.logScale}
                  svgRef={svgRef}
                />
                {scatterQuery.data.stats && (
                  <ScatterStatsPanel stats={scatterQuery.data.stats} />
                )}
              </>
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
