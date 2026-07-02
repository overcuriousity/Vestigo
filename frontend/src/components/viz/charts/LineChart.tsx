import { useRef, useState } from "react";
import { scaleLinear, scaleTime } from "d3-scale";
import { line as d3line, curveMonotoneX } from "d3-shape";
import { max as d3max, bisector } from "d3-array";
import { timeFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { buildSeriesColorMap } from "@/components/viz/lib/colors";
import type { FieldTimeseriesResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtTick = timeFormat("%b %d %H:%M");
const fmtFull = timeFormat("%Y-%m-%d %H:%M:%S UTC");
const bisectDate = bisector((d: Date) => d).left;

interface LineChartProps {
  data: FieldTimeseriesResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
}

/**
 * Multi-series line chart — per-value event counts over time, restricted to
 * the top values (see `EventQueryService.field_value_timeseries`). A
 * crosshair + tooltip shows every series' value at the hovered bucket, per
 * the dataviz skill's line-chart interaction default.
 */
export function LineChart({ data, svgRef, height = 260 }: LineChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  const ref = svgRef ?? fallbackRef;

  if (data.series.length === 0 || data.series[0].buckets.length === 0) {
    return (
      <div className="flex h-[220px] items-center justify-center text-sm text-[var(--color-fg-muted)]">
        No data in the current filter range.
      </div>
    );
  }

  const dates = data.series[0].buckets.map((b) => new Date(b.start));
  const maxCount = Math.max(1, d3max(data.series, (s) => d3max(s.buckets, (b) => b.count) ?? 0) ?? 0);
  const colorMap = buildSeriesColorMap(data.series.map((s) => s.value));

  return (
    <div className="relative flex flex-col gap-2">
      <ChartFrame height={height} svgRef={ref}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleTime()
            .domain([dates[0], dates[dates.length - 1]])
            .range([0, innerWidth]);
          const y = scaleLinear().domain([0, maxCount]).nice().range([innerHeight, 0]);
          const lineGen = d3line<{ start: string; count: number }>()
            .curve(curveMonotoneX)
            .x((d) => x(new Date(d.start)))
            .y((d) => y(d.count));

          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmtCount(v)} />
              <AxisBottom scale={x} innerHeight={innerHeight} tickFormat={(v) => fmtTick(v as Date)} />
              {data.series.map((s) => (
                <path
                  key={s.value}
                  d={lineGen(s.buckets) ?? undefined}
                  fill="none"
                  stroke={colorMap.get(s.value) ?? "var(--color-accent)"}
                  strokeWidth={1.75}
                />
              ))}
              {hoverIdx != null && (
                <line
                  x1={x(dates[hoverIdx])}
                  x2={x(dates[hoverIdx])}
                  y1={0}
                  y2={innerHeight}
                  stroke="var(--viz-axis)"
                  strokeWidth={1}
                  strokeDasharray="2,2"
                />
              )}
              {/* Full-height hover strip drives the crosshair + tooltip. */}
              <rect
                x={0}
                y={0}
                width={innerWidth}
                height={innerHeight}
                fill="transparent"
                onMouseMove={(e) => {
                  const rect = (e.target as SVGRectElement).ownerSVGElement?.getBoundingClientRect();
                  if (!rect) return;
                  const localX = e.clientX - rect.left - margin.left;
                  const target = x.invert(localX);
                  let idx = bisectDate(dates, target, 1);
                  idx = Math.min(dates.length - 1, Math.max(0, idx));
                  if (
                    idx > 0 &&
                    target.getTime() - dates[idx - 1].getTime() < dates[idx].getTime() - target.getTime()
                  ) {
                    idx -= 1;
                  }
                  setHoverIdx(idx);
                }}
                onMouseLeave={() => setHoverIdx(null)}
              />
            </>
          );
        }}
      </ChartFrame>
      <Legend
        entries={data.series.map((s) => ({
          label: s.value,
          color: colorMap.get(s.value) ?? "var(--color-accent)",
        }))}
      />
      {hoverIdx != null && (
        <ChartTooltip
          x={80}
          y={16}
          visible
        >
          <div>{fmtFull(dates[hoverIdx])}</div>
          {data.series.map((s) => (
            <div key={s.value}>
              <span style={{ color: colorMap.get(s.value) }}>●</span> {s.value}:{" "}
              <strong>{fmtCount(s.buckets[hoverIdx]?.count ?? 0)}</strong>
            </div>
          ))}
        </ChartTooltip>
      )}
    </div>
  );
}
