/**
 * RoutineCollapseStat — compact toolbar stat shown while routine collapse is
 * active. Collapse must announce itself (forensic rule: hiding events is
 * never silent), so this renders the timeline-wide hidden count — and the
 * noise share it represents — with a one-click escape hatch.
 */
import { Repeat } from "lucide-react";
import { Tooltip } from "@/components/ui/Tooltip";
import { formatRoutineStat } from "@/lib/format";

interface Props {
  /** Timeline-wide count of events hidden by routine patterns. */
  count: number;
  /** Timeline-wide event total (ready sources); 0 when unknown. */
  timelineTotal: number;
  /** Turns collapse off ("show them"). */
  onShow: () => void;
}

export function RoutineCollapseStat({ count, timelineTotal, onShow }: Props) {
  return (
    <Tooltip content="Timeline-wide count, independent of active filters. Hidden events belong to patterns marked routine under Tools → Explore. Click to show them again.">
      <button
        onClick={onShow}
        className="flex items-center gap-1.5 rounded px-2 py-1 text-xs text-[var(--color-accent)] bg-[var(--color-accent-dim)] hover:underline"
      >
        <Repeat size={11} />
        <span>{formatRoutineStat(count, timelineTotal)}</span>
      </button>
    </Tooltip>
  );
}
