import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { NumericPlotFrame } from "@/components/viz/primitives/NumericPlotFrame";
import { boxPlotStats } from "@/components/viz/lib/stats";
import type { FieldNumericResponse } from "@/api/types";

const fmtValue = formatNum(",.3~f");
const BOX_WIDTH = 90;

interface BoxPlotProps {
  stats: FieldNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
}

/** Vertical box plot (five-number summary) for a numeric field — median,
 * quartile box, and 1.5*IQR whiskers. Built from the server's quantiles, so
 * no raw values are shipped to the client. */
export function BoxPlot({ stats, svgRef, height = 260, color = "var(--color-accent)" }: BoxPlotProps) {
  const box = boxPlotStats(stats);
  if (!box) {
    return <ChartEmptyState>No numeric values in the current filter range.</ChartEmptyState>;
  }

  return (
    <NumericPlotFrame
      svgRef={svgRef}
      height={height}
      min={box.min}
      max={box.max}
      yTickFormat={(v) => fmtValue(v)}
    >
      {({ cx, margin, y, setHover }) => {
        const boxTop = y(box.q3);
        const boxBottom = y(box.q1);

        const show = (label: string, value: number, py: number) => () =>
          setHover({ x: cx + margin.left, y: py + margin.top, label: `${label}: ${fmtValue(value)}` });

        return (
          <>
            {/* Whiskers */}
            <line
              x1={cx}
              x2={cx}
              y1={y(box.whiskerHigh)}
              y2={boxTop}
              stroke="var(--viz-axis)"
              strokeWidth={1.5}
            />
            <line
              x1={cx}
              x2={cx}
              y1={boxBottom}
              y2={y(box.whiskerLow)}
              stroke="var(--viz-axis)"
              strokeWidth={1.5}
            />
            <line
              x1={cx - BOX_WIDTH / 4}
              x2={cx + BOX_WIDTH / 4}
              y1={y(box.whiskerHigh)}
              y2={y(box.whiskerHigh)}
              stroke="var(--viz-axis)"
              strokeWidth={1.5}
              onMouseEnter={show("Upper whisker", box.whiskerHigh, y(box.whiskerHigh))}
              onMouseLeave={() => setHover(null)}
            />
            <line
              x1={cx - BOX_WIDTH / 4}
              x2={cx + BOX_WIDTH / 4}
              y1={y(box.whiskerLow)}
              y2={y(box.whiskerLow)}
              stroke="var(--viz-axis)"
              strokeWidth={1.5}
              onMouseEnter={show("Lower whisker", box.whiskerLow, y(box.whiskerLow))}
              onMouseLeave={() => setHover(null)}
            />
            {/* Box (Q1-Q3) */}
            <rect
              x={cx - BOX_WIDTH / 2}
              y={boxTop}
              width={BOX_WIDTH}
              height={Math.max(1, boxBottom - boxTop)}
              fill={color}
              fillOpacity={0.25}
              stroke={color}
              strokeWidth={1.5}
              onMouseEnter={show("Q1–Q3", box.q1, (boxTop + boxBottom) / 2)}
              onMouseLeave={() => setHover(null)}
            />
            {/* Median */}
            <line
              x1={cx - BOX_WIDTH / 2}
              x2={cx + BOX_WIDTH / 2}
              y1={y(box.median)}
              y2={y(box.median)}
              stroke={color}
              strokeWidth={2.5}
              onMouseEnter={show("Median", box.median, y(box.median))}
              onMouseLeave={() => setHover(null)}
            />
          </>
        );
      }}
    </NumericPlotFrame>
  );
}
