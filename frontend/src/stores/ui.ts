/**
 * UI preferences store — persisted to localStorage.
 * Handles column config, panel layout toggles, histogram, and sort direction.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface UiState {
  /** Per-timeline column selections, keyed by "caseId/timelineId". */
  visibleColumnsByTimeline: Record<string, string[]>;
  setVisibleColumns: (key: string, cols: string[]) => void;

  /** Whether the analysis panel is open. */
  analysisPanelOpen: boolean;
  setAnalysisPanelOpen: (open: boolean) => void;

  /** Whether the filter rail is collapsed on mobile. */
  filterRailOpen: boolean;
  setFilterRailOpen: (open: boolean) => void;

  /** Whether the time histogram is shown above the event grid. */
  histogramOpen: boolean;
  setHistogramOpen: (open: boolean) => void;

  /** Chronological sort direction for the event grid. */
  sortDir: "asc" | "desc";
  setSortDir: (dir: "asc" | "desc") => void;

  /** Width of the event detail panel in pixels. */
  detailPanelWidth: number;
  setDetailPanelWidth: (w: number) => void;
}

export const DEFAULT_COLUMNS = [
  "timestamp",
  "artifact",
  "message",
];

export const RETIRED_COLUMN_IDS: Record<string, string> = {
  source: "artifact",
  source_long: "artifact_long",
};

const KNOWN_COLUMN_IDS = new Set([
  ...DEFAULT_COLUMNS,
  "source_id",
  "artifact_long",
  "timestamp_desc",
  "display_name",
  "tags",
  "_annotations",
]);

function migrateColumns(cols: string[] | undefined): string[] {
  if (!Array.isArray(cols)) return [...DEFAULT_COLUMNS];
  const mapped = cols
    .map((id) => RETIRED_COLUMN_IDS[id] || id)
    .filter((id) => KNOWN_COLUMN_IDS.has(id) || !id.startsWith("_"));
  const deduped = [...new Set(mapped)];
  return deduped.length > 0 ? deduped : [...DEFAULT_COLUMNS];
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      visibleColumnsByTimeline: {},
      setVisibleColumns: (key, cols) =>
        set((s) => ({
          visibleColumnsByTimeline: { ...s.visibleColumnsByTimeline, [key]: cols },
        })),

      analysisPanelOpen: false,
      setAnalysisPanelOpen: (open) => set({ analysisPanelOpen: open }),

      filterRailOpen: true,
      setFilterRailOpen: (open) => set({ filterRailOpen: open }),

      histogramOpen: true,
      setHistogramOpen: (open) => set({ histogramOpen: open }),

      sortDir: "desc",
      setSortDir: (dir) => set({ sortDir: dir }),

      detailPanelWidth: 384,
      setDetailPanelWidth: (w) => set({ detailPanelWidth: w }),
    }),
    {
      name: "tv-ui",
      version: 1,
      migrate: (persistedState, version) => {
        const state = persistedState as UiState;
        if (version < 1) {
          const migrated: Record<string, string[]> = {};
          for (const [key, cols] of Object.entries(state.visibleColumnsByTimeline || {})) {
            migrated[key] = migrateColumns(cols);
          }
          state.visibleColumnsByTimeline = migrated;
        }
        return state;
      },
    },
  ),
);
