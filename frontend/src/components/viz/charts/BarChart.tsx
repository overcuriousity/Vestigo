import { useRef, useState } from "react";
import { scaleBand, scaleLinear } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottom } from "@/components/viz/primitives/Axis";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { buildSeriesColorMap, OTHER_LABEL } from "@/components/viz/lib/colors";
import type { FieldTermsResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const LABEL_COL = 140;

interface BarChartProps {
  terms: FieldTermsResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Row height in px — total chart height grows with the value count. */
  rowHeight?: number;
}

/**
 * Horizontal bar chart for a categorical (nominal/ordinal) field's top
 * values — one bar per value, with a truthful "Other" bar for the remainder
 * outside the top-N so the chart never silently hides the tail.
 *
 * Value labels sit in a fixed left column and counts sit past the bar end —
 * both always rendered on the neutral chart surface (never on the colored
 * fill), which is what satisfies the categorical palette's relief
 * requirement for its sub-3:1 slots without per-bar contrast logic.
 */
export function BarChart({ terms, svgRef, height, rowHeight = 26 }: BarChartProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    label: string;
    count: number;
  } | null>(null);
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  const ref = svgRef ?? fallbackRef;

  const rows = terms.values.map((v) => ({ label: v.value, count: v.count }));
  if (terms.other_count > 0) rows.push({ label: OTHER_LABEL, count: terms.other_count });

  if (rows.length === 0) {
    return (
      <div className="flex h-[160px] items-center justify-center text-sm text-[var(--color-fg-muted)]">
        No values in the current filter range.
      </div>
    );
  }

  const colorMap = buildSeriesColorMap(rows.map((r) => r.label));
  const resolvedHeight = height ?? Math.max(120, rows.length * rowHeight + 40);

  return (
    <div className="relative">
      <ChartFrame
        height={resolvedHeight}
        svgRef={ref}
        margin={{ top: 8, right: 52, bottom: 28, left: LABEL_COL }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          const y = scaleBand()
            .domain(rows.map((r) => r.label))
            .range([0, innerHeight])
            .padding(0.25);
          const maxCount = Math.max(1, ...rows.map((r) => r.count));
          const x = scaleLinear().domain([0, maxCount]).nice().range([0, innerWidth]);

          return (
            <>
              <AxisBottom
                scale={x}
                innerHeight={innerHeight}
                ticks={4}
                tickFormat={(v) => fmtCount(v as number)}
              />
              {rows.map((r) => {
                const by = y(r.label) ?? 0;
                const bw = x(r.count);
                const bh = y.bandwidth();
                const color = colorMap.get(r.label) ?? "var(--color-accent)";
                const label =
                  r.label.length > 22 ? r.label.slice(0, 21) + "…" : r.label;
                return (
                  <g
                    key={r.label}
                    onMouseEnter={() =>
                      setHover({
                        x: bw + margin.left + 6,
                        y: by + bh / 2 + margin.top,
                        label: r.label,
                        count: r.count,
                      })
                    }
                    onMouseLeave={() => setHover(null)}
                  >
                    <text
                      x={-8}
                      y={by + bh / 2}
                      dy="0.32em"
                      textAnchor="end"
                      fontSize={11}
                      fill="var(--viz-ink-primary)"
                    >
                      {label}
                    </text>
                    <rect x={0} y={by} width={Math.max(1, bw)} height={bh} fill={color} />
                    <text
                      x={bw + 6}
                      y={by + bh / 2}
                      dy="0.32em"
                      fontSize={10}
                      fill="var(--viz-ink-muted)"
                    >
                      {fmtCount(r.count)}
                    </text>
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
            {hover.label}: <strong>{fmtCount(hover.count)}</strong>
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
