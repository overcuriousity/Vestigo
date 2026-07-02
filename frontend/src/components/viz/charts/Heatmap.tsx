import { useRef, useState } from "react";
import { scaleBand } from "d3-scale";
import { timeFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottomBand } from "@/components/viz/primitives/Axis";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { sequentialColor } from "@/components/viz/lib/colors";
import type { FieldTimeseriesResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtFull = timeFormat("%Y-%m-%d %H:%M UTC");
const ROW_HEIGHT = 24;

interface HeatmapProps {
  data: FieldTimeseriesResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
}

/**
 * Value × time density heatmap — one row per top value, one column per time
 * bucket, cell shade = event count (sequential ramp). Good for spotting a
 * burst concentrated in one value (e.g. one status code spiking) versus a
 * broad, correlated spike across values.
 */
export function Heatmap({ data, svgRef, height }: HeatmapProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    value: string;
    start: string;
    count: number;
  } | null>(null);
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  const ref = svgRef ?? fallbackRef;

  if (data.series.length === 0 || data.series[0].buckets.length === 0) {
    return (
      <div className="flex h-[220px] items-center justify-center text-sm text-[var(--color-fg-muted)]">
        No data in the current filter range.
      </div>
    );
  }

  const bucketStarts = data.series[0].buckets.map((b) => b.start);
  const maxCount = Math.max(1, ...data.series.flatMap((s) => s.buckets.map((b) => b.count)));
  const resolvedHeight = height ?? Math.max(120, data.series.length * ROW_HEIGHT + 44);
  const labelCol = 130;

  return (
    <div className="relative">
      <ChartFrame
        height={resolvedHeight}
        svgRef={ref}
        margin={{ top: 8, right: 8, bottom: 44, left: labelCol }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          const xBand = scaleBand().domain(bucketStarts).range([0, innerWidth]).padding(0.04);
          const yBand = scaleBand()
            .domain(data.series.map((s) => s.value))
            .range([0, innerHeight])
            .padding(0.08);

          return (
            <>
              <AxisBottomBand
                scale={xBand}
                innerHeight={innerHeight}
                rotate
                labelFormat={(v) => fmtFull(new Date(v))}
              />
              {data.series.map((s) => {
                const ry = yBand(s.value) ?? 0;
                return (
                  <g key={s.value}>
                    <text
                      x={-8}
                      y={ry + yBand.bandwidth() / 2}
                      dy="0.32em"
                      textAnchor="end"
                      fontSize={11}
                      fill="var(--viz-ink-primary)"
                    >
                      {s.value.length > 20 ? s.value.slice(0, 19) + "…" : s.value}
                    </text>
                    {s.buckets.map((b) => {
                      const rx = xBand(b.start) ?? 0;
                      return (
                        <rect
                          key={b.start}
                          x={rx}
                          y={ry}
                          width={xBand.bandwidth()}
                          height={yBand.bandwidth()}
                          fill={b.count === 0 ? "var(--viz-grid)" : sequentialColor(b.count / maxCount)}
                          onMouseEnter={() =>
                            setHover({
                              x: rx + xBand.bandwidth() / 2 + margin.left,
                              y: ry + margin.top,
                              value: s.value,
                              start: b.start,
                              count: b.count,
                            })
                          }
                          onMouseLeave={() => setHover(null)}
                        />
                      );
                    })}
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
            {hover.value}
            <br />
            {fmtFull(new Date(hover.start))}
            <br />
            <strong>{fmtCount(hover.count)}</strong> events
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
