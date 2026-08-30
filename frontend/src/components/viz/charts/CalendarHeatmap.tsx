import { useState } from "react";
import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { sequentialColor } from "@/components/viz/lib/colors";
import type { CalendarResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];
const MAX_CELL = 16;
const GAP = 2;
const DAY_MS = 86_400_000;

/** `YYYY-MM-DD` → the UTC midnight Date; the reverse via `isoDate`. */
const parseDay = (s: string) => new Date(`${s}T00:00:00Z`);
const isoDate = (d: Date) => d.toISOString().slice(0, 10);
/** ISO weekday index: 0 = Monday … 6 = Sunday. */
const isoWeekday = (d: Date) => (d.getUTCDay() + 6) % 7;

interface CalendarHeatmapProps {
  data: CalendarResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
}

/**
 * One cell per UTC day, weeks as columns, ISO weekdays as rows — the
 * "which days carried the activity" view. Cell shade = event count on the
 * shared sequential ramp; a day with no events is an outlined empty cell,
 * deliberately distinct from the ramp's lowest step so "few" and "none"
 * never look alike. The server keeps the latest 53 weeks (`truncated`,
 * `dropped`) and the caption says so; this component draws what it got.
 */
export function CalendarHeatmap({ data, svgRef }: CalendarHeatmapProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    date: string;
    count: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  if (data.start == null || data.end == null || data.weeks === 0) {
    return (
      <ChartEmptyState hint="Events without a usable timestamp are excluded from time-based charts.">
        No dated events match the current filters.
      </ChartEmptyState>
    );
  }

  const counts = new Map(data.days.map((d) => [d.date, d.count]));
  const start = parseDay(data.start); // a Monday, by the server's construction
  const endDay = parseDay(data.end);
  const endSunday = new Date(endDay.getTime() + (6 - isoWeekday(endDay)) * DAY_MS);
  const weeks = Math.round(((endSunday.getTime() - start.getTime()) / DAY_MS + 1) / 7);
  const maxCount = Math.max(1, data.max_count);

  return (
    <div className="relative">
      <ChartFrame
        height={7 * (MAX_CELL + GAP) + 44}
        svgRef={ref}
        margin={{ top: 18, right: 8, bottom: 8, left: 36 }}
      >
        {({ innerWidth, margin }) => {
          // The columns must span `innerWidth` and never overflow it. A
          // floored integer cell with a 3px floor broke that below ~265px (a
          // thumbnail, a Story snapshot, a narrow panel): 53 columns needed
          // more room than the <svg> had and the overflow was clipped — and
          // weeks run left→right, so what vanished was the *most recent* days
          // while the caption still claimed 53 weeks. Deriving the step from
          // the width instead makes the fit exact at every size.
          const step = Math.min(MAX_CELL + GAP, innerWidth / weeks);
          const cell = Math.max(1, step - GAP);
          const cells: React.ReactNode[] = [];
          const monthLabels: React.ReactNode[] = [];
          let lastMonth = -1;
          for (let w = 0; w < weeks; w += 1) {
            for (let d = 0; d < 7; d += 1) {
              const day = new Date(start.getTime() + (w * 7 + d) * DAY_MS);
              const date = isoDate(day);
              const count = counts.get(date) ?? 0;
              const x = w * step;
              const y = d * step;
              if (d === 0 && day.getUTCMonth() !== lastMonth) {
                lastMonth = day.getUTCMonth();
                monthLabels.push(
                  <text key={date} x={x} y={-6} fontSize={10} fill="var(--viz-ink-primary)">
                    {MONTHS[lastMonth]}
                  </text>,
                );
              }
              // The grid runs to the Sunday of `data.end`'s week, so its last
              // column can hold up to six days the query never covered. Drawn
              // as cells they were outlined exactly like a genuine zero day —
              // the figure asserting "nothing happened" for days it was never
              // asked about. Leave them blank. Placed after the month label so
              // the axis is unaffected either way.
              if (day.getTime() > endDay.getTime()) continue;
              cells.push(
                <rect
                  key={date}
                  data-cal-day
                  data-date={date}
                  x={x}
                  y={y}
                  width={cell}
                  height={cell}
                  rx={2}
                  fill={count === 0 ? "none" : sequentialColor(count / maxCount)}
                  stroke={count === 0 ? "var(--viz-grid)" : "none"}
                  onMouseEnter={() =>
                    setHover({ x: x + cell / 2 + margin.left, y: y + margin.top, date, count })
                  }
                  onMouseLeave={() => setHover(null)}
                />,
              );
            }
          }
          return (
            <>
              {/* Every other weekday label, so rows never crowd at small cells. */}
              {DAY_LABELS.map((label, i) =>
                i % 2 === 0 ? (
                  <text
                    key={label}
                    x={-6}
                    y={i * step + cell / 2}
                    dy="0.32em"
                    textAnchor="end"
                    fontSize={10}
                    fill="var(--viz-ink-primary)"
                  >
                    {label}
                  </text>
                ) : null,
              )}
              {monthLabels}
              {cells}
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            <div>
              {DAY_LABELS[isoWeekday(parseDay(hover.date))]} {hover.date} (UTC)
            </div>
            <div>
              <strong>{fmtCount(hover.count)}</strong>{" "}
              {data.field ? `events with ${data.field}` : "events"}
            </div>
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
