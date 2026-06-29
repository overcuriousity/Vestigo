import { useState } from "react";
import { X, AlertTriangle, Search, BookOpen } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { AnomaliesList } from "./AnomaliesList";
import { SimilarEvents } from "./SimilarEvents";
import { EmbeddingStatusBanner } from "./EmbeddingStatusBanner";
import { MethodologyPanel } from "./MethodologyPanel";
import { cn } from "@/lib/cn";
import type { Event, Timeline } from "@/api/types";

type Tab = "anomalies" | "similar" | "methodology";

interface Props {
  caseId: string;
  timelineId: string;
  timeline: Timeline;
  hasVectors: boolean;
  similarAnchor: Event | null;
  onClose: () => void;
  onSelectEvent: (event: Event) => void;
  onSimilarClose: () => void;
}

export function AnalysisPanel({
  caseId,
  timelineId,
  timeline,
  hasVectors,
  similarAnchor,
  onClose,
  onSelectEvent,
  onSimilarClose,
}: Props) {
  const [tab, setTab] = useState<Tab>(similarAnchor ? "similar" : "anomalies");

  // Auto-switch to similar when anchor is set
  if (similarAnchor && tab !== "similar") setTab("similar");

  return (
    <div className="flex h-full w-80 shrink-0 flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-surface)]">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <h3 className="flex-1 text-sm font-semibold text-[var(--color-fg-primary)]">
          Analysis
        </h3>
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X size={14} />
        </Button>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-[var(--color-border)]">
        {([
          ["anomalies", AlertTriangle, "Anomalies"],
          ["similar", Search, "Similarity"],
          ["methodology", BookOpen, "Method"],
        ] as [Tab, React.ElementType, string][]).map(([id, Icon, label]) => (
          <button
            key={id}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 py-2.5 text-xs font-medium transition-base border-b-2",
              tab === id
                ? "border-[var(--color-accent)] text-[var(--color-accent)]"
                : "border-transparent text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
            )}
            onClick={() => setTab(id)}
          >
            <Icon size={12} />
            {label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {!hasVectors && (
          <div className="mb-4">
            <EmbeddingStatusBanner status="not_embedded" timeline={timeline} />
          </div>
        )}

        {tab === "anomalies" && (
          <AnomaliesList
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
          />
        )}

        {tab === "similar" && similarAnchor ? (
          <SimilarEvents
            caseId={caseId}
            timelineId={timelineId}
            anchorEvent={similarAnchor}
            onClose={onSimilarClose}
            onSelectEvent={onSelectEvent}
          />
        ) : tab === "similar" ? (
          <p className="text-xs text-[var(--color-fg-muted)]">
            Click the search icon on any event row to find similar events.
          </p>
        ) : null}

        {tab === "methodology" && (
          <MethodologyPanel
            caseId={caseId}
            timelineId={timelineId}
            timeline={timeline}
          />
        )}
      </div>
    </div>
  );
}
