/**
 * The bar and the "12.4 MB / 40.0 MB · 3.1 MB/s · ~14s left" line, shared by
 * `JobStatusRow` (server-side jobs) and `TransferProgressRow` (browser-side
 * byte transfers).
 *
 * Shared because the two must not drift: an analyst watching a case import
 * sees a job's bar and an upload's bar within the same dialog, seconds apart,
 * and a different rate format or a differently-behaved bar between them reads
 * as two unrelated things happening.
 *
 * A null `total` is not "no progress" — it is a transfer whose size the
 * browser genuinely cannot know (a chunked response with no `Content-Length`,
 * or a job phase that hasn't counted its items yet). That renders as an
 * indeterminate bar plus whatever *is* known, never as a missing bar: a bar
 * that vanishes mid-transfer reads as a stall.
 */
import { Progress } from "@/components/ui/Progress";
import { fmtBytes, fmtRate, progressPct } from "@/lib/format";
import { fmtDuration } from "@/lib/time";

export interface ProgressMeterProps {
  processed: number | null | undefined;
  total: number | null | undefined;
  rate_bps?: number | null;
  eta_s?: number | null;
  /** Render the counts as byte sizes ("12.4 MB / 40.0 MB"). Off for unit
   * counts, where the percentage in the caller's own status line says it. */
  bytes?: boolean;
  /** Show the bar even with nothing counted yet — for work that is definitely
   * running but has not published a denominator (an archive phase that hashes
   * one big file, say). Without this such a phase renders no bar at all, which
   * is indistinguishable from a stall. */
  indeterminate?: boolean;
  /** Suppress the bar but keep the readout — a failed job should not animate. */
  barHidden?: boolean;
  className?: string;
}

export function ProgressMeter({
  processed,
  total,
  rate_bps,
  eta_s,
  bytes,
  indeterminate,
  barHidden,
  className,
}: ProgressMeterProps) {
  const pct = progressPct(processed, total);
  const moving = processed != null && processed > 0;
  // Nothing is known at all, and the caller hasn't asserted that work is under
  // way — render nothing rather than an indeterminate bar for a transfer that
  // may not have started.
  if (pct == null && !moving && !indeterminate) return null;

  const readout = bytes
    ? [fmtBytes(processed), total != null && total > 0 ? fmtBytes(total) : null]
        .filter(Boolean)
        .join(" / ")
    : null;
  const rate = fmtRate(rate_bps);
  const eta = rate != null && eta_s != null ? `~${fmtDuration(eta_s)} left` : null;
  const detailLine = [readout, rate, eta].filter(Boolean).join(" · ");

  return (
    <div className={className}>
      {!barHidden && <Progress value={pct} className="mt-1.5" />}
      {detailLine && (
        <div className="mt-1 font-mono text-[10px] text-[var(--color-fg-muted)] tabular-nums">
          {detailLine}
        </div>
      )}
    </div>
  );
}
