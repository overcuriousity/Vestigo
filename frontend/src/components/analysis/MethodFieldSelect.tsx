/**
 * MethodFieldSelect — the single-field knob (`kind: "field"`).
 *
 * The counterpart to `AnomalyFieldPicker`, which answers "which fields" for the
 * methods that scan a set. This answers "which field" for the ones that take
 * exactly one: `series_field`, charset's `group_field`, log templates' `field`.
 *
 * Both used to exist and both were lost in the rail-plus-overlay refactor, which
 * replaced eleven per-detector views with one generic knob renderer that types
 * every knob as a text box. A field name is not free text — it is one of a
 * fixed set of columns plus whatever attribute keys this timeline happens to
 * carry, and an analyst cannot be expected to know the `attr:` token spelling
 * for a source they ingested an hour ago.
 *
 * The static options come from the knob (each method's are different); the
 * dynamic half is every `attr:*` token the cardinality inventory reports, which
 * is exactly how the deleted views built their dropdowns.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { anomaliesApi } from "@/api/anomalies";
import { anomalyFieldLabel } from "@/lib/format";
import type { MethodKnob } from "./method-registry";

export function MethodFieldSelect({
  caseId,
  timelineId,
  knob,
  value,
  onChange,
}: {
  caseId: string;
  timelineId: string;
  knob: MethodKnob;
  value: string;
  onChange: (next: string) => void;
}) {
  const { data } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "fields"],
    queryFn: () => anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  const options = useMemo(() => {
    const dynamic = (data?.fields ?? [])
      .filter((f) => f.token.startsWith("attr:"))
      .map((f) => ({ value: f.token, label: anomalyFieldLabel(f.token) }));
    return [...(knob.fieldOptions ?? []), ...dynamic];
  }, [data, knob.fieldOptions]);

  // Only the control: the caller wraps it in the same labelled chrome as the
  // text and number knobs, and the size comes from there by inheritance. A
  // second copy of that chrome here would be a second place to keep in step,
  // and would need its own arbitrary font size to match.
  return (
    <select
      aria-label={knob.label}
      data-testid={`method-knob-${knob.param}`}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="bg-transparent text-[var(--color-fg-primary)] outline-none"
    >
      {/* The empty choice is the method's own default, so it is named after what
          the method then does rather than left blank — "(none)" beside a knob
          that silently means "the whole scope" reads as a missing value. */}
      <option value="">{knob.noneLabel ?? knob.placeholder}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
