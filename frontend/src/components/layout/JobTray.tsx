/**
 * GlobalJobTray — live ingest/embed job progress shown as a fixed
 * bottom-right toast tray so it never overlaps a page's own toolbar.
 * Polls /api/jobs/{id} for each active job, updates store on completion.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, CheckCircle, XCircle, X } from "lucide-react";
import { jobsApi } from "@/api/jobs";
import { useJobsStore, type TrackedJob } from "@/stores/jobs";
import { Progress } from "@/components/ui/Progress";
import { cn } from "@/lib/cn";

/** Mirrors cli/progress.py's `_fmt_duration` so web ETAs read like the CLI. */
function fmtDuration(seconds: number): string {
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${String(rs).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${String(rm).padStart(2, "0")}m ${String(rs).padStart(2, "0")}s`;
}

function JobRow({ job }: { job: TrackedJob }) {
  const { updateJob, dismiss } = useJobsStore();
  const qc = useQueryClient();

  const isTerminal = job.status === "completed" || job.status === "failed";

  useQuery({
    queryKey: ["job", job.id],
    queryFn: async () => {
      const j = await jobsApi.get(job.id);
      updateJob(j);
      if (j.status === "completed") {
        for (const key of job.invalidate ?? []) {
          qc.invalidateQueries({ queryKey: key });
        }
      }
      return j;
    },
    enabled: !isTerminal && !job.dismissed,
    // Poll briskly at first (progress moving fast), then back off — a job
    // that's been running for minutes doesn't need sub-second-scale polling.
    refetchInterval: (query) => {
      const polls = query.state.dataUpdateCount;
      if (polls < 10) return 1200;
      if (polls < 30) return 3000;
      return 8000;
    },
    refetchIntervalInBackground: false,
  });

  const pct =
    job.progress && job.progress.total > 0
      ? Math.round((job.progress.processed / job.progress.total) * 100)
      : null;

  // Kalman-filtered rate/ETA, computed server-side (core/eta.py) and shown only
  // while the job is actively running — the same estimate the CLI prints.
  const rate = job.progress?.rate_bps;
  const etaS = job.progress?.eta_s;
  const showEta = job.status === "running" && rate != null && rate > 0;
  const etaLine = showEta
    ? [
        `${(rate / 1e6).toFixed(1)} MB/s`,
        etaS != null ? `~${fmtDuration(etaS)} left` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : null;

  const icon =
    job.status === "completed" ? (
      <CheckCircle size={14} className="text-[var(--color-success)] shrink-0" />
    ) : job.status === "failed" ? (
      <XCircle size={14} className="text-[var(--color-danger)] shrink-0" />
    ) : (
      <Loader2
        size={14}
        className="animate-spin text-[var(--color-accent)] shrink-0"
      />
    );

  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-xs w-72",
        job.status === "failed" && "border-[var(--color-danger)]/40",
      )}
    >
      <div className="mt-0.5">{icon}</div>
      <div className="flex-1 min-w-0">
        <div className="truncate font-medium text-[var(--color-fg-primary)]">
          {job.label}
        </div>
        <div className="text-[var(--color-fg-muted)] capitalize">
          {job.status}
          {pct != null && ` · ${pct}%`}
        </div>
        {pct != null && job.status !== "failed" && (
          <Progress value={pct} className="mt-1.5" />
        )}
        {etaLine && (
          <div className="mt-1 font-mono text-[10px] text-[var(--color-fg-muted)] tabular-nums">
            {etaLine}
          </div>
        )}
        {job.error && (
          <div className="mt-1 text-[var(--color-danger)] line-clamp-2 break-all">{job.error}</div>
        )}
      </div>
      {isTerminal && (
        <button
          onClick={() => dismiss(job.id)}
          className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
}

export function JobTray() {
  const jobs = useJobsStore((s) => s.jobs);
  const visible = Object.values(jobs).filter((j) => !j.dismissed);

  if (visible.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {visible.map((j) => (
        <JobRow key={j.id} job={j} />
      ))}
    </div>
  );
}
