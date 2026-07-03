import { useState } from "react";
import { scaleLinear, scaleTime } from "d3-scale";
import { max as d3max } from "d3-array";
import { utcFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import type { HistogramBucket } from "@/api/types";

const fmtCount = formatNum(",d");
// utcFormat, not timeFormat — bucket starts are UTC instants and the tooltip
// says "UTC"; timeFormat would silently render them in the browser's zone.
const fmtTick = utcFormat("%b %d %H:%M");
const fmtFull = utcFormat("%Y-%m-%d %H:%M:%S UTC");

interface TimeHistogramProps {
  buckets: HistogramBucket[];
  /** Optional dimmed "context" series (e.g. total event volume) drawn behind
   * the primary bars, at the same bucket boundaries. */
  contextBuckets?: HistogramBucket[];
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
}

/** Bucketed event-count histogram over time — single series. Used by the
 * per-value histogram modal and as the interval/ratio time chart on the
 * Visualization page. */
export function TimeHistogram({
  buckets,
  contextBuckets,
  svgRef,
  height = 220,
  color = "var(--color-accent)",
}: TimeHistogramProps) {
  const [hover, setHover] = useState<{ x: number; y: number; bucket: HistogramBucket } | null>(
    null,
  );
  const ref = useChartRef(svgRef);

  if (buckets.length === 0) {
    return <ChartEmptyState>No data in the current filter range.</ChartEmptyState>;
  }

  // Domain spans whichever series covers more time — `contextBuckets` (the
  // unscoped total) usually does, since a focused value may not occur across
  // the entire filtered range. Callers should fetch both series with the
  // same explicit start/end/bucket-count so bar widths line up (see
  // `FieldHistogramModal`); this is just a defensive fallback.
  const widerSeries =
    (contextBuckets?.length ?? 0) > buckets.length ? contextBuckets! : buckets;
  const dates = widerSeries.map((b) => new Date(b.start));
  const domainMax = dates.length > 1 ? dates[dates.length - 1] : dates[0];
  const contextMax = d3max(contextBuckets ?? [], (b) => b.count) ?? 0;
  const maxCount = Math.max(1, d3max(buckets, (b) => b.count) ?? 0, contextMax);

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleTime().domain([dates[0], domainMax]).range([0, innerWidth]);
          const y = scaleLinear().domain([0, maxCount]).nice().range([innerHeight, 0]);
          const barWidth = Math.max(1, innerWidth / widerSeries.length - 1);

          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmtCount(v)} />
              <AxisBottom
                scale={x}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtTick(v as Date)}
              />
              {contextBuckets?.map((b, i) => {
                const bx = x(new Date(b.start));
                return (
                  <rect
                    key={`ctx-${i}`}
                    x={bx}
                    y={y(b.count)}
                    width={barWidth}
                    height={innerHeight - y(b.count)}
                    fill="var(--color-fg-disabled)"
                    opacity={0.35}
                  />
                );
              })}
              {buckets.map((b, i) => {
                const bx = x(new Date(b.start));
                return (
                  <rect
                    key={i}
                    x={bx}
                    y={y(b.count)}
                    width={barWidth}
                    height={innerHeight - y(b.count)}
                    fill={color}
                    onMouseEnter={() =>
                      setHover({
                        x: bx + barWidth / 2 + margin.left,
                        y: y(b.count) + margin.top,
                        bucket: b,
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
            {fmtFull(new Date(hover.bucket.start))}
            <br />
            <strong>{fmtCount(hover.bucket.count)}</strong> events
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
