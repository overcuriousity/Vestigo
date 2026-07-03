import { useState } from "react";
import { scaleLinear } from "d3-scale";
import { line as d3line, curveStepAfter } from "d3-shape";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { svgLocalPoint } from "@/components/viz/lib/pointer";
import { ecdfFromBins, numericDomain } from "@/components/viz/lib/stats";
import type { FieldNumericResponse } from "@/api/types";

const fmtValue = formatNum(",.3~f");
const fmtPct = formatNum(".0%");

interface EcdfChartProps {
  stats: FieldNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
}

/** Empirical CDF — what fraction of events fall at or below a given value.
 * Reads tail behavior (p95/p99) more precisely than a histogram, useful for
 * latency/size fields when hunting a long tail or a hard cutoff. */
export function EcdfChart({ stats, svgRef, height = 220, color = "var(--color-accent)" }: EcdfChartProps) {
  const [hover, setHover] = useState<{ x: number; y: number; value: number; p: number } | null>(
    null,
  );
  const ref = useChartRef(svgRef);

  const points = ecdfFromBins(stats.bins);
  if (stats.count === 0 || points.length === 0 || stats.min == null || stats.max == null) {
    return <ChartEmptyState>No numeric values in the current filter range.</ChartEmptyState>;
  }

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleLinear()
            .domain(numericDomain(stats.min!, stats.max!))
            .range([0, innerWidth]);
          const y = scaleLinear().domain([0, 1]).range([innerHeight, 0]);
          const lineGen = d3line<{ x: number; p: number }>()
            .curve(curveStepAfter)
            .x((d) => x(d.x))
            .y((d) => y(d.p));

          return (
            <>
              <AxisLeft
                scale={y}
                innerWidth={innerWidth}
                ticks={5}
                tickFormat={(v) => fmtPct(v)}
              />
              <AxisBottom
                scale={x}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtValue(v as number)}
              />
              <path d={lineGen(points) ?? undefined} fill="none" stroke={color} strokeWidth={2} />
              <rect
                x={0}
                y={0}
                width={innerWidth}
                height={innerHeight}
                fill="transparent"
                onMouseMove={(e) => {
                  const local = svgLocalPoint(e, margin);
                  if (!local) return;
                  const localX = local.x;
                  const value = x.invert(localX);
                  let nearest = points[0];
                  for (const p of points) {
                    if (p.x <= value) nearest = p;
                    else break;
                  }
                  setHover({
                    x: localX + margin.left,
                    y: y(nearest.p) + margin.top,
                    value,
                    p: nearest.p,
                  });
                }}
                onMouseLeave={() => setHover(null)}
              />
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            ≤ {fmtValue(hover.value)}: <strong>{fmtPct(hover.p)}</strong>
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
