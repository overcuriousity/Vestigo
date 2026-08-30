import { useState } from "react";
import { scaleLinear, scaleTime } from "d3-scale";
import { line as d3line, curveStepAfter } from "d3-shape";
import { bisector } from "d3-array";
import { utcFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { MarksOverlay } from "@/components/viz/primitives/MarksOverlay";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { seriesColorVar } from "@/components/viz/lib/colors";
import { svgLocalPoint } from "@/components/viz/lib/pointer";
import { cumulativeChartDomain } from "@/components/viz/lib/timeDomain";
import type { CumulativeResponse, ResolvedMark } from "@/api/types";

const fmtInt = formatNum(",d");
const fmtNum = formatNum(",.2~f");
// utcFormat, not timeFormat — bucket starts are UTC instants (see LineChart).
const fmtTick = utcFormat("%b %d %H:%M");
const fmtFull = utcFormat("%Y-%m-%d %H:%M:%S UTC");
const bisectDate = bisector((d: Date) => d).left;

interface CumulativeStepProps {
  data: CumulativeResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Resolved marks to overlay (see lib/marks.ts); drawn last, above the step. */
  marks?: ResolvedMark[];
}

/** What one unit of the y axis is, for the tooltip and the caption. */
export function quantityNoun(data: CumulativeResponse): string {
  if (data.quantity === "events") return "events";
  if (data.quantity === "sum") return `sum of ${data.field}`;
  return `distinct ${data.field}`;
}

/**
 * Running total over time drawn as a step — the value holds until the next
 * bucket changes it, and the line never interpolates between measurements
 * (a diagonal would assert growth inside a bucket nobody measured). The y
 * axis is the accumulated quantity; the tooltip shows a bucket's own
 * contribution beside the running value.
 */
export function CumulativeStep({ data, svgRef, height = 260, marks = [] }: CumulativeStepProps) {
  const [hover, setHover] = useState<{ x: number; y: number; index: number } | null>(null);
  const ref = useChartRef(svgRef);
  const fmt = data.quantity === "sum" ? fmtNum : fmtInt;

  if (data.buckets.length === 0 || data.min == null || data.max == null) {
    return (
      <ChartEmptyState hint="Events without a usable timestamp are excluded from time-based charts.">
        No dated events match the current filters.
      </ChartEmptyState>
    );
  }

  const dates = data.buckets.map((b) => new Date(b.start));
  // The drawn axis, computed once in lib/timeDomain so the caption can reason
  // about the same interval. The step holds through the last bucket, so it
  // ends one bucket after the last start (or at `max` if that is later).
  const domain = cumulativeChartDomain(data)!;
  const lastEnd = domain[1];
  // The ceiling is the highest running value, not the final one: `sum` over a
  // signed measure is not monotonic, and a series that peaks at 500 before
  // settling at 20 would otherwise be drawn against a [0, 20] axis. The floor
  // is 0 unless the running total actually goes below it.
  const values = data.buckets.map((b) => b.value).concat(data.total);
  const yMax = Math.max(1e-9, ...values);
  const yMin = Math.min(0, ...values);
  const noun = quantityNoun(data);

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleTime().domain(domain).range([0, innerWidth]);
          const y = scaleLinear().domain([yMin, yMax]).nice().range([innerHeight, 0]);
          const points: [Date, number][] = data.buckets.map((b, i) => [dates[i], b.value]);
          points.push([lastEnd, data.total]);
          const path = d3line<[Date, number]>()
            .x((p) => x(p[0]))
            .y((p) => y(p[1]))
            .curve(curveStepAfter)(points);
          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmt(v)} />
              <AxisBottom
                scale={x}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtTick(v as Date)}
              />
              <path
                data-cumulative-step
                d={path ?? ""}
                fill="none"
                stroke={seriesColorVar(0)}
                strokeWidth={1.5}
              />
              {hover && (
                <line
                  x1={x(dates[hover.index])}
                  x2={x(dates[hover.index])}
                  y1={0}
                  y2={innerHeight}
                  stroke="var(--viz-axis)"
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
                  const local = svgLocalPoint(e, margin);
                  if (!local) return;
                  const target = x.invert(local.x);
                  // The bucket whose start is at or before the pointer — the
                  // step's value there is that bucket's running total.
                  let idx = bisectDate(dates, target, 1) - 1;
                  idx = Math.min(dates.length - 1, Math.max(0, idx));
                  setHover({ x: local.x + margin.left, y: margin.top, index: idx });
                }}
                onMouseLeave={() => setHover(null)}
              />
              <MarksOverlay
                marks={marks}
                x={(d) => x(d)}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
              />
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            <div>{fmtFull(dates[hover.index])}</div>
            <div>
              <strong>{fmt(data.buckets[hover.index].value)}</strong> {noun} so far
            </div>
            {/* Signed, not `+`-prefixed: a "sum" quantity over a field with
                negative values has negative deltas (which is why `yMin`
                admits them), and a hard-coded sign read `+-3.5`. */}
            <div>
              {data.buckets[hover.index].delta < 0 ? "" : "+"}
              {fmt(data.buckets[hover.index].delta)} in this bucket
            </div>
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
