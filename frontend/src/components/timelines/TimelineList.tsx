import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Clock, Cpu, Merge } from "lucide-react";
import { timelinesApi } from "@/api/timelines";
import { sourcesApi } from "@/api/sources";
import { useHealth } from "@/api/health";
import { fmtRelative } from "@/lib/time";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { CreateTimelineDialog } from "./CreateTimelineDialog";
import { DeleteTimelineDialog } from "./DeleteTimelineDialog";
import { EditFieldMappingsDialog } from "./EditFieldMappingsDialog";
import { EmbedWizard } from "./EmbedWizard";
import { EnrichersDialog } from "./EnrichersDialog";
import { AgentTokensDialog } from "./AgentTokensDialog";
import type { Source, Timeline } from "@/api/types";

interface Props {
  caseId: string;
}

/**
 * Embedded/total counts, computed from each source's actual `vector_count`.
 * Sources get a default embedding automatically on ingest, so this reflects
 * whether search actually works right now — not whether the curated
 * field-selection wizard has ever been run for this timeline.
 */
function EmbeddingBadge({
  tl,
  sourcesById,
}: {
  tl: Timeline;
  sourcesById: Map<string, Source>;
}) {
  if (tl.source_ids.length === 0) return null;

  const embeddedCount = tl.source_ids.filter(
    (id) => (sourcesById.get(id)?.vector_count ?? 0) > 0,
  ).length;
  const total = tl.source_ids.length;

  if (embeddedCount < total) {
    return (
      <Badge variant="muted" className="flex items-center gap-1">
        <Cpu size={9} /> Embedding {embeddedCount}/{total}
      </Badge>
    );
  }
  return (
    <Badge variant="accent" className="flex items-center gap-1">
      <Cpu size={9} /> Embedded
    </Badge>
  );
}

function TimelineRow({
  caseId,
  tl,
  sourcesById,
  mcpEnabled,
}: {
  caseId: string;
  tl: Timeline;
  sourcesById: Map<string, Source>;
  mcpEnabled: boolean;
}) {
  return (
    <div
      data-tour={tl.is_default ? "all-sources-timeline" : undefined}
      className="group flex items-center gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-5 py-3 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-elevated)] transition-base"
    >
      <Clock size={16} className="shrink-0 text-[var(--color-info)] opacity-70" />
      <Link
        to={`/cases/${caseId}/timelines/${tl.id}`}
        className="flex-1 min-w-0"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-[var(--color-fg-primary)] truncate">
            {tl.name}
          </span>
          {tl.is_default && <Badge variant="accent">default</Badge>}
          {tl.field_mappings && Object.keys(tl.field_mappings).length > 0 && (
            <span
              title={Object.entries(tl.field_mappings)
                .map(([name, raws]) => `${name} ← ${raws.join(", ")}`)
                .join("\n")}
            >
              <Badge variant="muted" className="flex items-center gap-1">
                <Merge size={9} /> {Object.keys(tl.field_mappings).length} mapped
              </Badge>
            </span>
          )}
          <EmbeddingBadge tl={tl} sourcesById={sourcesById} />
        </div>
        <div className="mt-1 flex items-center gap-3 text-xs text-[var(--color-fg-muted)]">
          <span>{tl.source_ids.length} source{tl.source_ids.length !== 1 ? "s" : ""}</span>
          {tl.is_stale && (
            <span className="text-[var(--color-warning)]">
              New sources aren't covered by the curated embedding — re-embed
              to include them
            </span>
          )}
          {!tl.is_stale && tl.embedded_at && (
            <span>Curated embed applied {fmtRelative(tl.embedded_at)}</span>
          )}
          <span>Updated {fmtRelative(tl.updated_at)}</span>
        </div>
      </Link>
      <div className="flex items-center gap-2 shrink-0">
        {tl.source_ids.length > 0 && (
          <EditFieldMappingsDialog caseId={caseId} timeline={tl} />
        )}
        {tl.source_ids.length > 0 && (
          <EmbedWizard caseId={caseId} timeline={tl} iconTrigger />
        )}
        {tl.source_ids.length > 0 && (
          <EnrichersDialog caseId={caseId} timeline={tl} />
        )}
        {mcpEnabled && <AgentTokensDialog caseId={caseId} timeline={tl} />}
        {!tl.is_default && <DeleteTimelineDialog caseId={caseId} timeline={tl} />}
      </div>
    </div>
  );
}

export function TimelineList({ caseId }: Props) {
  const { data: timelines, isLoading, error } = useQuery({
    queryKey: ["timelines", caseId],
    queryFn: () => timelinesApi.list(caseId),
    refetchInterval: 15_000,
  });
  // Drives the per-row embedding badge, which reflects real vector_count
  // rather than the curated-wizard flag — polled while auto-embed jobs run.
  const { data: sources } = useQuery({
    queryKey: ["sources", caseId],
    queryFn: () => sourcesApi.list(caseId),
    refetchInterval: 15_000,
  });
  const sourcesById = new Map((sources ?? []).map((s) => [s.id, s]));
  const { data: health } = useHealth();

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
          Timelines
        </h2>
        <CreateTimelineDialog caseId={caseId} />
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
      {timelines && timelines.length === 0 && (
        <p className="py-8 text-center text-sm text-[var(--color-fg-muted)]">
          No timelines yet. Create one to group sources.
        </p>
      )}
      {timelines && (
        <div className="space-y-2">
          {timelines.map((tl) => (
            <TimelineRow
              key={tl.id}
              caseId={caseId}
              tl={tl}
              sourcesById={sourcesById}
              mcpEnabled={health?.mcp_enabled ?? false}
            />
          ))}
        </div>
      )}
    </div>
  );
}
