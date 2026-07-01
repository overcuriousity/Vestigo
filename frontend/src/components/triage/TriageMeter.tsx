/**
 * TriageMeter — compact summary bar shown in the Explorer header.
 * Shows session-momentum (events you've triaged this session) and the
 * anomaly-reviewed progress derived from system annotations.
 */
import { SessionMomentum } from "./SessionMomentum";
import { Progress } from "@/components/ui/Progress";
import { Tooltip } from "@/components/ui/Tooltip";
import { SESSION_START } from "@/lib/session";
import type { Annotation } from "@/api/types";

interface Props {
  annotations: Annotation[];
  totalEvents: number | null;
}

function computeProgress(annotations: Annotation[]) {
  const byEvent = new Map<string, Annotation[]>();
  for (const a of annotations) {
    const list = byEvent.get(a.event_id) ?? [];
    list.push(a);
    byEvent.set(a.event_id, list);
  }

  // Anomaly events = those tagged by system
  const anomalyEventIds = new Set<string>(
    annotations
      .filter((a) => a.annotation_type === "anomaly" && a.origin === "system")
      .map((a) => a.event_id),
  );

  // Anomalies reviewed = anomaly events that also have a user annotation
  const anomaliesReviewed = [...anomalyEventIds].filter((eid) =>
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
    totalAnomalies: anomalyEventIds.size,
    anomaliesReviewed,
    triagedThisSession: sessionEventIds.size,
  };
}

export function TriageMeter({ annotations, totalEvents: _totalEvents }: Props) {
  const { totalAnomalies, anomaliesReviewed, triagedThisSession } =
    computeProgress(annotations);

  const anomalyPct =
    totalAnomalies > 0 ? Math.round((anomaliesReviewed / totalAnomalies) * 100) : 0;

  return (
    <div className="flex items-center gap-4">
      {/* Session momentum */}
      <SessionMomentum count={triagedThisSession} />

      {/* Anomalies meter — only shown when anomalies exist */}
      {totalAnomalies > 0 && (
        <Tooltip
          content={`${anomaliesReviewed} / ${totalAnomalies} anomalies reviewed`}
        >
          <div className="hidden sm:flex items-center gap-2 w-28">
            <div className="flex-1">
              <p className="text-xs font-medium uppercase tracking-wide text-[var(--color-fg-muted)] mb-1">
                Anomalies
              </p>
              <Progress
                value={anomalyPct}
                indicatorClassName="bg-[var(--color-anomaly)]"
              />
            </div>
            <span className="text-xs font-mono text-[var(--color-anomaly)]">
              {anomalyPct}%
            </span>
          </div>
        </Tooltip>
      )}
    </div>
  );
}
