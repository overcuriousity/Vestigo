import { useRef, useState } from "react";
import { arc, pie } from "d3-shape";
import { format as formatNum } from "d3-format";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { buildSeriesColorMap, OTHER_LABEL } from "@/components/viz/lib/colors";
import type { FieldTermsResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtPct = formatNum(".0%");

interface PieChartProps {
  terms: FieldTermsResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
}

/** Donut chart for a categorical field's top values — identity is never
 * color-alone here: every non-trivial slice gets a direct label, and the
 * legend lists every value regardless of slice size. */
export function PieChart({ terms, svgRef, height = 260 }: PieChartProps) {
  const [hover, setHover] = useState<{ x: number; y: number; label: string; count: number } | null>(
    null,
  );
  const fallbackRef = useRef<SVGSVGElement | null>(null);
  const ref = svgRef ?? fallbackRef;

  const rows = terms.values.map((v) => ({ label: v.value, count: v.count }));
  if (terms.other_count > 0) rows.push({ label: OTHER_LABEL, count: terms.other_count });
  const total = rows.reduce((s, r) => s + r.count, 0);

  if (rows.length === 0 || total === 0) {
    return (
      <div className="flex h-[160px] items-center justify-center text-sm text-[var(--color-fg-muted)]">
        No values in the current filter range.
      </div>
    );
  }

  const colorMap = buildSeriesColorMap(rows.map((r) => r.label));
  const pieLayout = pie<{ label: string; count: number }>()
    .value((d) => d.count)
    .sort(null);
  const arcs = pieLayout(rows);

  return (
    <div className="relative flex flex-col gap-2">
      <ChartFrame height={height} svgRef={ref} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
        {({ innerWidth, innerHeight, margin }) => {
          const radius = Math.min(innerWidth, innerHeight) / 2;
          const cx = innerWidth / 2;
          const cy = innerHeight / 2;
          const arcGen = arc<ReturnType<typeof pieLayout>[number]>()
            .innerRadius(radius * 0.55)
            .outerRadius(radius);
          const labelArc = arc<ReturnType<typeof pieLayout>[number]>()
            .innerRadius(radius * 0.8)
            .outerRadius(radius * 0.8);

          return (
            <g transform={`translate(${cx},${cy})`}>
              {arcs.map((a) => {
                const path = arcGen(a) ?? undefined;
                const frac = a.data.count / total;
                const [lx, ly] = labelArc.centroid(a);
                return (
                  <g
                    key={a.data.label}
                    onMouseEnter={() =>
                      setHover({
                        x: cx + margin.left + lx,
                        y: cy + margin.top + ly,
                        label: a.data.label,
                        count: a.data.count,
                      })
                    }
                    onMouseLeave={() => setHover(null)}
                  >
                    <path
                      d={path}
                      fill={colorMap.get(a.data.label) ?? "var(--color-accent)"}
                      stroke="var(--color-bg-elevated)"
                      strokeWidth={2}
                    />
                    {frac >= 0.06 && (
                      <text
                        x={lx}
                        y={ly}
                        textAnchor="middle"
                        dy="0.32em"
                        fontSize={10}
                        fill="var(--viz-ink-primary)"
                      >
                        {fmtPct(frac)}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          );
        }}
      </ChartFrame>
      <Legend
        entries={rows.map((r) => ({
          label: `${r.label} (${fmtCount(r.count)})`,
          color: colorMap.get(r.label) ?? "var(--color-accent)",
        }))}
      />
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            {hover.label}: <strong>{fmtCount(hover.count)}</strong> ({fmtPct(hover.count / total)})
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
