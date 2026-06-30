/**
 * FrequencyView — time-series bar chart with anomalous windows highlighted.
 *
 * Calls the frequency detector endpoint.  Anomalous buckets are rendered in a
 * different color; clicking one fires onRangeSelect so the event explorer
 * zooms to that window (same brush contract as TimelineHistogram).
 *
 * No chart dependency — hand-rolled div bars (airgap-safe), same idiom as
 * TimelineHistogram.tsx.
 */
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  RefreshCw,
  Tag,
  Info,
  TrendingUp,
  TrendingDown,
} from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import type { FrequencyFinding } from "@/api/types";
import { cn } from "@/lib/cn";

interface Props {
  caseId: string;
  timelineId: string;
  /** Called when an anomalous window is clicked — zoom the event explorer. */
  onRangeSelect?: (start: string, end: string) => void;
}

const SERIES_FIELD_OPTIONS = [
  { value: "artifact", label: "Artifact type" },
  { value: "timestamp_desc", label: "Event category" },
  { value: "display_name", label: "Display name" },
  { value: "parser_name", label: "Parser" },
  { value: "source_file", label: "Source file" },
];

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

interface FreqFindingRowProps {
  finding: FrequencyFinding;
  onRangeSelect?: (start: string, end: string) => void;
}

function FreqFindingRow({ finding, onRangeSelect }: FreqFindingRowProps) {
  const isSpike = finding.z_score > 0;
  const severity =
    Math.abs(finding.z_score) >= 5
      ? "high"
      : Math.abs(finding.z_score) >= 3
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
      onClick={() => onRangeSelect?.(finding.window_start, finding.window_end)}
      title={`Click to zoom to ${fmtTs(finding.window_start)} – ${fmtTs(finding.window_end)}`}
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
          <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-fg-muted)]">
            {finding.series_field}
          </span>
          <span className="font-mono text-xs text-[var(--color-fg-primary)] font-medium break-all">
            {finding.series_value}
          </span>
        </div>

        {/* Window label */}
        <div className="text-[10px] text-[var(--color-fg-muted)]">
          {fmtTs(finding.window_start)} – {fmtTs(finding.window_end)}
        </div>

        {/* Count vs expected */}
        <div className="flex flex-wrap items-center gap-2 text-[10px]">
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
    </div>
  );
}

export function FrequencyView({ caseId, timelineId, onRangeSelect }: Props) {
  const [seriesField, setSeriesField] = useState("artifact");
  const [zThreshold, _setZThreshold] = useState(2.5);
  const qc = useQueryClient();

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies-frequency", caseId, timelineId, seriesField],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "frequency",
        series_field: seriesField,
        limit: 30,
      }),
    staleTime: 60_000,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "frequency",
        series_field: seriesField,
        limit: 30,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = (data?.results ?? []).filter(
    (r): r is FrequencyFinding => r.type === "frequency",
  );

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)] shrink-0">
          Group by
        </span>
        <select
          value={seriesField}
          onChange={(e) => setSeriesField(e.target.value)}
          className="flex-1 min-w-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-0.5 text-[11px] text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
        >
          {SERIES_FIELD_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <button
          title="Refresh"
          className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)]"
          onClick={() => refetch()}
        >
          <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Status line */}
      {data && (
        <div className="flex items-center gap-2 text-[10px] text-[var(--color-fg-muted)]">
          <span className="capitalize">{data.method}</span>
          <span>·</span>
          <span>z ≥ {zThreshold}</span>
          <span>·</span>
          <span>{data.baseline_size.toLocaleString()} events in baseline</span>
          {data.status !== "ok" && (
            <span className="text-[var(--color-warning)]">
              · {data.status.replace(/_/g, " ")}
            </span>
          )}
        </div>
      )}

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
          {findings.map((f, i) => (
            <FreqFindingRow
              key={`${f.series_value}:${f.window_start}:${i}`}
              finding={f}
              onRangeSelect={onRangeSelect}
            />
          ))}
        </div>
      )}

      {/* Tag action */}
      {findings.length > 0 && (
        <div className="flex items-center gap-2 pt-1 border-t border-[var(--color-border)]">
          <Button
            size="sm"
            variant="ghost"
            disabled={tagMutation.isPending}
            onClick={() => tagMutation.mutate()}
            className="gap-1.5 text-xs"
          >
            {tagMutation.isPending ? <Spinner size={11} /> : <Tag size={11} />}
            Tag {findings.length} windows
          </Button>
          {tagMutation.isSuccess && (
            <span className="text-[10px] text-[var(--color-success)]">
              ✓ {(tagMutation.data as { tagged?: number } | undefined)?.tagged ?? 0} tagged
            </span>
          )}
          {tagMutation.isError && (
            <span className="text-[10px] text-[var(--color-error)]">Failed</span>
          )}
        </div>
      )}

      {/* Hint */}
      <div className="flex items-start gap-1.5 text-[10px] text-[var(--color-fg-muted)]">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          Click any window to zoom the event explorer to that time range.
          Z-score measures how many standard deviations above/below the
          series mean this window's event count is.
        </span>
      </div>
    </div>
  );
}
