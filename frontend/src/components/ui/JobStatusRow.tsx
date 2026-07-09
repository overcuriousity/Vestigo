/**
 * Presentational job status row shared by GlobalJobTray (per-browser tracked
 * jobs) and CaseJobsPanel (shared, case-scoped job visibility) — the two
 * differ in data source and lifecycle, not in how a job's status renders.
 */
import { Loader2, CheckCircle, XCircle, X } from "lucide-react";
import type { Job } from "@/api/types";
import { Progress } from "@/components/ui/Progress";
import { fmtDuration } from "@/lib/time";
import { cn } from "@/lib/cn";

interface Props {
  label: string;
  status: Job["status"];
  progress: Job["progress"];
  error: string | null;
  onDismiss?: () => void;
  className?: string;
}

export function JobStatusRow({ label, status, progress, error, onDismiss, className }: Props) {
  const isTerminal = status === "completed" || status === "failed";

  const pct =
    progress && progress.total > 0 ? Math.round((progress.processed / progress.total) * 100) : null;

  const rate = progress?.rate_bps;
  const etaS = progress?.eta_s;
  const showEta = status === "running" && rate != null && rate > 0;
  const etaLine = showEta
    ? [`${(rate / 1e6).toFixed(1)} MB/s`, etaS != null ? `~${fmtDuration(etaS)} left` : null]
        .filter(Boolean)
        .join(" · ")
    : null;

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
        <div className="text-[var(--color-fg-muted)] capitalize">
          {status}
          {pct != null && ` · ${pct}%`}
        </div>
        {pct != null && status !== "failed" && <Progress value={pct} className="mt-1.5" />}
        {etaLine && (
          <div className="mt-1 font-mono text-[10px] text-[var(--color-fg-muted)] tabular-nums">
            {etaLine}
          </div>
        )}
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
