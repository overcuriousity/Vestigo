/**
 * InvestigatePanel — the single right-hand investigation surface, replacing the
 * old sibling AnalysisPanel + BaselineManager. The Anomalies tab reads
 * top-to-bottom as one workflow:
 *
 *   1. Scope    — FrameBar picks the global frame (scan all / compare baseline);
 *                 the dense baseline-builder form lives in an overlay drawer
 *                 ("Manage baselines" / histogram mark-mode opens it).
 *   2. Findings — FindingsFeed: one cross-detector ranked inbox built from the
 *                 detector sweep, detector chips as filters.
 *   3. Advanced — the per-detector accordion (field pickers + tuning knobs),
 *                 grouped in three categories, collapsed by default.
 *   4. Dispositions — the analyst's verdicts, collapsible at the bottom.
 *
 * Patterns (repeating-sequence mining + routine suppression), Similarity and
 * Method are sibling top tabs.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Repeat,
  Search,
  ShieldCheck,
  SlidersHorizontal,
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
import { FindingsFeed } from "./FindingsFeed";
import { PatternsView } from "./PatternsView";
import { BaselineBuilderDrawer } from "./BaselineBuilderDrawer";
import { NormalValuesList } from "./WindowsNormality";
import { TriageBurndown } from "./TriageBurndown";
import { timelinesApi } from "@/api/timelines";
import { dispositionsApi } from "@/api/dispositions";
import { useUiStore } from "@/stores/ui";
import { useBaselineStore } from "@/stores/baseline";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

type Tab = "anomalies" | "patterns" | "similar" | "methodology";

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
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const setFrame = useBaselineStore((s) => s.setFrame);
  const markMode = useBaselineStore((s) => s.markMode);
  const pendingRange = useBaselineStore((s) => s.pendingRange);
  const setBaselineBuilderOpen = useUiStore((s) => s.setBaselineBuilderOpen);

  useEffect(() => {
    if (similarAnchor) setTab("similar");
  }, [similarAnchor]);

  // Marking on the histogram is only meaningful for building a baseline — pull
  // the user to the baseline frame. The builder drawer is deliberately NOT
  // opened here: it would overlay the histogram and make the drag impossible.
  useEffect(() => {
    if (markMode) {
      setTab("anomalies");
      setFrame("baseline");
    }
  }, [markMode, setFrame]);

  // A brushed range landed — now open the drawer so it shows up in the window
  // editor (BaselineSection consumes pendingRange on mount).
  useEffect(() => {
    if (pendingRange) setBaselineBuilderOpen(true);
  }, [pendingRange, setBaselineBuilderOpen]);

  const { data: timeline } = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId, timelineId),
    refetchInterval: 30_000,
  });
  const { data: sources } = useQuery({
    queryKey: ["timeline-sources", caseId, timelineId],
    queryFn: () => timelinesApi.listSources(caseId, timelineId),
  });

  // Verdict counts for the Dispositions header — the persistent "my triage
  // work so far" signal, visible even while the section is collapsed. The
  // ["dispositions", …] prefix is invalidated by useDisposition on every
  // verdict, so these tick up immediately.
  const { data: dispositionData } = useQuery({
    queryKey: ["dispositions", caseId, timelineId, "all"],
    queryFn: () => dispositionsApi.list(caseId, timelineId),
  });
  const verdictCounts = (() => {
    const counts = { normal: 0, dismissed: 0, confirmed: 0, routine: 0 };
    for (const d of dispositionData?.dispositions ?? []) counts[d.kind] += 1;
    return counts;
  })();
  const verdictSummary = (["normal", "dismissed", "confirmed", "routine"] as const)
    .filter((k) => verdictCounts[k] > 0)
    .map((k) => `${verdictCounts[k]} ${k}`)
    .join(" · ");

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
            ["patterns", Repeat, "Patterns"],
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
                    windows against a period you declare normal (build one via{" "}
                    <em>Manage baselines</em> or by dragging on the histogram).
                  </li>
                  <li>
                    <strong>Findings</strong> — every detector's best findings in one
                    ranked feed. Chips filter by detector;{" "}
                    <strong>Advanced</strong> opens a detector's full view with field
                    selection and tuning.
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

            {/* 2. Unified findings feed. It publishes the histogram/grid
                anomaly markers by default; while Advanced is open the expanded
                detector view owns the markers instead (exactly one publisher,
                so the two never fight over the shared marker state). */}
            <FindingsFeed
              caseId={caseId}
              timelineId={timelineId}
              onSelectEvent={onSelectEvent}
              onJumpToTime={onJumpToTime}
              onAnomalyMarkers={advancedOpen ? undefined : onAnomalyMarkers}
            />

            {/* 3. Advanced: the per-detector accordion, collapsed by default */}
            <div className="mt-4 border-t border-[var(--color-border)] pt-3">
              <button
                onClick={() => setAdvancedOpen((v) => !v)}
                className="mb-2 flex w-full items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
              >
                {advancedOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <SlidersHorizontal size={12} />
                Advanced — per-detector views
              </button>
              {advancedOpen && (
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
              )}
            </div>

            {/* 4. Dispositions */}
            <div className="mt-4 border-t border-[var(--color-border)] pt-3">
              <button
                onClick={() => setNormalOpen((v) => !v)}
                className="mb-2 flex w-full items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
              >
                {normalOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <ShieldCheck size={12} />
                Dispositions
                <InfoHint content={GLOSSARY.normalValues} />
                {verdictSummary && (
                  <span className="ml-auto font-mono text-[10px] font-normal normal-case tracking-normal text-[var(--color-fg-muted)]">
                    {verdictSummary}
                  </span>
                )}
              </button>
              {normalOpen && (
                <div className="space-y-3">
                  <TriageBurndown caseId={caseId} timelineId={timelineId} />
                  <NormalValuesList caseId={caseId} timelineId={timelineId} />
                </div>
              )}
            </div>
          </>
        )}

        {tab === "patterns" && (
          <PatternsView
            caseId={caseId}
            timelineId={timelineId}
            onSelectEvent={onSelectEvent}
            onDrillField={onDrillField}
            onJumpToTime={onJumpToTime}
          />
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

      {/* Baseline builder — overlay drawer, opened from FrameBar / mark-mode. */}
      <BaselineBuilderDrawer caseId={caseId} timelineId={timelineId} />
    </div>
  );
}
