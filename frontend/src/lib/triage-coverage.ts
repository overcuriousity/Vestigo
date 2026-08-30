/**
 * triage-coverage — pure functions computing per-detector triage coverage
 * (findings reviewed vs. outstanding) from a detector-sweep response plus the
 * timeline's disposition rows.
 *
 * Semantics (deliberate, forensically honest):
 * - Denominator = current finding population: `total_findings` (post-normal-
 *   suppression, pre-limit) plus `dismissed_count`. `normal` verdicts are NOT
 *   in it — they extend the baseline, so the covered values no longer exist as
 *   findings; their count is disclosed separately via `verdictsByKind`.
 * - Numerator = dismissed findings plus fetched findings covered by a
 *   confirmed/routine disposition.
 * - Truncation: the sweep fetches a capped slice (limit 50). When the server
 *   reports more findings than fetched, the unfetched tail can't be checked
 *   for coverage and `dismissed_count` itself is slice-scoped — the numerator
 *   is then a lower bound (`truncated: true`; render "≥X / Y", never a
 *   percentage).
 */
import type {
  AnomaliesResponse,
  AnomalyFinding,
  Disposition,
  DispositionKind,
  DispositionStatsResponse,
  FieldTimeseriesResponse,
} from "@/api/types";
import type { DetectorId, DetectorMeta } from "@/components/analysis/detector-registry";

/**
 * True when disposition row `d` covers finding `f` under `detectorKey` (the
 * API detector key, e.g. "value_novelty"). Mirrors — but does not replace —
 * `matchesTarget` in hooks/useDisposition.ts, which matches a just-declared
 * `DispositionTarget` for optimistic cache filtering; this one matches
 * persisted `Disposition` rows.
 */
export function dispositionCoversFinding(
  f: AnomalyFinding,
  d: Disposition,
  detectorKey: string,
): boolean {
  const detectorOk =
    d.detector === "*" ||
    d.detector === detectorKey ||
    // Routine verdicts are declared on sequence_motif (Tools -> Explore) but
    // cover the identical (series_field, " → "-joined n-gram) key that
    // sequence_novelty findings surface — exact key equality, no containment.
    (d.detector === "sequence_motif" && d.kind === "routine" && detectorKey === "sequence_novelty");
  if (!detectorOk) return false;
  if (d.field !== null && d.value !== null) {
    const det = (f.details ?? {}) as Record<string, unknown>;
    return det.allowlist_field === d.field && det.allowlist_value === d.value;
  }
  return d.event_id !== null && f.event_id === d.event_id;
}

export interface DetectorCoverage {
  /** Findings actually fetched (the severity-ranked slice). */
  fetched: number;
  /** Post-suppression finding count before the limit cap. */
  totalFindings: number;
  /** Findings hidden by `dismissed` dispositions (slice-scoped server count). */
  dismissed: number;
  /** Fetched findings covered by a confirmed/routine disposition. */
  coveredVisible: number;
  /** dismissed + coveredVisible — a lower bound when `truncated`. */
  reviewed: number;
  /** totalFindings + dismissed — the current finding population. */
  denominator: number;
  /** True when the server reported more findings than were fetched. */
  truncated: boolean;
  /** Disposition rows applicable to this detector, grouped by kind (exact —
   * server rows, not slice-derived). `"*"` rows count under every detector. */
  verdictsByKind: Record<DispositionKind, number>;
}

const KINDS: DispositionKind[] = ["normal", "dismissed", "confirmed", "routine"];

/** Coverage for one detector; null when the sweep errored or found no data. */
export function computeDetectorCoverage(
  meta: DetectorMeta,
  response: AnomaliesResponse | null | undefined,
  dispositions: Disposition[],
): DetectorCoverage | null {
  if (!response || response.status !== "ok") return null;

  const applicable = dispositions.filter(
    (d) =>
      d.detector === "*" ||
      d.detector === meta.detector ||
      (d.detector === "sequence_motif" && d.kind === "routine" && meta.detector === "sequence_novelty"),
  );
  const verdictsByKind = Object.fromEntries(KINDS.map((k) => [k, 0])) as Record<
    DispositionKind,
    number
  >;
  for (const d of applicable) verdictsByKind[d.kind] += 1;

  const fetched = response.results.length;
  const totalFindings = response.total_findings ?? fetched;
  const dismissed = response.dismissed_count ?? 0;
  // Dismissed rows revealed via include_dismissed are already in the server's
  // dismissed_count — don't double count them as covered.
  const coveredVisible = response.results.filter(
    (f) => !f.dismissed && applicable.some((d) => dispositionCoversFinding(f, d, meta.detector)),
  ).length;

  return {
    fetched,
    totalFindings,
    dismissed,
    coveredVisible,
    reviewed: dismissed + coveredVisible,
    denominator: totalFindings + dismissed,
    truncated: totalFindings > fetched,
    verdictsByKind,
  };
}

const DAY_MS = 86_400_000;

/**
 * Shape the disposition stats into the LineChart's timeseries contract:
 * one series per verdict kind, cumulative counts, zero-filled across the
 * full day range (the API emits only days with activity). Day buckets start
 * at UTC midnight.
 */
export function dispositionStatsToTimeseries(
  stats: DispositionStatsResponse,
): FieldTimeseriesResponse {
  const kinds: DispositionKind[] = ["normal", "dismissed", "confirmed", "routine"];
  if (stats.days.length === 0) {
    return {
      field: "verdict",
      interval_seconds: 86_400,
      min: null,
      max: null,
      series: [],
      // Four fixed verdict kinds, all of them drawn: nothing is ever cut here.
      distinct: 0,
      other_count: 0,
      series_truncated: false,
    };
  }
  const first = Date.parse(`${stats.days[0].date}T00:00:00Z`);
  const last = Date.parse(`${stats.days[stats.days.length - 1].date}T00:00:00Z`);
  const byDate = new Map(stats.days.map((d) => [d.date, d]));

  const buckets: string[] = [];
  for (let t = first; t <= last; t += DAY_MS) {
    buckets.push(new Date(t).toISOString());
  }

  const series = kinds.map((kind) => {
    let running = 0;
    return {
      value: kind,
      buckets: buckets.map((start) => {
        const day = byDate.get(start.slice(0, 10));
        // Cumulative: gap days carry the previous total forward.
        if (day) running = day.cumulative[kind];
        return { start, count: running };
      }),
    };
  });

  return {
    field: "verdict",
    interval_seconds: 86_400,
    min: buckets[0],
    max: buckets[buckets.length - 1],
    series,
    distinct: series.length,
    other_count: 0,
    series_truncated: false,
  };
}

/** Roll-up across detectors for the feed-level summary line. */
export function summarizeCoverage(
  byId: Partial<Record<DetectorId, DetectorCoverage | null>>,
): { reviewed: number; denominator: number; anyTruncated: boolean } {
  let reviewed = 0;
  let denominator = 0;
  let anyTruncated = false;
  for (const cov of Object.values(byId)) {
    if (!cov) continue;
    reviewed += cov.reviewed;
    denominator += cov.denominator;
    anyTruncated ||= cov.truncated;
  }
  return { reviewed, denominator, anyTruncated };
}
