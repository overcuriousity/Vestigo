import { useLayoutEffect, useRef, useState } from "react";

/**
 * Floating hover tooltip for chart marks — positioned absolutely within the
 * chart's `ChartFrame` (which is `position: relative`). Every chart owns its
 * own hover state and passes `x`/`y` in pixel coordinates of that container.
 *
 * The centered (`translate(-50%)`) tooltip is clamped to its positioning
 * container's width, so marks near the left/right edge get an edge-flushed
 * tooltip instead of one half-clipped outside the chart.
 */
interface ChartTooltipProps {
  x: number;
  y: number;
  visible: boolean;
  children: React.ReactNode;
}

export function ChartTooltip({ x, y, visible, children }: ChartTooltipProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [left, setLeft] = useState(x);

  useLayoutEffect(() => {
    const el = ref.current;
    const parent = el?.offsetParent as HTMLElement | null;
    if (!el || !parent) {
      setLeft(x);
      return;
    }
    const half = el.offsetWidth / 2;
    const pad = 4;
    setLeft(Math.min(Math.max(x, half + pad), parent.clientWidth - half - pad));
  }, [x, visible, children]);

  if (!visible) return null;
  return (
    <div
      ref={ref}
      style={{ left, top: y - 8, transform: "translate(-50%, -100%)" }}
      className="pointer-events-none absolute z-10 whitespace-nowrap rounded border border-[var(--color-border)] bg-[var(--color-bg-overlay)] px-2 py-1 text-xs text-[var(--color-fg-secondary)] shadow-md"
    >
      {children}
    </div>
  );
}
