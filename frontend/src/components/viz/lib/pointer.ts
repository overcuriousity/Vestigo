/**
 * Pointer position of a mouse event in a chart's inner plot coordinates
 * (origin at the top-left of the plot area, i.e. after the frame margin) —
 * the input `scale.invert()` expects. Returns null when the event target is
 * detached from an SVG. The one event-coupled helper in lib/; everything
 * else here stays pure.
 */
export function svgLocalPoint(
  e: React.MouseEvent<SVGElement>,
  margin: { top: number; left: number },
): { x: number; y: number } | null {
  const rect = (e.target as SVGElement).ownerSVGElement?.getBoundingClientRect();
  if (!rect) return null;
  return {
    x: e.clientX - rect.left - margin.left,
    y: e.clientY - rect.top - margin.top,
  };
}
