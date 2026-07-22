import { useState } from "react";
import { scaleBand, scaleLinear } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottomBand, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { BoxMark, PointStrip, ViolinMark } from "@/components/viz/charts/distributionMarks";
import { kdeFromBins, numericDomain } from "@/components/viz/lib/stats";
import { buildSeriesColorMap } from "@/components/viz/lib/colors";
import type { ChartValueClick } from "@/components/viz/lib/interaction";
import type { FieldNumericGroupedResponse } from "@/api/types";

const fmtValue = formatNum(",.3~f");
const fmtInt = formatNum(",d");

interface GroupedDistributionProps {
  data: FieldNumericGroupedResponse;
  /** Which mark to draw per group — the same aggregation feeds both. */
  mark: "box" | "violin";
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  showPoints?: boolean;
  onValueClick?: (click: ChartValueClick) => void;
}

/**
 * One numeric distribution per category — the lecture's "Antwortvariable
 * (intervallskaliert) × Gruppierungsvariable (kategorial)" plot.
 *
 * Every group shares one y-scale spanning the response's global [min, max],
 * and violins share one density normalizer, so widths and heights are
 * comparable across groups rather than each group being scaled to its own
 * peak. Groups outside the server's top-N are absent, not merged into an
 * "Other" box — a box over unrelated categories would be meaningless; the
 * caption reports what was left out.
 */
export function GroupedDistribution({
  data,
  mark,
  svgRef,
  height = 300,
  showPoints = false,
  onValueClick,
}: GroupedDistributionProps) {
  const [hover, setHover] = useState<{ x: number; y: number; label: string } | null>(null);
  const ref = useChartRef(svgRef);

  if (data.total === 0 || data.groups.length === 0 || data.min == null || data.max == null) {
    return (
      <ChartEmptyState hint="The response field must be numeric and the grouping field categorical.">
        No numeric values for this field/group combination in range.
      </ChartEmptyState>
    );
  }

  const colors = buildSeriesColorMap(data.groups.map((g) => ({ key: g.value })));
  // One normalizer across groups: a wide violin then means "more events at
  // this value", not "this group's own busiest value".
  const globalMaxDensity = Math.max(
    1e-9,
    ...data.groups.flatMap((g) => kdeFromBins(g.bins).map((d) => d.density)),
  );
  const pointsByGroup = new Map<string, number[]>();
  for (const [group, value] of data.points?.values ?? []) {
    const list = pointsByGroup.get(group);
    if (list) list.push(value);
    else pointsByGroup.set(group, [value]);
  }

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref} margin={{ top: 16, right: 24, bottom: 64, left: 64 }}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleBand()
            .domain(data.groups.map((g) => g.value))
            .range([0, innerWidth])
            .padding(0.25);
          const y = scaleLinear()
            .domain(numericDomain(data.min!, data.max!))
            .nice()
            .range([innerHeight, 0]);
          const band = x.bandwidth();

          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmtValue(v as number)} />
              <AxisBottomBand scale={x} innerHeight={innerHeight} rotate />
              {data.groups.map((group, gi) => {
                const cx = (x(group.value) ?? 0) + band / 2;
                const color = colors.get(group.value) ?? "var(--color-accent)";
                const label =
                  `${group.value}: n = ${fmtInt(group.count)}` +
                  (group.quantiles["0.5"] != null
                    ? `, median ${fmtValue(group.quantiles["0.5"])}`
                    : "");
                return (
                  <g
                    key={group.value}
                    onClick={(e) =>
                      onValueClick?.({
                        entries: [[data.group_field, group.value]],
                        clientX: e.clientX,
                        clientY: e.clientY,
                      })
                    }
                    onMouseEnter={() =>
                      setHover({ x: cx + margin.left, y: margin.top + 12, label })
                    }
                    onMouseLeave={() => setHover(null)}
                    style={onValueClick ? { cursor: "pointer" } : undefined}
                  >
                    {mark === "box" ? (
                      <BoxMark
                        dist={group}
                        cx={cx}
                        width={Math.min(band * 0.7, 90)}
                        y={y}
                        color={color}
                        fmt={fmtValue}
                      />
                    ) : (
                      <ViolinMark
                        dist={group}
                        cx={cx}
                        halfWidth={Math.min(band * 0.45, 90)}
                        y={y}
                        color={color}
                        maxDensity={globalMaxDensity}
                      />
                    )}
                    {showPoints && (
                      <PointStrip
                        values={pointsByGroup.get(group.value) ?? []}
                        cx={cx}
                        spread={Math.min(band * 0.3, 40)}
                        y={y}
                        seed={gi * 977}
                      />
                    )}
                  </g>
                );
              })}
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover?.label}
      </ChartTooltip>
    </div>
  );
}
