/**
 * Chart-type metadata: which scales of measurement each chart type suits and
 * which aggregation feeds it. Shared by the Visualize page's rail and the
 * task presets.
 */
import type { ChartType, Scale } from "./chartConfig";

export type DataKind = "time" | "terms" | "numeric" | "timeseries";

export const CHART_META: Record<
  ChartType,
  { label: string; scales: Scale[]; dataKind: DataKind; supportsCompare?: boolean }
> = {
  // Event count over time needs no field, so it is meaningful whatever scale
  // the currently-picked field has — available under every scale.
  time: {
    label: "Time histogram (events over time)",
    scales: ["nominal", "ordinal", "interval", "ratio"],
    dataKind: "time",
    supportsCompare: true,
  },
  bar: {
    label: "Bar",
    scales: ["nominal", "ordinal"],
    dataKind: "terms",
    supportsCompare: true,
  },
  // pie/box/violin/ecdf have no honest two-layer encoding, so they're left
  // without supportsCompare — the rail hides Compare for them.
  pie: { label: "Pie / Donut", scales: ["nominal"], dataKind: "terms" },
  heatmap: {
    label: "Heatmap (value × time)",
    scales: ["nominal", "ordinal", "interval"],
    dataKind: "timeseries",
  },
  line: {
    label: "Line / Area (value × time)",
    scales: ["interval", "ratio"],
    dataKind: "timeseries",
  },
  histogram: {
    label: "Histogram",
    scales: ["interval", "ratio"],
    dataKind: "numeric",
    supportsCompare: true,
  },
  box: { label: "Box plot", scales: ["ratio"], dataKind: "numeric" },
  violin: { label: "Violin plot", scales: ["ratio"], dataKind: "numeric" },
  ecdf: { label: "ECDF", scales: ["ratio"], dataKind: "numeric" },
};

export const SCALES: Scale[] = ["nominal", "ordinal", "interval", "ratio"];

export const chartTypesFor = (s: Scale): ChartType[] =>
  (Object.keys(CHART_META) as ChartType[]).filter((c) => CHART_META[c].scales.includes(s));
