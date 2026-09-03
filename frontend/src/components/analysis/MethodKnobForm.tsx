/**
 * MethodKnobForm — one method's parameters as controls, shared by the sheet's
 * method mode and the detector wizard's configure step.
 *
 * Reports `(params, blocker)` on every change rather than owning a submit:
 * the sheet wraps it in a form with a Run button, the wizard with a Next
 * button, and neither should know how the other submits. `blocker` is the
 * reason the knobs as they stand cannot be run (a `value_combo` with one
 * field), or null.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { METHODS_BY_ID, type MethodId, type MethodKnob, type MethodMeta } from "./method-registry";
import { AnomalyFieldPicker } from "./AnomalyFieldPicker";
import { MethodFieldSelect } from "./MethodFieldSelect";
import { useFieldOverrides } from "@/hooks/useFieldOverrides";
import { cn } from "@/lib/cn";

/**
 * Turn the knob inputs into the params object the findings endpoint takes.
 *
 * Blanks are omitted rather than sent as empty strings: an untouched knob means
 * "use the method's default", and sending "" would either 422 or, worse, be
 * coerced into a different question under a cache key claiming otherwise.
 * Numbers are coerced here because the endpoint's per-method models are typed,
 * and a numeric knob arriving as a string is the analyst's typing, not intent.
 *
 * A `fields` selection is sent as a list, which `_FieldsParams._join_fields`
 * accepts alongside the comma-joined string. `null` there is the picker's "auto"
 * — the same untouched-knob case, so it is omitted for the same reason. An
 * empty list never reaches here: `knobBlocker` refuses the run, because a scan
 * over no fields comes back as an empty result set and reads as "clean".
 */
export function buildParams(
  meta: MethodMeta,
  raw: Record<string, string>,
  fields: Record<string, string[] | null>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const knob of meta.knobs) {
    if (knob.kind === "fields") {
      const selected = fields[knob.param];
      if (selected) out[knob.param] = selected;
      continue;
    }
    const value = (raw[knob.param] ?? "").trim();
    if (!value) continue;
    out[knob.param] = knob.kind === "number" ? Number(value) : value;
  }
  return out;
}

/**
 * Why the knobs as they stand cannot be run, or `null` if they can.
 *
 * `AnomalyFieldPicker` lets a selection fall below its own floor on purpose —
 * unchecking down to one chip on the way to a different pair is a normal thing
 * to do — and only warns; disabling the run is the caller's half of that pair,
 * and without it `value_combo` sends one field and comes back as a 422 the
 * sheet can only render as "this method failed to run".
 *
 * The floor is at least one field even where the method declares none: an
 * explicit empty selection is "scan nothing", which returns an empty result set
 * indistinguishable from "the data is clean". `null` — the picker's auto — is
 * always runnable, since the method chooses its own fields there.
 */
export function knobBlocker(meta: MethodMeta, fields: Record<string, string[] | null>): string | null {
  for (const knob of meta.knobs) {
    if (knob.kind !== "fields") continue;
    const selected = fields[knob.param];
    if (!selected) continue;
    const floor = Math.max(1, knob.picker?.minSelected ?? 1);
    if (selected.length >= floor) continue;
    return floor > 1
      ? `Pick at least ${floor} fields to combine, or reset to auto.`
      : "Pick at least one field to scan, or reset to auto.";
  }
  return null;
}

/**
 * Turn a stored params object back into the form's two state maps, so the
 * wizard's edit mode opens on what is configured rather than on "auto".
 * A `fields` value may be a list or the comma string the backend also accepts.
 */
export function seedFromParams(
  meta: MethodMeta,
  params: Record<string, unknown>,
): { values: Record<string, string>; fields: Record<string, string[] | null> } {
  const values: Record<string, string> = {};
  const fields: Record<string, string[] | null> = {};
  for (const knob of meta.knobs) {
    const raw = params[knob.param];
    if (raw === undefined || raw === null) continue;
    if (knob.kind === "fields") {
      fields[knob.param] = Array.isArray(raw) ? raw.map(String) : String(raw).split(",");
    } else {
      values[knob.param] = String(raw);
    }
  }
  return { values, fields };
}

/** One line under each control in the wizard — the guidance attached to the knob. */
export function knobHelp(knob: MethodKnob): string {
  switch (knob.param) {
    case "fields":
      return "Which fields to scan. Auto lets Vestigo pick from this timeline's inventory, applying the pins and exclusions the case team declared.";
    case "series_field":
      return "The field whose values form the series — one series per value. Pick something with a few distinct values that mean something, like a host.";
    case "group_field":
      return "Learn one alphabet per value of this field instead of one for the whole scope.";
    case "z_threshold":
      return "How far from the expected count a bucket must be to count as a spike or silence. Higher is stricter.";
    case "fdr_q":
      return "The share of reported findings you are willing to have be false discoveries. 0.05 means one in twenty.";
    case "min_ratio":
      return "The smallest change worth reporting, as a ratio. 2.0 means the share at least doubled or halved.";
    case "min_skew_seconds":
      return "Ignore timestamps that run backwards by less than this many seconds.";
    case "ngram_size":
      return "How many consecutive events form one sequence. Three is a good default.";
    case "max_gap_seconds":
      return "Break a sequence when consecutive events are farther apart than this.";
    case "field":
      return "The text field to cluster into templates. Usually the message.";
    case "order":
      return "Which templates to show first: most common, first seen, or last seen.";
    case "only_new":
      return "Only templates that never appeared in the baseline window.";
    default:
      return knob.label;
  }
}

