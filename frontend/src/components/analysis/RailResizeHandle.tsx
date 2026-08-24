/**
 * RailResizeHandle — the drag edge on the Investigate rail.
 *
 * The rail is the only fixed-width surface the Investigate flow spends, which
 * makes its width the analyst's whole horizontal budget for the findings list.
 * Leaving it at whatever the store last persisted means a findings title is
 * either permanently truncated or permanently stealing room from the grid, with
 * nothing to do about either — so the drag the old panel owned lives on here.
 *
 * Its own component rather than lines in ExplorerPage: the window listeners and
 * the drag ref are self-contained, and ExplorerPage is already the largest file
 * in the app.
 */
import { useCallback, useEffect, useRef } from "react";
import { useUiStore } from "@/stores/ui";

/** Matches the old InvestigatePanel's clamp, so a persisted width still fits. */
const MIN_WIDTH = 320;
const MAX_WIDTH = 720;

export function RailResizeHandle() {
  const investigatePanelWidth = useUiStore((s) => s.investigatePanelWidth);
  const setInvestigatePanelWidth = useUiStore((s) => s.setInvestigatePanelWidth);
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);

  const onDragStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragState.current = { startX: e.clientX, startWidth: investigatePanelWidth };
    },
    [investigatePanelWidth],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragState.current) return;
      // The rail sits on the right, so dragging left widens it.
      const delta = dragState.current.startX - e.clientX;
      setInvestigatePanelWidth(
        Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, dragState.current.startWidth + delta)),
      );
    }
    function onMouseUp() {
      dragState.current = null;
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [setInvestigatePanelWidth]);

  return (
    <div
      data-testid="investigate-rail-resize"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the Investigate rail"
      onMouseDown={onDragStart}
      className="absolute left-0 top-0 z-10 h-full w-1 cursor-col-resize opacity-0 transition-opacity hover:bg-[var(--color-accent)] hover:opacity-100"
      style={{ marginLeft: -2 }}
    />
  );
}
