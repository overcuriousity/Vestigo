import { useState } from "react";
import { scaleBand } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottomBand } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { OTHER_KEY, OTHER_LABEL, sequentialColor } from "@/components/viz/lib/colors";
import type { ChartValueClickHandler } from "@/components/viz/lib/interaction";
import type { FieldPivotResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const ROW_HEIGHT = 24;

const displayLabel = (key: string) => (key === OTHER_KEY ? OTHER_LABEL : key);
const truncate = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

interface PivotHeatmapProps {
  data: FieldPivotResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  onValueClick?: ChartValueClickHandler;
}

/**
 * Field × field co-occurrence heatmap — one row per top-N Y value, one
 * column per top-N X value, cell shade = joint event count. The place to
 * see which accounts touch which hosts, which IPs hit which ports. Server
 * cells with `""` on an axis are that axis's "outside top-N" rollup and
 * render as an explicit Other row/column rather than being dropped.
 * Clicking a cell reports both field=value pairs (a conjunction) to
 * `onValueClick`; Other cells are not clickable (no single value to filter).
 */
export function PivotHeatmap({ data, svgRef, height, onValueClick }: PivotHeatmapProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    xKey: string;
    yKey: string;
    count: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  if (data.cells.length === 0) {
    return (
      <ChartEmptyState hint="Both fields need a non-empty value on the same events for a pair to count.">
        No events with both fields set match the current filters.
      </ChartEmptyState>
    );
  }

  const hasOtherX = data.cells.some((c) => c.x === "");
  const hasOtherY = data.cells.some((c) => c.y === "");
  const xKeys = [...data.x_values, ...(hasOtherX ? [OTHER_KEY] : [])];
  const yKeys = [...data.y_values, ...(hasOtherY ? [OTHER_KEY] : [])];

  // NUL-joined key: a NUL can't appear in a displayable ClickHouse String
  // value, so "a b" x "c" can never collide with "a" x "b c".
  const cellKey = (x: string, y: string) => `${x}\u0000${y}`;
  const counts = new Map<string, number>();
  let maxCount = 1;
  for (const c of data.cells) {
    counts.set(cellKey(c.x === "" ? OTHER_KEY : c.x, c.y === "" ? OTHER_KEY : c.y), c.count);
    if (c.count > maxCount) maxCount = c.count;
  }

  const resolvedHeight = height ?? Math.max(160, yKeys.length * ROW_HEIGHT + 76);
  const labelCol = 130;

  return (
    <div className="relative">
      <ChartFrame
        height={resolvedHeight}
        svgRef={ref}
        margin={{ top: 8, right: 8, bottom: 68, left: labelCol }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          const xBand = scaleBand().domain(xKeys).range([0, innerWidth]).padding(0.06);
          const yBand = scaleBand().domain(yKeys).range([0, innerHeight]).padding(0.08);

          return (
            <>
              <AxisBottomBand
                scale={xBand}
                innerHeight={innerHeight}
                rotate
                labelFormat={displayLabel}
              />
              {yKeys.map((yKey) => {
                const ry = yBand(yKey) ?? 0;
                return (
                  <g key={yKey}>
                    <text
                      x={-8}
                      y={ry + yBand.bandwidth() / 2}
                      dy="0.32em"
                      textAnchor="end"
                      fontSize={11}
                      fill={yKey === OTHER_KEY ? "var(--viz-ink-muted)" : "var(--viz-ink-primary)"}
                    >
                      {truncate(displayLabel(yKey), 20)}
                    </text>
                    {xKeys.map((xKey) => {
                      const count = counts.get(cellKey(xKey, yKey)) ?? 0;
                      const rx = xBand(xKey) ?? 0;
                      const clickable =
                        onValueClick != null &&
                        count > 0 &&
                        xKey !== OTHER_KEY &&
                        yKey !== OTHER_KEY;
                      return (
                        <rect
                          key={xKey}
                          x={rx}
                          y={ry}
                          width={xBand.bandwidth()}
                          height={yBand.bandwidth()}
                          fill={count === 0 ? "var(--viz-grid)" : sequentialColor(count / maxCount)}
                          style={clickable ? { cursor: "pointer" } : undefined}
                          onMouseEnter={() =>
                            setHover({
                              x: rx + xBand.bandwidth() / 2 + margin.left,
                              y: ry + margin.top,
                              xKey,
                              yKey,
                              count,
                            })
                          }
                          onMouseLeave={() => setHover(null)}
                          onClick={
                            clickable
                              ? (e) =>
                                  onValueClick({
                                    entries: [
                                      [data.field_x, xKey],
                                      [data.field_y, yKey],
                                    ],
                                    clientX: e.clientX,
                                    clientY: e.clientY,
                                  })
                              : undefined
                          }
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
            {data.field_x} = {displayLabel(hover.xKey)}
            <br />
            {data.field_y} = {displayLabel(hover.yKey)}
            <br />
            <strong>{fmtCount(hover.count)}</strong> events
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
