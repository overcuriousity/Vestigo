import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

export interface ChartMargin {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export const DEFAULT_MARGIN: ChartMargin = { top: 16, right: 16, bottom: 32, left: 48 };

interface ChartDims {
  width: number;
  height: number;
  innerWidth: number;
  innerHeight: number;
  margin: ChartMargin;
}

interface ChartFrameProps {
  height?: number;
  margin?: Partial<ChartMargin>;
  className?: string;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  /** Render prop — receives resolved dimensions once the container has a measured width. */
  children: (dims: ChartDims) => React.ReactNode;
}

/**
 * Responsive SVG chart container — measures its own width via ResizeObserver
 * (charts fill whatever column/panel they're placed in) and exposes the
 * plot-area dimensions (outer minus margin) to the render-prop children.
 *
 * Real `<svg>` output (not canvas) is deliberate: it's what makes SVG export
 * a plain `XMLSerializer` call and PNG export a canvas redraw at any
 * resolution (see `viz/lib/export.ts`).
 */
export function ChartFrame({
  height = 280,
  margin: marginOverride,
  className,
  svgRef,
  children,
}: ChartFrameProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const margin = { ...DEFAULT_MARGIN, ...marginOverride };

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w != null) setWidth(Math.max(0, Math.floor(w)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const innerWidth = Math.max(0, width - margin.left - margin.right);
  const innerHeight = Math.max(0, height - margin.top - margin.bottom);

  return (
    <div ref={containerRef} className={cn("relative w-full", className)}>
      {width > 0 && (
        <svg
          ref={svgRef}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
        >
          <g transform={`translate(${margin.left},${margin.top})`}>
            {children({ width, height, innerWidth, innerHeight, margin })}
          </g>
        </svg>
      )}
    </div>
  );
}
