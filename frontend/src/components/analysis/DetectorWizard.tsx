/**
 * DetectorWizard — how a detector gets onto a timeline.
 *
 * Nothing runs unprompted. An analyst picks a method from a list that says,
 * per card, when it is useful, what the analysis gate thinks of it on *this*
 * data and what it will cost; configures it with the same knob form the sheet
 * uses; reads back one sentence saying exactly what will be stored; applies.
 * The result is a shared, audited entry on the Timeline that the rail then
 * runs — through the same findings endpoint and cache as any ad hoc run.
 *
 * The gate stays advice here as everywhere: a `not_applicable` card is still
 * selectable, with the arithmetic behind the verdict beside it, and the
 * analyst who disagrees with it configures the method anyway.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, ShieldCheck } from "lucide-react";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { baselinesApi } from "@/api/baselines";
import { useAnalysisPlan } from "@/hooks/useAnalysisPlan";
import { useTimelineDetectors } from "@/hooks/useTimelineDetectors";
import { METHODS, METHODS_BY_ID, type MethodId } from "./method-registry";
import { MethodKnobForm } from "./MethodKnobForm";
import { NEEDS_BASELINE, summarize } from "./detector-wizard-summary";
import { cn } from "@/lib/cn";

type Step = "choose" | "configure" | "confirm";

interface Props {
  caseId: string;
  timelineId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Open straight on the configure step for this method (edit mode when configured). */
  initialMethod?: MethodId | null;
  /** The Signatures card: Sigma runs from its own tab and stays there. */
  onOpenSignatures: () => void;
}

