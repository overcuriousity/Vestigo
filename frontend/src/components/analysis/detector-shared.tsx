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
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { type UseMutationResult } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronsRight, ChevronUp, CircleCheck, Clock, EyeOff, Pin, RefreshCw, Tag } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useDisposition } from "@/hooks/useDisposition";
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

/**
 * The show/hide-dismissed reveal link. One source of truth for its wording
 * and styling — rendered by `ResultsBar` and by OrderViolationsView's bespoke
 * per-source notice bar.
 */
export function DismissedToggle({ shown, onToggle }: { shown: boolean; onToggle: () => void }) {
  return (
    <button
      className="text-[var(--color-accent)] hover:underline"
      onClick={onToggle}
      title={shown ? "Hide dismissed findings again" : "Reveal dismissed findings, dimmed, in place"}
    >
      {shown ? "hide" : "show"}
    </button>
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
  dismissedCount,
  showDismissed,
  onToggleDismissed,
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
  /** `dismissed_count` from the response — noise hidden by dispositions, never silently. */
  dismissedCount?: number;
  /** Reveal-dismissed toggle state (see `useShowDismissed`); rendered as a link. */
  showDismissed?: boolean;
  onToggleDismissed?: () => void;
}) {
  const truncated = serverTotal !== undefined && serverTotal > total;
  const hasDismissed = (dismissedCount ?? 0) > 0 || showDismissed;
  return (
    <div className="flex items-center justify-between text-[11px] text-[var(--color-fg-muted)]">
      <span>
        {truncated ? `${total} of ${serverTotal} findings` : `${total} finding${total === 1 ? "" : "s"}`}
        {hasMore ? ` · showing ${shownCount}` : ""}
        {hasDismissed ? ` · ${dismissedCount ?? 0} dismissed` : ""}
        {hasDismissed && onToggleDismissed && (
          <>
            {" "}
            <DismissedToggle shown={showDismissed ?? false} onToggle={onToggleDismissed} />
          </>
        )}
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
/**
 * Row-level channel between FindingShell and the action buttons rendered in
 * its `actions` slot: the shell's durable `confirmed` state (disables the Pin
 * and renders the badge), and a `flash` trigger so a verdict can tint the row
 * (e.g. green for "accepted as normal") for a beat before the optimistic
 * removal — removal reads as feedback, not as the row silently vanishing.
 */
const FindingRowCtx = createContext<{
  confirmed: boolean;
  flash: (kind: "normal") => void;
}>({ confirmed: false, flash: () => {} });

export function FindingShell({
  onClick,
  actions,
  details,
  highlight = false,
  dismissed = false,
  confirmed = false,
  title,
  children,
}: {
  onClick?: () => void;
  /** Action icon buttons (verdicts, drill, jump-to-time, …). */
  actions?: React.ReactNode;
  /** The finding's structured `details` — rendered as an expandable dump. */
  details: Record<string, unknown>;
  /** Accent highlight, e.g. temporal-mode "first seen" findings. */
  highlight?: boolean;
  /** Dismissed finding revealed via the show-dismissed toggle — dimmed. */
  dismissed?: boolean;
  /** Covered by a confirmed disposition — durable badge + tinted border. */
  confirmed?: boolean;
  title?: string;
  children: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const [flashing, setFlashing] = useState(false);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    },
    [],
  );
  const flash = (_kind: "normal") => {
    setFlashing(true);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashing(false), 600);
  };

  return (
    <div
      className={cn(
        "group rounded border transition-colors cursor-pointer",
        flashing
          ? "border-[var(--color-success)] bg-[var(--color-success)]/10"
          : confirmed && !dismissed
            ? "border-[var(--color-anomaly,var(--color-warning))]/50 hover:border-[var(--color-anomaly,var(--color-warning))]"
            : highlight && !dismissed
              ? "border-[var(--color-accent)]/40 bg-[var(--color-accent-dim)]"
              : "border-[var(--color-border)] hover:border-[var(--color-border-focus)]",
        dismissed && "opacity-60",
      )}
      title={title}
    >
      {/* Main row */}
      <div className="flex items-start gap-2 p-2" onClick={onClick}>
        {dismissed && (
          <span
            title="Dismissed — hidden as noise; revealed by the show-dismissed toggle"
            className="mt-0.5 shrink-0 text-[var(--color-fg-muted)]"
          >
            <EyeOff size={12} />
          </span>
        )}
        {confirmed && !dismissed && (
          <span
            title="Confirmed — escalated as a durable finding; survives detector re-runs"
            className="mt-0.5 flex shrink-0 items-center gap-1 rounded bg-[var(--color-anomaly,var(--color-warning))]/15 px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-anomaly,var(--color-warning))]"
          >
            <Pin size={10} />
            confirmed
          </span>
        )}
        <div className="min-w-0 flex-1 space-y-0.5">{children}</div>

        {/* Actions — dimmed at rest (not hidden: the verdict affordances must
            be discoverable without hover, incl. on touch), full on hover/focus. */}
        <div className="shrink-0 flex items-center gap-1 opacity-50 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
          <FindingRowCtx.Provider value={{ confirmed, flash }}>{actions}</FindingRowCtx.Provider>
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
 * Row actions shared by every finding row (dimmed at rest, full on hover):
 * verdicts, drill-to-filter and jump-to-time. Handler-less actions are
 * omitted. `ts` falls back across the caller (event timestamp, then
 * first_seen).
 */
export function FindingRowActions({
  field,
  value,
  ts,
  eventId,
  onDrillField,
  onJumpToTime,
  disposition,
  confirmed,
  jumpTitle,
}: {
  /** Explicit confirmed state for rows not wrapped in FindingShell (which
   * provides it via context) — e.g. FrequencyView's bespoke row. */
  confirmed?: boolean;
  /** Override for the jump-to-time tooltip (e.g. "Jump to first occurrence…"). */
  jumpTitle?: string;
  /** Field/value for the drill button; omit for detectors without one (order). */
  field?: string;
  value?: string;
  ts?: string | null;
  eventId?: string | null;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
  /**
   * Enables the Normal / Dismiss / Confirm disposition actions on the row.
   * `caseId`/`timelineId` locate the timeline; `detector` + the finding's
   * `details` (carrying the precomputed `allowlist_field`/`allowlist_value`)
   * form the value key — findings without one (positional, e.g.
   * timestamp_order) are dispositioned per event. `sourceId` + the row's
   * `eventId` scope event-level verdicts; Confirm needs both plus `content`
   * for the persisted annotation text.
   */
  disposition?: {
    caseId: string;
    timelineId: string;
    detector: string;
    details: Record<string, unknown>;
    sourceId?: string | null;
    /** Human-readable finding text stored when confirming. */
    content?: string;
  };
}) {
  // Always instantiate (rules of hooks); it only fires when a button renders.
  const dispositionMut = useDisposition(
    disposition?.caseId ?? "",
    disposition?.timelineId ?? "",
  );
  const rowCtx = useContext(FindingRowCtx);
  const row = { ...rowCtx, confirmed: confirmed ?? rowCtx.confirmed };
  const valueField = disposition?.details?.allowlist_field as string | undefined;
  const valueValue = disposition?.details?.allowlist_value as string | undefined;
  const hasValueKey = valueField !== undefined && valueValue !== undefined;
  const hasEvent = !!eventId && !!disposition?.sourceId;
  const canNormalOrDismiss = disposition !== undefined && (hasValueKey || hasEvent);
  const canConfirm = disposition !== undefined && hasEvent && disposition.detector !== "*";
  // Buttons that can't act still render, disabled with the reason — a row
  // where they'd silently vanish reads as a broken UI, not a scoping limit.
  const noScopeReason =
    "Unavailable: this finding has no field=value key and no representative event to scope a verdict to";

  const act = (kind: "normal" | "dismissed" | "confirmed") => {
    if (!disposition) return;
    const fire = () =>
      dispositionMut.mutate({
        kind,
        detector: disposition.detector,
        // Prefer the value key; fall back to the event for positional findings.
        field: hasValueKey ? valueField : undefined,
        value: hasValueKey ? valueValue : undefined,
        sourceId: disposition.sourceId ?? undefined,
        eventId: eventId ?? undefined,
        content: disposition.content,
        details: disposition.details,
      });
    if (kind === "normal") {
      // Flash the row green for a beat before the optimistic removal, so the
      // disappearance reads as "accepted", not as the row silently vanishing.
      row.flash("normal");
      setTimeout(fire, 300);
      return;
    }
    fire();
  };
  const scopeLabel = hasValueKey ? `${valueField}=${valueValue}` : "this event";

  return (
    <>
      {disposition && (
        <button
          title={
            canNormalOrDismiss
              ? `Normal: treat ${scopeLabel} as expected behavior — extends the baseline, no longer flagged by ${disposition.detector}`
              : noScopeReason
          }
          className={cn(
            "rounded p-0.5 text-[var(--color-fg-muted)]",
            canNormalOrDismiss
              ? "hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-success)]"
              : "cursor-not-allowed opacity-40",
          )}
          onClick={(e) => {
            e.stopPropagation();
            if (canNormalOrDismiss) act("normal");
          }}
          disabled={dispositionMut.isPending || !canNormalOrDismiss}
        >
          {dispositionMut.isPending ? <Spinner size={11} /> : <CircleCheck size={12} />}
        </button>
      )}
      {disposition && (
        <button
          title={
            canNormalOrDismiss
              ? `Dismiss: hide ${scopeLabel} as noise for this investigation — detectors keep scoring it`
              : noScopeReason
          }
          className={cn(
            "rounded p-0.5 text-[var(--color-fg-muted)]",
            canNormalOrDismiss
              ? "hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-fg-primary)]"
              : "cursor-not-allowed opacity-40",
          )}
          onClick={(e) => {
            e.stopPropagation();
            if (canNormalOrDismiss) act("dismissed");
          }}
          disabled={dispositionMut.isPending || !canNormalOrDismiss}
        >
          <EyeOff size={12} />
        </button>
      )}
      {disposition && disposition.detector !== "*" && (
        <button
          title={
            row.confirmed
              ? "Already confirmed — a durable finding covers this event"
              : canConfirm
                ? "Confirm: escalate as a durable finding — survives detector re-runs"
                : "Unavailable: no representative event to persist a confirmed finding against"
          }
          className={cn(
            "rounded p-0.5",
            row.confirmed
              ? "cursor-default text-[var(--color-anomaly,var(--color-warning))]"
              : canConfirm
                ? "text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-anomaly,var(--color-warning))]"
                : "cursor-not-allowed opacity-40 text-[var(--color-fg-muted)]",
          )}
          onClick={(e) => {
            e.stopPropagation();
            if (canConfirm && !row.confirmed) act("confirmed");
          }}
          disabled={dispositionMut.isPending || !canConfirm || row.confirmed}
        >
          <Pin size={12} fill={row.confirmed ? "currentColor" : "none"} />
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
          title={
            jumpTitle ??
            "Jump to this event's time in the grid — clears active filters (a breadcrumb lets you return to the filtered view)"
          }
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

