/**
 * InvestigateRail — the only fixed-width surface in the Investigate flow.
 *
 * Holds the findings inbox and nothing else. Every detail view (a finding, a
 * method, the Tools sheet) renders into the overlay owned by ExplorerPage, so
 * this rail's width is the *entire* horizontal budget Investigate spends —
 * which is what makes the old overflow structurally impossible rather than
 * merely tuned away.
 *
 * Findings group by evidence weight rather than by the subsystem that produced
 * them: a Sigma hit names a technique somebody asserted is malicious, a rare
 * value says something is unusual here. The rail has to say which kind of
 * claim the analyst is reading before it says anything else.
 *
 * Nothing runs unprompted. The rail runs exactly the detectors configured on
 * the timeline (`useTimelineDetectors`), names each one in the strip above
 * the feed, and otherwise shows the way into the wizard. Per-method controls
 * live in the sheet and the wizard, where there is room for them.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Eye, EyeOff, Plus } from "lucide-react";
import { EVIDENCE_CLASSES, METHODS, type MethodId } from "./method-registry";
import {
  useIncludeDismissed,
  useStreamingSweep,
} from "@/hooks/useMethodFindings";
import { useTimelineReadiness } from "@/hooks/useTimelineReadiness";
import { useSigmaFindings } from "@/hooks/useSigmaFindings";
import { useTimelineDetectors } from "@/hooks/useTimelineDetectors";
import { baselinesApi } from "@/api/baselines";
import { Button } from "@/components/ui/Button";
import { DetectorStrip } from "./DetectorStrip";
import { FindingGroup } from "./FindingGroup";
import { SigmaFindingRows } from "./SigmaFindings";
import { AnalysisEmptyState } from "./detector-shared";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { DETECTORS } from "./detector-registry";
import { normalizeFinding } from "@/lib/finding-normalize";
import { isTemplateRow } from "@/api/analysis";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

/** See FindingGroup: method ids are API keys, detector-registry uses UI slugs. */
const DETECTOR_BY_API_KEY = Object.fromEntries(
  DETECTORS.map((d) => [d.detector, d]),
);

/**
 * Nothing to analyse — said once for the whole rail, with somewhere to go.
 *
 * The two arms are genuinely different situations: mid-ingest resolves itself
 * and needs only somewhere to watch, while an empty case needs an action and a
 * link to perform it.
 */
