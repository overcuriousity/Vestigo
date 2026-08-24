/**
 * finding-subject — what a finding is *about*, as field/value pairs.
 *
 * The rail row is the only place the subject was ever stated: the sheet opened
 * with "This value of captured_length appears in 1 event" and never named the
 * value. An analyst reads the sheet to decide whether a finding is real, and
 * the value is the first thing they need in order to recognize it — a session
 * id they issued themselves, a hostname they know, a length that is obviously
 * a header. Making them close the sheet to re-read the row it came from is the
 * one navigation the detail surface must not require.
 *
 * Structural rules, mirroring `finding-verdict`:
 *
 * - **Nothing is invented.** Every pair comes out of the payload.
 * - **The switch is exhaustive over the union**, so a new finding shape fails
 *   the type check here rather than rendering an anonymous subject.
 *
 * A finding whose subject is not a value — a whole distribution, a record's
 * clock — states what it *is* about instead, so the block never lies about
 * having isolated a value.
 */
import type { AnomalyFinding } from "@/api/types";
import type { MethodResult } from "@/api/analysis";
import { isTemplateRow } from "@/api/analysis";
import { anomalyFieldLabel as fieldLabel } from "@/lib/format";

export interface SubjectPair {
  /** The field this value belongs to, or what the subject is when it is not a value. */
  label: string;
  /** The value itself, rendered verbatim — never truncated by this module. */
  value: string;
}

function scoredSubject(f: AnomalyFinding): SubjectPair[] {
  switch (f.type) {
    case "value_novelty":
    case "numeric_range":
    case "charset":
    case "entropy":
    case "proportion_shift":
    case "interval_periodicity":
    case "sequence_novelty":
    case "sequence_motif":
      return [{ label: fieldLabel(f.field), value: String(f.value) }];
    case "value_combo":
      return f.fields.map((field, i) => ({
        label: fieldLabel(field),
        value: String(f.values[i] ?? ""),
      }));
    case "frequency":
      return [{ label: fieldLabel(f.series_field), value: String(f.series_value) }];
    case "timestamp_order":
      // No value is at fault here — a record's clock is. The timestamp that
      // ran backwards is the subject, and the record it belongs to is how the
      // analyst finds it again.
      return [
        { label: "timestamp", value: f.timestamp },
        { label: "previous record", value: f.prev_timestamp },
      ];
    case "value_distribution_drift":
      // The claim is about the whole value mix, so naming any one value would
      // misstate what was compared.
      return [{ label: "field", value: fieldLabel(f.field) }];
  }
}

/** The field/value pairs a finding is about, in the order they should be read. */
export function findingSubject(finding: MethodResult): SubjectPair[] {
  if (isTemplateRow(finding)) {
    return [{ label: "message shape", value: finding.template }];
  }
  return scoredSubject(finding);
}
