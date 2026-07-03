import { useState } from "react";
import { scaleBand } from "d3-scale";
import { utcFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottomBand } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { sequentialColor } from "@/components/viz/lib/colors";
import type { FieldTimeseriesResponse } from "@/api/types";

const fmtCount = formatNum(",d");
// utcFormat, not timeFormat — bucket starts are UTC instants and the label
// says "UTC"; timeFormat would silently render them in the browser's zone.
const fmtFull = utcFormat("%Y-%m-%d %H:%M UTC");
const fmtTickTime = utcFormat("%H:%M");
const fmtTickDay = utcFormat("%m-%d %H:%M");
const fmtTickYear = utcFormat("%y-%m-%d %H:%M");
const ROW_HEIGHT = 24;

/** Pick the shortest tick format that still disambiguates the bucket range:
 * time-only within one UTC day, month-day within one year, full date
 * otherwise. Keeps axis labels short enough to read instead of truncating
 * full timestamps to an identical shared prefix. */
function tickFormatter(starts: string[]): (v: string) => string {
  const first = new Date(starts[0]);
  const last = new Date(starts[starts.length - 1]);
  const sameDay =
    first.getUTCFullYear() === last.getUTCFullYear() &&
    first.getUTCMonth() === last.getUTCMonth() &&
    first.getUTCDate() === last.getUTCDate();
  const sameYear = first.getUTCFullYear() === last.getUTCFullYear();
  const fmt = sameDay ? fmtTickTime : sameYear ? fmtTickDay : fmtTickYear;
  return (v) => fmt(new Date(v));
}

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
  const ref = useChartRef(svgRef);

  if (data.series.length === 0 || data.series[0].buckets.length === 0) {
    return <ChartEmptyState>No data in the current filter range.</ChartEmptyState>;
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
        // Bottom margin fits the longest rotated tick label ("%y-%m-%d %H:%M",
        // ~84px long at -40° ≈ 54px of vertical extent + offsets) unclipped.
        margin={{ top: 8, right: 8, bottom: 72, left: labelCol }}
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
                labelFormat={tickFormatter(bucketStarts)}
                maxLabelChars={17}
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
