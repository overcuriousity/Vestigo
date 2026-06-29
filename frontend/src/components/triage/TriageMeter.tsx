/**
 * TriageMeter — compact summary bar shown in the Explorer header.
 * Shows session-momentum (events you've triaged this session) and the
 * outlier-reviewed progress derived from annotations.
 */
import { SessionMomentum } from "./SessionMomentum";
import { Progress } from "@/components/ui/Progress";
import { Tooltip } from "@/components/ui/Tooltip";
import { SESSION_START } from "@/lib/session";
import type { Annotation } from "@/api/types";

interface Props {
  annotations: Annotation[];
  totalEvents: number;
}

function computeProgress(annotations: Annotation[]) {
  const byEvent = new Map<string, Annotation[]>();
  for (const a of annotations) {
    const list = byEvent.get(a.event_id) ?? [];
    list.push(a);
    byEvent.set(a.event_id, list);
  }

  // Outlier events = those tagged by system
  const outlierEventIds = new Set<string>(
    annotations
      .filter((a) => a.annotation_type === "outlier" && a.origin === "system")
      .map((a) => a.event_id),
  );

  // Outliers reviewed = outlier events that also have a user annotation
  const outliersReviewed = [...outlierEventIds].filter((eid) =>
    (byEvent.get(eid) ?? []).some((a) => a.origin === "user"),
  ).length;

  // Events triaged this session = distinct events with a user annotation
  // created at or after SESSION_START
  const sessionEventIds = new Set<string>();
  for (const a of annotations) {
    if (a.origin === "user" && new Date(a.created_at).getTime() >= SESSION_START) {
      sessionEventIds.add(a.event_id);
    }
  }

  return {
    totalOutliers: outlierEventIds.size,
    outliersReviewed,
    triagedThisSession: sessionEventIds.size,
  };
}

export function TriageMeter({ annotations, totalEvents: _totalEvents }: Props) {
  const { totalOutliers, outliersReviewed, triagedThisSession } =
    computeProgress(annotations);

  const outlierPct =
    totalOutliers > 0 ? Math.round((outliersReviewed / totalOutliers) * 100) : 0;

  return (
    <div className="flex items-center gap-4">
      {/* Session momentum */}
      <SessionMomentum count={triagedThisSession} />

      {/* Outliers meter — only shown when outliers exist */}
      {totalOutliers > 0 && (
        <Tooltip
          content={`${outliersReviewed} / ${totalOutliers} outliers reviewed`}
        >
          <div className="hidden sm:flex items-center gap-2 w-28">
            <div className="flex-1">
              <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--color-fg-muted)] mb-1">
                Outliers
              </p>
              <Progress
                value={outlierPct}
                indicatorClassName="bg-[var(--color-outlier)]"
              />
            </div>
            <span className="text-[10px] font-mono text-[var(--color-outlier)]">
              {outlierPct}%
            </span>
          </div>
        </Tooltip>
      )}
    </div>
  );
}
