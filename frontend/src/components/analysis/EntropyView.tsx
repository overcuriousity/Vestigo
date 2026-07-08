/**
 * EntropyView — values whose Shannon character entropy falls outside the
 * field's learned band.
 *
 * Calls the entropy detector. Both modes use a Tukey IQR fence over
 * per-distinct-value entropies (whole corpus, or the baseline window in
 * temporal mode). Above-band values look random (DGA domains, encoded
 * payloads); below-band values look degenerate (padding, character stuffing).
 * The entropy + band are the explainability money-shot, rendered inline.
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
  ModeToggle,
  useBaselineRequest,
  RefreshButton,
  TagFindingsBar,
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
  type DetectorMode,
} from "./detector-shared";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, EntropyFinding, Event } from "@/api/types";
import { anomalyFieldLabel as fieldLabel, truncate } from "@/lib/format";
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

function EntropyRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: EntropyFinding;
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
          value={finding.value}
          ts={finding.event?.timestamp ?? finding.first_seen}
          eventId={finding.event_id}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
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
        <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {truncate(finding.value)}
        </span>
      </div>

      {/* Entropy + band (the explainability shot) */}
      <div className="text-xs text-[var(--color-fg-muted)]">
        {finding.entropy.toFixed(2)} bits — {above ? "above" : "below"} band{" "}
        <span className="font-mono text-[var(--color-fg-secondary)]">
          [{finding.lower.toFixed(2)}, {finding.upper.toFixed(2)}]
        </span>{" "}
        ({above ? "random-looking" : "degenerate/repetitive"})
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

export function EntropyView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const [mode, setMode] = useState<DetectorMode>("self");
  const { params: blParams, key: blKey } = useBaselineRequest(mode);
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "entropy", blKey, fieldsParam ?? "__auto__"],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "entropy",
        limit: 50,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    staleTime: 60_000,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "entropy",
        limit: 50,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () => (data?.results ?? []).filter((r): r is EntropyFinding => r.type === "entropy"),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return null;
      const label = `${fieldLabel(f.field)}=${truncate(f.value)}`;
      const bandDesc =
        data?.method === "temporal-iqr"
          ? "baseline-window entropy IQR fence"
          : "corpus entropy IQR fence";
      const look = f.direction === "above" ? "random-looking" : "degenerate/repetitive";
      const detail =
        `Entropy outlier: ${label} — ${f.entropy.toFixed(2)} bits, ${f.direction} the ` +
        `learned band [${f.lower.toFixed(2)}, ${f.upper.toFixed(2)}] (${bandDesc}; ${look}; ` +
        `${f.count} occurrence${f.count === 1 ? "" : "s"})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "entropy" as const,
        rawDetails: f.details,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        <ModeToggle mode={mode} onChange={setMode} />
        <span className="flex-1" />
        <AnomalyFieldPicker
          caseId={caseId}
          timelineId={timelineId}
          selected={selectedFields}
          onChange={setSelectedFields}
          autoIncludesIdentifiers
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
              ? "No entropy outliers. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "No fields with enough distinct baseline values (min length 6 chars). Pick fields explicitly above."
                : mode === "temporal"
                  ? "No detect-window values outside the baseline entropy band."
                  : "No values outside the corpus entropy band."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          {findings.map((f, i) => (
            <EntropyRow
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
          {mode === "temporal"
            ? "Temporal mode learns the entropy band [q1−1.5·IQR, q3+1.5·IQR] from the baseline window's distinct values and flags detect-window values outside it."
            : "Self-baseline mode flags values outside the Tukey fence [q1−1.5·IQR, q3+1.5·IQR] over the entropy of every distinct value."}{" "}
          Entropy is computed per distinct value from character frequencies alone — high ≈ random-looking (DGA, encoded payloads), low ≈ repetitive — never from what a value means.
        </span>
      </div>
    </div>
  );
}
