/**
 * detector-registry — the single client-side list of statistical detectors:
 * id, API detector key, icon, label, hint, category (for the grouped
 * "Advanced" accordion) and the unit its raw score is expressed in (shown in
 * the unified findings feed — scores are NOT comparable across detectors, so
 * the feed interleaves by per-detector rank and labels each raw score).
 *
 * The sequence_motif miner is deliberately absent: it is discovery, not
 * anomaly detection, and lives in the Tools sheet's Explore section
 * (PatternsView).
 */
import {
  Activity,
  Hash,
  Layers,
  ListOrdered,
  Percent,
  Replace,
  Rewind,
  Ruler,
  Shuffle,
  Timer,
  Type,
} from "lucide-react";
import type { AnomalyParams } from "@/api/anomalies";

export type DetectorId =
  | "novelty"
  | "combo"
  | "frequency"
  | "shift"
  | "interval"
  | "drift"
  | "sequence"
  | "order"
  | "range"
  | "charset"
  | "entropy";

export type DetectorCategory = "values" | "volume" | "sequences";

export interface DetectorMeta {
  id: DetectorId;
  detector: NonNullable<AnomalyParams["detector"]>;
  icon: React.ElementType;
  label: string;
  hint: string;
  category: DetectorCategory;
  /** Unit label for the raw score in the unified feed (units differ per detector). */
  scoreUnit: string;
}

export const DETECTOR_CATEGORIES: { id: DetectorCategory; label: string }[] = [
  { id: "values", label: "Values" },
  { id: "volume", label: "Volume & timing" },
  { id: "sequences", label: "Sequences" },
];

export const DETECTORS: DetectorMeta[] = [
  { id: "novelty", detector: "value_novelty", icon: Hash, label: "Rare values", hint: "Rare or first-seen field values", category: "values", scoreUnit: "surprise" },
  { id: "combo", detector: "value_combo", icon: Layers, label: "Value combos", hint: "Rare combinations of fields", category: "values", scoreUnit: "surprise" },
  { id: "range", detector: "numeric_range", icon: Ruler, label: "Numeric range", hint: "Values outside a learned band", category: "values", scoreUnit: "× band" },
  { id: "charset", detector: "charset", icon: Type, label: "Charset novelty", hint: "Never-seen characters", category: "values", scoreUnit: "surprise" },
  { id: "entropy", detector: "entropy", icon: Shuffle, label: "Entropy outliers", hint: "Random or degenerate strings", category: "values", scoreUnit: "× band" },
  { id: "frequency", detector: "frequency", icon: Activity, label: "Frequency", hint: "Count spikes and silences", category: "volume", scoreUnit: "|z|" },
  { id: "shift", detector: "proportion_shift", icon: Percent, label: "Proportion shift", hint: "Value shares that change between windows", category: "volume", scoreUnit: "G" },
  { id: "interval", detector: "interval_periodicity", icon: Timer, label: "Interval cadence", hint: "Broken heartbeats and new beaconing", category: "volume", scoreUnit: "−log₁₀ p" },
  { id: "drift", detector: "value_distribution_drift", icon: Replace, label: "Distribution drift", hint: "Whole-field value-mix changes between windows", category: "volume", scoreUnit: "−log₁₀ p" },
  { id: "order", detector: "timestamp_order", icon: Rewind, label: "Timestamp order", hint: "Timestamps running backwards", category: "volume", scoreUnit: "s skew" },
  { id: "sequence", detector: "sequence_novelty", icon: ListOrdered, label: "Event sequences", hint: "Never-seen event orderings (n-grams)", category: "sequences", scoreUnit: "surprise" },
];

export const DETECTORS_BY_ID = Object.fromEntries(DETECTORS.map((d) => [d.id, d])) as Record<
  DetectorId,
  DetectorMeta
>;
