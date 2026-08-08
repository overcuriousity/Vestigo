/**
 * finding-evidence — whether a finding has a picture, and what it is about.
 *
 * Predicates rather than rendering, kept beside `finding-normalize` and
 * `finding-verdict` so the three shape-dispatching modules read together — and
 * so the component file stays components-only.
 *
 * The list of shapes without a figure is the interesting part. A rare value's
 * rarity *is* its score and a combo's claim is the co-occurrence itself:
 * neither carries a second number to compare against. Drawing something anyway
 * would put a chart on screen that no measurement backs, which in a forensic
 * tool is worse than drawing nothing.
 */
import type { MethodResult } from "@/api/analysis";
import { isTemplateRow } from "@/api/analysis";
import { anomalyFieldLabel as fieldLabel } from "@/lib/format";

/** Whether `FindingEvidence` will draw anything for this finding. */
export function hasEvidence(finding: MethodResult): boolean {
  if (isTemplateRow(finding)) return false;
  return finding.type !== "value_novelty" && finding.type !== "value_combo";
}

/** The field a band or bar pair is about, for the Evidence heading. */
export function evidenceCaption(finding: MethodResult): string | null {
  if (isTemplateRow(finding)) return null;
  if (finding.type === "frequency") return fieldLabel(finding.series_field);
  if ("field" in finding) return fieldLabel(finding.field);
  return null;
}
