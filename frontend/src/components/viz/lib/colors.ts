/**
 * Chart color assignment — categorical identity + sequential magnitude.
 *
 * The 8-slot categorical palette (`--viz-series-1..8` in index.css) is
 * CVD-validated (see the dataviz skill's `validate_palette.js`) and its
 * *order* is the safety mechanism: colors are assigned by index in this
 * fixed order and never reassigned when a filter changes which series are
 * present, so a value's color stays stable across interactions. A 9th+
 * series folds into "Other" rather than generating a new hue.
 */

export const CATEGORICAL_SLOTS = 8;

/** Resolve categorical slot `index` (0-based, wraps past 8) to its CSS var. */
export function seriesColorVar(index: number): string {
  const slot = (index % CATEGORICAL_SLOTS) + 1;
  return `var(--viz-series-${slot})`;
}

/** Fixed color for values outside the top-N ("Other" bucket). */
export const OTHER_COLOR = "var(--color-fg-disabled)";
export const OTHER_LABEL = "Other";

/**
 * Assign a stable categorical color to each value, in the order given —
 * so the same field's values keep the same color across chart types
 * (bar/pie/line/heatmap all built from the same `field_terms`-ordered
 * value list). `OTHER_LABEL` always gets the fixed neutral color.
 */
export function buildSeriesColorMap(values: string[]): Map<string, string> {
  const map = new Map<string, string>();
  let idx = 0;
  for (const v of values) {
    if (v === OTHER_LABEL) {
      map.set(v, OTHER_COLOR);
      continue;
    }
    map.set(v, seriesColorVar(idx));
    idx++;
  }
  return map;
}

/** Sequential ramp steps (light -> dark), for numeric magnitude encoding. */
const SEQUENTIAL_STEPS = [
  "var(--viz-sequential-100)",
  "var(--viz-sequential-300)",
  "var(--viz-sequential-500)",
  "var(--viz-sequential-700)",
] as const;

/** Map t in [0, 1] to a step of the sequential ramp (nearest, clamped). */
export function sequentialColor(t: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0));
  const idx = Math.min(
    SEQUENTIAL_STEPS.length - 1,
    Math.floor(clamped * SEQUENTIAL_STEPS.length),
  );
  return SEQUENTIAL_STEPS[idx];
}
