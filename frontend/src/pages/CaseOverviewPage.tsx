import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { casesApi } from "@/api/cases";
import { sourcesApi } from "@/api/sources";
import { TimelineList } from "@/components/timelines/TimelineList";
import { SourceList } from "@/components/sources/SourceList";
import { Spinner } from "@/components/ui/Spinner";
import { Badge } from "@/components/ui/Badge";
import { fmtRelative } from "@/lib/time";
import { FolderOpen, Cpu } from "lucide-react";

function EmbeddingStatusBadge({ caseId }: { caseId: string }) {
  const { data: sources } = useQuery({
    queryKey: ["sources", caseId],
    queryFn: () => sourcesApi.list(caseId),
    refetchInterval: 15_000,
  });

  if (!sources || sources.length === 0) return null;
  const embedded = sources.filter((s) => s.vector_count > 0).length;
  const allEmbedded = embedded === sources.length;

  return (
    <Badge variant={allEmbedded ? "success" : "accent"}>
      <Cpu size={11} className="mr-1" />
      {embedded}/{sources.length} sources embedded
    </Badge>
  );
}

export function CaseOverviewPage() {
  const { caseId } = useParams<{ caseId: string }>();

  const { data: case_, isLoading, error } = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => casesApi.get(caseId!),
    enabled: !!caseId,
  });

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (error || !case_) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-[var(--color-danger)]">
        {error ? (error as Error).message : "Case not found"}
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {/* Case header */}
        <div className="mb-8 flex items-start gap-4">
          <FolderOpen
            size={28}
            className="mt-0.5 shrink-0 text-[var(--color-accent)]"
          />
          <div>
            <h1 className="text-xl font-semibold text-[var(--color-fg-primary)]">
              {case_.name}
            </h1>
            {case_.description && (
              <p className="mt-1 text-sm text-[var(--color-fg-muted)]">
                {case_.description}
              </p>
            )}
            <p className="mt-1.5 text-xs text-[var(--color-fg-muted)]">
              Created {fmtRelative(case_.created_at)} · ID{" "}
              <span className="font-mono">{case_.id}</span>
            </p>
          </div>
          <div className="ml-auto">
            <EmbeddingStatusBadge caseId={caseId!} />
          </div>
        </div>

        <div className="space-y-8">
          <SourceList caseId={caseId!} />
          <TimelineList caseId={caseId!} />
        </div>
      </div>
    </div>
  );
}
