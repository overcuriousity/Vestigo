/**
 * Presentational job status row shared by GlobalJobTray (per-browser tracked
 * jobs) and CaseJobsPanel (shared, case-scoped job visibility) — the two
 * differ in data source and lifecycle, not in how a job's status renders.
 */
import { Loader2, CheckCircle, XCircle, X } from "lucide-react";
import type { Job } from "@/api/types";
import { ProgressMeter } from "@/components/ui/ProgressMeter";
import { progressPct } from "@/lib/format";
import { cn } from "@/lib/cn";

interface Props {
  label: string;
  status: Job["status"];
  progress: Job["progress"];
  error: string | null;
  /** Resolved human phase copy (see `lib/jobPhases.ts`), shown after the
   * status word. This row stays presentational — callers resolve the text. */
  detail?: string | null;
  onDismiss?: () => void;
  className?: string;
}

export function JobStatusRow({
  label,
  status,
  progress,
  error,
  detail,
  onDismiss,
  className,
}: Props) {
  const isTerminal = status === "completed" || status === "failed";
  const pct = progressPct(progress?.processed, progress?.total);
  // A finished or failed job's rate is history, not a live reading.
  const running = status === "running";

  const icon =
    status === "completed" ? (
      <CheckCircle size={14} className="text-[var(--color-success)] shrink-0" />
    ) : status === "failed" ? (
      <XCircle size={14} className="text-[var(--color-danger)] shrink-0" />
    ) : (
      <Loader2 size={14} className="animate-spin text-[var(--color-accent)] shrink-0" />
    );

  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-xs",
        status === "failed" && "border-[var(--color-danger)]/40",
        className,
      )}
    >
      <div className="mt-0.5">{icon}</div>
      <div className="flex-1 min-w-0">
        <div className="truncate font-medium text-[var(--color-fg-primary)]">{label}</div>
        <div className="text-[var(--color-fg-muted)]">
          {/* `capitalize` stays scoped to the status word — it would title-case
              every word of the phase copy otherwise. */}
          <span className="capitalize">{status}</span>
          {detail && ` · ${detail}`}
          {pct != null && ` · ${pct}%`}
        </div>
        <ProgressMeter
          processed={progress?.processed}
          total={progress?.total}
          rate_bps={running ? progress?.rate_bps : null}
          eta_s={running ? progress?.eta_s : null}
          // A running job always shows a moving bar, even mid-phase before that
          // phase knows how many items it covers.
          indeterminate={running}
          barHidden={status === "failed"}
        />
        {error && <div className="mt-1 text-[var(--color-danger)] line-clamp-2 break-all">{error}</div>}
      </div>
      {isTerminal && onDismiss && (
        <button
          onClick={onDismiss}
          className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
}