/** Render `reason_facts` as a short parenthetical, in declaration order. */
function facts(reasonFacts: Record<string, number | string | boolean> | undefined): string | null {
  if (!reasonFacts) return null;
  const parts = Object.entries(reasonFacts).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`);
  return parts.length ? parts.join(" · ") : null;
}

const CARD =
  "flex flex-col gap-1 rounded border p-2 text-left text-xs transition-base hover:border-[var(--color-border-strong)]";

export function DetectorWizard({
  caseId,
  timelineId,
  open,
  onOpenChange,
  initialMethod,
  onOpenSignatures,
}: Props) {
  const detectors = useTimelineDetectors(caseId, timelineId);
  const { planById } = useAnalysisPlan(caseId, timelineId);
  const { data: baselines } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
    enabled: open,
  });

  const [step, setStep] = useState<Step>("choose");
  const [method, setMethod] = useState<MethodId | null>(null);
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [blocker, setBlocker] = useState<string | null>(null);
  const [frame, setFrame] = useState<"self" | "baseline">("self");
  const [baselineId, setBaselineId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const existing = method ? detectors.byMethod.get(method) : undefined;

  // Seed for a method: its stored entry when configured, else defaults — a
  // baseline-only method starts on the baseline frame because it has no other.
  const seed = (id: MethodId | null) => {
    const entry = id ? detectors.byMethod.get(id) : undefined;
    setMethod(id);
    setParams(entry?.params ?? {});
    setBlocker(null);
    setFrame(entry?.frame ?? (id && NEEDS_BASELINE.has(id) ? "baseline" : "self"));
    setBaselineId(entry?.baseline_id ?? null);
  };

  // Reset on every open. `initialMethod` opens straight on configure —
  // editing a configured entry, or adding one named by the caller.
  useEffect(() => {
    if (!open) return;
    setError(null);
    seed(initialMethod ?? null);
    setStep(initialMethod ? "configure" : "choose");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seed once per open
  }, [open, initialMethod]);

  const choose = (id: MethodId) => {
    seed(id);
    setStep("configure");
  };

  const baselineName = useMemo(
    () => baselines?.baselines.find((b) => b.id === baselineId)?.name ?? null,
    [baselines, baselineId],
  );
  const scopeOk = frame === "self" || baselineId !== null;
  const canProceed = method !== null && blocker === null && scopeOk;
  const meta = method ? METHODS_BY_ID[method] : null;

  const apply = async () => {
    if (!method || !canProceed) return;
    setError(null);
    try {
      await detectors.set(method, {
        params,
        frame,
        baseline_id: frame === "baseline" ? baselineId : null,
      });
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const title =
    step === "choose" || !meta
      ? "Add a detector"
      : existing
        ? `Edit ${meta.label}`
        : `Configure ${meta.label}`;
  const description =
    step === "choose" || !meta
      ? "Nothing runs until you choose it. Each detector answers one kind of question; pick the one that matches yours."
      : step === "configure"
        ? meta.what
        : "This is exactly what will be stored and run for everyone on the case.";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={title} description={description} className="max-w-2xl">
        {step === "choose" && (
          <div className="grid gap-2 sm:grid-cols-2" data-testid="wizard-step-choose">
            {METHODS.map((m) => {
              const plan = planById[m.id];
              const configured = detectors.byMethod.has(m.id);
              const gated = plan?.status === "not_applicable";
              return (
                <button
                  key={m.id}
                  type="button"
                  data-testid={`wizard-card-${m.id}`}
                  onClick={() => choose(m.id)}
                  className={cn(
                    CARD,
                    gated
                      ? "border-dashed border-[var(--color-border)]"
                      : "border-[var(--color-border)] bg-[var(--color-bg-elevated)]",
                  )}
                >
                  <span className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
                    <m.icon size={12} className="text-[var(--color-fg-muted)]" />
                    {m.label}
                    {configured && (
                      <Check
                        size={11}
                        className="text-[var(--color-accent)]"
                        aria-label="configured"
                      />
                    )}
                    <span className="ml-auto font-normal text-[var(--color-fg-muted)]">
                      {m.costClass === "heavy" ? "Full scan" : "Cheap"}
                    </span>
                  </span>
                  <span className="text-[var(--color-fg-secondary)]">{m.useWhen}</span>
                  {gated && (
                    <span className="text-[var(--color-warning)]">
                      Cannot apply here: {plan.reason}
                      {facts(plan.reason_facts) ? ` (${facts(plan.reason_facts)})` : ""}. You can
                      still configure it.
                    </span>
                  )}
                  {plan?.status === "needs_setup" && (
                    <span className="text-[var(--color-fg-muted)]">
                      Needs a baseline — you will pick one next.
                    </span>
                  )}
                </button>
              );
            })}
            <button
              type="button"
              data-testid="wizard-card-sigma"
              onClick={() => {
                onOpenChange(false);
                onOpenSignatures();
              }}
              className={cn(CARD, "border-[var(--color-border)]")}
            >
              <span className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
                <ShieldCheck size={12} className="text-[var(--color-fg-muted)]" />
                Signatures (Sigma)
              </span>
              <span className="text-[var(--color-fg-secondary)]">
                Use this when you want known techniques matched by name. Runs from the Signatures
                tab and stays there.
              </span>
            </button>
          </div>
        )}

        {step === "configure" && method && (
          <div data-testid="wizard-step-configure" className="space-y-3">
            <MethodKnobForm
              caseId={caseId}
              timelineId={timelineId}
              methodId={method}
              initialParams={existing?.params}
              onChange={(p, b) => {
                setParams(p);
                setBlocker(b);
              }}
              verbose
            />
            <fieldset className="space-y-1 text-xs">
              <legend className="font-semibold text-[var(--color-fg-secondary)]">
                Compare against
              </legend>
              {!NEEDS_BASELINE.has(method) && (
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="frame"
                    checked={frame === "self"}
                    onChange={() => setFrame("self")}
                  />
                  The whole timeline (self-baseline)
                </label>
              )}
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="frame"
                  checked={frame === "baseline"}
                  onChange={() => setFrame("baseline")}
                />
                A baseline definition
              </label>
              {frame === "baseline" && (
                <select
                  data-testid="wizard-baseline"
                  value={baselineId ?? ""}
                  onChange={(e) => setBaselineId(e.target.value || null)}
                  className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-xs"
                >
                  <option value="">Pick a baseline…</option>
                  {(baselines?.baselines ?? []).map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </select>
              )}
              {frame === "baseline" && (baselines?.baselines.length ?? 0) === 0 && (
                <p className="text-[var(--color-fg-muted)]">
                  No baseline on this timeline yet. Build one from Tools → Scope, then come back.
                </p>
              )}
              <p className="text-[var(--color-fg-muted)]">
                {NEEDS_BASELINE.has(method)
                  ? "This method compares a known-normal window against suspect windows, so it needs a baseline."
                  : "With a baseline, the method learns from the baseline window and reports on the suspect windows instead of the whole timeline."}
              </p>
            </fieldset>
            {blocker && (
              <p data-testid="wizard-blocker" className="text-xs text-[var(--color-warning)]">
                {blocker}
              </p>
            )}
            <div className="flex justify-between">
              <Button variant="ghost" size="sm" onClick={() => setStep("choose")}>
                <ArrowLeft size={11} /> Back
              </Button>
              <Button
                variant="outline"
                size="sm"
                data-testid="wizard-next"
                disabled={!canProceed}
                onClick={() => setStep("confirm")}
              >
                Next <ArrowRight size={11} />
              </Button>
            </div>
          </div>
        )}

        {step === "confirm" && meta && (
          <div data-testid="wizard-step-confirm" className="space-y-3">
            <p
              data-testid="wizard-summary"
              className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-sm text-[var(--color-fg-primary)]"
            >
              {summarize(meta, params, frame, baselineName)}
            </p>
            <p className="text-xs text-[var(--color-fg-muted)]">
              Stored on the timeline for everyone on the case and recorded in the audit trail. The
              first open pays the scan; later opens read the cached answer until the data or the
              settings change.
            </p>
            {error && (
              <p data-testid="wizard-error" className="text-xs text-[var(--color-danger)]">
                Not saved: {error}
              </p>
            )}
            <div className="flex justify-between">
              <Button variant="ghost" size="sm" onClick={() => setStep("configure")}>
                <ArrowLeft size={11} /> Back
              </Button>
              <Button
                variant="accent"
                size="sm"
                data-testid="wizard-apply"
                disabled={detectors.isSaving || !detectors.canEdit}
                onClick={() => void apply()}
              >
                <span data-testid="wizard-apply-label">{existing ? "Save changes" : "Apply"}</span>
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
