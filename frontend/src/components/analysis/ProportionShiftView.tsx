/**
 * ProportionShiftView — values whose *share* of events shifted significantly
 * between the baseline window and a suspect window.
 *
 * Calls the proportion_shift detector: per (field, value, suspect window) a
 * 2×2 G-test on the value's share, Benjamini–Hochberg FDR across the whole
 * run, plus a rate-ratio effect floor. Temporal-only — unlike every other
 * detector view this one requires the "Compare windows" frame with an active
 * baseline definition; there is no self-baseline mode to fall back to.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, TrendingUp, TrendingDown } from "lucide-react";
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
  useBaselineRequest,
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type { AnomalyMarker, Event, ProportionShiftFinding } from "@/api/types";
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

/** "0.5%" / "0.05%" — enough precision to read small shares without noise. */
function pct(rate: number): string {
  const v = rate * 100;
  return `${v.toPrecision(v >= 1 ? 3 : 2)}%`;
}

function ShiftRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: ProportionShiftFinding;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  const up = finding.direction === "up";
  const vanished = finding.count === 0;
  const lastSeenBaseline = finding.details["last_seen_baseline"] as string | undefined;

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
          ts={finding.event?.timestamp ?? finding.first_seen ?? lastSeenBaseline}
          eventId={finding.event_id}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
          markNormal={{
            caseId,
            timelineId,
            detector: "proportion_shift",
            details: finding.details,
            sourceId: finding.event?.source_id,
          }}
        />
      }
    >
      {/* Field + direction + value */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        {up ? (
          <TrendingUp size={12} className="shrink-0 text-[var(--color-error)]" />
        ) : (
          <TrendingDown size={12} className="shrink-0 text-[var(--color-warning)]" />
        )}
        <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {truncate(finding.value)}
        </span>
      </div>

      {/* Share change (the explainability shot) */}
      <div className="text-xs text-[var(--color-fg-muted)]">
        share of events{" "}
        <span className="font-mono text-[var(--color-fg-secondary)]">
          {pct(finding.baseline_rate)} → {vanished ? "absent" : pct(finding.window_rate)}
        </span>{" "}
        {vanished ? (
          <>(vanished from {String(finding.details["window_label"] ?? "the suspect window")})</>
        ) : (
          <>
            (
            <span className="font-mono text-[var(--color-fg-secondary)]">
              ×{finding.rate_ratio.toFixed(finding.rate_ratio >= 10 ? 0 : 1)}
            </span>
            , {up ? "more" : "less"} frequent)
          </>
        )}
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          q{" "}
          <strong className="text-[var(--color-fg-secondary)]">
            {finding.q_value < 0.000001 ? "<1e-6" : finding.q_value.toPrecision(2)}
          </strong>
        </span>
        <span>
          count{" "}
          <strong className="text-[var(--color-fg-secondary)]">
            {finding.count}
          </strong>{" "}
          vs {finding.baseline_count} baseline
        </span>
        <span>
          G <strong className="text-[var(--color-fg-secondary)]">{finding.g_statistic.toFixed(1)}</strong>
        </span>
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
        {vanished && lastSeenBaseline && <span>last seen {fmtTs(lastSeenBaseline)}</span>}
      </div>
    </FindingShell>
  );
}

export function ProportionShiftView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  // Temporal-only: useBaselineRequest returns needsBaseline=false in the
  // "self" frame (other detectors fall back to a self-baseline scan there);
  // this detector has no such mode, so it gates on the frame itself too.
  const frame = useBaselineStore((s) => s.frame);
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);
  const enabled = frame === "baseline" && !needsBaseline;

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "proportion_shift", blKey, fieldsParam ?? "__auto__"],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "proportion_shift",
        limit: 50,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    staleTime: 60_000,
    enabled,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "proportion_shift",
        limit: 50,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () =>
      (data?.results ?? []).filter(
        (r): r is ProportionShiftFinding => r.type === "proportion_shift",
      ),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts =
        f.event?.timestamp ?? f.first_seen ?? (f.details["window_start"] as string | undefined);
      if (!ts) return null;
      const label = `${fieldLabel(f.field)}=${truncate(f.value)}`;
      const detail =
        f.count === 0
          ? `Proportion shift: ${label} — present ${f.baseline_count}× in the baseline ` +
            `(${pct(f.baseline_rate)}) but absent from the suspect window ` +
            `(G=${f.g_statistic.toFixed(1)}, q=${f.q_value.toPrecision(2)})`
          : `Proportion shift: ${label} — share of events went ${pct(f.baseline_rate)} → ` +
            `${pct(f.window_rate)} (×${f.rate_ratio.toFixed(1)}, ${f.direction}) ` +
            `(G=${f.g_statistic.toFixed(1)}, q=${f.q_value.toPrecision(2)})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "proportion_shift" as const,
        rawDetails: f.details,
        windowEnd: (f.details["window_end"] as string | undefined) ?? null,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  const cap = useCappedFindings(findings);

  if (frame !== "baseline") {
    return (
      <div className="flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 text-xs text-[var(--color-fg-muted)]">
        <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--color-warning)]" />
        <span>
          Proportion shift compares a value's share of events between two
          windows, so it always needs a baseline — switch the frame to{" "}
          <strong>Compare windows</strong> and pick a baseline definition. It has
          no scan-all-events mode.
        </span>
      </div>
    );
  }
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
              ? "No proportion shifts. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "Nothing to test — the baseline window has no events, or no scanned field produced candidate values."
                : "No value's share of events changed significantly (and by at least the minimum ratio) between the baseline and the suspect windows."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} />
          {cap.shown.map((f, i) => (
            <ShiftRow
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
          Each (field, value, window) gets a 2×2 G-test of the value's share of
          events, baseline vs suspect window; all tests in the run are corrected
          together (Benjamini–Hochberg FDR), and a finding also needs the share
          to change by at least the minimum ratio. Values absent from the
          baseline are excluded — Rare values owns those; a value that vanishes
          from a suspect window is a maximal "down". Events cluster in bursts,
          so treat q-values as a ranking aid, not an exact false-positive
          probability.
        </span>
      </div>
    </div>
  );
}
