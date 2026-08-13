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
 * The old five-tab split is gone. So is the Advanced accordion: per-method
 * controls live in the sheet, where there is room for them.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Eye, EyeOff } from "lucide-react";
import { EVIDENCE_CLASSES, METHODS, type MethodId } from "./method-registry";
import {
  useIncludeDismissed,
  useStreamingSweep,
} from "@/hooks/useMethodFindings";
import { useTimelineReadiness } from "@/hooks/useTimelineReadiness";
import { useSigmaFindings } from "@/hooks/useSigmaFindings";
import { useMutedMethods } from "@/hooks/useMutedMethods";
import { ScopeStrip } from "./ScopeStrip";
import { DetectorMuteStrip } from "./DetectorMuteStrip";
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
 * The analyst questions from the design round, surviving as filters over one
 * stream rather than as containers. Containers would put a finding somewhere
 * you have to remember to look; a filter never hides anything by default.
 */
const PRESETS: {
  id: string;
  label: string;
  methods: MethodId[] | null;
  /** Whether Sigma hits — the only non-method findings — survive this filter. */
  sigma: boolean;
}[] = [
  { id: "all", label: "Everything", methods: null, sigma: true },
  // The one preset that is *only* Sigma. It shipped as "Evidence integrity"
  // because nothing could ever fill the Named group, so a Known-bad pill would
  // have filtered to a guaranteed-empty list.
  { id: "known", label: "Known-bad", methods: [], sigma: true },
  {
    id: "changed",
    label: "Changed vs. baseline",
    methods: ["proportion_shift", "value_distribution_drift", "frequency"],
    sigma: false,
  },
  {
    id: "repeats",
    label: "Repeating",
    methods: ["interval_periodicity", "sequence_novelty", "log_template"],
    sigma: false,
  },
  {
    id: "unusual",
    label: "Unusual values",
    methods: ["value_novelty", "value_combo", "charset", "entropy"],
    sigma: false,
  },
  {
    id: "integrity",
    label: "Evidence integrity",
    methods: ["timestamp_order", "numeric_range"],
    sigma: false,
  },
];

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
  onSelectEvent,
  onJumpToTime,
  onDrillField,
  onAnomalyMarkers,
  onComboDrill,
  onFrequencyDrill,
  onTagFilter,
}: Props) {
  const { byMethod, scope, done, total, planLoading } = useStreamingSweep(
    caseId,
    timelineId,
  );
  // Read from the hook rather than from the sweep's return, so the strip and
  // the feed cannot disagree about what is muted — the sweep uses the very
  // same hook to decide what not to fetch.
  const mute = useMutedMethods(caseId, timelineId);
  const muted = mute.muted;
  const { stillIngesting, nothingToAnalyse } = useTimelineReadiness(
    caseId,
    timelineId,
  );
  const { includeDismissed, setIncludeDismissed } = useIncludeDismissed();
  const [preset, setPreset] = useState("all");
  // Findings below their method's `railFloor` are out of the ranked feed until
  // asked for. Session state, not persisted: it is a reading choice about this
  // sweep, and a floor silently still-lifted from last week would be the same
  // undisclosed filtering it exists to avoid.
  const [showWeak, setShowWeak] = useState(false);
  // The Named-techniques group's only source. Empty until Sigma is
  // configured *and* a run has completed on this timeline — a rule that has
  // not been run is not a finding about anything.
  const { findings: sigmaFindings } = useSigmaFindings(caseId, timelineId);

  const active = PRESETS.find((p) => p.id === preset) ?? PRESETS[0];
  // Muted methods leave `visible` entirely rather than rendering as an empty
  // group. An empty group reads as "checked, clear" — the one misread this
  // whole surface is built to prevent — and a muted method was not checked.
  // The strip above carries the count instead.
  const visible = useMemo(
    () =>
      METHODS.filter(
        (m) =>
          (active.methods === null || active.methods.includes(m.id)) &&
          !muted.has(m.id) &&
          byMethod[m.id],
      ),
    [active, byMethod, muted],
  );

  // Publish findings onto the histogram and grid. Without this the marks the
  // old panel put there simply vanish, and the timeline stops showing where
  // the findings are — which is most of how an analyst navigates to them.
  //
  // Muted methods are filtered here too, not just out of `visible`: disabling
  // the query does not evict what react-query already cached, so a method
  // muted mid-session keeps returning findings. "A muted detector leaves the
  // feed, the histogram and the grid marks" has to hold on this pass as well.
  const markers = useMemo(() => {
    const out: AnomalyMarker[] = [];
    for (const meta of METHODS) {
      if (muted.has(meta.id)) continue;
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
  }, [byMethod, muted]);

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
    .join("");

  useEffect(() => {
    if (!onAnomalyMarkers) return;
    onAnomalyMarkers(markersRef.current);
    return () => onAnomalyMarkers([]);
  }, [markerSig, onAnomalyMarkers]);

  // Muted methods this preset would otherwise have shown. Distinguishes "this
  // preset is empty because nothing applies" from "…because you silenced it".
  const presetMuted = METHODS.filter(
    (m) =>
      (active.methods === null || active.methods.includes(m.id)) &&
      muted.has(m.id),
  );
  const skipped = visible.filter((m) => byMethod[m.id].status !== "applicable");
  const errored = visible.filter((m) => byMethod[m.id].error);
  const showSigma = active.sigma && sigmaFindings.length > 0;
  const anyFindings =
    visible.some((m) => byMethod[m.id].findings.length > 0) || showSigma;
  // Both empty states are claims about *what this preset shows*, so they are
  // counted over `visible` — the sweep's global done/total would let a preset
  // whose own methods never ran inherit "clear" from methods it hides.
  const visibleRunnable = visible.filter(
    (m) => byMethod[m.id].status === "applicable",
  );
  const visibleSettled = visibleRunnable.every((m) => !byMethod[m.id].pending);

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
      <DetectorMuteStrip mute={mute} />
      <ScopeStrip scope={scope} onOpen={() => onOpenTools("scope")} />

      <div className="flex flex-wrap gap-1">
        {PRESETS.map((p) => (
          <button
            key={p.id}
            onClick={() => setPreset(p.id)}
            aria-pressed={preset === p.id}
            className={cn(
              "rounded-full border px-2 py-0.5 text-[10px] font-medium transition-base",
              preset === p.id
                ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-accent)]"
                : "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-border-strong)]",
            )}
          >
            {p.label}
          </button>
        ))}
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

      {/* One method failing must not take the stream with it. */}
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
            methods are unaffected — open Tools to retry.
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

      {/* "No findings" is a claim that every method that could run *here*, ran.
          It may not be made while the plan is still resolving (nothing is
          runnable yet, so the check is vacuously true), while any of this
          preset's methods is still pending, or when none of them was runnable
          at all — a preset whose methods all need setup checked nothing. */}
      {!anyFindings &&
        !planLoading &&
        visibleRunnable.length > 0 &&
        visibleSettled && (
          <AnalysisEmptyState hint="Open Tools to run a method the gate skipped, or set a baseline to enable the comparison methods.">
            No findings under this scope.
          </AnalysisEmptyState>
        )}

      {/* Every method under this preset gated off. Not the same statement as
          "nothing found" — nothing ran. And when the reason nothing ran is that
          the analyst muted it, saying "no method applies" would blame the gate
          for a choice somebody made: two different situations, two states.
          Both are guarded by `!anyFindings` because a preset can list findings
          that came from no method at all: Known-bad is `methods: []` and draws
          entirely on Sigma, so it has zero runnable methods *by construction*
          and would otherwise disclaim the rule hits printed right above it. */}
      {!anyFindings &&
        !planLoading &&
        visibleRunnable.length === 0 &&
        presetMuted.length > 0 && (
          <AnalysisEmptyState hint="Unmute a detector in the strip above to put it back in the sweep, or open Tools to run one without unmuting it.">
            Every detector for this view is muted.
          </AnalysisEmptyState>
        )}
      {!anyFindings &&
        !planLoading &&
        visibleRunnable.length === 0 &&
        presetMuted.length === 0 && (
          <AnalysisEmptyState hint="Open Tools to see why each was skipped and run one anyway, or set a baseline to enable the comparison methods.">
            No method applies to this data yet.
          </AnalysisEmptyState>
        )}

      {/* A skipped method is never a zero — a zero reads as "checked, clear",
          and these were not checked. The count routes into the accounting. */}
      {skipped.length > 0 && (
        <button
          data-testid="skipped-summary"
          onClick={() => onOpenTools("methods")}
          className="w-full rounded border border-dashed border-[var(--color-border)] px-2 py-1.5 text-left text-[11px] text-[var(--color-fg-muted)] transition-base hover:border-[var(--color-border-strong)] hover:text-[var(--color-fg-secondary)]"
        >
          {skipped.length} method{skipped.length === 1 ? "" : "s"} not
          applicable here — see why, or run anyway
        </button>
      )}
    </div>
  );
}
