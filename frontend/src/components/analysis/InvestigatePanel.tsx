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
  Sigma,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useCapabilities } from "@/api/health";
import { Button } from "@/components/ui/Button";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { InfoHint } from "@/components/ui/InfoHint";
import { SimilarEvents } from "./SimilarEvents";
import { SemanticSearch } from "./SemanticSearch";
import { EmbeddingStatusBanner } from "./EmbeddingStatusBanner";
import { MethodologyPanel } from "./MethodologyPanel";
import { FrameBar } from "./FrameBar";
import { AnalysisEmptyState } from "./detector-shared";
import { GLOSSARY } from "@/lib/glossary";
import { DetectorAccordion } from "./DetectorAccordion";
import { FindingsFeed } from "./FindingsFeed";
import { PatternsView } from "./PatternsView";
import { TemplatesView } from "./TemplatesView";
import { SigmaPanel } from "./SigmaPanel";
import { BaselineBuilderDrawer } from "./BaselineBuilderDrawer";
import { NormalValuesList } from "./WindowsNormality";
import { TriageBurndown } from "./TriageBurndown";
import { timelinesApi } from "@/api/timelines";
import { dispositionsApi } from "@/api/dispositions";
import { useUiStore } from "@/stores/ui";
import { useBaselineStore } from "@/stores/baseline";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

type Tab = "anomalies" | "patterns" | "sigma" | "similar" | "methodology";

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
  onTagFilter?: (tag: string) => void;
}

/**
 * The one place that says "there is nothing here yet", for whichever analysis tab
 * is open. Every tab in this panel reads the same timeline, so the answer never
 * differs between them — saying it per tab was how thirteen detector views ended
 * up each asserting it separately, in wording that drifted.
 *
 * The two arms are genuinely different situations: mid-ingest resolves itself and
 * needs only somewhere to watch, while an empty case needs an action and a link
 * to perform it.
 */
