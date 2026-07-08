/**
 * detector-shared — scaffolding shared by every statistical-detector view
 * (rare values, value combos, frequency, timestamp order, numeric range).
 *
 * Each view keeps its own bespoke finding-row *body*; the chrome around it —
 * mode toggle, status line, expandable details dump, tag action, marker/runId
 * plumbing — is identical across detectors and lives here so five views don't
 * drift apart.
 */
import { useEffect, useState } from "react";
import { useMutation, type UseMutationResult } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronsRight, ChevronUp, Clock, RefreshCw, Tag } from "lucide-react";
import { eventsApi } from "@/api/events";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type {
  AnomaliesResponse,
  AnomalyMarker,
  Event,
  TagAnomaliesResponse,
} from "@/api/types";
import { cn } from "@/lib/cn";
import { tagResultLabel } from "@/lib/format";

export type DetectorMode = "self" | "temporal";

/**
 * Resolve the request params + queryKey fragment for a detector's temporal
 * mode, honoring the active saved baseline definition (from the baseline
 * store) over the legacy midpoint split. Every detector view calls this
 * instead of hard-coding `temporal: mode === "temporal"` so a change to the
 * active baseline re-runs the scan and all seven views stay consistent.
 */
export function useBaselineRequest(mode: DetectorMode): {
  params: { temporal?: boolean; baseline_id?: string };
  key: string;
} {
  const activeBaselineId = useBaselineStore((s) => s.activeBaselineId);
  if (mode !== "temporal") return { params: { temporal: false }, key: "self" };
  if (activeBaselineId)
    return { params: { baseline_id: activeBaselineId }, key: `bl:${activeBaselineId}` };
  return { params: { temporal: true }, key: "temporal" };
}

// Auto-scan field selection for the string detectors (charset/entropy). Mirrors
// _select_auto_scan_tokens / _MAX_AUTO_SCAN_FIELDS / _AUTO_IDENTIFIER_RESERVE in
// db/anomaly_stats.py so the picker's "auto" preview matches what the backend
// actually scans (categorical + identifier fields, with reserved identifier
// slots) — the two must stay in sync.
export const AUTO_SCAN_MAX_FIELDS = 15;
const AUTO_IDENTIFIER_RESERVE = 5;

/**
 * Blend categorical and identifier field tokens under the auto-scan cap, each
 * list already best-first. Identifier fields get up to AUTO_IDENTIFIER_RESERVE
 * reserved slots so a wide categorical set can't crowd them out; each kind
 * backfills the other's unused slots.
 */
export function selectAutoScanTokens(cats: string[], ids: string[]): string[] {
  const reserve = Math.min(ids.length, AUTO_IDENTIFIER_RESERVE);
  const picked = cats.slice(0, AUTO_SCAN_MAX_FIELDS - reserve);
  picked.push(...ids.slice(0, AUTO_SCAN_MAX_FIELDS - picked.length));
  if (picked.length < AUTO_SCAN_MAX_FIELDS) {
    for (const t of cats) {
      if (picked.length >= AUTO_SCAN_MAX_FIELDS) break;
      if (!picked.includes(t)) picked.push(t);
    }
  }
  return picked;
}

/**
 * Encode a field selection for the anomalies API: null → auto (omit the param),
 * a non-empty set → comma-joined tokens, an empty set → the "__none__" sentinel
 * the backend recognises as "explicitly scan nothing". Returns undefined for
 * the auto case so callers can spread it conditionally.
 */
export function fieldsParamOf(selectedFields: string[] | null): string | undefined {
  if (selectedFields === null) return undefined;
  return selectedFields.length > 0 ? selectedFields.join(",") : "__none__";
}

/** Self-baseline / temporal pill pair, as used by the rare-values toolbar. */
export function ModeToggle({
  mode,
  onChange,
}: {
  mode: DetectorMode;
  onChange: (mode: DetectorMode) => void;
}) {
  return (
    <>
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
          onClick={() => onChange(m)}
        >
          {m === "self" ? "Self-baseline" : "Temporal"}
        </button>
      ))}
    </>
  );
}

/** Small refresh icon-button with fetching spinner, right end of every toolbar. */
export function RefreshButton({
  isFetching,
  onClick,
}: {
  isFetching: boolean;
  onClick: () => void;
}) {
  return (
    <button
      title="Refresh"
      className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)]"
      onClick={onClick}
    >
      <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
    </button>
  );
}

/**
 * "method · baseline · status" line under the toolbar. `extra` slots
 * detector-specific fragments (e.g. the frequency view's "z ≥ 2.5") between
 * the method and the baseline size.
 */
