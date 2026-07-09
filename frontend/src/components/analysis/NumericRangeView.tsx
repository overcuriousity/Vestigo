/**
 * NumericRangeView — numeric field values falling outside a learned band.
 *
 * Calls the numeric_range detector. Self-baseline mode uses a Tukey IQR fence
 * over the whole corpus; temporal mode learns the baseline window's min/max.
 * Each finding shows the value, its direction, and the band it violates — the
 * band is the explainability money-shot, rendered inline.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, MoveUp, MoveDown } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { AnomalyFieldPicker } from "./AnomalyFieldPicker";
import {
  DetectorStatusLine,
  FindingRowActions,
  FindingShell,
  NeedsBaselinePrompt,
  ResultsBar,
  RefreshButton,
  TagFindingsBar,
} from "./detector-shared";
import {
  useCappedFindings,
  useFindingsLimit,
  useBaselineRequest,
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, Event, NumericRangeFinding } from "@/api/types";
import { anomalyFieldLabel as fieldLabel } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
  onRunIdChange?: (runId: string | undefined) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}

/** Compact numeric formatting — avoids "50000.0" and huge decimal tails. */
function fmtNum(n: number): string {
  if (Number.isInteger(n)) return n.toLocaleString();
  return n.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

function RangeRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: NumericRangeFinding;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  const above = finding.direction === "above";

  return (
    <FindingShell
      details={finding.details}
      onClick={() => {
        if (finding.event_id) openEvent.mutate();
      }}
      actions={
        <FindingRowActions
          field={finding.field}
          value={String(finding.value)}
          ts={finding.event?.timestamp ?? finding.first_seen}
          eventId={finding.event_id}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
          markNormal={{
            caseId,
            timelineId,
            detector: "numeric_range",
            details: finding.details,
            sourceId: finding.event?.source_id,
          }}
        />
      }
    >
      {/* Field + value */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        {above ? (
          <MoveUp size={12} className="shrink-0 text-[var(--color-error)]" />
        ) : (
          <MoveDown size={12} className="shrink-0 text-[var(--color-warning)]" />
        )}
        <span className="font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {fmtNum(finding.value)}
        </span>
      </div>

      {/* Band (the explainability shot) */}
      <div className="text-xs text-[var(--color-fg-muted)]">
        {above ? "above" : "below"} band{" "}
        <span className="font-mono text-[var(--color-fg-secondary)]">
          [{fmtNum(finding.lower)}, {fmtNum(finding.upper)}]
        </span>
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          count <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong>
        </span>
        <span>
          severity{" "}
          <strong className="text-[var(--color-fg-secondary)]">{finding.score.toFixed(1)}×</strong>
        </span>
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function NumericRangeView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);
  const fl = useFindingsLimit();

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "numeric_range", blKey, fieldsParam ?? "__auto__", fl.limit],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "numeric_range",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    staleTime: 60_000,
    enabled: !needsBaseline,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "numeric_range",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () =>
      (data?.results ?? []).filter((r): r is NumericRangeFinding => r.type === "numeric_range"),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return null;
      const label = `${fieldLabel(f.field)}=${fmtNum(f.value)}`;
      const bandDesc = data?.method === "temporal-range" ? "baseline min/max" : "IQR fence";
      const detail =
        `Out-of-range value: ${label} — ${f.direction} the learned band ` +
        `[${fmtNum(f.lower)}, ${fmtNum(f.upper)}] (${bandDesc}; ${f.count} occurrence${
          f.count === 1 ? "" : "s"
        })`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "numeric_range" as const,
        rawDetails: f.details,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  const cap = useCappedFindings(findings);

  if (needsBaseline) return <NeedsBaselinePrompt />;

  const isTemporal = data?.method === "temporal-range";

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="flex-1" />
        <AnomalyFieldPicker
          caseId={caseId}
          timelineId={timelineId}
          selected={selectedFields}
          onChange={setSelectedFields}
          autoCount={15}
          numeric
        />
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      <DetectorStatusLine data={data} />

      {isLoading && (
        <div className="flex justify-center py-6">
          <Spinner size={18} />
        </div>
      )}

      {!isLoading && findings.length === 0 && (
        <div className="flex items-center gap-2 py-4 text-xs text-[var(--color-fg-muted)]">
          <Info size={13} />
          <span>
            {data?.status === "no_data"
              ? "No numeric out-of-range values. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "No numeric fields with enough baseline samples. Pick fields explicitly above."
                : isTemporal
                  ? "No values outside the baseline window's min/max range."
                  : "No numeric outliers outside the IQR fence."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} serverTotal={data?.total_findings} onLoadMore={fl.canRaise ? fl.raise : undefined} loadingMore={isFetching} />
          {cap.shown.map((f, i) => (
            <RangeRow
              key={`${f.field}:${f.value}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              finding={f}
              onSelectEvent={onSelectEvent}
              onDrillField={onDrillField}
              onJumpToTime={onJumpToTime}
            />
          ))}
        </div>
      )}

      {/* Tag action */}
      {findings.length > 0 && (
        <TagFindingsBar mutation={tagMutation} label={`Tag ${findings.length} as anomaly`} />
      )}

      {/* Methodology note */}
      <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)] pt-1">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          {isTemporal
            ? "Comparing windows: learns the exact min/max of the baseline window and flags suspect-window values outside it."
            : "Scanning all events: flags values outside the Tukey fence [q1−1.5·IQR, q3+1.5·IQR] over the whole corpus."}{" "}
          Numeric-looking ids (status codes, ports) qualify syntactically — prefer comparing against a baseline for those.
        </span>
      </div>
    </div>
  );
}
