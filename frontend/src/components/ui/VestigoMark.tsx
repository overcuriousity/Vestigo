/**
 * The Vestigo brand mark: a regular track of steps, one band out of cadence.
 *
 * The cadence bands carry `--color-accent` and the offset band carries
 * `--color-anomaly` — the same two colors the analysis views use for "expected"
 * and "this is the one that stands out", so the mark states the product's thesis
 * in the app's own vocabulary and follows the active theme.
 */
export function VestigoMark({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true">
      <polygon fill="var(--color-accent)" points="2,4 26,4 22,11 2,11" />
      <polygon fill="var(--color-accent)" points="8,15 32,15 28,22 8,22" />
      <polygon fill="var(--color-anomaly)" points="22,26 46,26 42,33 22,33" />
      <polygon fill="var(--color-accent)" points="20,37 44,37 40,44 20,44" />
    </svg>
  );
}
