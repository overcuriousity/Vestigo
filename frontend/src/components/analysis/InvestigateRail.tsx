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
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { EVIDENCE_CLASSES, METHODS, type MethodId } from "./method-registry";
import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { ScopeStrip } from "./ScopeStrip";
import { FindingGroup } from "./FindingGroup";
import { AnalysisEmptyState } from "./detector-shared";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { DETECTORS } from "./detector-registry";
import { normalizeFinding } from "@/lib/finding-normalize";
import { isTemplateRow } from "@/api/analysis";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

/** See FindingGroup: method ids are API keys, detector-registry uses UI slugs. */
const DETECTOR_BY_API_KEY = Object.fromEntries(DETECTORS.map((d) => [d.detector, d]));

/**
 * The analyst questions from the design round, surviving as filters over one
 * stream rather than as containers. Containers would put a finding somewhere
 * you have to remember to look; a filter never hides anything by default.
 */
const PRESETS: { id: string; label: string; methods: MethodId[] | null }[] = [
  { id: "all", label: "Everything", methods: null },
  {
    id: "changed",
    label: "Changed vs. baseline",
    methods: ["proportion_shift", "value_distribution_drift", "frequency"],
  },
  {
    id: "repeats",
    label: "Repeating",
    methods: ["interval_periodicity", "sequence_novelty", "log_template"],
  },
  {
    id: "unusual",
    label: "Unusual values",
    methods: ["value_novelty", "value_combo", "charset", "entropy"],
  },
  { id: "integrity", label: "Evidence integrity", methods: ["timestamp_order", "numeric_range"] },
];

interface Props {
  caseId: string;
  timelineId: string;
  onSelectFinding: (method: MethodId, rank: number) => void;
  onOpenTools: (section?: "methods" | "signatures" | "explore" | "scope") => void;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
  /** Drill a finding's field/value into the grid's filters. */
  onDrillField?: (field: string, value: string) => void;
  /** Publish every timestamped finding as a histogram/grid marker. */
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
  onComboDrill?: (pairs: [string, string][]) => void;
  onFrequencyDrill?: (field: string, value: string, start: string, end: string) => void;
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
}: Props) {
  const { byMethod, scope, done, total } = useStreamingSweep(caseId, timelineId);
  const [preset, setPreset] = useState("all");

  const active = PRESETS.find((p) => p.id === preset) ?? PRESETS[0];
  const visible = useMemo(
    () =>
      METHODS.filter(
        (m) => (active.methods === null || active.methods.includes(m.id)) && byMethod[m.id],
      ),
    [active, byMethod],
  );

  // Publish findings onto the histogram and grid. Without this the marks the
  // old panel put there simply vanish, and the timeline stops showing where
  // the findings are — which is most of how an analyst navigates to them.
  const markers = useMemo(() => {
    const out: AnomalyMarker[] = [];
    for (const meta of METHODS) {
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
          windowEnd: item.raw.type === "frequency" ? item.raw.window_end : undefined,
        });
      }
    }
    return out;
  }, [byMethod]);

  useEffect(() => {
    if (!onAnomalyMarkers) return;
    onAnomalyMarkers(markers);
    return () => onAnomalyMarkers([]);
  }, [markers, onAnomalyMarkers]);

  const skipped = visible.filter((m) => byMethod[m.id].status !== "applicable");
  const errored = visible.filter((m) => byMethod[m.id].error);
  const anyFindings = visible.some((m) => byMethod[m.id].findings.length > 0);

  return (
    <div className="space-y-2">
      {/* First-run explainer, collapsible and restorable from Settings. It
          moved here with the findings it explains rather than being dropped
          along with the tab that used to host it. */}
      <GuidancePanel id="investigate-anomalies" />
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
          <AlertTriangle size={11} className="mt-0.5 shrink-0 text-[var(--color-warning)]" />
          <span>
            {errored.map((m) => m.label).join(", ")} failed to run. Other methods are unaffected —
            open Tools to retry.
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
        />
      ))}

      {!anyFindings && done === total && (
        <AnalysisEmptyState hint="Open Tools to run a method the gate skipped, or set a baseline to enable the comparison methods.">
          No findings under this scope.
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
          {skipped.length} method{skipped.length === 1 ? "" : "s"} not applicable here — see why, or
          run anyway
        </button>
      )}
    </div>
  );
}
