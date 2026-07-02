import { useRef, useState } from "react";
import { scaleLinear } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import type { FieldNumericResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtValue = formatNum(",.3~f");

interface NumericHistogramProps {
  stats: FieldNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
}

/** Fixed-width value histogram for a numeric (interval/ratio) field —
 * distribution of magnitudes, e.g. response bytes, latency, request rate. */
export function NumericHistogram({
  stats,
  svgRef,
  height = 220,
  color = "var(--color-accent)",
}: NumericHistogramProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    x0: number;
    x1: number;
    count: number;
  } | null>(null);
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  const ref = svgRef ?? fallbackRef;

  if (stats.count === 0 || stats.bins.length === 0 || stats.min == null || stats.max == null) {
    return (
      <div className="flex h-[220px] items-center justify-center text-sm text-[var(--color-fg-muted)]">
        No numeric values in the current filter range.
      </div>
    );
  }

  const maxCount = Math.max(1, ...stats.bins.map((b) => b.count));

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleLinear().domain([stats.min!, stats.max!]).range([0, innerWidth]);
          const y = scaleLinear().domain([0, maxCount]).nice().range([innerHeight, 0]);
          const gap = 1;

          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmtCount(v)} />
              <AxisBottom
                scale={x}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtValue(v as number)}
              />
              {stats.bins.map((b, i) => {
                const bx = x(b.x0);
                const bw = Math.max(1, x(b.x1) - x(b.x0) - gap);
                return (
                  <rect
                    key={i}
                    x={bx}
                    y={y(b.count)}
                    width={bw}
                    height={innerHeight - y(b.count)}
                    fill={color}
                    onMouseEnter={() =>
                      setHover({
                        x: bx + bw / 2 + margin.left,
                        y: y(b.count) + margin.top,
                        x0: b.x0,
                        x1: b.x1,
                        count: b.count,
                      })
                    }
                    onMouseLeave={() => setHover(null)}
                  />
                );
              })}
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            [{fmtValue(hover.x0)}, {fmtValue(hover.x1)})<br />
            <strong>{fmtCount(hover.count)}</strong> events
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
