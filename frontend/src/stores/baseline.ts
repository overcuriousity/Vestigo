/**
 * Investigation-frame store — the single global scope every statistical
 * detector runs under, plus the histogram's mark-mode state.
 *
 * `frame` is the one explicit choice that governs all detectors (replacing the
 * old per-view self/temporal ModeToggle): `self` scans the whole corpus,
 * `baseline` compares the active saved definition's suspect windows against its
 * baseline window. A tiny shared store (rather than prop-drilling through the
 * panel into seven detector views) so every view reads `frame`/`activeBaselineId`
 * and includes it in its request/queryKey with a one-line change, and the
 * histogram + window editor coordinate mark mode without threading callbacks
 * through the whole explorer tree.
 */
import { create } from "zustand";

/** Global detector scope. `self` = whole corpus; `baseline` = compare windows. */
export type DetectorFrame = "self" | "baseline";

/** A [start, end) range brushed on the histogram, awaiting classification. */
export interface PendingRange {
  start: string;
  end: string;
}

/** Which builder draft row the next histogram brush fills. Lives here (not in
 * the builder component) so the builder drawer can get out of the way while a
 * brush is awaited and survive being hidden without losing the armed row. */
export type ArmedTarget = { kind: "baseline" } | { kind: "suspect"; index: number };

interface BaselineState {
  /** Global scope all detectors run under. */
  frame: DetectorFrame;
  setFrame: (frame: DetectorFrame) => void;
  /** ID of the active baseline definition, used when `frame === "baseline"`. */
  activeBaselineId: string | null;
  setActiveBaselineId: (id: string | null) => void;
  /** Histogram cursor mode: true = mark ranges, false = zoom/select. */
  markMode: boolean;
  setMarkMode: (markMode: boolean) => void;
  /** A range brushed in mark mode, awaiting assignment to a window row. */
  pendingRange: PendingRange | null;
  setPendingRange: (range: PendingRange | null) => void;
  /** The builder row the next brush fills; null = default (baseline row). */
  armed: ArmedTarget | null;
  setArmed: (armed: ArmedTarget | null) => void;
}

export const useBaselineStore = create<BaselineState>((set) => ({
  frame: "self",
  setFrame: (frame) => set({ frame }),
  activeBaselineId: null,
  // Selecting a definition implies the baseline frame; clearing it falls back
  // to self so a stale `baseline` frame never runs against no definition.
  setActiveBaselineId: (id) => set(id ? { activeBaselineId: id, frame: "baseline" } : { activeBaselineId: null, frame: "self" }),
  markMode: false,
  // Entering or leaving mark mode drops any half-finished brush state; leaving
  // also disarms so a hidden builder drawer reappears (see BaselineBuilderDrawer).
  setMarkMode: (markMode) => set({ markMode, pendingRange: null, ...(markMode ? {} : { armed: null }) }),
  pendingRange: null,
  setPendingRange: (range) => set({ pendingRange: range }),
  armed: null,
  setArmed: (armed) => set({ armed }),
}));
