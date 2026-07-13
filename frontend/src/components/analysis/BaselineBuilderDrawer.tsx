/**
 * BaselineBuilderDrawer — overlay drawer hosting the (dense, 30-input-capable)
 * BaselineSection window-editor form, moved out of the Anomalies tab's inline
 * flow so the default view stays scannable. Opened from FrameBar's "Manage
 * baselines" button and after a histogram mark-mode brush lands (the brushed
 * range must land in the editor). State lives in useUiStore so the Explorer's
 * histogram brush can open it from outside the panel.
 *
 * While mark mode is armed (a brush is being awaited) the drawer stays mounted
 * — so BaselineSection's draft survives — but hides itself and drops pointer
 * events, otherwise it would cover the histogram and make dragging impossible.
 * A small floating pill replaces it with a cancel affordance.
 */
import { Crosshair, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { useUiStore } from "@/stores/ui";
import { useBaselineStore } from "@/stores/baseline";
import { cn } from "@/lib/cn";
import { BaselineSection } from "./WindowsNormality";

interface Props {
  caseId: string;
  timelineId: string;
}

export function BaselineBuilderDrawer({ caseId, timelineId }: Props) {
  const open = useUiStore((s) => s.baselineBuilderOpen);
  const setOpen = useUiStore((s) => s.setBaselineBuilderOpen);
  const markMode = useBaselineStore((s) => s.markMode);
  const setMarkMode = useBaselineStore((s) => s.setMarkMode);
  if (!open) return null;

  // Mark mode is on: a histogram brush is being awaited — step aside.
  // BaselineSection turns mark mode off once the brush lands in a row.
  const awaitingBrush = markMode;

  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-40",
          awaitingBrush && "pointer-events-none opacity-0",
        )}
      >
        {/* Backdrop */}
        <div
          className="absolute inset-0 bg-black/40"
          onClick={() => setOpen(false)}
          aria-hidden
        />
        {/* Drawer */}
        <div className="absolute right-0 top-0 flex h-full w-[min(560px,100vw)] flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-surface)] shadow-xl">
          <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
            <h3 className="flex-1 text-sm font-semibold text-[var(--color-fg-primary)]">
              Baselines &amp; suspect windows
            </h3>
            <Button variant="ghost" size="icon" onClick={() => setOpen(false)}>
              <X size={14} />
            </Button>
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            <BaselineSection caseId={caseId} timelineId={timelineId} />
          </div>
        </div>
      </div>

      {/* Floating hint while the drawer is out of the way for a brush. */}
      {awaitingBrush && (
        <div className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full border border-[var(--color-accent)] bg-[var(--color-bg-surface)] py-1.5 pl-3 pr-1.5 text-xs text-[var(--color-fg-primary)] shadow-lg">
          <Crosshair size={13} className="text-[var(--color-accent)]" />
          <span>Drag on the histogram to mark the window</span>
          <Button variant="ghost" size="icon" title="Cancel marking" onClick={() => setMarkMode(false)}>
            <X size={13} />
          </Button>
        </div>
      )}
    </>
  );
}
