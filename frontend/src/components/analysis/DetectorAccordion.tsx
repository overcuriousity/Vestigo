/**
 * DetectorAccordion — the unified detector overview. Every detector is one row
 * with a live finding count (a sweep that follows the active frame); expanding
 * a row drills into that detector's full ranked findings inline. This replaces
 * the old detector dropdown *and* the separate "Run all detectors" block — the
 * overview is the run-all, and one open row is the per-detector view.
 *
 * Only the expanded detector's view mounts, so exactly one detector publishes
 * histogram markers / run-id at a time (matching the old one-view-at-a-time
 * behavior), and collapsing all clears the overlay.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Hash,
  Layers,
  Percent,
  RefreshCw,
  Rewind,
  Ruler,
  Shuffle,
  Timer,
  Type,
} from "lucide-react";
import { anomaliesApi, type AnomalyParams } from "@/api/anomalies";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import { ValueNoveltyView } from "./ValueNoveltyView";
import { ComboNoveltyView } from "./ComboNoveltyView";
import { FrequencyView } from "./FrequencyView";
import { OrderViolationsView } from "./OrderViolationsView";
import { NumericRangeView } from "./NumericRangeView";
import { CharsetNoveltyView } from "./CharsetNoveltyView";
import { EntropyView } from "./EntropyView";
import { ProportionShiftView } from "./ProportionShiftView";
import { IntervalPeriodicityView } from "./IntervalPeriodicityView";
import { cn } from "@/lib/cn";
import type { AnomalyMarker, Event } from "@/api/types";

type DetectorId =
  | "novelty"
  | "combo"
  | "frequency"
  | "shift"
  | "interval"
  | "order"
  | "range"
  | "charset"
  | "entropy";

const DETECTORS: {
  id: DetectorId;
  detector: NonNullable<AnomalyParams["detector"]>;
  icon: React.ElementType;
  label: string;
  hint: string;
}[] = [
  { id: "novelty", detector: "value_novelty", icon: Hash, label: "Rare values", hint: "Rare or first-seen field values" },
  { id: "combo", detector: "value_combo", icon: Layers, label: "Value combos", hint: "Rare combinations of fields" },
  { id: "frequency", detector: "frequency", icon: Activity, label: "Frequency", hint: "Count spikes and silences" },
  { id: "shift", detector: "proportion_shift", icon: Percent, label: "Proportion shift", hint: "Value shares that change between windows" },
  { id: "interval", detector: "interval_periodicity", icon: Timer, label: "Interval cadence", hint: "Broken heartbeats and new beaconing" },
  { id: "range", detector: "numeric_range", icon: Ruler, label: "Numeric range", hint: "Values outside a learned band" },
  { id: "charset", detector: "charset", icon: Type, label: "Charset novelty", hint: "Never-seen characters" },
  { id: "entropy", detector: "entropy", icon: Shuffle, label: "Entropy outliers", hint: "Random or degenerate strings" },
  { id: "order", detector: "timestamp_order", icon: Rewind, label: "Timestamp order", hint: "Timestamps running backwards" },
];

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onComboDrill?: (pairs: [string, string][]) => void;
  onFrequencyDrill?: (field: string, value: string, start: string, end: string) => void;
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
  onAnomalyRunId?: (runId: string | undefined) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

export function DetectorAccordion(props: Props) {
  const { caseId, timelineId } = props;
  const [open, setOpen] = useState<DetectorId | null>("novelty");

  const frame = useBaselineStore((s) => s.frame);
  const activeBaselineId = useBaselineStore((s) => s.activeBaselineId);
  const inBaselineFrame = frame === "baseline";
  const needsBaseline = inBaselineFrame && !activeBaselineId;
  const frameKey = inBaselineFrame ? (activeBaselineId ?? "none") : "self";

  // Overview sweep — a count per detector under the active frame. Auto-runs and
  // caches; the counts are a triage hint (the expanded view, which honors the
  // analyst's field selection, is authoritative).
  const { data: counts, isFetching, refetch } = useQuery({
    queryKey: ["detector-sweep", caseId, timelineId, frameKey],
    enabled: !needsBaseline,
    staleTime: 60_000,
    queryFn: async () => {
      const blParams = inBaselineFrame && activeBaselineId ? { baseline_id: activeBaselineId } : {};
      const pairs = await Promise.all(
        DETECTORS.map((d) =>
          anomaliesApi
            .list(caseId, timelineId, { detector: d.detector, limit: 50, persist: false, ...blParams })
            .then((r) => [d.id, r.results.length] as const)
            .catch(() => [d.id, -1] as const),
        ),
      );
      return Object.fromEntries(pairs) as Record<DetectorId, number>;
    },
  });

  return (
    <div className="rounded border border-[var(--color-border)]">
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-2 py-1.5">
        <span className="flex-1 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Detectors
        </span>
        <button
          onClick={() => refetch()}
          disabled={needsBaseline || isFetching}
          className="rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] disabled:opacity-40"
          title="Re-run overview counts"
        >
          <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
        </button>
      </div>

      {DETECTORS.map((d) => {
        const isOpen = open === d.id;
        const count = counts?.[d.id];
        return (
          <div key={d.id} className="border-b border-[var(--color-border)] last:border-b-0">
            <button
              onClick={() => setOpen(isOpen ? null : d.id)}
              className="flex w-full items-center gap-2 px-2 py-2 text-left hover:bg-[var(--color-bg-hover)]"
            >
              {isOpen ? (
                <ChevronDown size={13} className="shrink-0 text-[var(--color-fg-muted)]" />
              ) : (
                <ChevronRight size={13} className="shrink-0 text-[var(--color-fg-muted)]" />
              )}
              <d.icon size={13} className="shrink-0 text-[var(--color-fg-muted)]" />
              <span className="flex min-w-0 flex-1 flex-col leading-tight">
                <span className="text-xs font-medium text-[var(--color-fg-primary)]">{d.label}</span>
                {!isOpen && <span className="truncate text-[10px] text-[var(--color-fg-muted)]">{d.hint}</span>}
              </span>
              <CountBadge count={needsBaseline ? undefined : count} loading={isFetching && count === undefined} />
            </button>
            {isOpen && (
              <div className="border-t border-[var(--color-border-subtle)] bg-[var(--color-bg-base)] p-2.5">
                <DetectorBody id={d.id} {...props} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function CountBadge({ count, loading }: { count?: number; loading: boolean }) {
  if (loading) return <Spinner size={11} />;
  if (count === undefined) return <span className="text-[10px] text-[var(--color-fg-muted)]">—</span>;
  if (count < 0) return <span className="text-[10px] text-[var(--color-warning)]">err</span>;
  return (
    <span
      className={cn(
        "min-w-[1.25rem] rounded px-1 py-0.5 text-center font-mono text-[11px] font-semibold",
        count > 0
          ? "bg-[var(--color-anomaly-dim)] text-[var(--color-anomaly)]"
          : "text-[var(--color-fg-muted)]",
      )}
    >
      {count}
    </span>
  );
}

/** Renders the one expanded detector's full view. */
function DetectorBody({ id, ...props }: Props & { id: DetectorId }) {
  const shared = {
    caseId: props.caseId,
    timelineId: props.timelineId,
    onSelectEvent: props.onSelectEvent,
    onFindingsChange: props.onAnomalyMarkers,
    onRunIdChange: props.onAnomalyRunId,
    onJumpToTime: props.onJumpToTime,
  };
  switch (id) {
    case "novelty":
      return <ValueNoveltyView {...shared} onDrillField={props.onDrillField} />;
    case "combo":
      return <ComboNoveltyView {...shared} onComboDrill={props.onComboDrill} />;
    case "frequency":
      return <FrequencyView {...shared} onDrillField={props.onFrequencyDrill} />;
    case "shift":
      return <ProportionShiftView {...shared} onDrillField={props.onDrillField} />;
    case "interval":
      return <IntervalPeriodicityView {...shared} onDrillField={props.onDrillField} />;
    case "order":
      return <OrderViolationsView {...shared} />;
    case "range":
      return <NumericRangeView {...shared} onDrillField={props.onDrillField} />;
    case "charset":
      return <CharsetNoveltyView {...shared} onDrillField={props.onDrillField} />;
    case "entropy":
      return <EntropyView {...shared} onDrillField={props.onDrillField} />;
  }
}
