/**
 * ValueNoveltyView — ranked list of rare / first-seen field values.
 *
 * Calls the value_novelty detector endpoint and shows each finding as an
 * interactive row: field badge + value + surprise score + first-seen timestamp
 * + click-to-drill.  "First-seen in detect window" findings are highlighted.
 *
 * No chart dependency — scores rendered as inline proportional bars
 * via MiniSparkline (div-bar idiom, airgap-safe).
 */
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  RefreshCw,
  Tag,
  ChevronsRight,
  Info,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import type { Event, ValueNoveltyFinding } from "@/api/types";
import { cn } from "@/lib/cn";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  /** Called when analyst drills into findings — passes a field filter. */
  onDrillField?: (field: string, value: string) => void;
}

/** Friendly display label for a field token. */
function fieldLabel(token: string): string {
  if (token.startsWith("attr:")) return token.slice(5);
  const map: Record<string, string> = {
    artifact: "artifact",
    timestamp_desc: "desc",
    display_name: "display",
    message: "message",
    artifact_long: "artifact_long",
    parser_name: "parser",
    source_file: "source_file",
  };
  return map[token] ?? token;
}

/** Format an ISO timestamp for compact display. */
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

/** Score-to-width percentage (capped at 100). */
function scorePct(score: number, maxScore: number): number {
  return maxScore > 0 ? Math.min(100, Math.round((score / maxScore) * 100)) : 0;
}

interface FindingRowProps {
  finding: ValueNoveltyFinding;
  maxScore: number;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  isFirstSeen: boolean;
}

function FindingRow({
  finding,
  maxScore,
  onSelectEvent,
  onDrillField,
  isFirstSeen,
}: FindingRowProps) {
  const [expanded, setExpanded] = useState(false);
  const pct = scorePct(finding.score, maxScore);

  return (
    <div
      className={cn(
        "group rounded border transition-colors cursor-pointer",
        isFirstSeen
          ? "border-[var(--color-accent)]/40 bg-[var(--color-accent-dim)]"
          : "border-[var(--color-border)] hover:border-[var(--color-border-focus)]",
      )}
    >
      {/* Main row */}
      <div
        className="flex items-start gap-2 p-2"
        onClick={() => {
          if (finding.event && onSelectEvent) {
            onSelectEvent(finding.event as unknown as Event);
          }
        }}
      >
        {/* Rarity bar */}
        <div className="mt-1.5 shrink-0 w-16 h-1.5 rounded-full bg-[var(--color-bg-elevated)] overflow-hidden">
          <div
            className={cn(
              "h-full rounded-full transition-all",
              isFirstSeen ? "bg-[var(--color-accent)]" : "bg-[var(--color-warning)]",
            )}
            style={{ width: `${pct}%` }}
          />
        </div>

        <div className="min-w-0 flex-1 space-y-0.5">
          {/* Field badge + value */}
          <div className="flex flex-wrap items-center gap-1">
            <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-fg-muted)]">
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
          <div className="flex flex-wrap items-center gap-2 text-[10px] text-[var(--color-fg-muted)]">
            <span>
              count <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong>
            </span>
            <span>
              surprise <strong className="text-[var(--color-fg-secondary)]">{finding.score.toFixed(2)}</strong>
            </span>
            {finding.first_seen && (
              <span>first {fmtTs(finding.first_seen)}</span>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="shrink-0 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          {onDrillField && (
            <button
              title={`Filter to ${finding.field}=${finding.value}`}
              className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
              onClick={(e) => {
                e.stopPropagation();
                onDrillField(finding.field, finding.value);
              }}
            >
              <ChevronsRight size={12} />
            </button>
          )}
          <button
            title={expanded ? "Collapse" : "Details"}
            className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)]"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
          >
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 space-y-1 text-[10px] font-mono text-[var(--color-fg-muted)]">
          {Object.entries(finding.details).map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <span className="w-24 shrink-0">{k}</span>
              <span className="text-[var(--color-fg-secondary)] break-all">
                {String(v)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ValueNoveltyView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
}: Props) {
  const [mode, setMode] = useState<"self" | "temporal">("self");
  const qc = useQueryClient();

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies-novelty", caseId, timelineId, mode],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "value_novelty",
        limit: 50,
      }),
    staleTime: 60_000,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, { detector: "value_novelty", limit: 50 }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = (data?.results ?? []).filter(
    (r): r is ValueNoveltyFinding => r.type === "value_novelty",
  );
  const maxScore = Math.max(1, ...findings.map((r) => r.score));

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Mode
        </span>
        {(["self", "temporal"] as const).map((m) => (
          <button
            key={m}
            className={cn(
              "rounded px-2 py-0.5 text-[10px] font-medium transition-colors",
              mode === m
                ? "bg-[var(--color-accent)] text-white"
                : "bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
            )}
            onClick={() => setMode(m)}
          >
            {m === "self" ? "Self-baseline" : "Temporal"}
          </button>
        ))}
        <span className="flex-1" />
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
            No rare values detected.{" "}
            {data?.status === "no_data"
              ? "No events ingested yet."
              : "All field values appear frequently."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          {findings.map((f, i) => (
            <FindingRow
              key={`${f.field}:${f.value}:${i}`}
              finding={f}
              maxScore={maxScore}
              onSelectEvent={onSelectEvent}
              onDrillField={onDrillField}
              isFirstSeen={data?.method === "temporal"}
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
            Tag {findings.length} as anomaly
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

      {/* Methodology note */}
      <div className="flex items-start gap-1.5 text-[10px] text-[var(--color-fg-muted)] pt-1">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          Rare ≠ malicious. Rare values appear in &lt;= rarity floor events.
          Score = −log(count/total); higher is rarer.
        </span>
      </div>
    </div>
  );
}
