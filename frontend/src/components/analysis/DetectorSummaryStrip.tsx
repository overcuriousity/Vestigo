/**
 * DetectorSummaryStrip — a "run all detectors" overview above the per-detector
 * views. On demand it fires every detector once (persist=false) against the
 * active baseline/mode and shows a per-detector finding count; clicking a count
 * jumps to that detector's tab. The per-detector views stay the primary result
 * UX — this is just a triage-at-a-glance sweep.
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Layers, Play } from "lucide-react";
import { anomaliesApi, type AnomalyParams } from "@/api/anomalies";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import { cn } from "@/lib/cn";

type SubTab = "novelty" | "combo" | "frequency" | "order" | "range" | "charset" | "entropy";

const SWEEP: { subTab: SubTab; detector: NonNullable<AnomalyParams["detector"]>; label: string }[] = [
  { subTab: "novelty", detector: "value_novelty", label: "Rare values" },
  { subTab: "combo", detector: "value_combo", label: "Combos" },
  { subTab: "frequency", detector: "frequency", label: "Frequency" },
  { subTab: "range", detector: "numeric_range", label: "Range" },
  { subTab: "charset", detector: "charset", label: "Charset" },
  { subTab: "entropy", detector: "entropy", label: "Entropy" },
  { subTab: "order", detector: "timestamp_order", label: "Order" },
];

interface Props {
  caseId: string;
  timelineId: string;
  onSelect: (subTab: SubTab) => void;
}

export function DetectorSummaryStrip({ caseId, timelineId, onSelect }: Props) {
  const activeBaselineId = useBaselineStore((s) => s.activeBaselineId);
  const [counts, setCounts] = useState<Record<SubTab, number> | null>(null);

  const sweep = useMutation({
    mutationFn: async () => {
      const blParams = activeBaselineId ? { baseline_id: activeBaselineId } : {};
      const results = await Promise.all(
        SWEEP.map((d) =>
          anomaliesApi
            .list(caseId, timelineId, {
              detector: d.detector,
              limit: 50,
              persist: false,
              ...blParams,
            })
            .then((r) => [d.subTab, r.results.length] as const)
            .catch(() => [d.subTab, 0] as const),
        ),
      );
      return Object.fromEntries(results) as Record<SubTab, number>;
    },
    onSuccess: setCounts,
  });

  return (
    <div className="mb-3 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-2">
      <div className="mb-1.5 flex items-center gap-2">
        <Layers size={12} className="text-[var(--color-fg-muted)]" />
        <span className="flex-1 text-xs font-medium text-[var(--color-fg-secondary)]">
          Run all detectors{activeBaselineId ? " (against active baseline)" : ""}
        </span>
        <Button size="sm" variant="ghost" className="gap-1 text-xs" disabled={sweep.isPending} onClick={() => sweep.mutate()}>
          {sweep.isPending ? <Spinner size={11} /> : <Play size={11} />}
          Run
        </Button>
      </div>
      {counts && (
        <div className="flex flex-wrap gap-1">
          {SWEEP.map((d) => (
            <button
              key={d.subTab}
              onClick={() => onSelect(d.subTab)}
              className={cn(
                "flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] transition-colors",
                counts[d.subTab] > 0
                  ? "border-[var(--color-anomaly)]/40 text-[var(--color-fg-primary)] hover:bg-[var(--color-anomaly-dim)]"
                  : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:border-[var(--color-border-focus)]",
              )}
              title={`Open ${d.label}`}
            >
              {d.label}
              <span className="font-mono font-semibold">{counts[d.subTab]}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
