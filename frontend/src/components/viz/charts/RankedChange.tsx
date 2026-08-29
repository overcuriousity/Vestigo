import { useState } from "react";
import { scaleBand, scaleLinear } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottom } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { valueLabeller } from "@/components/viz/lib/fieldDisplay";
import type { ChangeResponse, ChangeRow } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtPct = formatNum(".1%");
const fmtPp = formatNum("+.1f");
const LABEL_COL = 140;
const STATUS_COL = 84;
const ROW_H = 22;
const DOT_R = 4.5;
//: The reference window is grey and the current window the accent — the same
//: pair BarChart's grouped mode uses, so a reader moving between figures
//: never has to relearn which layer is which.
const COMPARISON_COLOR = "var(--color-fg-disabled)";
const PRIMARY_COLOR = "var(--color-accent)";

interface RankedChangeProps {
  data: ChangeResponse;
  /** One row per value (dumbbell, default) or two columns joined by slopes. */
  layout?: "dumbbell" | "slope";
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
}

/** The row-end status in words or percentage points; "−" is a real minus sign. */
export function changeStatusLabel(row: ChangeRow): string {
  if (row.status === "new" || row.status === "vanished" || row.status === "same") return row.status;
  return `${fmtPp(row.delta_share * 100).replace("-", "−")} pp`;
}

/**
 * Ranked change between two windows. The encoded quantity is each value's
 * **share of its own window** — the two windows are rarely the same size,
 * so a count comparison would mostly measure the window sizes. Rows arrive
 * ranked by |Δ share| from the server; this component draws what it got.
 */