interface Props {
  /** The field knobs offer this timeline's own columns, so both ids are needed. */
  caseId: string;
  timelineId: string;
  methodId: MethodId;
  /** Opens the form on these values (edit mode) instead of on defaults. */
  initialParams?: Record<string, unknown>;
  /** Called on every change with the params as they stand and why they cannot run (or null). */
  onChange: (params: Record<string, unknown>, blocker: string | null) => void;
  /** Show each knob's help text under the control (the wizard); the sheet stays compact. */
  verbose?: boolean;
}

export function MethodKnobForm({
  caseId,
  timelineId,
  methodId,
  initialParams,
  onChange,
  verbose = false,
}: Props) {
  const meta = METHODS_BY_ID[methodId];
  const seed = useMemo(() => seedFromParams(meta, initialParams ?? {}), [meta, initialParams]);
  const [values, setValues] = useState<Record<string, string>>(seed.values);
  // Field selections are kept apart from the typed knobs: `null` is a real
  // value here ("let the method choose"), which an empty string cannot express.
  const [fields, setFields] = useState<Record<string, string[] | null>>(seed.fields);

  // Switching methods must not carry the previous method's typing across —
  // the knobs look the same and the params would silently be the old ones.
  // Guarded on the method actually changing, so re-rendering with a new seed
  // identity never throws away the analyst's next edit.
  const seededFor = useRef(methodId);
  useEffect(() => {
    if (seededFor.current === methodId) return;
    seededFor.current = methodId;
    setValues(seed.values);
    setFields(seed.fields);
  }, [methodId, seed]);

  // The durable half of the same decision: which fields this method reads at
  // all, declared once for the case. Read-only members see the state and not
  // the control (`canEdit` false → no `onDeclare`).
  const { forMethod, declare, canEdit, saveError } = useFieldOverrides(caseId, timelineId);
  const overrides = forMethod(methodId);

  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  useEffect(() => {
    onChangeRef.current(buildParams(meta, values, fields), knobBlocker(meta, fields));
  }, [values, fields, meta]);

  return (
    <div className="flex flex-wrap items-start gap-2" data-testid="method-knob-form">
      {meta.knobs.map((knob) => (
        <div key={knob.param} className={cn(verbose && "w-full")}>
          {/* Which fields to scan is a choice among this timeline's own columns,
              ranked by cardinality — the picker is the control for it, and a
              text box asking the analyst to recall `attr:` token spellings is
              not a smaller version of the same thing. */}
          {knob.kind === "fields" ? (
            <AnomalyFieldPicker
              caseId={caseId}
              timelineId={timelineId}
              selected={fields[knob.param] ?? null}
              onChange={(tokens) => setFields((f) => ({ ...f, [knob.param]: tokens }))}
              minSelected={knob.picker?.minSelected}
              maxSelected={knob.picker?.maxSelected}
              autoCount={knob.picker?.autoCount}
              autoIncludesIdentifiers={knob.picker?.autoIncludesIdentifiers}
              autoLabel={knob.picker?.autoLabel}
              numeric={knob.picker?.numeric}
              overrides={overrides}
              onDeclare={
                canEdit ? (token, state) => declare(methodId, token, state) : undefined
              }
            />
          ) : (
            <label
              data-testid="method-knob"
              className="flex items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
            >
              {knob.label}
              {/* A field name comes from the timeline's own inventory; anything
                  else is the analyst's own number or string. */}
              {knob.kind === "field" ? (
                <MethodFieldSelect
                  caseId={caseId}
                  timelineId={timelineId}
                  knob={knob}
                  value={values[knob.param] ?? ""}
                  onChange={(next) => setValues((v) => ({ ...v, [knob.param]: next }))}
                />
              ) : (
                <input
                  aria-label={knob.label}
                  data-testid={`method-knob-${knob.param}`}
                  type={knob.kind === "number" ? "number" : "text"}
                  value={values[knob.param] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [knob.param]: e.target.value }))}
                  placeholder={knob.placeholder}
                  className="w-16 bg-transparent font-mono text-[11px] text-[var(--color-fg-primary)] outline-none placeholder:text-[var(--color-fg-disabled)]"
                />
              )}
            </label>
          )}
          {verbose && (
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">{knobHelp(knob)}</p>
          )}
        </div>
      ))}
      {/* The chip snaps back to the server's answer on the next render, which
          on its own reads as "nothing happened". A declaration the analyst
          believes the whole case now inherits has to say when it was not
          stored. */}
      {saveError && (
        <p data-testid="field-declare-error" className="w-full text-xs text-[var(--color-danger)]">
          Field declaration not saved: {saveError}
        </p>
      )}
    </div>
  );
}
