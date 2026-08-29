/**
 * Which figures the gallery lights for a (scale, field) pair, and the one
 * sentence that explains each greyed one. Pure: the rail renders it, the
 * tests read it.
 */
import type { ChartType, Scale } from "./chartConfig";
import { CHART_META, chartTypesFor } from "./chartMeta";
import { chartTypesForField } from "./chartOptions";
import { SCALE_DISPLAY } from "./scaleDisplay";
import { fieldTokenLabel } from "./fieldDisplay";
import { isTimeField } from "./timeFields";

export interface GalleryEntry {
  chartType: ChartType;
  legal: boolean;
  /** Why it is greyed; null when legal. */
  reason: string | null;
}

export function galleryEntries(scale: Scale, field: string | null): GalleryEntry[] {
  const legalForField = new Set(chartTypesForField(scale, field));
  const legalForScale = new Set(chartTypesFor(scale));
  return (Object.keys(CHART_META) as ChartType[]).map((chartType) => {
    if (legalForField.has(chartType)) return { chartType, legal: true, reason: null };
    const label = CHART_META[chartType].label;
    if (legalForScale.has(chartType) && field && isTimeField(field)) {
      return {
        chartType,
        legal: false,
        reason: `${label} can't plot ${fieldTokenLabel(field)} — a time part has no numeric values.`,
      };
    }
    const needs = CHART_META[chartType].scales.map((s) => SCALE_DISPLAY[s].label).join(" or ");
    return { chartType, legal: false, reason: `${label} needs ${needs}.` };
  });
}
