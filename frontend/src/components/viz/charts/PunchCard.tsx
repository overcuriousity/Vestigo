import { useState } from "react";
import { scaleBand } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottomBand } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { sequentialColor } from "@/components/viz/lib/colors";
import type { PunchcardResponse } from "@/api/types";

const fmtCount = formatNum(",d");
// ISO day-of-week (ClickHouse toDayOfWeek): 1 = Monday … 7 = Sunday.
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAYS = ["1", "2", "3", "4", "5", "6", "7"];
const HOURS = Array.from({ length: 24 }, (_, h) => String(h));
const ROW_HEIGHT = 26;

interface PunchCardProps {
  data: PunchcardResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
}

/**
 * Day-of-week × hour-of-day activity heatmap ("punch card"), UTC — the
 * "does anything run nights or weekends?" view. Rows are ISO weekdays
 * (Mon–Sun), columns are UTC hours; cell shade = event count on the shared
 * sequential ramp. The server response is sparse; the full 7×24 grid is
 * zero-filled here so quiet cells render as explicit zeros, not gaps.
 */
export function PunchCard({ data, svgRef, height }: PunchCardProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    dow: number;
    hour: number;
    count: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  if (data.total === 0) {
    return (
      <ChartEmptyState hint="Events without a usable timestamp are excluded from time-based charts.">
        No dated events match the current filters.
      </ChartEmptyState>
    );
  }

  const counts = new Map<string, number>();
  for (const c of data.cells) counts.set(`${c.dow}:${c.hour}`, c.count);
  const maxCount = Math.max(1, data.max_count);
  const resolvedHeight = height ?? 7 * ROW_HEIGHT + 52;

  return (
    <div className="relative">
      <ChartFrame
        height={resolvedHeight}
        svgRef={ref}
        margin={{ top: 8, right: 8, bottom: 36, left: 44 }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          const xBand = scaleBand().domain(HOURS).range([0, innerWidth]).padding(0.06);
          const yBand = scaleBand().domain(DAYS).range([0, innerHeight]).padding(0.1);

          return (
            <>
              <AxisBottomBand
                scale={xBand}
                innerHeight={innerHeight}
                labelFormat={(h) => `${h.padStart(2, "0")}h`}
              />
              {DAYS.map((d, di) => {
                const ry = yBand(d) ?? 0;
                return (
                  <g key={d}>
                    <text
                      x={-8}
                      y={ry + yBand.bandwidth() / 2}
                      dy="0.32em"
                      textAnchor="end"
                      fontSize={11}
                      fill="var(--viz-ink-primary)"
                    >
                      {DAY_LABELS[di]}
                    </text>
                    {HOURS.map((h) => {
                      const count = counts.get(`${d}:${h}`) ?? 0;
                      const rx = xBand(h) ?? 0;
                      return (
                        <rect
                          key={h}
                          x={rx}
                          y={ry}
                          width={xBand.bandwidth()}
                          height={yBand.bandwidth()}
                          fill={count === 0 ? "var(--viz-grid)" : sequentialColor(count / maxCount)}
                          onMouseEnter={() =>
                            setHover({
                              x: rx + xBand.bandwidth() / 2 + margin.left,
                              y: ry + margin.top,
                              dow: di,
                              hour: Number(h),
                              count,
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
            {DAY_LABELS[hover.dow]} {String(hover.hour).padStart(2, "0")}:00–
            {String((hover.hour + 1) % 24).padStart(2, "0")}:00 UTC
            <br />
            <strong>{fmtCount(hover.count)}</strong> events
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
