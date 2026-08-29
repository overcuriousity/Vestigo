/**
 * When a *vertical* bar chart stops being an honest chart.
 *
 * The two orientations degrade differently, which is why this rule reads the
 * orientation rather than the count alone. Horizontal bars grow the frame with
 * the value count — one row of fixed height each — so the 500 `TOPN_MAX`
 * allows for a bar axis stays legible inside its scroll container. Vertical
 * bars share one fixed height and one fixed width, so every extra category
 * makes each band narrower: past a few dozen the bars are a pixel or two wide
 * and the rotated axis labels draw on top of each other, leaving a texture
 * that no longer reports values.
 *
 * Advisory only, exactly like `pieReadability.ts`: the chart still renders,
 * with a warning that names the cheaper fix (switch orientation) before the
 * lossy one (ask for fewer values).
 */

/** Bands past which a fixed-height vertical bar chart stops being readable. */
export const VERTICAL_BARS_COMFORTABLE_MAX = 40;

export function barReadabilityWarning(
  bands: number,
  orientation: "horizontal" | "vertical",
): string | null {
  if (orientation !== "vertical" || bands <= VERTICAL_BARS_COMFORTABLE_MAX) return null;
  return (
    `${bands} bars in a fixed-height vertical chart — past about ${VERTICAL_BARS_COMFORTABLE_MAX} ` +
    "the bars are a pixel or two wide and the labels overdraw. Switch to horizontal, which grows " +
    "with the value count, or lower Top values."
  );
}
