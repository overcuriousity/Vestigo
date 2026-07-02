/**
 * Floating hover tooltip for chart marks — positioned absolutely within the
 * chart's `ChartFrame` (which is `position: relative`). Every chart owns its
 * own hover state and passes `x`/`y` in pixel coordinates of that container.
 */
interface ChartTooltipProps {
  x: number;
  y: number;
  visible: boolean;
  children: React.ReactNode;
}

export function ChartTooltip({ x, y, visible, children }: ChartTooltipProps) {
  if (!visible) return null;
  return (
    <div
      style={{ left: x, top: y - 8, transform: "translate(-50%, -100%)" }}
      className="pointer-events-none absolute z-10 whitespace-nowrap rounded border border-[var(--color-border)] bg-[var(--color-bg-overlay)] px-2 py-1 text-xs text-[var(--color-fg-secondary)] shadow-md"
    >
      {children}
    </div>
  );
}
