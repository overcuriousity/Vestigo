/**
 * UI preferences store — persisted to localStorage.
 * Handles column config, panel layout toggles, histogram, and sort direction.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Density = "comfortable" | "compact";

interface UiState {
  /** Layout density — comfortable (default) or compact. */
  density: Density;
  setDensity: (density: Density) => void;

  /** Per-timeline column selections, keyed by "caseId/timelineId". */
  visibleColumnsByTimeline: Record<string, string[]>;
  setVisibleColumns: (key: string, cols: string[]) => void;

  /** Whether the Investigate panel (frame + detectors + windows) is open. */
  investigatePanelOpen: boolean;
  setInvestigatePanelOpen: (open: boolean) => void;

  /** Whether the baseline-builder drawer (the big window-editor form) is open. */
  baselineBuilderOpen: boolean;
  setBaselineBuilderOpen: (open: boolean) => void;

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

  /** Width of the investigate panel in pixels. */
  investigatePanelWidth: number;
  setInvestigatePanelWidth: (w: number) => void;

  /** Persisted event grid column widths (px), keyed by column id. */
  columnWidths: Record<string, number>;
  setColumnWidth: (id: string, width: number) => void;

  /**
   * Collapsed guidance panels, keyed by `GuidancePanel` id. Lives here rather
   * than in raw localStorage so a reset re-renders panels that are already
   * mounted — the old implementation read its flag once into `useState`, so
   * clearing storage left every open panel visually unchanged until remount.
   */
  collapsedGuidance: Record<string, boolean>;
  setGuidanceCollapsed: (id: string, collapsed: boolean) => void;
  /** Re-expand every guidance panel. Wired to the Settings control. */
  resetGuidance: () => void;
}

const LEGACY_GUIDANCE_PREFIX = "vestigo-guidance-";

/**
 * Adopt the pre-v5 `vestigo-guidance-<id>` localStorage keys so nobody's
 * dismissals resurface on upgrade, and clear them so the two stores cannot
 * disagree later.
 */
function adoptLegacyGuidanceKeys(): Record<string, boolean> {
  const adopted: Record<string, boolean> = {};
  try {
    for (const key of Object.keys(localStorage)) {
      if (!key.startsWith(LEGACY_GUIDANCE_PREFIX)) continue;
      if (localStorage.getItem(key) === "collapsed") {
        adopted[key.slice(LEGACY_GUIDANCE_PREFIX.length)] = true;
      }
      localStorage.removeItem(key);
    }
  } catch {
    // localStorage unavailable (private mode) — nothing to adopt.
  }
  return adopted;
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
      density: "comfortable",
      setDensity: (density) => set({ density }),

      visibleColumnsByTimeline: {},
      setVisibleColumns: (key, cols) =>
        set((s) => ({
          visibleColumnsByTimeline: { ...s.visibleColumnsByTimeline, [key]: cols },
        })),

      investigatePanelOpen: false,
      setInvestigatePanelOpen: (open) => set({ investigatePanelOpen: open }),

      baselineBuilderOpen: false,
      setBaselineBuilderOpen: (open) => set({ baselineBuilderOpen: open }),

      filterRailOpen: true,
      setFilterRailOpen: (open) => set({ filterRailOpen: open }),

      histogramOpen: true,
      setHistogramOpen: (open) => set({ histogramOpen: open }),

      sortDir: "desc",
      setSortDir: (dir) => set({ sortDir: dir }),

      detailPanelWidth: 420,
      setDetailPanelWidth: (w) => set({ detailPanelWidth: w }),

      investigatePanelWidth: 400,
      setInvestigatePanelWidth: (w) => set({ investigatePanelWidth: w }),

      columnWidths: {},
      setColumnWidth: (id, width) =>
        set((s) => ({ columnWidths: { ...s.columnWidths, [id]: width } })),

      collapsedGuidance: {},
      setGuidanceCollapsed: (id, collapsed) =>
        set((s) => ({ collapsedGuidance: { ...s.collapsedGuidance, [id]: collapsed } })),
      resetGuidance: () => set({ collapsedGuidance: {} }),
    }),
    {
      name: "vestigo-ui",
      version: 5,
      migrate: (persistedState, version) => {
        const state = persistedState as UiState;
        if (version < 1) {
          const migrated: Record<string, string[]> = {};
          for (const [key, cols] of Object.entries(state.visibleColumnsByTimeline || {})) {
            migrated[key] = migrateColumns(cols);
          }
          state.visibleColumnsByTimeline = migrated;
        }
        if (version < 2) {
          state.columnWidths = state.columnWidths ?? {};
        }
        if (version < 3) {
          state.density = state.density ?? "comfortable";
        }
        if (version < 4) {
          // Renamed analysisPanelWidth → investigatePanelWidth; carry the
          // persisted width forward so a saved drag survives the rename.
          const legacy = (state as unknown as { analysisPanelWidth?: number })
            .analysisPanelWidth;
          state.investigatePanelWidth = legacy ?? state.investigatePanelWidth ?? 400;
          delete (state as unknown as { analysisPanelWidth?: number }).analysisPanelWidth;
        }
        if (version < 5) {
          // Guidance dismissal moved out of its own `vestigo-guidance-*` keys.
          // Only reached when this store has persisted state to migrate; a
          // browser that dismissed guidance without ever writing a UI
          // preference just sees the panels once more, which is the safe
          // direction to fail in.
          state.collapsedGuidance = {
            ...adoptLegacyGuidanceKeys(),
            ...(state.collapsedGuidance ?? {}),
          };
        }
        return state;
      },
    },
  ),
);
