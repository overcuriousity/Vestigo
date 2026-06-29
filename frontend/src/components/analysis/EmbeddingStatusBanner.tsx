import { Cpu } from "lucide-react";
import { EmbedWizard } from "@/components/timelines/EmbedWizard";
import type { Timeline } from "@/api/types";

interface Props {
  status: "ok" | "not_embedded";
  /** Pass timeline so the wizard can pre-populate and show model info. */
  timeline: Timeline;
}

export function EmbeddingStatusBanner({ status, timeline }: Props) {
  if (status === "ok") return null;

  return (
    <div className="flex items-center gap-3 rounded border border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 px-3 py-2.5 text-xs">
      <Cpu size={14} className="text-[var(--color-warning)] shrink-0" />
      <p className="flex-1 text-[var(--color-fg-secondary)]">
        No embeddings found. Generate embeddings to enable similarity search and
        anomaly detection.
      </p>
      <EmbedWizard
        caseId={timeline.case_id}
        timelineId={timeline.id}
        timeline={timeline}
      />
    </div>
  );
}
