/**
 * Deterministic jitter for strip overlays.
 *
 * `Math.random` would re-roll on every render, so a chart would look
 * different each time it repainted and an SVG/PNG export would not match
 * what the analyst clicked export on. Hashing the point index instead keeps
 * the strip stable and reproducible, which is what a forensic export needs.
 */

/** Pseudo-random offset in [-1, 1], stable for a given index. */
export function jitterOffset(index: number): number {
  const x = Math.sin(index * 12.9898) * 43758.5453;
  return (x - Math.floor(x)) * 2 - 1;
}
