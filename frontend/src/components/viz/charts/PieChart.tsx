import { useState } from "react";
import { arc, pie } from "d3-shape";
import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { buildSeriesColorMap, OTHER_KEY, OTHER_LABEL } from "@/components/viz/lib/colors";
import type { ChartValueClickHandler } from "@/components/viz/lib/interaction";
import type { FieldTermsResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtPct = formatNum(".0%");

interface PieChartProps {
  terms: FieldTermsResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Click-to-filter: clicking a slice or legend entry reports its
   * field=value pair (the Other slice is never clickable). */
  onValueClick?: ChartValueClickHandler;
}

/** Donut chart for a categorical field's top values — identity is never
 * color-alone here: every non-trivial slice gets a direct label, and the
 * legend lists every value regardless of slice size. */
export function PieChart({ terms, svgRef, height = 260, onValueClick }: PieChartProps) {
  const [hover, setHover] = useState<{ x: number; y: number; label: string; count: number } | null>(
    null,
  );
  const ref = useChartRef(svgRef);

  const rows = terms.values.map((v) => ({ key: v.value, label: v.value, count: v.count }));
  if (terms.other_count > 0) {
    rows.push({ key: OTHER_KEY, label: OTHER_LABEL, count: terms.other_count });
  }
  const total = rows.reduce((s, r) => s + r.count, 0);

  if (rows.length === 0 || total === 0) {
    return (
      <ChartEmptyState size="sm" hint="Pick a different field, or clear the active filters.">
        No values for this field in range.
      </ChartEmptyState>
    );
  }

  const colorMap = buildSeriesColorMap(
    rows.map((r) => ({ key: r.key, isOther: r.key === OTHER_KEY })),
  );
  const pieLayout = pie<{ key: string; label: string; count: number }>()
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
                const clickable = onValueClick != null && a.data.key !== OTHER_KEY;
                return (
                  <g
                    key={a.data.key}
                    style={clickable ? { cursor: "pointer" } : undefined}
                    onClick={
                      clickable
                        ? (e) =>
                            onValueClick({
                              entries: [[terms.field, a.data.key]],
                              clientX: e.clientX,
                              clientY: e.clientY,
                            })
                        : undefined
                    }
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
                      fill={colorMap.get(a.data.key) ?? "var(--color-accent)"}
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
          color: colorMap.get(r.key) ?? "var(--color-accent)",
          key: r.key,
        }))}
        onEntryClick={
          onValueClick
            ? (key, e) => {
                if (key === OTHER_KEY) return;
                onValueClick({
                  entries: [[terms.field, key]],
                  clientX: e.clientX,
                  clientY: e.clientY,
                });
              }
            : undefined
        }
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
