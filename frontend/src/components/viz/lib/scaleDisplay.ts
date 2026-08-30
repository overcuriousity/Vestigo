/**
 * "Treat as" — the scale of measurement in the analyst's words.
 *
 * `ChartConfig.scale` keeps the Stevens vocabulary (it is the wire format the
 * agent, the URL and saved charts share); the rail shows these labels and
 * puts the Stevens term in the tooltip for the report reader who wants it.
 */
import type { Scale } from "./chartConfig";

export interface ScaleDisplay {
  label: string;
  /** One plain sentence: what the values are. */
  plain: string;
  /** The Stevens name, shown in the tooltip only. */
  stevens: string;
  examples: string;
}

export const SCALE_DISPLAY: Record<Scale, ScaleDisplay> = {
  nominal: {
    label: "Categories",
    plain: "Names with no order — identity only",
    stevens: "nominal",
    examples: "artifact type, HTTP method, a host name",
  },
  ordinal: {
    label: "Ordered categories",
    plain: "Categories with a rank but no distance between steps",
    stevens: "ordinal",
    examples: "log level (debug < info < warning < error), a size range",
  },
  interval: {
    label: "Number or time",
    plain: "Distances mean something, zero is arbitrary",
    stevens: "interval",
    examples: "a timestamp, a date",
  },
  ratio: {
    label: "Measure",
    plain: "A number with a true zero — differences and ratios both mean something",
    stevens: "ratio",
    examples: "bytes transferred, a duration, a count",
  },
};

export function scaleTooltip(scale: Scale): string {
  const d = SCALE_DISPLAY[scale];
  return `${d.plain} (${d.stevens} scale). e.g. ${d.examples}`;
}

/** The auto-notice under "Treat as" when the field probe pre-selects a scale. */
export function treatAsNotice(fieldLabel: string, scale: Scale, looksNumeric: boolean): string {
  const as = SCALE_DISPLAY[scale].label.toLowerCase();
  return looksNumeric
    ? `${fieldLabel} looks numeric — treating it as a ${as}; change this if its values are categories to you.`
    : `${fieldLabel} has no numeric values — treating it as ${as}.`;
}
