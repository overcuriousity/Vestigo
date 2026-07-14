/**
 * OrderViolationsView — events whose timestamp runs backwards in record order.
 *
 * Calls the timestamp_order detector (mode-less: no baseline/detect split) and
 * groups findings under a per-source header showing the source's total
 * violation count and worst backwards jump. Each row shows the violating
 * timestamp, the earlier predecessor it follows, and the skew in seconds.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, Rewind } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  DetectorStatusLine,
  DismissedToggle,
  FindingRowActions,
  FindingShell,
  RefreshButton,
  TagFindingsBar,
} from "./detector-shared";
import {
  useAnomalyMarkers,
  useDetectorRunId,
  useFindingsLimit,
  useShowDismissed,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, Event, TimestampOrderFinding } from "@/api/types";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  /** Called whenever the finding set changes — feeds the histogram overlay and event grid. */
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
  /** Called with the latest scan's persisted run_id, so the grid can filter to it. */
  onRunIdChange?: (runId: string | undefined) => void;
  /** Scrolls the main grid to this finding's timestamp, clearing filters first. */
  onJumpToTime?: (ts: string, eventId?: string) => void;
}

/** Human "3.5s" / "2m 05s" / "1h 03m" for a backwards jump. */
function fmtSkew(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${String(s).padStart(2, "0")}s`;
  }
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

function ViolationRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: TimestampOrderFinding;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  return (
    <FindingShell
      dismissed={finding.dismissed}
      confirmed={finding.confirmed}
      details={finding.details}
      onClick={() => openEvent.mutate()}
      title={`Event at ${fmtTs(finding.timestamp)} follows a record dated ${fmtTs(finding.prev_timestamp)}`}
      actions={
        <FindingRowActions
          ts={finding.timestamp}
          eventId={finding.event_id}
          onJumpToTime={onJumpToTime}
          disposition={{
            caseId,
            timelineId,
            detector: "timestamp_order",
            details: finding.details,
            sourceId: finding.source_id,
          }}
        />
      }
    >
      {/* Skew headline */}
      <div className="flex flex-wrap items-center gap-1.5">
        <Rewind size={12} className="shrink-0 text-[var(--color-error)]" />
        <span className="font-mono text-xs font-medium text-[var(--color-error)]">
          −{fmtSkew(finding.skew_seconds)}
        </span>
        <span className="text-xs text-[var(--color-fg-muted)]">backwards</span>
      </div>

      {/* Timestamp ordering */}
      <div className="text-xs text-[var(--color-fg-secondary)]">
        <span className="font-mono">{fmtTs(finding.timestamp)}</span>
        <span className="text-[var(--color-fg-muted)]"> follows </span>
        <span className="font-mono">{fmtTs(finding.prev_timestamp)}</span>
      </div>

      {/* Record position */}
      <div className="text-xs text-[var(--color-fg-muted)]">
        byte offset {finding.byte_offset.toLocaleString()}
        {finding.line_number > 0 && <> · line {finding.line_number.toLocaleString()}</>}
      </div>
    </FindingShell>
  );
}

export function OrderViolationsView({
  caseId,
  timelineId,
  onSelectEvent,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const [minSkewInput, setMinSkewInput] = useState("1");
  const qc = useQueryClient();

  const debouncedMinSkew = useDebouncedValue(minSkewInput, 400);
  const parsedMinSkew = Number(debouncedMinSkew);
  const minSkewParam =
    Number.isFinite(parsedMinSkew) && parsedMinSkew >= 0 ? parsedMinSkew : undefined;

  const fl = useFindingsLimit(100);
  const sd = useShowDismissed();

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "timestamp_order", minSkewParam, fl.limit, sd.keyPart],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "timestamp_order",
        min_skew_seconds: minSkewParam,
        limit: fl.limit,
        ...(sd.enabled ? { include_dismissed: true } : {}),
      }),
    staleTime: 60_000,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "timestamp_order",
        min_skew_seconds: minSkewParam,
        limit: fl.limit,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () =>
      (data?.results ?? []).filter(
        (r): r is TimestampOrderFinding => r.type === "timestamp_order",
      ),
    [data],
  );

  // Group by source, preserving the worst-skew-first order within each group.
  const grouped = useMemo(() => {
    const bySource = new Map<string, TimestampOrderFinding[]>();
    for (const f of findings) {
      const list = bySource.get(f.source_id) ?? [];
      list.push(f);
      bySource.set(f.source_id, list);
    }
    return Array.from(bySource.entries());
  }, [findings]);

  useAnomalyMarkers(
    findings,
    (f): AnomalyMarker => ({
      ts: f.timestamp,
      label: `−${fmtSkew(f.skew_seconds)} @ ${f.source_id}`,
      detail:
        `Out-of-order timestamp: event at ${fmtTs(f.timestamp)} occurs after a ` +
        `record dated ${fmtTs(f.prev_timestamp)} (${f.skew_seconds.toFixed(1)}s backwards; ` +
        `record order = byte offset ${f.byte_offset.toLocaleString()})`,
      eventId: f.event_id,
      sourceId: f.source_id,
      detector: "timestamp_order" as const,
      rawDetails: f.details,
    }),
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Min jump
        </span>
        <span className="flex items-center gap-1 text-xs text-[var(--color-fg-muted)]">
          ≥
          <input
            type="number"
            min="0"
            step="0.5"
            value={minSkewInput}
            onChange={(e) => setMinSkewInput(e.target.value)}
            title="Minimum backwards jump in seconds — smaller jumps (logger jitter) are ignored"
            className="w-14 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1 py-0.5 text-xs text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
          />
          s
        </span>
        <span className="flex-1" />
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      <DetectorStatusLine data={data} baselineLabel="events scanned" />

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
              ? "No events with timestamps ingested yet."
              : "No out-of-order timestamps. Records are chronological in file order."}
          </span>
        </div>
      )}

      {/* Server-side truncation notice — the per-source "showing" counters
          below cover the per-source hydration cap, not the global limit.
          Also shown untruncated when dismissed findings exist, so the
          show-dismissed toggle always has a home. */}
      {((data?.total_findings ?? 0) > findings.length ||
        (data?.dismissed_count ?? 0) > 0 ||
        sd.enabled) && (
        <div className="flex items-center justify-between text-[11px] text-[var(--color-fg-muted)]">
          <span>
            {findings.length} of {data?.total_findings ?? findings.length} violations
            {((data?.dismissed_count ?? 0) > 0 || sd.enabled) && (
              <>
                {` · ${data?.dismissed_count ?? 0} dismissed`}{" "}
                <DismissedToggle shown={sd.enabled} onToggle={sd.toggle} />
              </>
            )}
          </span>
          {fl.canRaise && (
            <button
              className="text-[var(--color-accent)] hover:underline disabled:opacity-50"
              onClick={fl.raise}
              disabled={isFetching}
            >
              {isFetching ? "Loading…" : "Load more"}
            </button>
          )}
        </div>
      )}

      {/* Findings grouped by source */}
      {grouped.map(([sourceId, rows]) => {
        const summary = rows[0]?.details as
          | { source_total_violations?: number; source_max_skew?: number }
          | undefined;
        const totalViol = summary?.source_total_violations ?? rows.length;
        const maxSkew = summary?.source_max_skew ?? rows[0]?.skew_seconds ?? 0;
        return (
          <div key={sourceId} className="space-y-1.5">
            <div className="flex items-baseline justify-between gap-2 px-0.5">
              <span className="truncate font-mono text-xs font-medium text-[var(--color-fg-secondary)]">
                {sourceId}
              </span>
              <span className="shrink-0 text-[10px] text-[var(--color-fg-muted)]">
                {totalViol.toLocaleString()} violation{totalViol === 1 ? "" : "s"} · worst −
                {fmtSkew(maxSkew)}
                {rows.length < totalViol && <> · showing {rows.length}</>}
              </span>
            </div>
            <div className="space-y-1.5">
              {rows.map((f, i) => (
                <ViolationRow
                  key={`${f.event_id}:${i}`}
                  caseId={caseId}
                  timelineId={timelineId}
                  finding={f}
                  onSelectEvent={onSelectEvent}
                  onJumpToTime={onJumpToTime}
                />
              ))}
            </div>
          </div>
        );
      })}

      {/* Tag action */}
      {findings.length > 0 && (
        <TagFindingsBar mutation={tagMutation} label={`Tag ${findings.length} as anomaly`} />
      )}

      {/* Methodology note */}
      <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)] pt-1">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          Order = position in the source file (byte offset), not the parsed
          timestamp. Backwards jumps can indicate log tampering, clock resets,
          or legitimately interleaved multi-writer logs. Score = backwards jump
          in seconds.
        </span>
      </div>
    </div>
  );
}
