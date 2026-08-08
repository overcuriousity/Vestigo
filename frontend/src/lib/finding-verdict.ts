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
import { anomalyFieldLabel as fieldLabel, truncate } from "@/lib/format";

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
        tail: `from the record before it in ${f.source_id} (line ${f.line_number}) — a clock-integrity problem in the evidence, not in the behavior it records.`,
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
      return {
        lead: `This value's share of ${fieldLabel(f.field)} went from`,
        highlight: `${pct(f.baseline_rate)} to ${pct(f.window_rate)}`,
        tail: `between the baseline and suspect windows (${f.direction}, ${f.rate_ratio.toFixed(1)}×, q=${f.q_value.toExponential(1)}).`,
      };
    case "interval_periodicity":
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
      return {
        lead: `This ordering of ${fieldLabel(f.field)} values`,
        highlight: "never occurs in the baseline",
        tail: `, and occurs ${f.count} time${f.count === 1 ? "" : "s"} in the suspect window.`,
      };
    case "value_distribution_drift":
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
  }
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
