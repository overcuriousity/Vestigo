/**
 * FrequencyView — ranked list of frequency-anomaly findings (z-score spikes
 * or silences in a per-series event count).
 *
 * Calls the frequency detector endpoint and renders each finding as a row:
 * series field/value, observed vs. expected count, z-score, and a window
 * time range. Clicking a row (onDrillField/onJumpToTime) narrows the event
 * explorer to that series value and scrolls/highlights the anomalous window.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Info,
  TrendingUp,
  TrendingDown,
  Clock,
} from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { shouldInvalidate } from "@/hooks/useCaseStream";
import {
  DetectorStatusLine,
  NeedsBaselinePrompt,
  ResultsBar,
  RefreshButton,
  TagFindingsBar,
} from "./detector-shared";
import {
  useCappedFindings,
  useFindingsLimit,
  useAnomalyMarkers,
  useBaselineRequest,
  useDetectorRunId,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, FrequencyFinding } from "@/api/types";
import { cn } from "@/lib/cn";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  /**
   * Called when an anomalous window is clicked — narrows the explorer to the
   * window's time range and filters to series_field=series_value.
   */
  onDrillField?: (field: string, value: string, start: string, end: string) => void;
  /** Called whenever the finding set changes — feeds the histogram overlay and event grid. */
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
  /** Called with the latest scan's persisted run_id, so the grid can filter to it. */
  onRunIdChange?: (runId: string | undefined) => void;
  /** Scrolls the main grid to the window's start, clearing filters first, and highlights the window. */
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

const STATIC_SERIES_FIELD_OPTIONS = [
  { value: "artifact", label: "Artifact type", group: "standard" },
  { value: "timestamp_desc", label: "Event category", group: "standard" },
  { value: "display_name", label: "Display name", group: "standard" },
  { value: "parser_name", label: "Parser", group: "standard" },
  { value: "source_file", label: "Source file", group: "standard" },
];

