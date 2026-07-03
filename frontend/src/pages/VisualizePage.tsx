/**
 * VisualizePage — full statistical visualization workbench.
 *
 * Inherits the Explorer's current filters/time-range from the URL (same
 * `paramsToFilters` the Explorer itself reads), so a chart here always
 * matches whatever the analyst was just looking at in the grid. The analyst
 * picks a field, declares its scale of measurement, and gets the chart
 * types appropriate to that scale — each backed by one of the three
 * `vizApi` aggregations (or the Explorer's own time histogram for the
 * "events over time" case).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, HelpCircle } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { vizApi } from "@/api/viz";
import { timelinesApi } from "@/api/timelines";
import { paramsToFilters } from "@/lib/queryParams";
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
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";

type Scale = "nominal" | "ordinal" | "interval" | "ratio";
type ChartType = "bar" | "pie" | "heatmap" | "line" | "histogram" | "box" | "violin" | "ecdf";
type DataKind = "terms" | "numeric" | "timeseries";

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

const CHART_META: Record<ChartType, { label: string; scales: Scale[]; dataKind: DataKind }> = {
  bar: { label: "Bar", scales: ["nominal", "ordinal"], dataKind: "terms" },
  pie: { label: "Pie / Donut", scales: ["nominal"], dataKind: "terms" },
  heatmap: {
    label: "Heatmap (value × time)",
    scales: ["nominal", "ordinal", "interval"],
    dataKind: "timeseries",
  },
  line: { label: "Line / Area (value × time)", scales: ["interval", "ratio"], dataKind: "timeseries" },
  histogram: { label: "Histogram", scales: ["interval", "ratio"], dataKind: "numeric" },
  box: { label: "Box plot", scales: ["ratio"], dataKind: "numeric" },
  violin: { label: "Violin plot", scales: ["ratio"], dataKind: "numeric" },
  ecdf: { label: "ECDF", scales: ["ratio"], dataKind: "numeric" },
};

const SCALES: Scale[] = ["nominal", "ordinal", "interval", "ratio"];

const chartTypesFor = (s: Scale): ChartType[] =>
  (Object.keys(CHART_META) as ChartType[]).filter((c) => CHART_META[c].scales.includes(s));

export function VisualizePage() {
  const { caseId, timelineId } = useParams<{ caseId: string; timelineId: string }>();
  const [searchParams] = useSearchParams();
  const filters = useMemo(() => paramsToFilters(searchParams), [searchParams]);

  const [field, setField] = useState<string | null>(null);
  const [scale, setScale] = useState<Scale>("nominal");
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [topN, setTopN] = useState(10);
  const [bins, setBins] = useState(30);
  // Last field the numeric probe auto-suggested a scale for — state (not a
  // ref) because it also gates whether the probe query needs to run at all.
  const [autoProbedField, setAutoProbedField] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);

  const timelineQuery = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const fieldsQuery = useQuery({
    queryKey: ["anomaly-fields", caseId, timelineId],
    queryFn: () => anomaliesApi.fields(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  // Default to the first recommended field once the list loads.
  useEffect(() => {
    if (field == null && fieldsQuery.data?.fields.length) {
      const first = fieldsQuery.data.fields.find((f) => f.recommended) ?? fieldsQuery.data.fields[0];
      setField(first.token);
    }
  }, [field, fieldsQuery.data]);

  const dataKind = CHART_META[chartType].dataKind;

  // Probe numeric-ness only when actually needed: once per field change (to
  // auto-suggest a scale) and while a numeric chart type is displayed (as its
  // data source) — not on every bins change while looking at a terms chart.
  const numericQuery = useQuery({
    queryKey: ["viz-field-numeric", caseId, timelineId, field, filters, bins],
    queryFn: () => vizApi.fieldNumeric(caseId!, timelineId!, field!, filters, bins),
    enabled:
      !!(caseId && timelineId && field) && (dataKind === "numeric" || field !== autoProbedField),
  });

  // Auto-suggest a scale once per field change; the analyst can still
  // override it manually afterward without being reset until the field
  // changes again.
  useEffect(() => {
    if (!field || field === autoProbedField) return;
    if (numericQuery.data == null) return;
    setAutoProbedField(field);
    const isNumeric = numericQuery.data.count > 0;
    setScale(isNumeric ? "ratio" : "nominal");
    setChartType(isNumeric ? "histogram" : "bar");
  }, [field, autoProbedField, numericQuery.data]);

  // Keep chartType valid when the analyst switches scale — clamped at event
  // time rather than in an effect, so there is never a render with an
  // inconsistent scale/chartType pair. (Every scale has at least one chart
  // type in CHART_META.)
  const handleScaleChange = (s: Scale) => {
    setScale(s);
    if (!CHART_META[chartType].scales.includes(s)) setChartType(chartTypesFor(s)[0]);
  };

  // The slider's valid range differs by data kind (terms charts allow up to
  // 50 values, timeseries up to 20 series); clamp the shared state on
  // chart-type switch so the request always matches what the slider shows.
  const maxTopN = dataKind === "timeseries" ? 20 : 50;
  const effectiveTopN = Math.min(topN, maxTopN);

  const termsQuery = useQuery({
    queryKey: ["viz-field-terms", caseId, timelineId, field, filters, effectiveTopN],
    queryFn: () => vizApi.fieldTerms(caseId!, timelineId!, field!, filters, effectiveTopN),
    enabled: !!(caseId && timelineId && field) && dataKind === "terms",
  });

  const timeseriesQuery = useQuery({
    queryKey: ["viz-field-timeseries", caseId, timelineId, field, filters, effectiveTopN],
    queryFn: () => vizApi.fieldTimeseries(caseId!, timelineId!, field!, filters, 60, effectiveTopN),
    enabled: !!(caseId && timelineId && field) && dataKind === "timeseries",
  });

  const availableChartTypes = chartTypesFor(scale);

  const captionLines = [
    `TraceVector — visualization — case ${caseId} / timeline ${timelineId ?? ""}`,
    field ? `field: ${field} (${scale}) — ${CHART_META[chartType].label}` : undefined,
    filters.q ? `search: ${filters.q}` : undefined,
    filters.start || filters.end ? `range: ${filters.start ?? "…"} to ${filters.end ?? "…"}` : undefined,
  ].filter((l): l is string => !!l);

  const loading =
    (dataKind === "terms" && termsQuery.isLoading) ||
    (dataKind === "numeric" && numericQuery.isLoading) ||
    (dataKind === "timeseries" && timeseriesQuery.isLoading);

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
        </div>

        {/* Field picker */}
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Field
          </label>
          <Select value={field ?? undefined} onValueChange={(v) => setField(v)}>
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
        </div>

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
          <Select value={chartType} onValueChange={(v) => setChartType(v as ChartType)}>
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
              onChange={(e) => setBins(Number(e.target.value))}
              className="w-full accent-[var(--color-accent)]"
            />
          </div>
        )}
        {(dataKind === "terms" || dataKind === "timeseries") && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Top values: {effectiveTopN}
            </label>
            <input
              type="range"
              min={3}
              max={maxTopN}
              step={1}
              value={effectiveTopN}
              onChange={(e) => setTopN(Number(e.target.value))}
              className="w-full accent-[var(--color-accent)]"
            />
          </div>
        )}

        <div className="mt-auto border-t border-[var(--color-border)] pt-3">
          <ExportControls
            svgRef={svgRef}
            filename={`${field ?? "visualization"}_${chartType}`}
            captionLines={captionLines}
          />
        </div>
      </div>

      {/* Canvas */}
      <div className="flex-1 overflow-auto p-4">
        {!field ? (
          <div className="flex h-full items-center justify-center text-sm text-[var(--color-fg-muted)]">
            {fieldsQuery.isLoading ? <Spinner size={20} /> : "Choose a field to visualize."}
          </div>
        ) : loading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner size={24} />
          </div>
        ) : (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
            {chartType === "bar" && termsQuery.data && (
              <BarChart terms={termsQuery.data} svgRef={svgRef} />
            )}
            {chartType === "pie" && termsQuery.data && (
              <PieChart terms={termsQuery.data} svgRef={svgRef} />
            )}
            {chartType === "heatmap" && timeseriesQuery.data && (
              <Heatmap data={timeseriesQuery.data} svgRef={svgRef} />
            )}
            {chartType === "line" && timeseriesQuery.data && (
              <LineChart data={timeseriesQuery.data} svgRef={svgRef} />
            )}
            {chartType === "histogram" && numericQuery.data && (
              <NumericHistogram stats={numericQuery.data} svgRef={svgRef} />
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
          </div>
        )}
      </div>
    </div>
  );
}
