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
import { Layers, ScanLine } from "lucide-react";
import { baselinesApi } from "@/api/baselines";
import { useBaselineStore } from "@/stores/baseline";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { GLOSSARY } from "@/lib/glossary";

interface Props {
  caseId: string;
  timelineId: string;
}

export function FrameBar({ caseId, timelineId }: Props) {
  const { frame, setFrame, activeBaselineId } = useBaselineStore();

  const { data } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
  });
  const active = (data?.baselines ?? []).find((d) => d.id === activeBaselineId) ?? null;

  const status =
    frame === "self"
      ? "Every detector scans all events."
      : active
        ? `Comparing ${active.suspect_windows.length} suspect window${active.suspect_windows.length === 1 ? "" : "s"} against “${active.name}”.`
        : "Pick or build a baseline below to compare against.";

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
      <p className="text-[11px] text-[var(--color-fg-muted)]">{status}</p>
    </div>
  );
}