export function DetectorStatusLine({
  data,
  extra,
  baselineLabel = "events in baseline",
}: {
  data: AnomaliesResponse | undefined;
  extra?: React.ReactNode;
  baselineLabel?: string;
}) {
  if (!data) return null;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span className="capitalize">{data.method}</span>
        {extra && (
          <>
            <span>·</span>
            {extra}
          </>
        )}
        <span>·</span>
        <span>
          {data.baseline_size.toLocaleString()} {baselineLabel}
        </span>
        {data.status !== "ok" && (
          <span className="text-[var(--color-warning)]">· {data.status.replace(/_/g, " ")}</span>
        )}
      </div>
      {data.warnings && data.warnings.length > 0 && (
        <ul className="space-y-0.5">
          {data.warnings.map((w, i) => (
            <li
              key={i}
              className="flex items-start gap-1 text-[11px] text-[var(--color-warning)]"
            >
              <AlertTriangle size={11} className="mt-0.5 shrink-0" />
              <span>{w}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Finding-row chrome: bordered/hover container, action-icon slot revealed on
 * hover, expand toggle, and the expandable `details` key/value dump. The
 * detector-specific row body is passed as `children`.
 */
export function FindingShell({
  onClick,
  actions,
  details,
  highlight = false,
  title,
  children,
}: {
  onClick?: () => void;
  /** Hover-revealed action icon buttons (drill, jump-to-time, …). */
  actions?: React.ReactNode;
  /** The finding's structured `details` — rendered as an expandable dump. */
  details: Record<string, unknown>;
  /** Accent highlight, e.g. temporal-mode "first seen" findings. */
  highlight?: boolean;
  title?: string;
  children: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className={cn(
        "group rounded border transition-colors cursor-pointer",
        highlight
          ? "border-[var(--color-accent)]/40 bg-[var(--color-accent-dim)]"
          : "border-[var(--color-border)] hover:border-[var(--color-border-focus)]",
      )}
      title={title}
    >
      {/* Main row */}
      <div className="flex items-start gap-2 p-2" onClick={onClick}>
        <div className="min-w-0 flex-1 space-y-0.5">{children}</div>

        {/* Actions */}
        <div className="shrink-0 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          {actions}
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
          {Object.entries(details).map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <span className="w-24 shrink-0">{k}</span>
              <span className="text-[var(--color-fg-secondary)] break-all">{String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** "Tag N as anomaly" bar under the findings list, with result/error label. */
export function TagFindingsBar({
  mutation,
  label,
}: {
  mutation: UseMutationResult<TagAnomaliesResponse, unknown, void>;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2 pt-1 border-t border-[var(--color-border)]">
      <Button
        size="sm"
        variant="ghost"
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
        className="gap-1.5 text-xs"
      >
        {mutation.isPending ? <Spinner size={11} /> : <Tag size={11} />}
        {label}
      </Button>
      {mutation.isSuccess && (
        <span className="text-xs text-[var(--color-success)]">
          {tagResultLabel(mutation.data)}
        </span>
      )}
      {mutation.isError && <span className="text-xs text-[var(--color-error)]">Failed</span>}
    </div>
  );
}

/**
 * Publish the active view's findings as histogram/grid markers, clearing
 * them on unmount or when the finding set changes. `build` may return null
 * to skip findings without a usable timestamp.
 */
export function useAnomalyMarkers<T>(
  findings: T[],
  build: (finding: T) => AnomalyMarker | null,
  onFindingsChange?: (markers: AnomalyMarker[]) => void,
) {
  useEffect(() => {
    if (!onFindingsChange) return;
    const markers = findings
      .map(build)
      .filter((m): m is AnomalyMarker => m !== null);
    onFindingsChange(markers);
    return () => onFindingsChange([]);
    // `build` closes over per-render display data derived from the same
    // query result as `findings` (stable react-query reference) — keying the
    // effect on `findings` alone matches the pre-extraction behavior and
    // avoids a re-fire loop on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [findings]);
}

/**
 * Hover-revealed row actions shared by every finding row: drill-to-filter and
 * jump-to-time. Both are omitted when their handler is absent. `ts` falls back
 * across the caller (event timestamp, then first_seen).
 */
export function FindingRowActions({
  field,
  value,
  ts,
  eventId,
  onDrillField,
  onJumpToTime,
}: {
  /** Field/value for the drill button; omit for detectors without one (order). */
  field?: string;
  value?: string;
  ts?: string | null;
  eventId?: string | null;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  return (
    <>
      {onDrillField && field !== undefined && value !== undefined && (
        <button
          title={`Filter to ${field}=${value}`}
          className="rounded p-0.5 hover:bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-accent)]"
          onClick={(e) => {
            e.stopPropagation();
            onDrillField(field, value);
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
            if (ts) onJumpToTime(ts, eventId ?? undefined);
          }}
        >
          <Clock size={12} />
        </button>
      )}
    </>
  );
}

/** Mutation that fetches a finding's full event by id and surfaces it. */
export function useOpenEvent(
  caseId: string,
  timelineId: string,
  eventId: string | null | undefined,
  onSelectEvent: (event: Event) => void,
) {
  return useMutation({
    mutationFn: () => eventsApi.getById(caseId, timelineId, eventId!),
    onSuccess: (event) => {
      if (event) onSelectEvent(event);
    },
  });
}

/** Publish the active view's persisted run_id, clearing it on unmount. */
export function useDetectorRunId(
  runId: string | null | undefined,
  onRunIdChange?: (runId: string | undefined) => void,
) {
  useEffect(() => {
    if (!onRunIdChange) return;
    onRunIdChange(runId ?? undefined);
    return () => onRunIdChange(undefined);
  }, [runId, onRunIdChange]);
}
