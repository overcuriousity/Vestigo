import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Database, Cpu, Download, Trash2 } from "lucide-react";
import { sourcesApi } from "@/api/sources";
import { fmtRelative } from "@/lib/time";
import { fmtNum, fmtBytes, fmtParserName, truncateHash } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { Button } from "@/components/ui/Button";
import { UploadDialog } from "@/components/timelines/UploadDialog";
import type { Source } from "@/api/types";

interface Props {
  caseId: string;
}

function SourceRow({ caseId, source }: { caseId: string; source: Source }) {
  return (
    <div className="group flex items-center gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-5 py-3 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-elevated)] transition-base">
      <FileText size={16} className="shrink-0 text-[var(--color-accent)] opacity-70" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-[var(--color-fg-primary)] truncate">
            {source.name}
          </span>
          {source.parser && (
            <Badge variant="muted">{fmtParserName(source.parser)}</Badge>
          )}
          {source.status !== "ready" && (
            <Badge variant="accent">
              <span className="flex items-center gap-1">
                <Spinner size={10} /> Ingesting
              </span>
            </Badge>
          )}
        </div>
        <div className="mt-1 flex items-center gap-3 text-xs text-[var(--color-fg-muted)]">
          <span className="flex items-center gap-1">
            <Database size={11} /> {fmtNum(source.event_count)} events
          </span>
          {source.vector_count > 0 && (
            <span className="flex items-center gap-1">
              <Cpu size={11} /> {fmtNum(source.vector_count)} vectors
            </span>
          )}
          <span>{fmtBytes(source.size_bytes)}</span>
          <span className="font-mono" title={source.file_hash}>
            {truncateHash(source.file_hash, 12)}
          </span>
          <span>Updated {fmtRelative(source.updated_at)}</span>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Button variant="ghost" size="icon" asChild title="Download original file">
          <a href={sourcesApi.downloadUrl(caseId, source.id)} download>
            <Download size={14} />
          </a>
        </Button>
        <DeleteSourceButton caseId={caseId} source={source} />
      </div>
    </div>
  );
}

function DeleteSourceButton({ caseId, source }: { caseId: string; source: Source }) {
  const qc = useQueryClient();
  const { mutate, isPending } = useMutation({
    mutationFn: () => sourcesApi.delete(caseId, source.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sources", caseId] });
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
    },
  });

  return (
    <Button
      variant="ghost"
      size="icon"
      title="Delete source"
      disabled={isPending}
      onClick={() => mutate()}
    >
      <Trash2 size={14} className="text-[var(--color-danger)]" />
    </Button>
  );
}

export function SourceList({ caseId }: Props) {
  const { data: sources, isLoading, error } = useQuery({
    queryKey: ["sources", caseId],
    queryFn: () => sourcesApi.list(caseId),
    refetchInterval: 15_000,
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
          Sources
        </h2>
        <UploadDialog caseId={caseId} />
      </div>
      {isLoading && (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      )}
      {error && (
        <p className="text-sm text-[var(--color-danger)]">
          {(error as Error).message}
        </p>
      )}
      {sources && sources.length === 0 && (
        <p className="py-8 text-center text-sm text-[var(--color-fg-muted)]">
          No sources yet. Upload a log file to get started.
        </p>
      )}
      {sources && (
        <div className="space-y-2">
          {sources.map((source) => (
            <SourceRow key={source.id} caseId={caseId} source={source} />
          ))}
        </div>
      )}
    </div>
  );
}
