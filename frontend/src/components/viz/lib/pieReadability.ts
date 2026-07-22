/**
 * When a pie stops being an honest chart.
 *
 * Two failure modes from the visualization literature (Cleveland & McGill's
 * accuracy ranking puts angle near the bottom):
 *   1. too many slices — past a handful, angles blur together;
 *   2. slices of nearly equal size — the eye cannot rank them, so the chart
 *      says "these are different" while showing nothing readable.
 *
 * Both are advisory: the chart still renders, with a warning that names the
 * better mark. The same rule runs in `propose_chart`, so an agent proposing a
 * pie gets the identical caution the analyst sees.
 */
import { PIE_COMFORTABLE_MAX } from "./chartMeta";
import type { FieldTermsResponse } from "@/api/types";

/** Relative gap below which two neighbouring slices read as "the same size". */
const INDISTINCT_RELATIVE_GAP = 0.1;

export function pieReadabilityWarning(terms: FieldTermsResponse): string | null {
  const counts = terms.values.map((v) => v.count).filter((c) => c > 0);
  const slices = counts.length + (terms.other_count > 0 ? 1 : 0);
  if (slices > PIE_COMFORTABLE_MAX) {
    return (
      `${slices} slices — past about ${PIE_COMFORTABLE_MAX}, judging angles gets unreliable. ` +
      "A bar chart (length) or waffle (countable cells) reads more accurately."
    );
  }
  const sorted = [...counts].sort((a, b) => b - a);
  for (let i = 0; i + 1 < sorted.length; i++) {
    const gap = (sorted[i] - sorted[i + 1]) / sorted[i];
    if (gap < INDISTINCT_RELATIVE_GAP) {
      return (
        "Two slices differ by less than 10% — that gap is not readable as an angle. " +
        "Use a bar chart to compare them by length."
      );
    }
  }
  return null;
}
