/**
 * finding-normalize — flatten the 11 detector-specific finding shapes into one
 * FeedItem the unified findings feed can render, and interleave the
 * per-detector ranked lists into one cross-detector order.
 *
 * Ranking rationale: detector scores live on incomparable scales (surprise,
 * |z|, G, −log₁₀ p, seconds of skew, band distances). Normalizing them would
 * invent false precision, so the feed interleaves by per-detector rank — every
 * detector's #1 finding first (in registry display order), then every #2, … —
 * and shows the raw score with its per-detector unit label.
 */
import type { DetectorId, DetectorMeta } from "@/components/analysis/detector-registry";
import type { AnomalyFinding } from "@/api/types";
import { anomalyFieldLabel as fieldLabel, shortId, truncate } from "@/lib/format";

export interface FeedItem {
  detectorId: DetectorId;
  detector: string;
  detectorLabel: string;
  icon: React.ElementType;
  /** Main row line, e.g. "artifact = ssh_login". */
  title: string;
  /** Detector-specific meta line. */
  subtitle: string;
  scoreRaw: number;
  scoreUnit: string;
  /** Best timestamp for jump-to-time (event ts, then first_seen). */
  ts: string | null;
  eventId: string | null;
  sourceId: string | null;
  /** Per-detector rank (0-based) — the interleave key. */
  rank: number;
  raw: AnomalyFinding;
}

function pair(field: string, value: string | number): string {
  return `${fieldLabel(field)} = ${truncate(String(value), 60)}`;
}

export function normalizeFinding(meta: DetectorMeta, f: AnomalyFinding, rank: number): FeedItem {
  let title: string;
  let subtitle: string;
  let ts: string | null = f.event?.timestamp ?? null;
  switch (f.type) {
    case "value_novelty":
      title = pair(f.field, f.value);
      subtitle = `×${f.count}${f.first_seen ? ` · first ${f.first_seen}` : ""}`;
      ts = ts ?? f.first_seen;
      break;
    case "value_combo":
      title = f.fields.map((fl, i) => pair(fl, f.values[i] ?? "")).join(" + ");
      subtitle = `×${f.count} combination`;
      ts = ts ?? f.first_seen;
      break;
    case "frequency":
      title = pair(f.series_field, f.series_value);
      subtitle = `${f.observed} observed vs ${f.expected.toFixed(1)} expected at ${f.window_start}`;
      ts = ts ?? f.window_start;
      break;
    case "timestamp_order":
      // Not the whole source id: it is ~60 characters of which the first 24 are
      // identical for every source in the case, so it wrapped over three lines
      // and buried the one number that is the finding.
      title = `Backwards timestamp at line ${f.line_number}`;
      subtitle = `${f.skew_seconds.toFixed(1)}s behind the previous record · source ${shortId(f.source_id)}`;
      ts = ts ?? f.timestamp;
      break;
    case "numeric_range":
      title = pair(f.field, f.value);
      subtitle = `${f.direction} the band [${f.lower}, ${f.upper}] ×${f.count}`;
      ts = ts ?? f.first_seen;
      break;
    case "charset":
      title = pair(f.field, f.value);
      subtitle = `novel characters: ${f.novel_chars.join(" ")}`;
      ts = ts ?? f.first_seen;
      break;
    case "entropy":
      title = pair(f.field, f.value);
      subtitle = `${f.entropy.toFixed(2)} bits, ${f.direction} [${f.lower.toFixed(2)}, ${f.upper.toFixed(2)}]`;
      ts = ts ?? f.first_seen;
      break;
    case "proportion_shift":
      title = pair(f.field, f.value);
      subtitle = `share ${(f.baseline_rate * 100).toFixed(2)}% → ${(f.window_rate * 100).toFixed(2)}% (${f.direction}, q=${f.q_value.toExponential(1)})`;
      ts = ts ?? f.first_seen;
      break;
    case "interval_periodicity":
      title = pair(f.field, f.value);
      subtitle =
        f.direction === "new_regularity"
          ? `new regularity (beaconing), CV ${f.window_cv ?? "—"} (q=${f.q_value.toExponential(1)})`
          : `cadence ${f.direction}, ×${f.count} in window (q=${f.q_value.toExponential(1)})`;
      ts = ts ?? f.first_seen;
      break;
    case "sequence_novelty":
      title = `${fieldLabel(f.field)}: ${truncate(f.value, 70)}`;
      subtitle = `never in baseline · ×${f.count} in ${String(f.details["window_label"] ?? "the suspect window")}`;
      ts = ts ?? f.first_seen;
      break;
    case "value_distribution_drift":
      title = `${fieldLabel(f.field)} distribution drift`;
      subtitle = `${f.test === "ks" ? "KS" : "G-test"} ${f.direction} in ${f.window_label} (q=${f.q_value.toExponential(1)})`;
      ts = ts ?? f.first_seen;
      break;
    case "sequence_motif":
      // Not part of the sweep (mined from Tools -> Explore) — handled for
      // exhaustiveness so the union stays covered if that ever changes.
      title = `${fieldLabel(f.field)}: ${truncate(f.value, 70)}`;
      subtitle = `×${f.support}${f.period_seconds !== null ? ` every ~${f.period_seconds}s` : ""}`;
      ts = ts ?? f.first_seen;
      break;
  }
  return {
    detectorId: meta.id,
    detector: meta.detector,
    detectorLabel: meta.label,
    icon: meta.icon,
    title,
    subtitle,
    scoreRaw: f.score,
    scoreUnit: meta.scoreUnit,
    ts,
    eventId: "event_id" in f ? f.event_id : null,
    sourceId: f.event?.source_id ?? null,
    rank,
    raw: f,
  };
}

/**
 * Interleave per-detector ranked lists: all rank-0 items first (in the order
 * the lists are passed, i.e. registry display order), then all rank-1, …
 */
export function interleaveByRank(lists: FeedItem[][]): FeedItem[] {
  const out: FeedItem[] = [];
  const longest = Math.max(0, ...lists.map((l) => l.length));
  for (let rank = 0; rank < longest; rank++) {
    for (const list of lists) {
      const item = list[rank];
      if (item) out.push(item);
    }
  }
  return out;
}
