/**
 * IntervalPeriodicityView — values whose arrival *cadence* changed between the
 * baseline window and a suspect window.
 *
 * Calls the interval_periodicity detector: per (field, value), inter-arrival
 * deltas are computed within each window; a baseline-regular value whose rate
 * breaks (missed/accelerated — Poisson-rate test, covers per-value silence)
 * or a baseline-bursty value that becomes suspiciously regular (Greenwood
 * spacing test — beaconing). Temporal-only, like ProportionShiftView: it
 * requires the "Compare windows" frame with an active baseline definition.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, Radio, TimerOff, TimerReset } from "lucide-react";
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
  useShowDismissed,
  useBaselineRequest,
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type { AnomalyMarker, Event, IntervalPeriodicityFinding } from "@/api/types";
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

/** "45 s" / "12.5 min" / "3.2 h" — median cadence at readable precision. */
function fmtInterval(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 120) return `${seconds.toPrecision(seconds >= 10 ? 3 : 2)} s`;
  if (seconds < 7200) return `${(seconds / 60).toPrecision(3)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toPrecision(3)} h`;
  return `${(seconds / 86400).toPrecision(3)} d`;
}

function directionIcon(direction: IntervalPeriodicityFinding["direction"]) {
  if (direction === "new_regularity")
    return <Radio size={12} className="shrink-0 text-[var(--color-error)]" />;
  if (direction === "missed")
    return <TimerOff size={12} className="shrink-0 text-[var(--color-warning)]" />;
  return <TimerReset size={12} className="shrink-0 text-[var(--color-error)]" />;
}

function IntervalRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: IntervalPeriodicityFinding;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  const beacon = finding.direction === "new_regularity";
  const silent = finding.count === 0;
  const lastSeenBaseline = finding.details["last_seen_baseline"] as string | undefined;
  const expectedCount = finding.details["expected_count"] as number | undefined;
  const rateRatio = finding.details["rate_ratio"] as number | undefined;

  return (
    <FindingShell
      dismissed={finding.dismissed}
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
          disposition={{
            caseId,
            timelineId,
            detector: "interval_periodicity",
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
        {directionIcon(finding.direction)}
        <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {truncate(finding.value)}
        </span>
      </div>

      {/* Cadence change (the explainability shot) */}
      <div className="text-xs text-[var(--color-fg-muted)]">
        {beacon ? (
          <>
            bursty in the baseline, now every{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {fmtInterval(finding.window_median_interval)}
            </span>{" "}
            (CV{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {finding.window_cv ?? "—"}
            </span>
            , beaconing pattern)
          </>
        ) : silent ? (
          <>
            arrived every{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {fmtInterval(finding.baseline_median_interval)}
            </span>{" "}
            in the baseline — 0
            {expectedCount ? ` of ~${Math.round(expectedCount)} expected` : ""} events in{" "}
            {String(finding.details["window_label"] ?? "the suspect window")}
          </>
        ) : (
          <>
            cadence{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {fmtInterval(finding.baseline_median_interval)} →{" "}
              {fmtInterval(finding.window_median_interval)}
            </span>{" "}
            (rate ×{rateRatio !== undefined ? rateRatio.toFixed(rateRatio >= 10 ? 0 : 1) : "?"},{" "}
            {finding.direction})
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
          <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong> vs{" "}
          {finding.baseline_count} baseline
        </span>
        {finding.baseline_cv !== null && !beacon && (
          <span>
            baseline CV{" "}
            <strong className="text-[var(--color-fg-secondary)]">{finding.baseline_cv}</strong>
          </span>
        )}
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
        {silent && lastSeenBaseline && <span>last seen {fmtTs(lastSeenBaseline)}</span>}
      </div>
    </FindingShell>
  );
}

export function IntervalPeriodicityView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  // Temporal-only, same frame gating as ProportionShiftView: cadence can only
  // change between two windows, so there is no self-baseline fallback.
  const frame = useBaselineStore((s) => s.frame);
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);
  const fl = useFindingsLimit();
  const sd = useShowDismissed();
  const enabled = frame === "baseline" && !needsBaseline;

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: [
      "anomalies",
      caseId,
      timelineId,
      "interval_periodicity",
      blKey,
      fieldsParam ?? "__auto__",
      fl.limit,
      sd.enabled,
    ],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "interval_periodicity",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
        ...(sd.enabled ? { include_dismissed: true } : {}),
      }),
    staleTime: 60_000,
    enabled,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "interval_periodicity",
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
      (data?.results ?? []).filter(
        (r): r is IntervalPeriodicityFinding => r.type === "interval_periodicity",
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
        f.direction === "new_regularity"
          ? `Interval cadence: ${label} — bursty in the baseline but arrives every ` +
            `~${fmtInterval(f.window_median_interval)} in the suspect window ` +
            `(beaconing; q=${f.q_value.toPrecision(2)})`
          : f.count === 0
            ? `Interval cadence: ${label} — arrived every ~${fmtInterval(f.baseline_median_interval)} ` +
              `in the baseline but is silent in the suspect window (q=${f.q_value.toPrecision(2)})`
            : `Interval cadence: ${label} — arrival rate ${f.direction} in the suspect window ` +
              `(${fmtInterval(f.baseline_median_interval)} → ${fmtInterval(f.window_median_interval)}; ` +
              `q=${f.q_value.toPrecision(2)})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "interval_periodicity" as const,
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
          Interval cadence compares a value's arrival rhythm between two
          windows, so it always needs a baseline — switch the frame to{" "}
          <strong>Compare windows</strong> and pick a baseline definition. It
          has no scan-all-events mode.
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
              ? "No cadence findings. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "Nothing to test — the baseline window has no events, or no scanned field produced candidate values."
                : "No value's arrival cadence changed significantly — no regular value broke rhythm, and no bursty value became suspiciously regular."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar
            total={cap.total}
            shownCount={cap.shown.length}
            hasMore={cap.hasMore}
            expanded={cap.expanded}
            onToggle={cap.toggle}
            serverTotal={data?.total_findings}
            onLoadMore={fl.canRaise ? fl.raise : undefined}
            loadingMore={isFetching}
            dismissedCount={data?.dismissed_count}
            showDismissed={sd.enabled}
            onToggleDismissed={sd.toggle}
          />
          {cap.shown.map((f, i) => (
            <IntervalRow
              key={`${f.field}:${f.value}:${f.direction}:${i}`}
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
          Inter-arrival gaps are measured per (field, value) inside each window.
          Values that were regular in the baseline are tested for rate breaks
          (a silent heartbeat is the strongest case); values that were bursty
          are tested for suspiciously even spacing (beaconing). All tests share
          one Benjamini–Hochberg correction, and each direction has effect
          floors so significance alone never flags. First-seen values are
          excluded — Rare values owns those.
        </span>
      </div>
    </div>
  );
}
