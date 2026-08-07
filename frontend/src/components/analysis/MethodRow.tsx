/**
 * MethodRow — one method's line in the Tools sheet's accounting.
 *
 * Three shapes, because three situations are genuinely different:
 *
 *   applicable      — it ran; the count is the answer.
 *   not_applicable  — it *cannot* produce a finding on this data. Shows the
 *                     arithmetic behind that claim and stays runnable, because
 *                     the gate is advice and an analyst may disagree with it.
 *   needs_setup     — an analyst action would make it applicable. Offers that
 *                     action instead of "run anyway", which here would only
 *                     produce a guaranteed-empty scan.
 *
 * The reason is never rendered as a bare verdict. "Not applicable" is a shrug;
 * "no field parses as numeric (0 of 19 sampled)" is a claim someone can check
 * and argue with, which is the standard this panel is held to.
 *
 * A fourth situation hides inside "applicable": the method ran and could not
 * score (`insufficient_data`, `no_data` — a suspect window too small, a field
 * absent from the scan). That is not a clean zero, and showing it as one is the
 * "checked, clear" misread this whole surface exists to prevent, so it renders
 * as a dash with the runner's own words rather than a count.
 */
import { AlertTriangle, Play, Settings2 } from "lucide-react";
import type { MethodState } from "@/hooks/useMethodFindings";
import type { MethodId } from "./method-registry";
import { cn } from "@/lib/cn";

/**
 * The runner's own statuses for "scanned, could not reach a verdict".
 * `db/anomaly_stats.py` is the source of these strings.
 */
const UNSCORED_STATUSES = new Set(["insufficient_data", "no_data"]);

/** Render `reason_facts` as a short parenthetical, in declaration order. */
function facts(reasonFacts: Record<string, number | string | boolean> | undefined): string | null {
  if (!reasonFacts) return null;
  const parts = Object.entries(reasonFacts).map(
    ([key, value]) => `${key.replace(/_/g, " ")} ${value}`,
  );
  return parts.length ? parts.join(" · ") : null;
}

export function MethodRow({
  state,
  onRun,
  onOpen,
  onSetupBaseline,
}: {
  state: MethodState;
  onRun: (method: MethodId) => void;
  onOpen: (method: MethodId) => void;
  onSetupBaseline: () => void;
}) {
  const { meta, plan, status } = state;
  const gated = status !== "applicable";
  const detail = gated ? facts(plan?.reason_facts) : null;
  // The runner's own verdict about the data, only meaningful once it has run.
  const unscored =
    !gated && !state.error && !state.pending && UNSCORED_STATUSES.has(state.dataStatus ?? "");
  const warning = state.warnings?.[0];

  return (
    <div
      data-testid={`method-row-${meta.id}`}
      className={cn(
        "flex items-center gap-2 rounded border px-2 py-1.5 text-xs",
        gated
          ? "border-dashed border-[var(--color-border)]"
          : "border-[var(--color-border)] bg-[var(--color-bg-elevated)]",
      )}
    >
      <meta.icon size={12} className="shrink-0 text-[var(--color-fg-muted)]" />
      <div className="min-w-0 flex-1">
        <b className="block font-medium text-[var(--color-fg-primary)]">{meta.label}</b>
        <em
          data-testid={`method-detail-${meta.id}`}
          className="not-italic text-[11px] text-[var(--color-fg-muted)]"
        >
          {gated ? (
            <>
              {plan?.reason}
              {detail && <span className="font-mono"> ({detail})</span>}
            </>
          ) : state.error ? (
            "failed to run"
          ) : unscored ? (
            // The runner's warning says *which* data it lacked; the status
            // alone would be another bare verdict.
            (warning ?? "ran, but the data could not be scored")
          ) : (
            <>
              {meta.hint}
              {/* A warning on an answer that *did* score is a caveat on that
                  answer, not a replacement for it. */}
              {warning && (
                <span className="block text-[var(--color-warning)]">{warning}</span>
              )}
            </>
          )}
        </em>
      </div>

      {(state.error || unscored) && (
        <AlertTriangle size={12} className="shrink-0 text-[var(--color-warning)]" />
      )}

      {!gated && !state.error && (
        <span
          data-testid={`method-count-${meta.id}`}
          className={cn(
            "shrink-0 font-mono font-semibold",
            unscored
              ? "text-[var(--color-fg-muted)]"
              : state.total > 0
                ? "text-[var(--color-anomaly)]"
                : "text-[var(--color-fg-disabled)]",
          )}
          // A dash, not a zero: "0" asserts the method looked and found
          // nothing, which is exactly what it could not do.
          title={unscored ? "This method ran but could not score this data" : undefined}
        >
          {unscored ? "—" : state.total}
        </span>
      )}

      {status === "needs_setup" ? (
        <button
          onClick={onSetupBaseline}
          className="shrink-0 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[11px] text-[var(--color-accent)] hover:border-[var(--color-accent)]"
        >
          Set a baseline
        </button>
      ) : status === "not_applicable" || state.error ? (
        <button
          onClick={() => onRun(meta.id)}
          className="flex shrink-0 items-center gap-1 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[11px] text-[var(--color-accent)] hover:border-[var(--color-accent)]"
        >
          <Play size={9} />
          {state.error ? "Retry" : "Run anyway"}
        </button>
      ) : (
        <button
          onClick={() => onOpen(meta.id)}
          title="Method detail, parameters and query"
          className="shrink-0 rounded border border-transparent px-1 py-0.5 text-[var(--color-fg-muted)] hover:border-[var(--color-border)] hover:text-[var(--color-fg-secondary)]"
        >
          <Settings2 size={11} />
        </button>
      )}
    </div>
  );
}
