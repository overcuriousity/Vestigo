import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { NumericPlotFrame } from "@/components/viz/primitives/NumericPlotFrame";
import { BoxMark, PointStrip } from "@/components/viz/charts/distributionMarks";
import { boxPlotStats } from "@/components/viz/lib/stats";
import type { FieldNumericResponse } from "@/api/types";

const fmtValue = formatNum(",.3~f");
const BOX_WIDTH = 90;

interface BoxPlotProps {
  stats: FieldNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
  /** Overlay the response's sampled raw values as a jittered strip — only
   * drawn when the request asked for `points` (see `vizApi.fieldNumeric`). */
  showPoints?: boolean;
  /** Pin the value axis to a shared [min, max]. Facet panels pass the range
   * across all panels, so a box drawn higher really does mean larger values
   * rather than a differently-scaled axis. */
  domain?: [number, number];
}

/** Vertical box plot (five-number summary) for a numeric field — median,
 * quartile box, and 1.5*IQR whiskers. Built from the server's quantiles, so
 * only an optional random sample of raw values ever reaches the client. */
export function BoxPlot({
  stats,
  svgRef,
  height = 260,
  color = "var(--color-accent)",
  showPoints = false,
  domain,
}: BoxPlotProps) {
  const box = boxPlotStats(stats);
  if (!box) {
    return (
      <ChartEmptyState hint="This field may not be numeric — try a Top-values (bar) chart instead.">
        No numeric values for this field in range.
      </ChartEmptyState>
    );
  }

  return (
    <NumericPlotFrame
      svgRef={svgRef}
      height={height}
      min={domain?.[0] ?? box.min}
      max={domain?.[1] ?? box.max}
      yTickFormat={(v) => fmtValue(v)}
    >
      {({ margin, y, cx, setHover }) => (
        <>
          <BoxMark
            dist={stats}
            cx={cx}
            width={BOX_WIDTH}
            y={y}
            color={color}
            fmt={fmtValue}
            hover={{
              onHover: (label, atY) =>
                setHover({ x: cx + margin.left, y: atY + margin.top, label }),
              onLeave: () => setHover(null),
            }}
          />
          {showPoints && stats.points && (
            <PointStrip values={stats.points.values} cx={cx} spread={BOX_WIDTH / 2} y={y} />
          )}
        </>
      )}
    </NumericPlotFrame>
  );
}