export function RankedChange({ data, layout = "dumbbell", svgRef, height }: RankedChangeProps) {
  const [hover, setHover] = useState<{ x: number; y: number; row: ChangeRow } | null>(null);
  const ref = useChartRef(svgRef);
  const labelOf = valueLabeller(data.field);

  if (data.rows.length === 0) {
    return (
      <ChartEmptyState hint="Both windows are empty for this field under the current filters and the comparison layer.">
        No values in either window.
      </ChartEmptyState>
    );
  }

  const rows = data.rows;
  const maxShare = Math.max(1e-9, ...rows.map((r) => Math.max(r.primary_share, r.comparison_share)));
  const legend = (
    <Legend
      entries={[
        { label: "Reference window (comparison)", color: COMPARISON_COLOR, muted: true },
        { label: "This window (primary)", color: PRIMARY_COLOR },
      ]}
    />
  );
  const tooltip = (
    <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
      {hover && (
        <>
          <div>{labelOf(hover.row.value)}</div>
          <div>
            this window: <strong>{fmtPct(hover.row.primary_share)}</strong> ({fmtCount(hover.row.primary)} of{" "}
            {fmtCount(data.primary_total)})
          </div>
          <div>
            reference: <strong>{fmtPct(hover.row.comparison_share)}</strong> (
            {fmtCount(hover.row.comparison)} of {fmtCount(data.comparison_total)})
          </div>
          <div>{changeStatusLabel(hover.row)}</div>
        </>
      )}
    </ChartTooltip>
  );

  if (layout === "slope") {
    const frameHeight = height ?? Math.max(260, rows.length * 18 + 60);
    return (
      <div className="relative">
        <ChartFrame
          height={frameHeight}
          svgRef={ref}
          margin={{ top: 24, right: LABEL_COL, bottom: 12, left: LABEL_COL }}
        >
          {({ innerWidth, innerHeight, margin }) => {
            const y = scaleLinear().domain([0, maxShare]).nice().range([innerHeight, 0]);
            return (
              <>
                <text x={0} y={-10} textAnchor="middle" fontSize={10} fill="var(--viz-ink-muted)">
                  reference window
                </text>
                <text x={innerWidth} y={-10} textAnchor="middle" fontSize={10} fill="var(--viz-ink-muted)">
                  this window
                </text>
                <line x1={0} x2={0} y1={0} y2={innerHeight} stroke="var(--viz-axis)" />
                <line x1={innerWidth} x2={innerWidth} y1={0} y2={innerHeight} stroke="var(--viz-axis)" />
                {rows.map((r) => {
                  const up = r.status === "rose" || r.status === "new";
                  const color = up ? PRIMARY_COLOR : "var(--viz-ink-muted)";
                  return (
                    <g
                      key={r.value}
                      data-change-row={r.value}
                      onMouseEnter={() =>
                        setHover({ x: innerWidth / 2 + margin.left, y: y(r.primary_share) + margin.top, row: r })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      <line
                        data-change-slope
                        data-value={r.value}
                        x1={0}
                        y1={y(r.comparison_share)}
                        x2={innerWidth}
                        y2={y(r.primary_share)}
                        stroke={color}
                        strokeWidth={1.5}
                      />
                      <text
                        data-change-label={r.value}
                        x={-8}
                        y={y(r.comparison_share)}
                        dy="0.32em"
                        textAnchor="end"
                        fontSize={10}
                        fill="var(--viz-ink-primary)"
                      >
                        {labelOf(r.value)} {fmtPct(r.comparison_share)}
                      </text>
                      <text
                        data-change-label={r.value}
                        x={innerWidth + 8}
                        y={y(r.primary_share)}
                        dy="0.32em"
                        fontSize={10}
                        fill="var(--viz-ink-primary)"
                      >
                        {fmtPct(r.primary_share)} {labelOf(r.value)} · {changeStatusLabel(r)}
                      </text>
                    </g>
                  );
                })}
              </>
            );
          }}
        </ChartFrame>
        {legend}
        {tooltip}
      </div>
    );
  }

  const frameHeight = height ?? rows.length * ROW_H + 44;
  return (
    <div className="relative">
      <ChartFrame
        height={frameHeight}
        svgRef={ref}
        margin={{ top: 8, right: STATUS_COL, bottom: 32, left: LABEL_COL }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleLinear().domain([0, maxShare]).nice().range([0, innerWidth]);
          const y = scaleBand<string>()
            .domain(rows.map((r) => r.value))
            .range([0, innerHeight])
            .paddingInner(0.3);
          return (
            <>
              <AxisBottom
                scale={x}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtPct(v as number)}
              />
              {rows.map((r) => {
                const cy = (y(r.value) ?? 0) + y.bandwidth() / 2;
                const cx1 = x(r.comparison_share);
                const cx2 = x(r.primary_share);
                return (
                  <g
                    key={r.value}
                    data-change-row={r.value}
                    onMouseEnter={() =>
                      setHover({ x: Math.max(cx1, cx2) + margin.left, y: cy + margin.top, row: r })
                    }
                    onMouseLeave={() => setHover(null)}
                  >
                    <text
                      x={-8}
                      y={cy}
                      dy="0.32em"
                      textAnchor="end"
                      fontSize={11}
                      fill="var(--viz-ink-primary)"
                    >
                      {labelOf(r.value)}
                    </text>
                    <line
                      data-change-link
                      x1={cx1}
                      x2={cx2}
                      y1={cy}
                      y2={cy}
                      stroke="var(--viz-axis)"
                      strokeWidth={2}
                    />
                    <circle data-change-dot="comparison" cx={cx1} cy={cy} r={DOT_R} fill={COMPARISON_COLOR} />
                    <circle data-change-dot="primary" cx={cx2} cy={cy} r={DOT_R} fill={PRIMARY_COLOR} />
                    <text
                      data-change-status
                      x={innerWidth + 8}
                      y={cy}
                      dy="0.32em"
                      fontSize={10}
                      fill="var(--viz-ink-muted)"
                    >
                      {changeStatusLabel(r)}
                    </text>
                  </g>
                );
              })}
            </>
          );
        }}
      </ChartFrame>
      {legend}
      {tooltip}
    </div>
  );
}
