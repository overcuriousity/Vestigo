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
import { AlertTriangle, Bell, BellOff, Play, Settings2 } from "lucide-react";
import type { MethodState } from "@/hooks/useMethodFindings";
import type { MutedMethods } from "@/hooks/useMutedMethods";
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
  mute,
}: {
  state: MethodState;
  onRun: (method: MethodId) => void;
  onOpen: (method: MethodId) => void;
  onSetupBaseline: () => void;
  /** Omitted where a mute makes no sense; present, this row can toggle one. */
  mute?: MutedMethods;
}) {
  const { meta, plan, status } = state;
  const gated = status !== "applicable";
  const muted = mute?.isMuted(meta.id) ?? false;
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
        muted
          ? "border-dashed border-[var(--color-warning)] bg-transparent"
          : gated
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
          {muted ? (
            // Deliberately not the gate's reason: the gate may well consider
            // this method applicable, and it was left out because somebody
            // said so. Naming that keeps the two apart.
            "muted — kept out of the sweep by an analyst on this case"
          ) : gated ? (
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

      {!muted && (state.error || unscored) && (
        <AlertTriangle size={12} className="shrink-0 text-[var(--color-warning)]" />
      )}

      {/* No count for a muted method, and emphatically not a zero: its query
          never ran, so `total === 0` here means "not asked", not "asked and
          clear". */}
      {!muted && !gated && !state.error && (
        <span
          data-testid={`method-count-${meta.id}`}
          className={cn(
            "shrink-0 font-mono font-semibold",
            state.pending || unscored
              ? "text-[var(--color-fg-muted)]"
              : state.total > 0
                ? "text-[var(--color-anomaly)]"
                : "text-[var(--color-fg-disabled)]",
          )}
          // A dash, not a zero: "0" asserts the method looked and found
          // nothing, which is exactly what it could not do. A method still
          // queued behind the cheap set has not looked *yet* and has the same
          // `total === 0` — so it gets its own mark rather than borrowing
          // either answer.
          title={
            state.pending
              ? "Queued — this method has not run yet"
              : unscored
                ? "This method ran but could not score this data"
                : undefined
          }
        >
          {state.pending ? "…" : unscored ? "—" : state.total}
        </span>
      )}

      {/* Offered on every row, muted or not: a mute is reversible from the same
          place it is applied, and Tools is the surface that has to be able to
          list and undo one (the rule the template mute already follows). */}
      {mute && (
        <button
          data-testid={`method-mute-${meta.id}`}
          onClick={() => mute.toggle(meta.id)}
          disabled={!mute.canEdit || mute.isSaving}
          aria-pressed={muted}
          title={
            !mute.canEdit
              ? "Read-only access — muting changes shared case state"
              : muted
                ? "Unmute: put this method back in the sweep"
                : "Mute: keep this method's findings out of the feed and the histogram. It still runs from here."
          }
          className={cn(
            "shrink-0 rounded border border-transparent px-1 py-0.5 transition-base hover:border-[var(--color-border)] disabled:opacity-40",
            muted
              ? "text-[var(--color-warning)]"
              : "text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
          )}
        >
          {muted ? <BellOff size={11} /> : <Bell size={11} />}
        </button>
      )}

      {status === "needs_setup" ? (
        <button
          onClick={onSetupBaseline}
          className="shrink-0 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[11px] text-[var(--color-accent)] hover:border-[var(--color-accent)]"
        >
          Set a baseline
        </button>
      ) : muted || status === "not_applicable" || state.error ? (
        // A muted method reuses the gate's affordance because it is the same
        // promise: this was left out of the sweep, and you can still run it.
        // Running it does not unmute it — the two are different acts.
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
