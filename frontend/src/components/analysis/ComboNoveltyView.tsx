/**
 * ComboNoveltyView — rare / first-seen *combinations* of two or more fields.
 *
 * The multi-field sibling of ValueNoveltyView: calls the value_combo detector
 * and shows each finding as the field=value pairs of a rare tuple, plus its
 * surprise score. Auto mode combines the two highest-coverage recommended
 * fields; the analyst can pick 2–4 explicit fields instead.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronsRight, Clock, Info } from "lucide-react";
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
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, Event, ValueComboFinding } from "@/api/types";
import { cn } from "@/lib/cn";
import { anomalyFieldLabel as fieldLabel } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

const MIN_FIELDS = 2;
const MAX_FIELDS = 4;

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  /** Applies every (field, value) pair of a combination as a conjunctive filter. */
  onComboDrill?: (pairs: [string, string][]) => void;
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
  onRunIdChange?: (runId: string | undefined) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}

function comboLabel(f: ValueComboFinding): string {
  return f.fields.map((fld, i) => `${fieldLabel(fld)}=${f.values[i]}`).join(" · ");
}

function ComboRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onComboDrill,
  onJumpToTime,
  isFirstSeen,
}: {
  caseId: string;
  timelineId: string;
  finding: ValueComboFinding;
  onSelectEvent: (event: Event) => void;
  onComboDrill?: (pairs: [string, string][]) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
  isFirstSeen: boolean;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  const pairs = finding.fields.map((fld, i) => [fld, finding.values[i]] as [string, string]);

  return (
    <FindingShell
      highlight={isFirstSeen}
      details={finding.details}
      onClick={() => {
        if (finding.event_id) openEvent.mutate();
      }}
      actions={
        <>
          <FindingRowActions
            ts={finding.event?.timestamp ?? finding.first_seen}
            eventId={finding.event_id}
            disposition={{
              caseId,
              timelineId,
              detector: "value_combo",
              details: finding.details,
              sourceId: finding.event?.source_id,
            }}
          />
          {onComboDrill && (
            <button
              title="Filter to this combination"
              className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
              onClick={(e) => {
                e.stopPropagation();
                onComboDrill(pairs);
              }}
            >
              <ChevronsRight size={12} />
            </button>
          )}
          {onJumpToTime && (
            <button
              title="Jump to this event's time — clears active filters"
              className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
              onClick={(e) => {
                e.stopPropagation();
                const ts = finding.event?.timestamp ?? finding.first_seen;
                if (ts) onJumpToTime(ts, finding.event_id ?? undefined);
              }}
            >
              <Clock size={12} />
            </button>
          )}
        </>
      }
    >
      {/* Stacked field=value pairs */}
      <div className="space-y-0.5">
        {finding.fields.map((fld, i) => (
          <div key={fld} className="flex flex-wrap items-center gap-1">
            <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
              {fieldLabel(fld)}
            </span>
            <span
              className={cn(
                "font-mono text-xs break-all leading-tight",
                isFirstSeen
                  ? "text-[var(--color-accent)] font-medium"
                  : "text-[var(--color-fg-primary)]",
              )}
            >
              {finding.values[i]}
            </span>
          </div>
        ))}
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 pt-0.5 text-xs text-[var(--color-fg-muted)]">
        {isFirstSeen && (
          <span className="rounded bg-[var(--color-accent)] px-1 py-0.5 text-[9px] font-semibold text-white/90 uppercase tracking-wide">
            first seen
          </span>
        )}
        <span>
          count <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong>
        </span>
        <span>
          surprise{" "}
          <strong className="text-[var(--color-fg-secondary)]">{finding.score.toFixed(2)}</strong>
        </span>
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function ComboNoveltyView({
  caseId,
  timelineId,
  onSelectEvent,
  onComboDrill,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  // null = auto (top-2 recommended); string[] = explicit selection (≥ 2 to run).
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const explicitTooFew = selectedFields !== null && selectedFields.length < MIN_FIELDS;
  const fieldsParam = selectedFields !== null ? selectedFields.join(",") : undefined;
  const fl = useFindingsLimit();

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "value_combo", blKey, fieldsParam ?? "__auto__", fl.limit],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "value_combo",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    // Don't fire while the explicit selection is below the two-field minimum.
    enabled: !explicitTooFew && !needsBaseline,
    staleTime: 60_000,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "value_combo",
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
      (data?.results ?? []).filter((r): r is ValueComboFinding => r.type === "value_combo"),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return null;
      const label = comboLabel(f);
      const detail =
        data?.method === "temporal"
          ? `New combination: ${label} — absent from the ${(data.baseline_size ?? 0).toLocaleString()}-event ` +
            `baseline window; first appears in the detect window` +
            `${f.first_seen ? ` at ${fmtTs(f.first_seen)}` : ""} ` +
            `(${f.count} occurrence${f.count === 1 ? "" : "s"} here; surprise ${f.score.toFixed(2)})`
          : `Rare combination: ${label} — appears ${f.count} time${f.count === 1 ? "" : "s"}` +
            `${data?.baseline_size ? ` of ${data.baseline_size.toLocaleString()} events in the corpus` : ""} ` +
            `(surprise ${f.score.toFixed(2)})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "value_combo" as const,
        rawDetails: f.details,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  const cap = useCappedFindings(findings);

  if (needsBaseline) return <NeedsBaselinePrompt />;

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
          minSelected={MIN_FIELDS}
          maxSelected={MAX_FIELDS}
          autoCount={2}
          autoLabel="top 2"
        />
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      {!explicitTooFew && <DetectorStatusLine data={data} />}

      {explicitTooFew && (
        <div className="flex items-center gap-2 py-4 text-xs text-[var(--color-warning)]">
          <Info size={13} />
          <span>Pick at least two fields to combine, or reset to auto (top 2).</span>
        </div>
      )}

      {!explicitTooFew && isLoading && (
        <div className="flex justify-center py-6">
          <Spinner size={18} />
        </div>
      )}

      {!explicitTooFew && !isLoading && findings.length === 0 && (
        <div className="flex items-center gap-2 py-4 text-xs text-[var(--color-fg-muted)]">
          <Info size={13} />
          <span>
            {data?.status === "no_data"
              ? "No combinations detected. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "Not enough distinct fields to combine. Pick fields explicitly above."
                : "No rare combinations detected. All field pairings appear frequently."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} serverTotal={data?.total_findings} onLoadMore={fl.canRaise ? fl.raise : undefined} loadingMore={isFetching} dismissedCount={data?.dismissed_count} />
          {cap.shown.map((f, i) => (
            <ComboRow
              key={`${f.values.join("|")}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              finding={f}
              onSelectEvent={onSelectEvent}
              onComboDrill={onComboDrill}
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
          A combination can be rare even when each field's values are common —
          e.g. an <span className="font-mono">(action, hour)</span> pair like{" "}
          <span className="font-mono">(login_ok, 03:00)</span>. Auto mode combines the two
          highest-coverage recommended fields. Score = −log(count/total); higher is rarer.
        </span>
      </div>
    </div>
  );
}
