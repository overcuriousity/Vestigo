import { useState } from "react";
import { scaleLinear, scaleLog } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { numericDomain } from "@/components/viz/lib/stats";
import type { CompareNumericResponse, FieldNumericResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtValue = formatNum(",.3~f");

interface NumericHistogramProps {
  stats?: FieldNumericResponse;
  /** Two-layer numeric result — when set, the comparison layer renders as a
   * hollow outline behind the filled primary bars (shared bin edges are
   * guaranteed server-side) and `stats` is ignored. */
  compare?: CompareNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
  /** Log-scaled count axis — zero-count bins render as zero-height bars. */
  logScale?: boolean;
}

/** Fixed-width value histogram for a numeric (interval/ratio) field —
 * distribution of magnitudes, e.g. response bytes, latency, request rate. */
export function NumericHistogram({
  stats,
  compare,
  svgRef,
  height = 220,
  color = "var(--color-accent)",
  logScale = false,
}: NumericHistogramProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    x0: number;
    x1: number;
    count: number;
    comparison?: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  const bins = compare
    ? compare.bins.map((b) => ({ x0: b.x0, x1: b.x1, count: b.primary, comparison: b.comparison }))
    : (stats?.bins ?? []).map((b) => ({ ...b, comparison: undefined as number | undefined }));
  const min = compare ? compare.min : stats?.min;
  const max = compare ? compare.max : stats?.max;

  if (bins.length === 0 || min == null || max == null) {
    return <ChartEmptyState>No numeric values in the current filter range.</ChartEmptyState>;
  }

  const maxCount = Math.max(
    1,
    ...bins.map((b) => b.count),
    ...bins.map((b) => b.comparison ?? 0),
  );

  return (
    <div className="flex flex-col gap-2">
      {compare && (
        <Legend
          entries={[
            { label: "Filtered events", color },
            { label: "Comparison layer", color: "var(--color-fg-disabled)", muted: true },
          ]}
        />
      )}
      <div className="relative">
        <ChartFrame height={height} svgRef={ref}>
          {({ innerWidth, innerHeight, margin }) => {
            const x = scaleLinear().domain(numericDomain(min, max)).range([0, innerWidth]);
            // A log scale has no 0 — clamp to [1, max] and draw zero counts flat.
            const y = logScale
              ? scaleLog().domain([1, maxCount]).range([innerHeight, 0]).clamp(true)
              : scaleLinear().domain([0, maxCount]).nice().range([innerHeight, 0]);
            const topOf = (count: number) =>
              logScale && count < 1 ? innerHeight : y(Math.max(count, logScale ? 1 : 0));
            const gap = 1;

            return (
              <>
                <AxisLeft
                  scale={y as never}
                  innerWidth={innerWidth}
                  tickFormat={(v) => fmtCount(v)}
                />
                <AxisBottom
                  scale={x}
                  innerWidth={innerWidth}
                  innerHeight={innerHeight}
                  tickFormat={(v) => fmtValue(v as number)}
                />
                {bins.map((b, i) => {
                  const bx = x(b.x0);
                  const bw = Math.max(1, x(b.x1) - x(b.x0) - gap);
                  const py = topOf(b.count);
                  return (
                    <g key={i}>
                      {b.comparison != null && b.comparison > 0 && (
                        <rect
                          x={bx}
                          y={topOf(b.comparison)}
                          width={bw}
                          height={innerHeight - topOf(b.comparison)}
                          fill="none"
                          stroke="var(--color-fg-disabled)"
                          strokeWidth={1}
                        />
                      )}
                      <rect
                        x={bx}
                        y={py}
                        width={bw}
                        height={innerHeight - py}
                        fill={color}
                        opacity={compare ? 0.9 : 1}
                        onMouseEnter={() =>
                          setHover({
                            x: bx + bw / 2 + margin.left,
                            y: py + margin.top,
                            x0: b.x0,
                            x1: b.x1,
                            count: b.count,
                            comparison: b.comparison,
                          })
                        }
                        onMouseLeave={() => setHover(null)}
                      />
                    </g>
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
              {hover.comparison != null && <> · comparison: {fmtCount(hover.comparison)}</>}
            </>
          )}
        </ChartTooltip>
      </div>
    </div>
  );
}
