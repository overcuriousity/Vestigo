import { useRef, useState } from "react";
import { scaleLinear } from "d3-scale";
import { area as d3area, curveBasis } from "d3-shape";
import { format as formatNum } from "d3-format";
import { AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { kdeFromBins, numericDomain } from "@/components/viz/lib/stats";
import type { FieldNumericResponse } from "@/api/types";

const fmtValue = formatNum(",.3~f");
const MAX_HALF_WIDTH = 90;

interface ViolinPlotProps {
  stats: FieldNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
}

/** Violin plot — the numeric field's distribution shape, from a smoothed
 * version of the server's fixed-width bin counts (see `kdeFromBins`). Shows
 * bimodality/skew a box plot's five-number summary would hide. */
export function ViolinPlot({
  stats,
  svgRef,
  height = 260,
  color = "var(--color-accent)",
}: ViolinPlotProps) {
  const [hover, setHover] = useState<{ x: number; y: number; label: string } | null>(null);
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  const ref = svgRef ?? fallbackRef;

  const density = kdeFromBins(stats.bins);
  if (stats.count === 0 || density.length === 0 || stats.min == null || stats.max == null) {
    return (
      <div className="flex h-[220px] items-center justify-center text-sm text-[var(--color-fg-muted)]">
        No numeric values in the current filter range.
      </div>
    );
  }

  const maxDensity = Math.max(1e-9, ...density.map((d) => d.density));

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref} margin={{ top: 16, right: 24, bottom: 24, left: 56 }}>
        {({ innerWidth, innerHeight, margin }) => {
          const y = scaleLinear()
            .domain(numericDomain(stats.min!, stats.max!))
            .nice()
            .range([innerHeight, 0]);
          const w = scaleLinear().domain([0, maxDensity]).range([0, MAX_HALF_WIDTH]);
          const cx = innerWidth / 2;

          const rightPath =
            d3area<{ x: number; density: number }>()
              .curve(curveBasis)
              .x0(cx)
              .x1((d) => cx + w(d.density))
              .y((d) => y(d.x))(density) ?? undefined;
          const leftPath =
            d3area<{ x: number; density: number }>()
              .curve(curveBasis)
              .x0(cx)
              .x1((d) => cx - w(d.density))
              .y((d) => y(d.x))(density) ?? undefined;

          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmtValue(v)} />
              <path d={rightPath} fill={color} fillOpacity={0.35} stroke={color} strokeWidth={1} />
              <path d={leftPath} fill={color} fillOpacity={0.35} stroke={color} strokeWidth={1} />
              {stats.quantiles["0.5"] != null && (
                <line
                  x1={cx - 12}
                  x2={cx + 12}
                  y1={y(stats.quantiles["0.5"])}
                  y2={y(stats.quantiles["0.5"])}
                  stroke="var(--viz-ink-primary)"
                  strokeWidth={2}
                />
              )}
              {/* Invisible hover strip along the value axis for a value-at-cursor tooltip. */}
              <rect
                x={cx - MAX_HALF_WIDTH}
                y={0}
                width={MAX_HALF_WIDTH * 2}
                height={innerHeight}
                fill="transparent"
                onMouseMove={(e) => {
                  const rect = (e.target as SVGRectElement).ownerSVGElement?.getBoundingClientRect();
                  if (!rect) return;
                  const localY = e.clientY - rect.top - margin.top;
                  const value = y.invert(localY);
                  setHover({ x: cx + margin.left, y: localY + margin.top, label: fmtValue(value) });
                }}
                onMouseLeave={() => setHover(null)}
              />
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover?.label}
      </ChartTooltip>
    </div>
  );
}
