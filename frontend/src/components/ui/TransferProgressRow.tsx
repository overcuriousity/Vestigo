/**
 * Live feedback for a browser-side byte transfer — an upload in flight, or a
 * download streaming into the page.
 *
 * Distinct from `JobStatusRow` because there is no job: these bytes are moving
 * before the server has created anything to poll (an upload), or after it has
 * finished and deleted it (a download). It reuses `ProgressMeter`, so the bar
 * and the rate line read identically to the job rows they sit beside.
 */
import { Loader2 } from "lucide-react";
import { ProgressMeter } from "@/components/ui/ProgressMeter";
import type { TransferState } from "@/hooks/useFileTransfer";
import { cn } from "@/lib/cn";

interface Props {
  label: string;
  /** Null before the first progress event — the row still renders, so the
   * transfer is visibly under way from the moment it starts. */
  state: TransferState | null;
  /** Denominator to use until the transfer reports one; for an upload that is
   * the local file's size, known before a single byte has left. */
  fallbackTotal?: number | null;
  onCancel?: () => void;
  cancelLabel?: string;
  className?: string;
}

export function TransferProgressRow({
  label,
  state,
  fallbackTotal,
  onCancel,
  cancelLabel = "Cancel",
  className,
}: Props) {
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-xs",
        className,
      )}
    >
      <Loader2 size={14} className="mt-0.5 shrink-0 animate-spin text-[var(--color-accent)]" />
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium text-[var(--color-fg-primary)]">{label}</div>
        <ProgressMeter
          processed={state?.loaded ?? 0}
          total={state?.total ?? fallbackTotal ?? null}
          rate_bps={state?.rate_bps}
          eta_s={state?.eta_s}
          bytes
          indeterminate
        />
      </div>
      {onCancel && (
        <button
          type="button"
          onClick={onCancel}
          className="shrink-0 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[var(--color-fg-secondary)] transition-base hover:bg-[var(--color-bg-hover)]"
        >
          {cancelLabel}
        </button>
      )}
    </div>
  );
}
