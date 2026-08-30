import { useState } from "react";
import { scaleBand, scaleTime } from "d3-scale";
import { utcFormat } from "d3-time-format";
import { AxisBottom } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { MarksOverlay } from "@/components/viz/primitives/MarksOverlay";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { valueLabeller } from "@/components/viz/lib/fieldDisplay";
import { lanesChartDomain } from "@/components/viz/lib/timeDomain";
import type { LaneInterval, LanesResponse, ResolvedMark } from "@/api/types";

// utcFormat, not timeFormat — instants are UTC (see LineChart, CumulativeStep).
const fmtTick = utcFormat("%b %d %H:%M");
const fmtFull = utcFormat("%Y-%m-%d %H:%M:%S");
const LABEL_COL = 140;
const ROW_H = 24;
const BAR_COLOR = "var(--color-accent)";
const OPEN_OPACITY = 0.55;
const ARROW = 7;

interface IntervalLanesProps {
  data: LanesResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Resolved marks, drawn across every lane. */
  marks?: ResolvedMark[];
}

interface Hover {
  x: number;
  y: number;
  lane: string;
  interval: LaneInterval;
}

/**
 * Interval lanes: one lane per value of the field, a bar per interval. An
 * open-ended interval (no end seen) runs to the slice end under an arrowhead
 * so "still open" never looks like "ended at the edge"; orphan ends are in
 * the caption, never on the canvas. The server has already ranked and capped
 * the lanes and paired the rows — this component draws what it got.
 */
export function IntervalLanes({ data, svgRef, height, marks = [] }: IntervalLanesProps) {
  const [hover, setHover] = useState<Hover | null>(null);
  const ref = useChartRef(svgRef);
  const labelOf = valueLabeller(data.field);

  const lanes = data.lanes.filter((lane) => lane.intervals.length > 0);
  if (lanes.length === 0 || !data.slice_start || !data.slice_end) {
    return (
      <ChartEmptyState
        hint={
          data.pairing === "next_end"
            ? "No events match the start filter or the end filter under the current filters — check that both name events that exist in this slice."
            : "No dated events with a value for this field under the current filters."
        }
      >
        No intervals to draw.
      </ChartEmptyState>
    );
  }

  const [sliceStart, sliceEnd] = lanesChartDomain(data)!;
  const frameHeight = height ?? lanes.length * ROW_H + 44;
  const legend = (
    <Legend
      entries={[
        { label: "interval", color: BAR_COLOR },
        { label: "no end seen — runs to the slice end", color: BAR_COLOR, muted: true },
      ]}
    />
  );
  const tooltip = (
    <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
      {hover && (
        <>
          <div>{labelOf(hover.lane)}</div>
          <div>
            start: <strong>{fmtFull(new Date(hover.interval.start))}</strong> (
            {hover.interval.start_event_id})
          </div>
          <div>
            end:{" "}
            {hover.interval.end ? (
              <>
                <strong>{fmtFull(new Date(hover.interval.end))}</strong> (
                {hover.interval.end_event_id})
              </>
            ) : (
              <strong>no end seen</strong>
            )}
          </div>
        </>
      )}
    </ChartTooltip>
  );

  return (
    <div className="relative">
      <ChartFrame
        height={frameHeight}
        svgRef={ref}
        margin={{ top: 8, right: 16, bottom: 32, left: LABEL_COL }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleTime().domain([sliceStart, sliceEnd]).range([0, innerWidth]);
          const y = scaleBand<string>()
            .domain(lanes.map((lane) => lane.key))
            .range([0, innerHeight])
            .paddingInner(0.35);
          const barH = y.bandwidth();
          return (
            <>
              <AxisBottom
                scale={x}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtTick(v as Date)}
              />
              {lanes.map((lane) => {
                const top = y(lane.key) ?? 0;
                return (
                  <g key={lane.key} data-lane={lane.key}>
                    <text
                      x={-8}
                      y={top + barH / 2}
                      dy="0.32em"
                      textAnchor="end"
                      fontSize={11}
                      fill="var(--viz-ink-primary)"
                    >
                      {labelOf(lane.key)}
                    </text>
                    <line
                      x1={0}
                      x2={innerWidth}
                      y1={top + barH / 2}
                      y2={top + barH / 2}
                      stroke="var(--viz-grid)"
                      strokeDasharray="2 3"
                    />
                    {lane.intervals.map((interval, i) => {
                      const open = interval.end == null;
                      const x1 = Math.max(0, x(new Date(interval.start)));
                      const x2 = open
                        ? innerWidth
                        : Math.min(innerWidth, x(new Date(interval.end!)));
                      const width = Math.max(2, x2 - x1);
                      return (
                        <g
                          key={`${interval.start_event_id}-${i}`}
                          onMouseEnter={() =>
                            setHover({
                              x: x1 + width / 2 + margin.left,
                              y: top + margin.top,
                              lane: lane.key,
                              interval,
                            })
                          }
                          onMouseLeave={() => setHover(null)}
                        >
                          <rect
                            data-lane-interval
                            data-open={open ? "true" : "false"}
                            x={x1}
                            y={top}
                            width={open ? Math.max(2, width - ARROW) : width}
                            height={barH}
                            rx={2}
                            fill={BAR_COLOR}
                            opacity={open ? OPEN_OPACITY : 1}
                          />
                          {open && (
                            <path
                              data-lane-open-arrow
                              d={`M${innerWidth - ARROW} ${top} L${innerWidth} ${top + barH / 2} L${innerWidth - ARROW} ${top + barH} Z`}
                              fill={BAR_COLOR}
                              opacity={OPEN_OPACITY}
                            />
                          )}
                        </g>
                      );
                    })}
                  </g>
                );
              })}
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
      {legend}
      {tooltip}
    </div>
  );
}
