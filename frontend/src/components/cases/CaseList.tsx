import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { casesApi } from "@/api/cases";
import { demoApi } from "@/api/demo";
import { useCapabilities } from "@/api/health";
import { useJobsStore } from "@/stores/jobs";
import { CaseCard } from "./CaseCard";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { FolderOpen } from "lucide-react";

/**
 * The demo case is seeded once per user on first login, and deleting it is
 * final — this is the only way back. Offered both in the empty state (where a
 * user who deleted theirs and has nothing else ends up) and as a quiet link
 * under a populated list, so it stays reachable for someone with real cases.
 */
function useDemoSeed() {
  const addJob = useJobsStore((s) => s.addJob);
  const jobs = useJobsStore((s) => s.jobs);
  const [jobId, setJobId] = useState<string | null>(null);

  const seed = useMutation({
    mutationFn: () => demoApi.seed(),
    onSuccess: ({ job_id }) => {
      setJobId(job_id);
      // Hand the job to the tray, which polls it and invalidates the case list
      // when it completes. The build takes ~10s; without tracking it the button
      // re-enables while the list is still empty and an impatient second click
      // seeds a second case.
      addJob(job_id, "Preparing the demo case", [["cases"]]);
    },
  });

  const tracked = jobId ? jobs[jobId] : undefined;
  const running =
    seed.isPending || (tracked !== undefined && tracked.status !== "completed" && tracked.status !== "failed");

  return {
    start: () => seed.mutate(),
    running,
    label: running ? "Preparing the demo case…" : "Load the demo case",
    error: seed.isError ? (seed.error as Error).message : null,
  };
}

export function CaseList() {
  const { data: cases, isLoading, error } = useQuery({
    queryKey: ["cases"],
    queryFn: () => casesApi.list(),
    refetchInterval: 30_000,
  });

  const { demo_case: demoAvailable } = useCapabilities();
  const demo = useDemoSeed();
  // Admins aside, a demo case in your list is always your own — the API keeps
  // other users' copies out of it — so its presence is the whole guard. The
  // server refuses a second one with a 409 regardless.
  const hasDemoCase = cases?.some((c) => c.is_demo) ?? false;
  const offerDemo = demoAvailable && !hasDemoCase;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-[var(--color-fg-muted)]">
        <Spinner size={20} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded border border-[var(--color-danger)]/30 bg-[var(--color-danger-dim)] px-4 py-3 text-sm text-[var(--color-danger)]">
        Failed to load cases: {(error as Error).message}
      </div>
    );
  }

  if (!cases || cases.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 py-20 text-[var(--color-fg-muted)]">
        <FolderOpen size={40} className="opacity-30" />
        <p className="text-sm">No investigation cases yet.</p>
        <p className="text-xs">Create a case to get started.</p>
        {offerDemo && (
          <div className="mt-2 flex flex-col items-center gap-1">
            <Button variant="outline" size="sm" disabled={demo.running} onClick={demo.start}>
              {demo.label}
            </Button>
            <p className="text-xs">A worked example investigation with fabricated data.</p>
            {demo.error && (
              <p className="text-xs text-[var(--color-danger)]">{demo.error}</p>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2" data-tour="case-list">
      {cases.map((c) => (
        <CaseCard key={c.id} case_={c} />
      ))}
      {offerDemo && (
        <div className="pt-2 text-center text-xs text-[var(--color-fg-muted)]">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-auto px-1 py-0 text-xs font-normal underline underline-offset-2 hover:bg-transparent disabled:no-underline"
            disabled={demo.running}
            onClick={demo.start}
          >
            {demo.label}
          </Button>
          {demo.error && <span className="ml-2 text-[var(--color-danger)]">{demo.error}</span>}
        </div>
      )}
    </div>
  );
}