function NoEventsState({
  caseId,
  stillIngesting,
}: {
  caseId: string;
  stillIngesting: boolean;
}) {
  return (
    <AnalysisEmptyState
      hint={
        stillIngesting ? (
          "The job tray in the top bar shows progress. Events become searchable as they land."
        ) : (
          <>
            Upload a log file on the{" "}
            <Link
              to={`/cases/${caseId}`}
              className="text-[var(--color-accent)] hover:underline"
            >
              case overview
            </Link>{" "}
            to begin — every method here works over a timeline's events, and
            this one has none yet.
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

interface Props {
  caseId: string;
  timelineId: string;
  onSelectFinding: (method: MethodId, rank: number) => void;
  onOpenTools: (
    section?: "methods" | "signatures" | "explore" | "scope",
  ) => void;
  /** Open the detector wizard — on one method to edit it, or on the list. */
  onAddDetector: (method?: MethodId) => void;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
  /** Drill a finding's field/value into the grid's filters. */
  onDrillField?: (field: string, value: string) => void;
  /** Publish every timestamped finding as a histogram/grid marker. */
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
  onComboDrill?: (pairs: [string, string][]) => void;
  onFrequencyDrill?: (
    field: string,
    value: string,
    start: string,
    end: string,
  ) => void;
  /** Filter the grid to a Sigma rule's hits, by its `sigma: <title>` tag. */
  onTagFilter?: (tag: string) => void;
}

export function InvestigateRail({
  caseId,
  timelineId,
  onSelectFinding,
  onOpenTools,
  onAddDetector,
  onSelectEvent,
  onJumpToTime,
  onDrillField,
  onAnomalyMarkers,
  onComboDrill,
  onFrequencyDrill,
  onTagFilter,
}: Props) {
  const { byMethod, done, total, planLoading } = useStreamingSweep(caseId, timelineId);
  // The list the sweep runs, read from the same hook so the strip and the
  // feed cannot disagree about what is configured.
  const detectors = useTimelineDetectors(caseId, timelineId);
  const { stillIngesting, nothingToAnalyse } = useTimelineReadiness(
    caseId,
    timelineId,
  );
  const { includeDismissed, setIncludeDismissed } = useIncludeDismissed();
  // Findings below their method's `railFloor` are out of the ranked feed until
  // asked for. Session state, not persisted: it is a reading choice about this
  // view, and a floor silently still-lifted from last week would be the same
  // undisclosed filtering it exists to avoid.
  const [showWeak, setShowWeak] = useState(false);
  // The Named-techniques group's only source. Empty until Sigma is
  // configured *and* a run has completed on this timeline — a rule that has
  // not been run is not a finding about anything.
  const { findings: sigmaFindings } = useSigmaFindings(caseId, timelineId);

  // Only to name a chip's baseline; the entry carries the id.
  const { data: baselines } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
    enabled: detectors.entries.some((e) => e.frame === "baseline"),
  });
  const baselineNames = useMemo(
    () => Object.fromEntries((baselines?.baselines ?? []).map((b) => [b.id, b.name])),
    [baselines],
  );

  // Unconfigured methods leave `visible` entirely rather than rendering as an
  // empty group. An empty group reads as "checked, clear" — the one misread
  // this whole surface is built to prevent — and an unconfigured method was
  // not checked.
  const visible = useMemo(
    () => METHODS.filter((m) => byMethod[m.id]?.configured),
    [byMethod],
  );

  // Publish findings onto the histogram and grid. Without this the marks the
  // old panel put there simply vanish, and the timeline stops showing where
  // the findings are — which is most of how an analyst navigates to them.
  //
  // Filtered to configured methods here too, not just in `visible`: disabling
  // a query does not evict what react-query already cached, so a detector
  // removed mid-session keeps returning findings until the cache turns over.
  const markers = useMemo(() => {
    const out: AnomalyMarker[] = [];
    for (const meta of visible) {
      const state = byMethod[meta.id];
      if (!state) continue;
      const detectorMeta = DETECTOR_BY_API_KEY[meta.id];
      if (!detectorMeta) continue;
      for (const raw of state.findings) {
        if (isTemplateRow(raw)) continue;
        const item = normalizeFinding(detectorMeta, raw, 0);
        if (!item.ts) continue;
        out.push({
          ts: item.ts,
          label: item.title,
          detail: `${item.detectorLabel}: ${item.title} — ${item.subtitle}`,
          eventId: item.eventId,
          sourceId: item.sourceId,
          detector: item.detector as AnomalyMarker["detector"],
          rawDetails: item.raw.details,
          windowEnd:
            item.raw.type === "frequency" ? item.raw.window_end : undefined,
        });
      }
    }
    return out;
  }, [byMethod, visible]);

  // Publication is keyed on the markers' *content*, not on the array's
  // identity. The parent stores what it receives in state, so an identity that
  // changed for any reason other than a real change — a churning hook upstream,
  // a re-render from anywhere — would be an unbreakable render loop that
  // freezes the Explorer. A cheap signature makes that structurally impossible
  // rather than dependent on every hook above staying memoized.
  const markersRef = useRef(markers);
  markersRef.current = markers;
  const markerSig = markers
    .map((m) => `${m.ts}|${m.eventId ?? ""}|${m.detector}`)
    .join("");

  useEffect(() => {
    if (!onAnomalyMarkers) return;
    onAnomalyMarkers(markersRef.current);
    return () => onAnomalyMarkers([]);
  }, [markerSig, onAnomalyMarkers]);

  const errored = visible.filter((m) => byMethod[m.id].error);
  const showSigma = sigmaFindings.length > 0;
  const anyFindings =
    visible.some((m) => byMethod[m.id].findings.length > 0) || showSigma;
  const nothingConfigured = detectors.entries.length === 0;
  const allSettled = visible.every((m) => !byMethod[m.id].pending);

  // Said before anything else, and instead of everything else: a findings list
  // over a timeline with no events is not "clear", it is unanswered.
  if (nothingToAnalyse) {
    return (
      <div className="space-y-2">
        <GuidancePanel id="investigate-anomalies" />
        <NoEventsState caseId={caseId} stillIngesting={stillIngesting} />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* First-run explainer, collapsible and restorable from Settings. It
          moved here with the findings it explains rather than being dropped
          along with the tab that used to host it. */}
      <GuidancePanel id="investigate-anomalies" />

      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Detectors
        </span>
        {detectors.canEdit && (
          <Button
            variant="ghost"
            size="sm"
            data-testid="add-detector"
            onClick={() => onAddDetector()}
          >
            <Plus size={11} /> Add detector
          </Button>
        )}
      </div>
      <DetectorStrip
        entries={detectors.entries}
        byMethod={byMethod}
        baselineNames={baselineNames}
        canEdit={detectors.canEdit}
        onEdit={(m) => onAddDetector(m)}
        onRemove={(m) => void detectors.remove(m)}
      />
      {detectors.saveError && (
        <p data-testid="detector-save-error" className="text-xs text-[var(--color-danger)]">
          Not saved: {detectors.saveError}
        </p>
      )}

      {/* Nothing runs until somebody chooses it, and the rail says so rather
          than showing an empty feed that reads as "clear". Sigma hits are the
          one thing that can be here without a configured detector. */}
      {nothingConfigured && !showSigma && (
        <AnalysisEmptyState hint="Nothing runs until you choose it. Each detector answers one kind of question — the wizard says which.">
          <span data-testid="no-detectors">No detectors configured on this timeline.</span>
        </AnalysisEmptyState>
      )}

      <div className="flex flex-wrap gap-1">
        {/* The reveal. A dismissal is presentation-only on the server and
            reversible there, so the UI owes a way back to it — without this a
            mis-click removes a finding from every surface permanently. */}
        <button
          data-testid="toggle-dismissed"
          onClick={() => setIncludeDismissed(!includeDismissed)}
          aria-pressed={includeDismissed}
          title={
            includeDismissed
              ? "Hide dismissed findings again"
              : "Show dismissed findings, dimmed, so a dismissal can be undone"
          }
          className={cn(
            "ml-auto flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium transition-base",
            includeDismissed
              ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-accent)]"
              : "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-border-strong)]",
          )}
        >
          {includeDismissed ? <Eye size={10} /> : <EyeOff size={10} />}
          Dismissed
        </button>
      </div>

      {done < total && (
        <p
          data-testid="sweep-progress"
          className="flex items-center gap-2 text-[11px] text-[var(--color-fg-muted)]"
        >
          <span className="h-0.5 flex-1 overflow-hidden rounded bg-[var(--color-border)]">
            <span
              className="block h-full bg-[var(--color-accent)] transition-[width] duration-300"
              style={{ width: `${total ? (done / total) * 100 : 0}%` }}
            />
          </span>
          <span className="shrink-0 font-mono">
            {done} of {total}
          </span>
        </p>
      )}

      {/* One detector failing must not take the stream with it. */}
      {errored.length > 0 && (
        <p
          data-testid="method-errors"
          className="flex items-start gap-1.5 rounded border border-[var(--color-warning)] bg-[var(--color-warning-dim)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
        >
          <AlertTriangle
            size={11}
            className="mt-0.5 shrink-0 text-[var(--color-warning)]"
          />
          <span>
            {errored.map((m) => m.label).join(", ")} failed to run. Other
            detectors are unaffected —{" "}
            <button
              type="button"
              onClick={() => onOpenTools("methods")}
              className="text-[var(--color-accent)] hover:underline"
            >
              open Tools to retry
            </button>
            .
          </span>
        </p>
      )}

      {EVIDENCE_CLASSES.map((cls) => (
        <FindingGroup
          key={cls.id}
          evidenceClass={cls}
          methods={visible.filter((m) => m.evidenceClass === cls.id)}
          byMethod={byMethod}
          caseId={caseId}
          timelineId={timelineId}
          onSelectFinding={onSelectFinding}
          onSelectEvent={onSelectEvent}
          onJumpToTime={onJumpToTime}
          onDrillField={onDrillField}
          onComboDrill={onComboDrill}
          onFrequencyDrill={onFrequencyDrill}
          extraRows={
            cls.id === "named" && showSigma ? (
              <SigmaFindingRows
                findings={sigmaFindings}
                onTagFilter={onTagFilter}
              />
            ) : null
          }
          extraCount={
            cls.id === "named" && showSigma ? sigmaFindings.length : 0
          }
          showWeak={showWeak}
          onShowWeak={() => setShowWeak(true)}
        />
      ))}

      {/* "No findings" is a claim that every configured detector ran and
          answered. Not while the plan is resolving, not while any is pending,
          and not when nothing is configured — that state is named above. */}
      {!anyFindings && !planLoading && !nothingConfigured && allSettled && (
        <AnalysisEmptyState hint="Edit a detector's fields or scope from its chip, or add another kind of question.">
          No findings from the configured detectors.
        </AnalysisEmptyState>
      )}
    </div>
  );
}
