/**
 * finding-verdict — one plain-language sentence per finding, with the number
 * that carries it pulled out for emphasis.
 *
 * The rail row states *what* was found in the detector's own vocabulary
 * ("share 0.10% → 4.20% (increase, q=1.2e-4)"). That is the right density for a
 * list, and the wrong one for the surface an analyst opens to decide whether a
 * finding is real. This module says the same thing as a claim: subject, the
 * evidence, and what makes it notable.
 *
 * Structural rules, both load-bearing:
 *
 * - **Every number comes from the finding.** Nothing here estimates, rounds to
 *   a rhetorically convenient figure, or describes data the payload does not
 *   contain. A verdict sentence is the most quotable thing on the screen and
 *   ends up in reports.
 * - **The switch is exhaustive over the union**, mirroring `finding-normalize`
 *   so the two read side by side. A new finding shape fails the type check here
 *   rather than silently rendering an empty claim.
 */
import type { AnomalyFinding } from "@/api/types";
import type { MethodResult } from "@/api/analysis";
import { isTemplateRow } from "@/api/analysis";
import {
  detailNumber,
  detailString,
  findingMode,
  restFrameLabel,
} from "@/lib/finding-frame";
import { anomalyFieldLabel as fieldLabel, shortId, truncate } from "@/lib/format";

export interface Verdict {
  /** Text before the emphasized span. */
  lead: string;
  /** The number or value the claim rests on. */
  highlight: string;
  /** Text after it. Ends the sentence. */
  tail: string;
}

const pct = (rate: number) => `${(rate * 100).toFixed(2)}%`;

