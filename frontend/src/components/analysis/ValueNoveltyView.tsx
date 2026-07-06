/**
 * ValueNoveltyView — ranked list of rare / first-seen field values.
 *
 * Calls the value_novelty detector endpoint and shows each finding as an
 * interactive row: field badge + value + surprise score + first-seen timestamp
 * + click-to-drill.  "First-seen in detect window" findings are highlighted.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  RefreshCw,
  Tag,
  ChevronsRight,
  Clock,
  Info,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { eventsApi } from "@/api/events";
import { shouldInvalidate } from "@/hooks/useCaseStream";
import { AnomalyFieldPicker } from "./AnomalyFieldPicker";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, Event, ValueNoveltyFinding } from "@/api/types";
import { cn } from "@/lib/cn";
import { anomalyFieldLabel as fieldLabel, tagResultLabel } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  /** Called when analyst drills into findings — passes a field filter. */
  onDrillField?: (field: string, value: string) => void;
  /** Called whenever the finding set changes — feeds the histogram overlay and event grid. */
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
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
  const [expanded, setExpanded] = useState(false);

  // The detector's finding only carries a lightweight, partial "event" stub
  // (missing artifact/tags/attributes/etc.) for bookkeeping — fetch the full
  // event record before handing it to the Event Detail panel.
  const openEvent = useMutation({
    mutationFn: () => eventsApi.getById(caseId, timelineId, finding.event_id!),
    onSuccess: (event) => {
      if (event) onSelectEvent(event);
    },
  });

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
          if (finding.event_id) {
            openEvent.mutate();
          }
        }}
      >
        <div className="min-w-0 flex-1 space-y-0.5">
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
        <div className="border-t border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 space-y-1 text-xs font-mono text-[var(--color-fg-muted)]">
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
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const [mode, setMode] = useState<"self" | "temporal">("self");
  // null = use backend smart default; string[] = explicit analyst selection.
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  // Compute the fields param for the API. null → omit param → backend
  // auto-selects. [] (explicitly deselected every field) can't be sent as an
  // empty string — the API client drops empty-string params — so it's sent as
  // the reserved "__none__" token, which the backend maps to "scan nothing".
  const fieldsParam =
    selectedFields === null
      ? undefined
      : selectedFields.length > 0
        ? selectedFields.join(",")
        : "__none__";

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies-novelty", caseId, timelineId, mode, fieldsParam ?? "__auto__"],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "value_novelty",
        limit: 50,
        temporal: mode === "temporal",
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    staleTime: 60_000,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "value_novelty",
        limit: 50,
        temporal: mode === "temporal",
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
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
        (r): r is ValueNoveltyFinding => r.type === "value_novelty",
      ),
    [data],
  );

  useEffect(() => {
    if (!onFindingsChange) return;
    const markers: AnomalyMarker[] = findings.flatMap((f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return [];
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
      return [
        {
          ts,
          label,
          detail,
          eventId: f.event_id,
          sourceId: f.event?.source_id,
          detector: "value_novelty" as const,
          rawDetails: f.details,
        },
      ];
    });
    onFindingsChange(markers);
    return () => onFindingsChange([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findings]);

  useEffect(() => {
    if (!onRunIdChange) return;
    onRunIdChange(data?.run_id ?? undefined);
    return () => onRunIdChange(undefined);
  }, [data?.run_id, onRunIdChange]);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Mode
        </span>
        {(["self", "temporal"] as const).map((m) => (
          <button
            key={m}
            className={cn(
              "rounded px-2 py-0.5 text-xs font-medium transition-colors",
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
        <AnomalyFieldPicker
          caseId={caseId}
          timelineId={timelineId}
          selected={selectedFields}
          onChange={setSelectedFields}
        />
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
        <div className="flex items-center gap-2 text-xs text-[var(--color-fg-muted)]">
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
          {findings.map((f, i) => (
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
            <span className="text-xs text-[var(--color-success)]">
              {tagResultLabel(tagMutation.data)}
            </span>
          )}
          {tagMutation.isError && (
            <span className="text-xs text-[var(--color-error)]">Failed</span>
          )}
        </div>
      )}

      {/* Methodology note */}
      <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)] pt-1">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          Rare ≠ malicious.{" "}
          {mode === "temporal"
            ? "Temporal mode ignores the rarity floor — it flags any value absent from the baseline window but present in the detect window."
            : "Self-baseline mode flags values that appear ≤ rarity floor times in the whole corpus."}{" "}
          Score = −log(count/total); higher is rarer.
        </span>
      </div>
    </div>
  );
}
