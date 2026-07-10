/**
 * InvestigatePanel — the single right-hand investigation surface, replacing the
 * old sibling AnalysisPanel + BaselineManager. The Anomalies tab reads
 * top-to-bottom as one workflow:
 *
 *   1. Scope     — FrameBar picks the global frame (scan all / compare baseline).
 *                  In the baseline frame the BaselineSection (build/select
 *                  definitions) is inline right here, where the frame needs it.
 *   2. Detectors — DetectorAccordion: every detector with a live count; expand
 *                  one to drill its ranked findings. (No dropdown, no separate
 *                  run-all — the overview is both.)
 *   3. Dispositions — the analyst's verdicts (normal / dismissed / confirmed),
 *                  collapsible at the bottom.
 *
 * Similarity and Method stay as sibling top tabs.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { InfoHint } from "@/components/ui/InfoHint";
import { SimilarEvents } from "./SimilarEvents";
import { SemanticSearch } from "./SemanticSearch";
import { EmbeddingStatusBanner } from "./EmbeddingStatusBanner";
import { MethodologyPanel } from "./MethodologyPanel";
import { FrameBar } from "./FrameBar";
import { GLOSSARY } from "@/lib/glossary";
import { DetectorAccordion } from "./DetectorAccordion";
import { BaselineSection, NormalValuesList } from "./WindowsNormality";
import { timelinesApi } from "@/api/timelines";
import { useUiStore } from "@/stores/ui";
import { useBaselineStore } from "@/stores/baseline";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

type Tab = "anomalies" | "similar" | "methodology";

interface Props {
  caseId: string;
  timelineId: string;
  hasVectors: boolean;
  similarAnchor: Event | null;
  onClose: () => void;
  onSelectEvent: (event: Event) => void;
  onSimilarClose: () => void;
  onDrillField?: (field: string, value: string) => void;
  onComboDrill?: (pairs: [string, string][]) => void;
  onFrequencyDrill?: (field: string, value: string, start: string, end: string) => void;
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
  onAnomalyRunId?: (runId: string | undefined) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

export function InvestigatePanel({
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
  const [normalOpen, setNormalOpen] = useState(false);

  const frame = useBaselineStore((s) => s.frame);
  const setFrame = useBaselineStore((s) => s.setFrame);
  const markMode = useBaselineStore((s) => s.markMode);

  useEffect(() => {
    if (similarAnchor) setTab("similar");
  }, [similarAnchor]);

  // Marking on the histogram is only meaningful for building a baseline — pull
  // the user to the Scope area (baseline frame) so the brushed range can land.
  useEffect(() => {
    if (markMode) {
      setTab("anomalies");
      setFrame("baseline");
    }
  }, [markMode, setFrame]);

  const { data: timeline } = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId, timelineId),
    refetchInterval: 30_000,
  });
  const { data: sources } = useQuery({
    queryKey: ["timeline-sources", caseId, timelineId],
    queryFn: () => timelinesApi.listSources(caseId, timelineId),
  });

  const showBanner = !hasVectors || (timeline?.is_stale ?? false);

  // ── Resize drag (mirrors EventDetailPanel) ─────────────────────────────
  const { investigatePanelWidth, setInvestigatePanelWidth } = useUiStore();
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);
  const onDragStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragState.current = { startX: e.clientX, startWidth: investigatePanelWidth };
    },
    [investigatePanelWidth],
  );
  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragState.current) return;
      const delta = dragState.current.startX - e.clientX;
      setInvestigatePanelWidth(Math.max(320, Math.min(720, dragState.current.startWidth + delta)));
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
  }, [setInvestigatePanelWidth]);

  return (
    <div
      className="relative flex h-full shrink-0 flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-surface)]"
      style={{ width: investigatePanelWidth }}
    >
      <div
        onMouseDown={onDragStart}
        className="absolute left-0 top-0 h-full w-1 cursor-col-resize opacity-0 hover:opacity-100 hover:bg-[var(--color-accent)] transition-opacity z-10"
        style={{ marginLeft: -2 }}
      />

      {/* Header */}
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <h3 className="flex-1 text-sm font-semibold text-[var(--color-fg-primary)]">Investigate</h3>
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X size={14} />
        </Button>
      </div>

      {/* Top-level tabs */}
      <div className="flex border-b border-[var(--color-border)]">
        {(
          [
            ["anomalies", AlertTriangle, "Anomalies"],
            ["similar", Search, "Similarity"],
            ["methodology", BookOpen, "Method"],
          ] as [Tab, React.ElementType, string][]
        ).map(([id, Icon, label]) => (
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
        {tab === "anomalies" && (
          <>
            {/* First-run explainer — folds away permanently once dismissed. */}
            <div className="mb-3">
              <GuidancePanel id="investigate-anomalies" title="How anomaly scanning works">
                <ol className="list-decimal space-y-1 pl-4">
                  <li>
                    <strong>Scope</strong> — <em>Scan all events</em> compares every event
                    against the whole corpus; <em>Compare baseline</em> scores suspect
                    windows against a period you declare normal.
                  </li>
                  <li>
                    A <strong>baseline</strong> is a known-good time window; a{" "}
                    <strong>suspect window</strong> is a period you investigate against it.
                    Type UTC times or drag on the histogram to set them.
                  </li>
                  <li>
                    <strong>Detectors</strong> each flag one kind of oddity (rare values,
                    frequency spikes, …). Expand one to see its ranked findings.
                  </li>
                  <li>
                    Disposition a finding: <strong>Normal</strong> extends the
                    baseline (stops surfacing in future scans),{" "}
                    <strong>Dismiss</strong> hides it as noise without changing
                    detection, <strong>Confirm</strong> escalates it durably.
                  </li>
                </ol>
              </GuidancePanel>
            </div>

            {/* 1. Scope */}
            <FrameBar caseId={caseId} timelineId={timelineId} />
            {frame === "baseline" && (
              <div className="mb-3">
                <BaselineSection caseId={caseId} timelineId={timelineId} />
              </div>
            )}

            {/* 2. Detectors */}
            <DetectorAccordion
              caseId={caseId}
              timelineId={timelineId}
              onSelectEvent={onSelectEvent}
              onDrillField={onDrillField}
              onComboDrill={onComboDrill}
              onFrequencyDrill={onFrequencyDrill}
              onAnomalyMarkers={onAnomalyMarkers}
              onAnomalyRunId={onAnomalyRunId}
              onJumpToTime={onJumpToTime}
            />

            {/* 3. Dispositions */}
            <div className="mt-4 border-t border-[var(--color-border)] pt-3">
              <button
                onClick={() => setNormalOpen((v) => !v)}
                className="mb-2 flex w-full items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
              >
                {normalOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <ShieldCheck size={12} />
                Dispositions
                <InfoHint content={GLOSSARY.normalValues} />
              </button>
              {normalOpen && <NormalValuesList caseId={caseId} timelineId={timelineId} />}
            </div>
          </>
        )}

        {tab === "similar" && (
          <div className="space-y-5">
            {showBanner && (
              <EmbeddingStatusBanner
                status={hasVectors ? "ok" : "not_embedded"}
                timeline={timeline ?? null}
                caseId={caseId}
              />
            )}
            <SemanticSearch caseId={caseId} timelineId={timelineId} onSelectEvent={onSelectEvent} />
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