interface FreqFindingRowProps {
  finding: FrequencyFinding;
  zThreshold: number;
  onDrillField?: (field: string, value: string, start: string, end: string) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

function FreqFindingRow({ finding, zThreshold, onDrillField, onJumpToTime }: FreqFindingRowProps) {
  const isSpike = finding.z_score > 0;
  // Severity bands scale off the analyst's own z_threshold (not fixed
  // constants) — otherwise a raised threshold (e.g. z >= 6) would still
  // paint every returned finding "high", since findings this close to a
  // hardcoded 5 would already all clear a threshold above it.
  const severity =
    Math.abs(finding.z_score) >= zThreshold * 2
      ? "high"
      : Math.abs(finding.z_score) >= zThreshold * 1.2
        ? "medium"
        : "low";

  return (
    <div
      className={cn(
        "group flex items-start gap-2 rounded border p-2 cursor-pointer transition-colors",
        severity === "high"
          ? "border-[var(--color-error)]/50 bg-[var(--color-error)]/5 hover:bg-[var(--color-error)]/10"
          : severity === "medium"
            ? "border-[var(--color-warning)]/50 bg-[var(--color-warning)]/5 hover:bg-[var(--color-warning)]/10"
            : "border-[var(--color-border)] hover:border-[var(--color-border-focus)]",
      )}
      onClick={() =>
        onDrillField?.(
          finding.series_field,
          finding.series_value,
          finding.window_start,
          finding.window_end,
        )
      }
      title={`Filter to ${finding.series_field}=${finding.series_value} and zoom to ${fmtTs(finding.window_start)} – ${fmtTs(finding.window_end)}`}
    >
      {/* Direction icon */}
      <div className="mt-0.5 shrink-0">
        {isSpike ? (
          <TrendingUp
            size={13}
            className={
              severity === "high"
                ? "text-[var(--color-error)]"
                : "text-[var(--color-warning)]"
            }
          />
        ) : (
          <TrendingDown size={13} className="text-[var(--color-fg-muted)]" />
        )}
      </div>

      <div className="min-w-0 flex-1 space-y-0.5">
        {/* Series value */}
        <div className="flex flex-wrap items-center gap-1">
          <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
            {finding.series_field}
          </span>
          <span className="font-mono text-xs text-[var(--color-fg-primary)] font-medium break-all">
            {finding.series_value}
          </span>
        </div>

        {/* Window label */}
        <div className="text-xs text-[var(--color-fg-muted)]">
          {fmtTs(finding.window_start)} – {fmtTs(finding.window_end)}
        </div>

        {/* Count vs expected */}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-[var(--color-fg-secondary)]">
            <strong>{finding.observed}</strong> events
          </span>
          <span className="text-[var(--color-fg-muted)]">
            expected {finding.expected.toFixed(1)}
          </span>
          <span
            className={cn(
              "font-semibold",
              severity === "high"
                ? "text-[var(--color-error)]"
                : severity === "medium"
                  ? "text-[var(--color-warning)]"
                  : "text-[var(--color-fg-muted)]",
            )}
          >
            z = {finding.z_score > 0 ? "+" : ""}
            {finding.z_score.toFixed(2)}
          </span>
        </div>
      </div>

      {onJumpToTime && (
        <div className="shrink-0 flex items-center opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            title="Jump to this window's start — clears active filters and highlights the window"
            className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
            onClick={(e) => {
              e.stopPropagation();
              onJumpToTime(finding.window_start, finding.event_id ?? undefined, finding.window_end);
            }}
          >
            <Clock size={12} />
          </button>
        </div>
      )}
    </div>
  );
}

export function FrequencyView({
  caseId,
  timelineId,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const [seriesField, setSeriesField] = useState("artifact");
  const [zThresholdInput, setZThresholdInput] = useState("2.5");
  const qc = useQueryClient();
  // Frequency has no local mode: it follows the global frame like every other
  // detector. `self` → whole-timeline z-score; `baseline` → score suspect
  // windows against the active definition's baseline.
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();

  // Debounce so a full detector scan doesn't fire on every keystroke
  // (including transient invalid states like a bare "-" or "").
  const debouncedZThresholdInput = useDebouncedValue(zThresholdInput, 400);

  // Only send a well-formed positive number; otherwise omit the param and let
  // the backend use its own default (also reflected back in `data.z_threshold`).
  const parsedZThreshold = Number(debouncedZThresholdInput);
  const zThresholdParam =
    Number.isFinite(parsedZThreshold) && parsedZThreshold > 0 ? parsedZThreshold : undefined;

  // Fetch dynamic attribute fields to extend the GROUP BY dropdown.
  const { data: fieldsData } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "fields"],
    queryFn: () => anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  const seriesFieldOptions = useMemo(() => {
    const attrOptions = (fieldsData?.fields ?? [])
      .filter((f) => f.token.startsWith("attr:"))
      .map((f) => ({
        value: f.token,
        label: f.token.slice(5) + (f.recommended ? "" : " ·"),
        group: "dynamic",
      }));
    return [...STATIC_SERIES_FIELD_OPTIONS, ...attrOptions];
  }, [fieldsData]);

  const fl = useFindingsLimit(30);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "frequency", seriesField, zThresholdParam, blKey, fl.limit],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "frequency",
        series_field: seriesField,
        z_threshold: zThresholdParam,
        limit: fl.limit,
        ...blParams,
      }),
    staleTime: 60_000,
    enabled: !needsBaseline,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "frequency",
        series_field: seriesField,
        z_threshold: zThresholdParam,
        limit: fl.limit,
        ...blParams,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ predicate: (query) => shouldInvalidate(query.queryKey, caseId) });
    },
  });

  // Memoized against `data` (stable react-query reference) so the marker
  // effect below doesn't re-fire — and loop — on every render.
  const findings = useMemo(
    () =>
      (data?.results ?? []).filter(
        (r): r is FrequencyFinding => r.type === "frequency",
      ),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f): AnomalyMarker => {
      const direction = f.z_score > 0 ? "spike" : "drop";
      const label = `${f.series_field}=${f.series_value} ${direction}`;
      // In self-baseline (non-temporal) z-score mode, "expected" comes from a
      // leave-one-out mean/std over the rest of the series — the flagged
      // window itself is excluded from its own baseline (see
      // anomaly_stats.py::find_frequency_anomalies) so a single spike can't
      // inflate its own baseline and suppress its own detection.
      const baselineClause =
        data?.method === "temporal-z-score"
          ? "expected from the pre-baseline event-count distribution"
          : "expected from the rest of this series' event-count distribution (this window excluded, leave-one-out)";
      const detail =
        `Frequency ${direction}: ${f.series_field}=${f.series_value} — ` +
        `${f.observed} events observed vs ${f.expected.toFixed(1)} ${baselineClause} ` +
        `(z=${f.z_score > 0 ? "+" : ""}${f.z_score.toFixed(2)}) between ` +
        `${fmtTs(f.window_start)}–${fmtTs(f.window_end)}`;
      return {
        ts: f.window_start,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "frequency" as const,
        rawDetails: f.details,
        windowEnd: f.window_end,
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
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)] shrink-0">
          Group by
        </span>
        <select
          value={seriesField}
          onChange={(e) => setSeriesField(e.target.value)}
          className="flex-1 min-w-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
        >
          <optgroup label="Standard">
            {seriesFieldOptions
              .filter((o) => o.group === "standard")
              .map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
          </optgroup>
          {seriesFieldOptions.some((o) => o.group === "dynamic") && (
            <optgroup label="Dynamic fields">
              {seriesFieldOptions
                .filter((o) => o.group === "dynamic")
                .map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
            </optgroup>
          )}
        </select>
        <span className="flex items-center gap-1 text-xs text-[var(--color-fg-muted)] shrink-0">
          z ≥
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={zThresholdInput}
            onChange={(e) => setZThresholdInput(e.target.value)}
            title="|z| cutoff — windows at or above this many standard deviations are flagged"
            className="w-12 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1 py-0.5 text-xs text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
          />
        </span>
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      <DetectorStatusLine
        data={data}
        extra={<span>z ≥ {data?.z_threshold ?? zThresholdParam ?? "?"}</span>}
      />

      {isLoading && (
        <div className="flex justify-center py-6">
          <Spinner size={18} />
        </div>
      )}

      {!isLoading && findings.length === 0 && (
        <div className="flex items-center gap-2 py-4 text-xs text-[var(--color-fg-muted)]">
          <Info size={13} />
          <span>
            No frequency anomalies detected.{" "}
            {data?.status === "no_data"
              ? "No events with timestamps ingested yet."
              : "All time windows are within the normal z-score band."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} serverTotal={data?.total_findings} onLoadMore={fl.canRaise ? fl.raise : undefined} loadingMore={isFetching} />
          {cap.shown.map((f, i) => (
            <FreqFindingRow
              key={`${f.series_value}:${f.window_start}:${i}`}
              finding={f}
              zThreshold={data?.z_threshold ?? zThresholdParam ?? 2.5}
              onDrillField={onDrillField}
              onJumpToTime={onJumpToTime}
            />
          ))}
        </div>
      )}

      {/* Tag action */}
      {findings.length > 0 && (
        <TagFindingsBar mutation={tagMutation} label={`Tag ${findings.length} windows`} />
      )}

      {/* Hint */}
      <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)]">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          Click any window to filter the explorer to that series value and
          time range. Z-score measures how many standard deviations
          above/below the series mean this window's event count is.
        </span>
      </div>
    </div>
  );
}
