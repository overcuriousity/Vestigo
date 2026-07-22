/**
 * Waffle-cell allocation.
 *
 * A share chart must not silently drop a category: rounding 0.4% to zero
 * cells would render an existing value invisible. So every non-empty value
 * gets one cell up front and the remaining cells are handed out by largest
 * remainder, which also guarantees the grid sums to exactly 100.
 */

const GRID = 10;
export const WAFFLE_CELLS = GRID * GRID;

export interface WaffleRow {
  key: string;
  label: string;
  count: number;
  cells: number;
}

export function allocateWaffleCells(
  rows: { key: string; label: string; count: number }[],
): WaffleRow[] {
  const positive = rows.filter((r) => r.count > 0);
  const total = positive.reduce((s, r) => s + r.count, 0);
  if (total === 0 || positive.length === 0) return [];
  const reserved = Math.min(positive.length, WAFFLE_CELLS);
  const remaining = WAFFLE_CELLS - reserved;
  const exact = positive.map((r) => (r.count / total) * remaining);
  const floors = exact.map(Math.floor);
  let left = remaining - floors.reduce((a, b) => a + b, 0);
  const order = exact
    .map((v, i) => ({ i, frac: v - Math.floor(v) }))
    .sort((a, b) => b.frac - a.frac);
  const extra: number[] = new Array(positive.length).fill(0);
  for (const { i } of order) {
    if (left <= 0) break;
    extra[i] += 1;
    left -= 1;
  }
  return positive.map((r, i) => ({ ...r, cells: 1 + floors[i] + extra[i] }));
}
