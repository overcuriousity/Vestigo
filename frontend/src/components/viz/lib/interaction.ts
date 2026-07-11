/**
 * Chart → page interaction contract for click-to-filter. A chart mark
 * (bar, slice, cell, ribbon, legend entry) reports the field=value pair(s)
 * it represents plus the click's viewport position; the Visualize page
 * decides what to offer (filter in / filter out / open in Explorer) via
 * `ChartActionPopover`. Charts never mutate filters themselves.
 */

export interface ChartValueClick {
  /** The `[fieldToken, value]` pairs the clicked mark represents — one for
   * single-field charts, two for pivot/sankey marks (a conjunction). */
  entries: [string, string][];
  /** Viewport coordinates of the click, for anchoring the action popover. */
  clientX: number;
  clientY: number;
}

export type ChartValueClickHandler = (click: ChartValueClick) => void;
