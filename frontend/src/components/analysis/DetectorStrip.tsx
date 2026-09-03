/**
 * DetectorStrip — every detector configured on this timeline, in one row.
 *
 * Replaces the mute strip. There is nothing to mute any more: a detector is
 * either configured (and runs) or not (and does not exist here). Each chip
 * names the method, the scope it runs under and what it found, and carries
 * the two acts an analyst with contribute access can take — edit (the wizard
 * on this method) and remove. Read-only members see the chips without them.
 */
import { Pencil, X } from "lucide-react";
import type { MethodState } from "@/hooks/useMethodFindings";
import type { KnownDetectorEntry } from "@/hooks/useTimelineDetectors";
import { METHODS_BY_ID, type MethodId } from "./method-registry";

interface Props {
  entries: KnownDetectorEntry[];
  byMethod: Record<MethodId, MethodState>;
  /** Baseline id → name, for the chip's scope label. */
  baselineNames: Record<string, string>;
  canEdit: boolean;
  onEdit: (method: MethodId) => void;
  onRemove: (method: MethodId) => void;
}

export function DetectorStrip({
  entries,
  byMethod,
  baselineNames,
  canEdit,
  onEdit,
  onRemove,
}: Props) {
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1" data-testid="detector-strip">
      {entries.map((entry) => {
        const meta = METHODS_BY_ID[entry.method];
        const state = byMethod[entry.method];
        const scope =
          entry.frame === "baseline"
            ? `vs ${baselineNames[entry.baseline_id ?? ""] ?? "baseline"}`
            : "whole timeline";
        return (
          <span
            key={entry.method}
            data-testid={`detector-chip-${entry.method}`}
            className="flex items-center gap-1 rounded-full border border-[var(--color-border)] px-2 py-0.5 text-[10px] text-[var(--color-fg-secondary)]"
          >
            <meta.icon size={10} className="text-[var(--color-fg-muted)]" />
            {meta.label}
            <span className="text-[var(--color-fg-muted)]">· {scope}</span>
            {/* A dash while queued, a mark on error — never a zero for a
                detector that has not answered yet. */}
            <span className="font-mono" title={state?.error ? "Failed to run" : undefined}>
              {state?.pending ? "…" : state?.error ? "!" : String(state?.total ?? 0)}
            </span>
            {canEdit && (
              <>
                <button
                  type="button"
                  title="Edit"
                  onClick={() => onEdit(entry.method)}
                  className="rounded p-0.5 hover:text-[var(--color-accent)]"
                >
                  <Pencil size={9} />
                </button>
                <button
                  type="button"
                  title="Remove"
                  onClick={() => onRemove(entry.method)}
                  className="rounded p-0.5 hover:text-[var(--color-danger)]"
                >
                  <X size={9} />
                </button>
              </>
            )}
          </span>
        );
      })}
    </div>
  );
}
