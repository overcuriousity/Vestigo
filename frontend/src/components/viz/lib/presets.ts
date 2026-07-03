/**
 * Task presets — forensic questions mapped to prefilled chart configs. A
 * preset is just a `ChartConfig` template (the current field is kept where
 * one is needed); everything stays editable after picking one.
 */
import type { ChartConfig } from "./chartConfig";

export interface ChartPreset {
  id: string;
  label: string;
  /** The forensic question this preset answers — shown as guidance. */
  question: string;
  /** Applied over the current config; `field` is preserved unless set here. */
  config: Partial<Omit<ChartConfig, "v" | "field">>;
}

export const CHART_PRESETS: ChartPreset[] = [
  {
    id: "subset-vs-all",
    label: "Compare a subset against everything",
    question:
      "Does my filtered subset (e.g. one source IP) dominate the total traffic anywhere — a DoS burst, an exfil window?",
    config: {
      chartType: "time",
      metric: "ratio",
      compare: { mode: "baseline" },
      options: {},
    },
  },
  {
    id: "events-over-time",
    label: "Events over time",
    question: "When did activity spike or go quiet under the current filters?",
    config: {
      chartType: "time",
      metric: "count",
      compare: { mode: "off" },
      options: {},
    },
  },
  {
    id: "top-values",
    label: "Top values",
    question: "Which values of this field occur most — top talkers, noisiest artifacts?",
    config: {
      chartType: "bar",
      scale: "nominal",
      metric: "count",
      compare: { mode: "off" },
      options: { orientation: "vertical", sort: "count" },
    },
  },
  {
    id: "values-over-time",
    label: "Values over time",
    question: "How do this field's top values shift over the investigation window?",
    config: {
      chartType: "line",
      scale: "interval",
      metric: "count",
      compare: { mode: "off" },
      options: {},
    },
  },
  {
    id: "numeric-distribution",
    label: "Distribution of a numeric field",
    question: "How are magnitudes distributed — bytes, durations, ports; any heavy tail?",
    config: {
      chartType: "histogram",
      scale: "ratio",
      metric: "count",
      compare: { mode: "off" },
      options: {},
    },
  },
];
