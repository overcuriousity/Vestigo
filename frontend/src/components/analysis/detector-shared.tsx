/**
 * detector-shared — component chrome shared by every statistical-detector view
 * (rare values, value combos, frequency, proportion shift, timestamp order,
 * numeric range, charset, entropy).
 *
 * Each view keeps its own bespoke finding-row *body*; the chrome around it —
 * mode toggle, status line, expandable details dump, tag action — is identical
 * across detectors and lives here so five views don't drift apart. The
 * non-component scaffolding (baseline-frame resolution, findings capping,
 * marker/runId plumbing) lives in detector-hooks.ts so this file only exports
 * components (react fast-refresh requirement).
 */
import { useState } from "react";
import { type UseMutationResult } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronsRight, ChevronUp, CircleCheck, Clock, RefreshCw, Tag } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useMarkNormal } from "@/hooks/useMarkNormal";
import type { AnomaliesResponse, TagAnomaliesResponse } from "@/api/types";
import { cn } from "@/lib/cn";
import { tagResultLabel } from "@/lib/format";
import { InfoHint } from "@/components/ui/InfoHint";
import { GLOSSARY } from "@/lib/glossary";

/**
 * Shown in place of a detector's findings when the global frame is `baseline`
 * but no definition is active — every view renders this instead of silently
 * running a self-baseline scan, so "compare windows" always means an explicit
 * baseline. The frame bar's definition dropdown / window editor is the fix.
 */
export function NeedsBaselinePrompt() {
  return (
    <div className="flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 text-xs text-[var(--color-fg-muted)]">
      <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--color-warning)]" />
      <span>
        Comparing against a baseline, but none is selected. Pick or build a
        baseline definition in <strong>Windows &amp; normality</strong> below,
        or switch the frame to <strong>Scan all events</strong>.
      </span>
    </div>
  );
}

/** "N findings · showing M" header + show-all/less toggle for a capped list.
 *
 * With the optional server props it also surfaces server-side truncation:
 * when `serverTotal` exceeds `total` (the server capped the scan at its
 * `limit`) the count reads "N of M findings" and a **Load more** button
 * raises the limit via `onLoadMore` (see `useFindingsLimit`) — findings are
 * never silently truncated.
 */
export function ResultsBar({
  total,
  shownCount,
  hasMore,
  expanded,
  onToggle,
  serverTotal,
  onLoadMore,
  loadingMore,
}: {
  total: number;
  shownCount: number;
  hasMore: boolean;
  expanded: boolean;
  onToggle: () => void;
  /** `total_findings` from the response — findings before the server limit. */
  serverTotal?: number;
  /** Raise the server limit and refetch; rendered only when truncated. */
  onLoadMore?: () => void;
  loadingMore?: boolean;
}) {
  const truncated = serverTotal !== undefined && serverTotal > total;
  return (
    <div className="flex items-center justify-between text-[11px] text-[var(--color-fg-muted)]">
      <span>
        {truncated ? `${total} of ${serverTotal} findings` : `${total} finding${total === 1 ? "" : "s"}`}
        {hasMore ? ` · showing ${shownCount}` : ""}
      </span>
      <span className="flex items-center gap-2">
        {truncated && onLoadMore && (
          <button
            className="text-[var(--color-accent)] hover:underline disabled:opacity-50"
            onClick={onLoadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
        {hasMore && (
          <button className="text-[var(--color-accent)] hover:underline" onClick={onToggle}>
            {expanded ? "Show fewer" : `Show all ${total}`}
          </button>
        )}
      </span>
    </div>
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
        <span className="flex items-center gap-1">
          <span className="capitalize">{data.method}</span>
          <InfoHint
            content={data.method.startsWith("temporal") ? GLOSSARY.temporal : GLOSSARY.selfBaseline}
            size={11}
          />
        </span>
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
  markNormal,
}: {
  /** Field/value for the drill button; omit for detectors without one (order). */
  field?: string;
  value?: string;
  ts?: string | null;
  eventId?: string | null;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
  /**
   * Enables the detector-scoped "Normal" action on the row. `caseId`/
   * `timelineId` locate the timeline; `detector` + the finding's `details`
   * (carrying the precomputed `allowlist_field`/`allowlist_value`) form the
   * suppression key. `sourceId` is only used for the positional fallback.
   */
  markNormal?: {
    caseId: string;
    timelineId: string;
    detector: string;
    details: Record<string, unknown>;
    sourceId?: string | null;
  };
}) {
  // Always instantiate (rules of hooks); it only fires when the button renders.
  const markNormalMut = useMarkNormal(markNormal?.caseId ?? "", markNormal?.timelineId ?? "");
  const allowlistField = markNormal?.details?.allowlist_field as string | undefined;
  const allowlistValue = markNormal?.details?.allowlist_value as string | undefined;
  const isPositional = markNormal?.detector === "timestamp_order";
  const canMarkNormal =
    markNormal !== undefined && (isPositional ? !!eventId : allowlistField !== undefined && allowlistValue !== undefined);

  return (
    <>
      {canMarkNormal && markNormal && (
        <button
          title={
            isPositional
              ? "Mark this event OK — this one event is no longer flagged"
              : `Treat ${allowlistField}=${allowlistValue} as normal — no longer flagged by ${markNormal.detector}`
          }
          className="rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-success)]"
          onClick={(e) => {
            e.stopPropagation();
            markNormalMut.mutate({
              detector: markNormal.detector,
              field: allowlistField,
              value: allowlistValue,
              sourceId: markNormal.sourceId ?? undefined,
              eventId: eventId ?? undefined,
            });
          }}
          disabled={markNormalMut.isPending}
        >
          {markNormalMut.isPending ? <Spinner size={11} /> : <CircleCheck size={12} />}
        </button>
      )}
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

