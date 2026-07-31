/**
 * FrameBar — the single global scope every detector runs under (replacing the
 * old per-view self/temporal toggle). "Scan all events" = self-baseline over
 * the whole corpus; "Compare baseline" = score the active definition's suspect
 * windows against its baseline window. The choice + active definition live in
 * useBaselineStore; a one-line status states exactly what is active. Picking or
 * building a definition happens in the BaselineSection rendered directly below
 * (in the baseline frame), so this bar stays a pure scope switch.
 */
import { useQuery } from "@tanstack/react-query";
import { Layers, ScanLine, SlidersHorizontal } from "lucide-react";
import { baselinesApi } from "@/api/baselines";
import { useBaselineStore } from "@/stores/baseline";
import { useUiStore } from "@/stores/ui";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { GLOSSARY } from "@/lib/glossary";

interface Props {
  caseId: string;
  timelineId: string;
}

export function FrameBar({ caseId, timelineId }: Props) {
  const { frame, setFrame, activeBaselineId } = useBaselineStore();
  const setBaselineBuilderOpen = useUiStore((s) => s.setBaselineBuilderOpen);

  const { data } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
  });
  const active = (data?.baselines ?? []).find((d) => d.id === activeBaselineId) ?? null;

  const status =
    frame === "self"
      ? "All events scanned. Temporal-only detectors (proportion shift, interval cadence, sequences, distribution drift) need a baseline and stay empty here."
      : active
        ? `Comparing ${active.suspect_windows.length} suspect window${active.suspect_windows.length === 1 ? "" : "s"} against “${active.name}”.`
        : "No baseline selected — open Manage baselines to pick or build one.";

  return (
    <div className="mb-3 space-y-1.5">
      <SegmentedControl
        value={frame}
        onChange={setFrame}
        options={[
          { id: "self", icon: ScanLine, label: "Scan all events", hint: GLOSSARY.scanAllEvents },
          { id: "baseline", icon: Layers, label: "Compare baseline", hint: GLOSSARY.compareBaseline },
        ]}
      />
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 text-[11px] text-[var(--color-fg-muted)]">{status}</p>
        {frame === "baseline" && (
          <button
            onClick={() => setBaselineBuilderOpen(true)}
            className="flex shrink-0 items-center gap-1 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[11px] text-[var(--color-fg-secondary)] hover:border-[var(--color-border-strong)] hover:text-[var(--color-fg-primary)]"
            title="Open the baseline builder — pick, edit or create baseline definitions and suspect windows"
          >
            <SlidersHorizontal size={11} />
            Manage baselines
          </button>
        )}
      </div>
    </div>
  );
}
