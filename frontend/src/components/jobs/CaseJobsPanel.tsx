/**
 * Shared, case-scoped background-job visibility: any user with case READ
 * access sees every ingest/embed/enrich job running on this case, not just
 * the browser tab that triggered it (unlike GlobalJobTray, which is
 * per-browser client-tracked state).
 */
import { useQuery } from "@tanstack/react-query";
import { jobsApi } from "@/api/jobs";
import { JobStatusRow } from "@/components/ui/JobStatusRow";
import { Spinner } from "@/components/ui/Spinner";

const KIND_LABELS: Record<string, string> = {
  ingest: "Ingest",
  embed: "Embed",
  enrich: "Enrich",
};

interface Props {
  caseId: string;
}

export function CaseJobsPanel({ caseId }: Props) {
  const { data: jobs, isLoading } = useQuery({
    queryKey: ["case-jobs", caseId],
    queryFn: () => jobsApi.listByCase(caseId),
    // Poll briskly while anything is in flight; back off (rather than stop)
    // once everything is terminal — another analyst may start a job on this
    // case at any time and shared visibility is the point of this panel.
    refetchInterval: (query) => {
      const data = query.state.data;
      const active = data?.some((j) => j.status === "queued" || j.status === "running");
      return active ? 5000 : 30_000;
    },
    refetchIntervalInBackground: false,
  });

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-4 py-3">
      <h2 className="mb-3 text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
        Background jobs
      </h2>
      {isLoading && (
        <div className="flex justify-center py-4">
          <Spinner />
        </div>
      )}
      {jobs && jobs.length === 0 && (
        <p className="text-xs text-[var(--color-fg-muted)]">No background jobs running.</p>
      )}
      {jobs && jobs.length > 0 && (
        <div className="space-y-2">
          {jobs.map((job) => (
            <JobStatusRow
              key={job.id}
              label={KIND_LABELS[job.kind] ?? job.kind}
              status={job.status}
              progress={job.progress}
              error={job.error}
            />
          ))}
        </div>
      )}
    </div>
  );
}
