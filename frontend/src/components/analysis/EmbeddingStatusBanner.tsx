import { Cpu, AlertTriangle } from "lucide-react";
import { EmbedWizard } from "@/components/timelines/EmbedWizard";
import type { Timeline } from "@/api/types";

interface Props {
  status: "ok" | "not_embedded";
  /** The timeline, used to launch the embedding wizard. */
  timeline: Timeline | null;
  caseId: string;
}

export function EmbeddingStatusBanner({ status, timeline, caseId }: Props) {
  if (!timeline) return null;

  // Stale: embedded but source set has changed.
  if (timeline.is_stale) {
    return (
      <div className="flex items-center gap-3 rounded border border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 px-3 py-2.5 text-xs">
        <AlertTriangle size={14} className="text-[var(--color-warning)] shrink-0" />
        <p className="flex-1 text-[var(--color-fg-secondary)]">
          Sources have changed since the last embedding run — similarity
          search results may be incomplete. Re-embed to include all sources.
        </p>
        <EmbedWizard caseId={caseId} timeline={timeline} />
      </div>
    );
  }

  // Not embedded at all.
  if (status === "not_embedded") {
    return (
      <div className="flex items-center gap-3 rounded border border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 px-3 py-2.5 text-xs">
        <Cpu size={14} className="text-[var(--color-warning)] shrink-0" />
        <p className="flex-1 text-[var(--color-fg-secondary)]">
          No embeddings found for this timeline. Generate embeddings to enable
          similarity search.
        </p>
        <EmbedWizard caseId={caseId} timeline={timeline} />
      </div>
    );
  }

  return null;
}
