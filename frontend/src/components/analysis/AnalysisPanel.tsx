import { useState, useEffect } from "react";
import { X, AlertTriangle, Search, BookOpen, Hash, Activity } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { ValueNoveltyView } from "./ValueNoveltyView";
import { FrequencyView } from "./FrequencyView";
import { SimilarEvents } from "./SimilarEvents";
import { SemanticSearch } from "./SemanticSearch";
import { EmbeddingStatusBanner } from "./EmbeddingStatusBanner";
import { MethodologyPanel } from "./MethodologyPanel";
import { timelinesApi } from "@/api/timelines";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

type Tab = "anomalies" | "similar" | "methodology";
type AnomalySubTab = "novelty" | "frequency";

interface Props {
  caseId: string;
  timelineId: string;
  hasVectors: boolean;
  similarAnchor: Event | null;
  onClose: () => void;
  onSelectEvent: (event: Event) => void;
  onSimilarClose: () => void;
  /** Passed to ValueNoveltyView so clicking a field drills into filtered events. */
  onDrillField?: (field: string, value: string) => void;
  /** Passed to FrequencyView — narrows the time range and the series field=value. */
  onFrequencyDrill?: (field: string, value: string, start: string, end: string) => void;
  /** Called with the active anomaly tab's findings — feeds the histogram overlay and event grid. */
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
  /** Scrolls the main grid to a finding's timestamp, clearing filters first. */
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

export function AnalysisPanel({
  caseId,
  timelineId,
  hasVectors,
  similarAnchor,
  onClose,
  onSelectEvent,
  onSimilarClose,
  onDrillField,
  onFrequencyDrill,
  onAnomalyMarkers,
  onJumpToTime,
}: Props) {
  const [tab, setTab] = useState<Tab>(similarAnchor ? "similar" : "anomalies");
  const [anomalySubTab, setAnomalySubTab] = useState<AnomalySubTab>("novelty");

  // Auto-switch to the similar tab when the anchor event is set.
  useEffect(() => {
    if (similarAnchor) setTab("similar");
  }, [similarAnchor]);

  // Load the timeline to drive stale banners.
  const { data: timeline } = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId, timelineId),
    refetchInterval: 30_000,
  });

  const { data: sources } = useQuery({
    queryKey: ["timeline-sources", caseId, timelineId],
    queryFn: () => timelinesApi.listSources(caseId, timelineId),
  });

  // Show the similarity banner when: not embedded at all, OR embedded-but-stale.
  const showBanner = !hasVectors || (timeline?.is_stale ?? false);

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

      {/* Top-level tabs */}
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

      {/* Anomaly sub-tabs (only visible on the anomalies tab) */}
      {tab === "anomalies" && (
        <div className="flex gap-px border-b border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5">
          <button
            className={cn(
              "flex flex-1 items-center justify-center gap-1 rounded py-1 text-[11px] font-medium transition-colors",
              anomalySubTab === "novelty"
                ? "bg-[var(--color-bg-elevated)] text-[var(--color-fg-primary)]"
                : "text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
            )}
            onClick={() => setAnomalySubTab("novelty")}
          >
            <Hash size={11} />
            Rare values
          </button>
          <button
            className={cn(
              "flex flex-1 items-center justify-center gap-1 rounded py-1 text-[11px] font-medium transition-colors",
              anomalySubTab === "frequency"
                ? "bg-[var(--color-bg-elevated)] text-[var(--color-fg-primary)]"
                : "text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
            )}
            onClick={() => setAnomalySubTab("frequency")}
          >
            <Activity size={11} />
            Frequency
          </button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4">
        {/* Similarity banner — only relevant for the similarity tab */}
        {tab === "similar" && showBanner && (
          <div className="mb-4">
            <EmbeddingStatusBanner
              status={hasVectors ? "ok" : "not_embedded"}
              timeline={timeline ?? null}
              caseId={caseId}
            />
          </div>
        )}

        {tab === "anomalies" && anomalySubTab === "novelty" && (
          <ValueNoveltyView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onDrillField={onDrillField}
            onFindingsChange={onAnomalyMarkers}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "frequency" && (
          <FrequencyView
            caseId={caseId}
            timelineId={timelineId}
            onDrillField={onFrequencyDrill}
            onFindingsChange={onAnomalyMarkers}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "similar" && (
          <div className="space-y-5">
            <SemanticSearch
              caseId={caseId}
              timelineId={timelineId}
              onSelectEvent={onSelectEvent}
            />
            <div className="border-t border-[var(--color-border)] pt-4">
              {similarAnchor ? (
                <SimilarEvents
                  caseId={caseId}
                  timelineId={timelineId}
                  anchorEvent={similarAnchor}
                  onClose={onSimilarClose}
                  onSelectEvent={onSelectEvent}
                />
              ) : (
                <p className="text-xs text-[var(--color-fg-muted)]">
                  Click the search icon on any event row to find similar events.
                </p>
              )}
            </div>
          </div>
        )}

        {tab === "methodology" && (
          <MethodologyPanel
            caseId={caseId}
            timelineId={timelineId}
            sources={sources ?? []}
          />
        )}
      </div>
    </div>
  );
}
