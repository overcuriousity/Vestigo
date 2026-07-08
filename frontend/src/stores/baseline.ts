/**
 * Active-baseline store — which saved baseline definition (baseline range +
 * suspect windows) the temporal anomaly detectors currently run against, plus
 * the histogram's mark-mode state.
 *
 * A tiny shared store (rather than prop-drilling through AnalysisPanel into
 * seven detector views) so every detector view can read the active
 * `baselineId` and include it in its request/queryKey with a one-line change,
 * and the histogram + BaselineManager can coordinate mark mode without
 * threading callbacks through the whole explorer tree.
 */
import { create } from "zustand";

/** A [start, end) range brushed on the histogram, awaiting classification. */
export interface PendingRange {
  start: string;
  end: string;
}

interface BaselineState {
  /** ID of the active baseline definition, or null for legacy/self modes. */
  activeBaselineId: string | null;
  setActiveBaselineId: (id: string | null) => void;
  /** Histogram cursor mode: true = mark ranges, false = zoom/select. */
  markMode: boolean;
  setMarkMode: (markMode: boolean) => void;
  /** A range brushed in mark mode, awaiting "set as baseline / add suspect". */
  pendingRange: PendingRange | null;
  setPendingRange: (range: PendingRange | null) => void;
}

export const useBaselineStore = create<BaselineState>((set) => ({
  activeBaselineId: null,
  setActiveBaselineId: (id) => set({ activeBaselineId: id }),
  markMode: false,
  setMarkMode: (markMode) => set({ markMode, pendingRange: markMode ? null : null }),
  pendingRange: null,
  setPendingRange: (range) => set({ pendingRange: range }),
}));
