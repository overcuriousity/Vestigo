/**
 * GlobalJobTray — live ingest/embed job progress shown as a fixed
 * bottom-right toast tray so it never overlaps a page's own toolbar.
 * Polls /api/jobs/{id} for each active job, updates store on completion.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { jobsApi } from "@/api/jobs";
import { useJobsStore, type TrackedJob } from "@/stores/jobs";
import { JobStatusRow } from "@/components/ui/JobStatusRow";

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

  return (
    <JobStatusRow
      label={job.label}
      status={job.status}
      progress={job.progress}
      error={job.error}
      className="w-72"
      onDismiss={isTerminal ? () => dismiss(job.id) : undefined}
    />
  );
}

export function JobTray() {
  const jobs = useJobsStore((s) => s.jobs);
  const visible = Object.values(jobs).filter((j) => !j.dismissed);

  if (visible.length === 0) return null;

  return (
    <div data-tour="job-tray" className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {visible.map((j) => (
        <JobRow key={j.id} job={j} />
      ))}
    </div>
  );
}
