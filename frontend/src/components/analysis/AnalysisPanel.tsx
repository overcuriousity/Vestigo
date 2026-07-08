import { useState, useEffect, useCallback, useRef } from "react";
import {
  X,
  AlertTriangle,
  Search,
  BookOpen,
  Hash,
  Activity,
  Rewind,
  Layers,
  Ruler,
  Shuffle,
  Type,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/Select";
import { ValueNoveltyView } from "./ValueNoveltyView";
import { ComboNoveltyView } from "./ComboNoveltyView";
import { FrequencyView } from "./FrequencyView";
import { OrderViolationsView } from "./OrderViolationsView";
import { NumericRangeView } from "./NumericRangeView";
import { CharsetNoveltyView } from "./CharsetNoveltyView";
import { EntropyView } from "./EntropyView";
import { SimilarEvents } from "./SimilarEvents";
import { SemanticSearch } from "./SemanticSearch";
import { EmbeddingStatusBanner } from "./EmbeddingStatusBanner";
import { MethodologyPanel } from "./MethodologyPanel";
import { DetectorSummaryStrip } from "./DetectorSummaryStrip";
import { timelinesApi } from "@/api/timelines";
import { useUiStore } from "@/stores/ui";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

type Tab = "anomalies" | "similar" | "methodology";
type AnomalySubTab = "novelty" | "combo" | "frequency" | "order" | "range" | "charset" | "entropy";

/**
 * Detector registry for the anomaly dropdown. Flat sub-tab buttons stopped
 * scaling past two detectors in the 320px panel — new detectors register
 * here (id, icon, name, one-line description) and add a render branch below.
 */
const DETECTORS: {
  id: AnomalySubTab;
  icon: React.ElementType;
  label: string;
  description: string;
}[] = [
  {
    id: "novelty",
    icon: Hash,
    label: "Rare values",
    description: "Rare or first-seen field values",
  },
  {
    id: "combo",
    icon: Layers,
    label: "Value combos",
    description: "Rare combinations of two or more fields",
  },
  {
    id: "frequency",
    icon: Activity,
    label: "Frequency",
    description: "Event-count spikes and silences per series",
  },
  {
    id: "order",
    icon: Rewind,
    label: "Timestamp order",
    description: "Timestamps running backwards in record order",
  },
  {
    id: "range",
    icon: Ruler,
    label: "Numeric range",
    description: "Numeric values outside a learned band",
  },
  {
    id: "charset",
    icon: Type,
    label: "Charset novelty",
    description: "Values containing never-seen characters",
  },
  {
    id: "entropy",
    icon: Shuffle,
    label: "Entropy outliers",
    description: "Random-looking or degenerate strings",
  },
];

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
  /** Passed to ComboNoveltyView — applies every (field, value) pair as a conjunction. */
  onComboDrill?: (pairs: [string, string][]) => void;
  /** Passed to FrequencyView — narrows the time range and the series field=value. */
  onFrequencyDrill?: (field: string, value: string, start: string, end: string) => void;
  /** Called with the active anomaly tab's findings — feeds the histogram overlay and event grid. */
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
  /** Called with the active anomaly tab's persisted run_id, so the grid can filter to it. */
  onAnomalyRunId?: (runId: string | undefined) => void;
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
  onComboDrill,
  onFrequencyDrill,
  onAnomalyMarkers,
  onAnomalyRunId,
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

  // ── Resize drag (mirrors EventDetailPanel) ─────────────────────────────
  const { analysisPanelWidth, setAnalysisPanelWidth } = useUiStore();
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragState.current = { startX: e.clientX, startWidth: analysisPanelWidth };
  }, [analysisPanelWidth]);

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragState.current) return;
      const delta = dragState.current.startX - e.clientX;
      const newWidth = Math.max(320, Math.min(720, dragState.current.startWidth + delta));
      setAnalysisPanelWidth(newWidth);
    }
    function onMouseUp() {
      dragState.current = null;
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [setAnalysisPanelWidth]);

  return (
    <div
      className="relative flex h-full shrink-0 flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-surface)]"
      style={{ width: analysisPanelWidth }}
    >
      {/* Drag handle — left edge */}
      <div
        onMouseDown={onDragStart}
        className="absolute left-0 top-0 h-full w-1 cursor-col-resize opacity-0 hover:opacity-100 hover:bg-[var(--color-accent)] transition-opacity z-10"
        style={{ marginLeft: -2 }}
      />
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

      {/* Detector selector (only visible on the anomalies tab) */}
      {tab === "anomalies" && (
        <div className="border-b border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5">
          <Select
            value={anomalySubTab}
            onValueChange={(v) => setAnomalySubTab(v as AnomalySubTab)}
          >
            <SelectTrigger className="h-7 px-2 text-xs" aria-label="Detector">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DETECTORS.map((d) => (
                <SelectItem key={d.id} value={d.id} className="h-auto py-1.5">
                  <span className="flex items-center gap-1.5">
                    <d.icon size={11} className="shrink-0 text-[var(--color-fg-muted)]" />
                    <span className="flex flex-col items-start leading-tight">
                      <span className="text-xs font-medium">{d.label}</span>
                      <span className="text-[10px] text-[var(--color-fg-muted)]">
                        {d.description}
                      </span>
                    </span>
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
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

        {tab === "anomalies" && (
          <DetectorSummaryStrip
            caseId={caseId}
            timelineId={timelineId}
            onSelect={setAnomalySubTab}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "novelty" && (
          <ValueNoveltyView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onDrillField={onDrillField}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "combo" && (
          <ComboNoveltyView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onComboDrill={onComboDrill}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "frequency" && (
          <FrequencyView
            caseId={caseId}
            timelineId={timelineId}
            onDrillField={onFrequencyDrill}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "order" && (
          <OrderViolationsView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "range" && (
          <NumericRangeView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onDrillField={onDrillField}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "charset" && (
          <CharsetNoveltyView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onDrillField={onDrillField}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
            onJumpToTime={onJumpToTime}
          />
        )}

        {tab === "anomalies" && anomalySubTab === "entropy" && (
          <EntropyView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onDrillField={onDrillField}
            onFindingsChange={onAnomalyMarkers}
            onRunIdChange={onAnomalyRunId}
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
            timeline={timeline}
            sources={sources ?? []}
          />
        )}
      </div>
    </div>
  );
}