function scoredVerdict(f: AnomalyFinding): Verdict {
  switch (f.type) {
    case "value_novelty":
      return {
        lead: `This value of ${fieldLabel(f.field)} appears in`,
        highlight: `${f.count} event${f.count === 1 ? "" : "s"}`,
        tail: `— a −log frequency of ${f.score.toFixed(2)}, among the rarest in the field.`,
      };
    case "value_combo":
      return {
        lead: "Each of these values is ordinary on its own. The combination occurs in",
        highlight: `${f.count} event${f.count === 1 ? "" : "s"}`,
        tail: "across the scanned corpus.",
      };
    case "frequency":
      return {
        lead: `One bucket of ${fieldLabel(f.series_field)} = ${truncate(String(f.series_value), 40)} holds`,
        highlight: `${f.observed} events`,
        tail: `against ${f.expected.toFixed(1)} expected — ${Math.abs(f.z_score).toFixed(1)} standard deviations from this series' own mean.`,
      };
    case "timestamp_order":
      return {
        lead: "This record's timestamp runs",
        highlight: `${f.skew_seconds.toFixed(1)}s backwards`,
        tail: `from the record before it, at line ${f.line_number} of source ${shortId(f.source_id)} — a clock-integrity problem in the evidence, not in the behavior it records.`,
      };
    case "numeric_range":
      return {
        lead: `${fieldLabel(f.field)} = ${f.value} sits`,
        highlight: `${f.direction} the learned band`,
        tail: `[${f.lower}, ${f.upper}], by ${f.score.toFixed(2)} band widths.`,
      };
    case "charset":
      return {
        lead: `This field's learned alphabet has never contained ${f.novel_chars.length === 1 ? "this character" : "these characters"}:`,
        highlight: f.novel_chars.join(" "),
        tail: `— present here, and in ${f.count} event${f.count === 1 ? "" : "s"} overall.`,
      };
    case "entropy":
      return {
        lead: "Character entropy of this value is",
        highlight: `${f.entropy.toFixed(2)} bits`,
        tail: `, ${f.direction} the band [${f.lower.toFixed(2)}, ${f.upper.toFixed(2)}] learned for ${fieldLabel(f.field)}.`,
      };
    case "proportion_shift":
      if (findingMode(f) === "self-g-test") {
        return {
          lead: `This value's share of ${fieldLabel(f.field)} is`,
          highlight: `${pct(f.window_rate)} in ${detailString(f.details, "window_label") ?? "this slice"}`,
          tail: `against ${pct(f.baseline_rate)} across ${restFrameLabel(f.details)} (${f.direction}, ${f.rate_ratio.toFixed(1)}×, q=${f.q_value.toExponential(1)}).`,
        };
      }
      return {
        lead: `This value's share of ${fieldLabel(f.field)} went from`,
        highlight: `${pct(f.baseline_rate)} to ${pct(f.window_rate)}`,
        tail: `between the baseline and suspect windows (${f.direction}, ${f.rate_ratio.toFixed(1)}×, q=${f.q_value.toExponential(1)}).`,
      };
    case "interval_periodicity":
      if (findingMode(f) === "self-cadence") {
        const median = detailNumber(f.details, "median_interval");
        const medianText = median === null ? "" : `, median gap ${median.toFixed(1)}s`;
        if (f.direction === "new_regularity") {
          const paused = detailNumber(f.details, "paused_intervals") ?? 0;
          return {
            lead: "Arrivals for this value are",
            highlight: "more regular than chance allows",
            tail: `— ${f.count} occurrences across the timeline${medianText}, coefficient of variation ${f.window_cv?.toFixed(2) ?? "—"} over the retained gaps (${paused} pause${paused === 1 ? "" : "s"} excluded; q=${f.q_value.toExponential(1)}).`,
          };
        }
        const longest = detailNumber(f.details, "longest_gap_seconds");
        const missed = detailNumber(f.details, "expected_arrivals_missed");
        const trailing = f.details["trailing"] === true;
        return {
          lead: "This value's arrivals",
          highlight: longest === null ? "fall silent" : `fall silent for ${longest.toFixed(0)}s`,
          tail: `${trailing ? "while its source keeps logging" : "in the middle of its run"}${medianText}${missed === null ? "" : `, about ${missed.toFixed(0)} arrivals missed`} (q=${f.q_value.toExponential(1)}).`,
        };
      }
      return f.direction === "new_regularity"
        ? {
            lead: "Arrivals for this value became",
            highlight: "more regular than chance allows",
            tail: `— ${f.count} occurrences in the suspect window, coefficient of variation ${f.window_cv?.toFixed(2) ?? "—"} (q=${f.q_value.toExponential(1)}).`,
          }
        : {
            lead: "This value's arrival cadence",
            highlight: f.direction === "missed" ? "stopped or thinned out" : "accelerated",
            tail: `— ${f.baseline_count} occurrences in the baseline window against ${f.count} in the suspect one (q=${f.q_value.toExponential(1)}).`,
          };
    case "sequence_novelty":
      if (findingMode(f) === "rare-ngram") {
        const total = detailNumber(f.details, "scope_ngram_total");
        const floor = detailNumber(f.details, "rarity_floor");
        return {
          lead: `This ordering of ${fieldLabel(f.field)} values occurs only`,
          highlight: `${f.count} time${f.count === 1 ? "" : "s"}`,
          tail: `among ${total?.toLocaleString() ?? "the"} sequences across the timeline${floor === null ? "" : ` (rarity floor ${floor})`}.`,
        };
      }
      return {
        lead: `This ordering of ${fieldLabel(f.field)} values`,
        highlight: "never occurs in the baseline",
        tail: `, and occurs ${f.count} time${f.count === 1 ? "" : "s"} in the suspect window.`,
      };
    case "value_distribution_drift":
      if (findingMode(f) === "self-drift") {
        return {
          lead: `The whole value mix of ${fieldLabel(f.field)} in ${f.window_label}`,
          highlight: `differs from the rest of the timeline (${f.direction})`,
          tail: `— ${f.test === "ks" ? "Kolmogorov–Smirnov" : "G-test"} over ${f.window_n} slice and ${f.baseline_n} other events, q=${f.q_value.toExponential(1)}.`,
        };
      }
      return {
        lead: `The whole value mix of ${fieldLabel(f.field)}`,
        highlight: `differs between the two windows (${f.direction})`,
        tail: `— ${f.test === "ks" ? "Kolmogorov–Smirnov" : "G-test"} over ${f.baseline_n} baseline and ${f.window_n} suspect events, q=${f.q_value.toExponential(1)}.`,
      };
    case "sequence_motif":
      // Mined, not detected — it answers "what is routine here?". Kept for
      // exhaustiveness; the sweep never routes one of these to the sheet.
      return {
        lead: "This ordering recurs",
        highlight: `${f.support} times`,
        tail: `across ${f.sources_count} source${f.sources_count === 1 ? "" : "s"} — a routine pattern, not a finding.`,
      };
    case "transition_time": {
      const stream = f.partition_value
        ? `${fieldLabel(f.partition_field ?? "")} = ${truncate(f.partition_value, 40)} moved `
        : "A stream moved ";
      const floor =
        f.reference_kind === "next-fastest"
          ? `its next-fastest such move anywhere on the timeline took ${fmtSeconds(f.reference_seconds)} (${f.count} transitions)`
          : `the baseline never saw it under ${fmtSeconds(f.reference_seconds)} across ${f.baseline_count} transitions`;
      return {
        lead: `${stream}${truncate(f.values[0] ?? "", 30)} → ${truncate(f.values[1] ?? "", 30)} in`,
        highlight: fmtSeconds(f.observed_seconds),
        tail: `— ${floor}${f.speedup === null ? "" : `, ${f.speedup.toFixed(1)}× faster`}.`,
      };
    }
    case "time_of_day": {
      const reference =
        findingMode(f) === "self-habit"
          ? `across the timeline its ${f.baseline_count} occurrences keep to`
          : `in the baseline its ${f.baseline_count} occurrences keep to`;
      const habit =
        f.habit_buckets.length === 1
          ? f.nearest_habit_label
          : `${f.habit_buckets.length} buckets, the nearest ${f.nearest_habit_label}`;
      return {
        lead: `${fieldLabel(f.field)} = ${truncate(String(f.value), 40)} occurs ${f.count} time${f.count === 1 ? "" : "s"} at ${f.bucket_label} (${f.timezone}),`,
        highlight: `${f.distance_hours % 1 === 0 ? f.distance_hours.toFixed(0) : f.distance_hours.toFixed(1)} h off its habit`,
        tail: `— ${reference} ${habit}.`,
      };
    }
    case "value_correlation": {
      const where =
        findingMode(f) === "self-rule-g-test"
          ? `in ${detailString(f.details, "window_label") ?? "this slice"} against the rest of the timeline`
          : `in ${detailString(f.details, "window_label") ?? "the suspect window"}`;
      return {
        lead: `${fieldLabel(f.fields[0] ?? "")} = ${truncate(f.values[0] ?? "", 40)} normally means ${fieldLabel(f.fields[1] ?? "")} = ${truncate(f.values[1] ?? "", 40)} (${pct(f.confidence)} of ${f.support} reference events). ${where} it did not hold in`,
        highlight: `${f.violations} of ${f.count} events`,
        tail: `— most often ${fieldLabel(f.fields[1] ?? "")} = ${truncate(f.top_violator, 40)} (×${f.top_violator_count}), against ${f.baseline_violations} of ${f.baseline_count} in the reference (q=${f.q_value.toExponential(1)}).`,
      };
    }
  }
}

/** Seconds as a short human duration; every figure comes from the finding. */
function fmtSeconds(s: number): string {
  if (s < 60) return `${s % 1 === 0 ? s.toFixed(0) : s.toFixed(1)} s`;
  if (s < 3600) return `${(s / 60).toFixed(1)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

export function findingVerdict(finding: MethodResult): Verdict {
  if (isTemplateRow(finding)) {
    return {
      lead: "This message shape accounts for",
      highlight: `${finding.count} event${finding.count === 1 ? "" : "s"}`,
      tail: "— a lead to read, not a scored finding.",
    };
  }
  return scoredVerdict(finding);
}