function NoEventsState({ caseId, stillIngesting }: { caseId: string; stillIngesting: boolean }) {
  return (
    <AnalysisEmptyState
      hint={
        stillIngesting ? (
          "The job tray in the top bar shows progress. Events become searchable as they land."
        ) : (
          <>
            Upload a log file on the{" "}
            <Link to={`/cases/${caseId}`} className="text-[var(--color-accent)] hover:underline">
              case overview
            </Link>{" "}
            to begin — every tab here works over a timeline's events, and this one has none
            yet.
          </>
        )
      }
    >
      {stillIngesting
        ? "This timeline's sources are still ingesting."
        : "No events in this timeline yet."}
    </AnalysisEmptyState>
  );
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
  onTagFilter,
}: Props) {
  const [tab, setTab] = useState<Tab>(similarAnchor ? "similar" : "anomalies");
  const [normalOpen, setNormalOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [patternsSubTab, setPatternsSubTab] = useState<"sequences" | "templates">("sequences");

  const setFrame = useBaselineStore((s) => s.setFrame);
  const markMode = useBaselineStore((s) => s.markMode);
  const pendingRange = useBaselineStore((s) => s.pendingRange);
  const setBaselineBuilderOpen = useUiStore((s) => s.setBaselineBuilderOpen);

  // Similarity is embedding-backed: without embeddings configured the tab is
  // absent, not disabled — the same treatment the agent gets when no LLM
  // endpoint is set (core/capabilities.py).
  const embeddingsAvailable = useCapabilities().embeddings;

  // Never render the similarity view without a tab to leave it by: the initial
  // state can land on "similar" (mounted with an anchor before health answers)
  // and the capability can flip while it is open. Falling back here rather than
  // rewriting `tab` keeps the choice: re-enabling embeddings restores it.
  const activeTab: Tab = tab === "similar" && !embeddingsAvailable ? "anomalies" : tab;

  useEffect(() => {
    if (similarAnchor && embeddingsAvailable) setTab("similar");
  }, [similarAnchor, embeddingsAvailable]);

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
    // The "still ingesting" empty state below promises events appear as they
    // land, so it has to notice when they do. ExplorerPage polls this same key
    // on the same terms; stating it here too means the promise does not quietly
    // depend on the panel's host still doing it.
    refetchInterval: (query) =>
      query.state.data?.some((s) => s.status !== "ready") ? 4000 : false,
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

  // Whether there is anything to analyse at all. Only the panel can answer this
  // — a detector view sees its own empty response and cannot tell "nothing was
  // ingested" from "this method found nothing", which is why thirteen views used
  // to each claim "No events ingested yet". Said once, here, with somewhere to go.
  // `status: "ingesting"` sources are excluded from timeline queries until ready,
  // so a mid-ingest timeline legitimately scans zero events and must say so
  // rather than read as empty.
  const readyEventCount = (sources ?? [])
    .filter((s) => s.status === "ready")
    .reduce((n, s) => n + s.event_count, 0);
  const stillIngesting = (sources ?? []).some((s) => s.status === "ingesting");
  const nothingToAnalyse = sources !== undefined && readyEventCount === 0;

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
            ["sigma", Sigma, "Sigma"],
            ...(embeddingsAvailable
              ? ([["similar", Search, "Similarity"]] as [Tab, React.ElementType, string][])
              : []),
            ["methodology", BookOpen, "Method"],
          ] as [Tab, React.ElementType, string][]
        ).map(([id, Icon, label]) => (
          <button
            key={id}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 py-2.5 text-xs font-medium transition-base border-b-2",
              activeTab === id
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
        {activeTab === "anomalies" && (
          <>
            {/* First-run explainer — collapsible, restorable from Settings. */}
            <div className="mb-3">
              <GuidancePanel id="investigate-anomalies" />
            </div>

            {nothingToAnalyse ? (
              <NoEventsState caseId={caseId} stillIngesting={stillIngesting} />
            ) : (
              <>
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
          </>
        )}

        {/* Guidance still renders on an empty timeline: "what would this tab do
            for me" is most worth answering before there is data, and the
            Anomalies tab above keeps its explainer for the same reason.
            `PatternsView`/`TemplatesView` normally each render their own, but
            neither mounts here — and with no events the sub-tab choice is not
            offered either, so the sequences explainer stands for the tab. */}
        {activeTab === "patterns" && nothingToAnalyse && (
          <>
            <div className="mb-3">
              <GuidancePanel id="investigate-patterns" />
            </div>
            <NoEventsState caseId={caseId} stillIngesting={stillIngesting} />
          </>
        )}

        {activeTab === "patterns" && !nothingToAnalyse && (
          <div className="space-y-3">
            <div className="flex gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-0.5 text-xs">
              {(
                [
                  ["sequences", "Sequences"],
                  ["templates", "Templates"],
                ] as [typeof patternsSubTab, string][]
              ).map(([id, label]) => (
                <button
                  key={id}
                  className={cn(
                    "flex-1 rounded px-2 py-1 font-medium transition-base",
                    patternsSubTab === id
                      ? "bg-[var(--color-bg-surface)] text-[var(--color-accent)]"
                      : "text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
                  )}
                  onClick={() => setPatternsSubTab(id)}
                >
                  {label}
                </button>
              ))}
            </div>
            {patternsSubTab === "sequences" && (
              <PatternsView
                caseId={caseId}
                timelineId={timelineId}
                onSelectEvent={onSelectEvent}
                onDrillField={onDrillField}
                onJumpToTime={onJumpToTime}
              />
            )}
            {patternsSubTab === "templates" && (
              <TemplatesView caseId={caseId} timelineId={timelineId} onDrillField={onDrillField} />
            )}
          </div>
        )}

        {/* Same treatment as Patterns, and for a sharper reason: SigmaPanel's
            Run button would happily scan an empty timeline and report zero
            matches, which reads as "these rules cleared you" rather than "there
            was nothing to match against". */}
        {activeTab === "sigma" && nothingToAnalyse && (
          <>
            <div className="mb-3">
              <GuidancePanel id="investigate-sigma" />
            </div>
            <NoEventsState caseId={caseId} stillIngesting={stillIngesting} />
          </>
        )}

        {activeTab === "sigma" && !nothingToAnalyse && (
          <SigmaPanel caseId={caseId} timelineId={timelineId} onTagFilter={onTagFilter} />
        )}

        {activeTab === "similar" && (
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

        {activeTab === "methodology" && (
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
