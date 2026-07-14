/**
 * ValueNoveltyView — ranked list of rare / first-seen field values.
 *
 * Calls the value_novelty detector endpoint and shows each finding as an
 * interactive row: field badge + value + surprise score + first-seen timestamp
 * + click-to-drill.  "First-seen in detect window" findings are highlighted.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { shouldInvalidate } from "@/hooks/useCaseStream";
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
  useBaselineRequest,
  useCappedFindings,
  useFindingsLimit,
  useShowDismissed,
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import type { Event, ValueNoveltyFinding } from "@/api/types";
import { cn } from "@/lib/cn";
import { anomalyFieldLabel as fieldLabel } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  /** Called when analyst drills into findings — passes a field filter. */
  onDrillField?: (field: string, value: string) => void;
  /** Called whenever the finding set changes — feeds the histogram overlay and event grid. */
  onFindingsChange?: (markers: import("@/api/types").AnomalyMarker[]) => void;
  /** Called with the latest scan's persisted run_id, so the grid can filter to it. */
  onRunIdChange?: (runId: string | undefined) => void;
  /** Scrolls the main grid to this finding's timestamp, clearing filters first. */
  onJumpToTime?: (ts: string, eventId?: string) => void;
}

interface FindingRowProps {
  caseId: string;
  timelineId: string;
  finding: ValueNoveltyFinding;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
  isFirstSeen: boolean;
}

function FindingRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
  isFirstSeen,
}: FindingRowProps) {
  // The detector's finding only carries a lightweight, partial "event" stub
  // (missing artifact/tags/attributes/etc.) for bookkeeping — fetch the full
  // event record before handing it to the Event Detail panel.
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  return (
    <FindingShell
      dismissed={finding.dismissed}
      confirmed={finding.confirmed}
      highlight={isFirstSeen}
      details={finding.details}
      onClick={() => {
        if (finding.event_id) {
          openEvent.mutate();
        }
      }}
      actions={
        <FindingRowActions
          field={finding.field}
          value={finding.value}
          ts={finding.event?.timestamp ?? finding.first_seen}
          eventId={finding.event_id}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
          disposition={{
            caseId,
            timelineId,
            detector: "value_novelty",
            details: finding.details,
            sourceId: finding.event?.source_id,
          }}
        />
      }
    >
      {/* Field badge + value */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        <span
          className={cn(
            "font-mono text-xs break-all leading-tight",
            isFirstSeen
              ? "text-[var(--color-accent)] font-medium"
              : "text-[var(--color-fg-primary)]",
          )}
        >
          {finding.value}
        </span>
        {isFirstSeen && (
          <span className="rounded bg-[var(--color-accent)] px-1 py-0.5 text-[9px] font-semibold text-white/90 uppercase tracking-wide">
            first seen
          </span>
        )}
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          count <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong>
        </span>
        <span>
          surprise <strong className="text-[var(--color-fg-secondary)]">{finding.score.toFixed(2)}</strong>
        </span>
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function ValueNoveltyView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  // null = use backend smart default; string[] = explicit analyst selection.
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  // Compute the fields param for the API. null → omit param → backend
  // auto-selects. [] (explicitly deselected every field) can't be sent as an
  // empty string — the API client drops empty-string params — so it's sent as
  // the reserved "__none__" token, which the backend maps to "scan nothing".
  const fieldsParam = fieldsParamOf(selectedFields);
  const fl = useFindingsLimit();
  const sd = useShowDismissed();

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "value_novelty", blKey, fieldsParam ?? "__auto__", fl.limit, sd.keyPart],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "value_novelty",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
        ...(sd.enabled ? { include_dismissed: true } : {}),
      }),
    staleTime: 60_000,
    enabled: !needsBaseline,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "value_novelty",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ predicate: (query) => shouldInvalidate(query.queryKey, caseId) });
    },
  });

  // Memoized against `data` (stable react-query reference) so the marker
  // hook below doesn't re-fire — and loop — on every render.
  const findings = useMemo(
    () =>
      (data?.results ?? []).filter(
        (r): r is ValueNoveltyFinding => r.type === "value_novelty",
      ),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return null;
      const label = `${fieldLabel(f.field)}=${f.value}`;
      // Temporal-mode findings are, by construction, absent from the
      // baseline window (the backend only returns baseline_cnt = 0 rows) —
      // a materially stronger, more specific claim than "rare", so say
      // exactly that rather than reusing the self-baseline phrasing.
      const detail =
        data?.method === "temporal"
          ? `New value: ${label} — absent from the ${(data.baseline_size ?? 0).toLocaleString()}-event ` +
            `baseline window; first appears in the detect window` +
            `${f.first_seen ? ` at ${fmtTs(f.first_seen)}` : ""} ` +
            `(${f.count} occurrence${f.count === 1 ? "" : "s"} here; surprise ${f.score.toFixed(2)})`
          : `Rare value: ${label} — appears ${f.count} time${f.count === 1 ? "" : "s"}` +
            `${data?.baseline_size ? ` of ${data.baseline_size.toLocaleString()} events in the corpus` : ""} ` +
            `(surprise ${f.score.toFixed(2)})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "value_novelty" as const,
        rawDetails: f.details,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  const cap = useCappedFindings(findings);

  if (needsBaseline) return <NeedsBaselinePrompt />;

  const isTemporal = data?.method === "temporal";

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
            {selectedFields !== null && selectedFields.length === 0
              ? "No fields selected to scan. Pick fields above, or reset to auto."
              : data?.status === "no_data"
                ? "No rare values detected. No events ingested yet."
                : "No rare values detected. All field values appear frequently."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} serverTotal={data?.total_findings} onLoadMore={fl.canRaise ? fl.raise : undefined} loadingMore={isFetching} dismissedCount={data?.dismissed_count} showDismissed={sd.enabled} onToggleDismissed={sd.toggle} />
          {cap.shown.map((f, i) => (
            <FindingRow
              key={`${f.field}:${f.value}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              finding={f}
              onSelectEvent={onSelectEvent}
              onDrillField={onDrillField}
              onJumpToTime={onJumpToTime}
              isFirstSeen={data?.method === "temporal"}
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
          Rare ≠ malicious.{" "}
          {isTemporal
            ? "Comparing windows: flags any value absent from the baseline window but present in a suspect window. Score = −log(count / suspect-window events)."
            : "Scanning all events: flags values that appear ≤ rarity floor times in the whole corpus. Score = −log(count/total); higher is rarer."}
        </span>
      </div>
    </div>
  );
}
