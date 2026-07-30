import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { casesApi } from "@/api/cases";
import { demoApi } from "@/api/demo";
import { useCapabilities } from "@/api/health";
import { CaseCard } from "./CaseCard";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { FolderOpen } from "lucide-react";

export function CaseList() {
  const queryClient = useQueryClient();
  const { data: cases, isLoading, error } = useQuery({
    queryKey: ["cases"],
    queryFn: () => casesApi.list(),
    refetchInterval: 30_000,
  });

  // The demo case is seeded once per user on first login and deleting it is
  // final — this is the only way back, so it renders exactly where a user who
  // deleted theirs ends up.
  const { demo_case: demoAvailable } = useCapabilities();
  const seedDemo = useMutation({
    mutationFn: () => demoApi.seed(),
    // The import runs as a background job; the list's own 30s refetch picks
    // the case up when it lands, so this just shortens the wait.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cases"] }),
  });

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
        {demoAvailable && (
          <div className="mt-2 flex flex-col items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              disabled={seedDemo.isPending}
              onClick={() => seedDemo.mutate()}
            >
              {seedDemo.isPending ? "Loading demo case…" : "Load the demo case"}
            </Button>
            <p className="text-xs">A worked example investigation with fabricated data.</p>
            {seedDemo.isError && (
              <p className="text-xs text-[var(--color-danger)]">
                {(seedDemo.error as Error).message}
              </p>
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
    </div>
  );
}
