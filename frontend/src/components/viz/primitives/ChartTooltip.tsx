import { useLayoutEffect, useRef, useState } from "react";

/**
 * Floating hover tooltip for chart marks — positioned absolutely within the
 * chart's `ChartFrame` (which is `position: relative`). Every chart owns its
 * own hover state and passes `x`/`y` in pixel coordinates of that container.
 *
 * The centered (`translate(-50%)`) tooltip is clamped to its positioning
 * container's width, so marks near the left/right edge get an edge-flushed
 * tooltip instead of one half-clipped outside the chart. It also flips
 * *below* the anchor when there is no room above: the default is to sit above
 * the mark (`translate(-100%)`), but a top-row mark — the top cell of a
 * correlation matrix, the tallest histogram bar — would otherwise render past
 * the frame's top edge and get clipped by the card. When the measured height
 * would overrun the top, it drops below the anchor instead.
 */
interface ChartTooltipProps {
  x: number;
  y: number;
  visible: boolean;
  children: React.ReactNode;
}

interface Placement {
  left: number;
  top: number;
  below: boolean;
}

export function ChartTooltip({ x, y, visible, children }: ChartTooltipProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [place, setPlace] = useState<Placement>({ left: x, top: y - 8, below: false });

  useLayoutEffect(() => {
    const el = ref.current;
    const parent = el?.offsetParent as HTMLElement | null;
    if (!el || !parent) {
      setPlace({ left: x, top: y - 8, below: false });
      return;
    }
    const half = el.offsetWidth / 2;
    const pad = 4;
    const left = Math.min(Math.max(x, half + pad), parent.clientWidth - half - pad);
    // Above the anchor by default; flip below when the tooltip would clip past
    // the frame's top edge (the mark sits in the top row).
    const below = y - pad - el.offsetHeight < 0;
    setPlace({ left, top: below ? y + pad + 8 : y - 8, below });
  }, [x, y, visible, children]);

  if (!visible) return null;
  return (
    <div
      ref={ref}
      style={{ left: place.left, top: place.top, transform: `translate(-50%, ${place.below ? "0" : "-100%"})` }}
      className="pointer-events-none absolute z-10 whitespace-nowrap rounded border border-[var(--color-border)] bg-[var(--color-bg-overlay)] px-2 py-1 text-xs text-[var(--color-fg-secondary)] shadow-md"
    >
      {children}
    </div>
  );
}
