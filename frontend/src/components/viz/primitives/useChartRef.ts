import { useRef } from "react";

/**
 * Resolve the SVG ref a chart hands to its ChartFrame: the caller-provided
 * ref when present (so export can grab the node), otherwise a local
 * fallback. The fallback ref is created unconditionally to keep hook order
 * stable across renders.
 */
export function useChartRef(svgRef?: React.RefObject<SVGSVGElement | null>) {
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  return svgRef ?? fallbackRef;
}
