import { useState } from "react";
import { scaleBand, scaleLinear, scaleLog } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisBottomBand, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { buildSeriesColorMap, OTHER_KEY, OTHER_LABEL } from "@/components/viz/lib/colors";
import type { CompareTermsResponse, FieldTermsResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const LABEL_COL = 140;

interface BarRow {
  key: string;
  label: string;
  count: number;
  /** Comparison layer's count for the same category (grouped mode only). */
  comparison?: number;
}

interface BarChartProps {
  terms?: FieldTermsResponse;
  /** Two-layer terms result — when set, renders grouped bars (primary filled,
   * comparison muted) over the shared category list and `terms` is ignored. */
  compare?: CompareTermsResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Row height in px (horizontal) — total chart height grows with the value count. */
  rowHeight?: number;
  orientation?: "horizontal" | "vertical";
  /** "count" keeps the server's count-descending order; "value" sorts
   * lexicographically by value (Other always stays last). */
  sort?: "count" | "value";
  /** Log-scaled value axis — zero counts render as zero-length bars. */
  logScale?: boolean;
}

/**
 * Bar chart for a categorical (nominal/ordinal) field's top values — one bar
 * per value, with a truthful "Other" bar for the remainder outside the top-N
 * so the chart never silently hides the tail. Horizontal (default) or
 * vertical; in compare mode both layers render as grouped bars over the
 * shared category list the backend fixed from the primary layer's top-N.
 *
 * Horizontal value labels sit in a fixed left column and counts sit past the
 * bar end — both always rendered on the neutral chart surface (never on the
 * colored fill), which is what satisfies the categorical palette's relief
 * requirement for its sub-3:1 slots without per-bar contrast logic.
 */
export function BarChart({
  terms,
  compare,
  svgRef,
  height,
  rowHeight = 26,
  orientation = "horizontal",
  sort = "count",
  logScale = false,
}: BarChartProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    label: string;
    count: number;
    comparison?: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  const rows: BarRow[] = compare
    ? compare.values.map((v) => ({
        key: v.value,
        label: v.value,
        count: v.primary,
        comparison: v.comparison,
      }))
    : (terms?.values ?? []).map((v) => ({ key: v.value, label: v.value, count: v.count }));
  const otherPrimary = compare ? compare.primary_other : (terms?.other_count ?? 0);
  const otherComparison = compare?.comparison_other ?? 0;
  if (otherPrimary > 0 || (compare && otherComparison > 0)) {
    rows.push({
      key: OTHER_KEY,
      label: OTHER_LABEL,
      count: otherPrimary,
      comparison: compare ? otherComparison : undefined,
    });
  }
  if (sort === "value") {
    rows.sort((a, b) =>
      a.key === OTHER_KEY ? 1 : b.key === OTHER_KEY ? -1 : a.label.localeCompare(b.label),
    );
  }

  if (rows.length === 0) {
    return <ChartEmptyState size="sm">No values in the current filter range.</ChartEmptyState>;
  }

  const grouped = compare != null;
  const colorMap = buildSeriesColorMap(
    rows.map((r) => ({ key: r.key, isOther: r.key === OTHER_KEY })),
  );
  const maxCount = Math.max(
    1,
    ...rows.map((r) => r.count),
    ...rows.map((r) => r.comparison ?? 0),
  );
  // A log scale has no 0 — clamp the domain to [1, max] and draw zero counts
  // as zero-length bars instead of extrapolating below the axis.
  const valueScale = (rangeMax: number, invert = false) => {
    const range = invert ? [rangeMax, 0] : [0, rangeMax];
    return logScale
      ? scaleLog().domain([1, maxCount]).range(range).clamp(true)
      : scaleLinear().domain([0, maxCount]).nice().range(range);
  };
  const barLength = (scaleFn: (v: number) => number, count: number) =>
    logScale && count < 1 ? 0 : scaleFn(Math.max(count, logScale ? 1 : 0));

  const legend = grouped ? (
    <Legend
      entries={[
        { label: "Filtered events", color: "var(--color-accent)" },
        { label: "Comparison layer", color: "var(--color-fg-disabled)", muted: true },
      ]}
    />
  ) : null;

  const tooltip = (
    <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
      {hover && (
        <>
          {hover.label}: <strong>{fmtCount(hover.count)}</strong>
          {hover.comparison != null && <> · comparison: {fmtCount(hover.comparison)}</>}
        </>
      )}
    </ChartTooltip>
  );

  if (orientation === "vertical") {
    const resolvedHeight = height ?? 300;
    return (
      <div className="flex flex-col gap-2">
        {legend}
        <div className="relative">
          <ChartFrame
            height={resolvedHeight}
            svgRef={ref}
            margin={{ top: 8, right: 16, bottom: 64, left: 48 }}
          >
            {({ innerWidth, innerHeight, margin }) => {
              const x = scaleBand()
                .domain(rows.map((r) => r.key))
                .range([0, innerWidth])
                .padding(0.25);
              const y = valueScale(innerHeight, true);
              const bandwidth = x.bandwidth();
              const subWidth = grouped ? bandwidth / 2 : bandwidth;

              return (
                <>
                  <AxisLeft
                    scale={y as never}
                    innerWidth={innerWidth}
                    tickFormat={(v) => fmtCount(v)}
                  />
                  <AxisBottomBand
                    scale={x}
                    innerHeight={innerHeight}
                    rotate
                    labelFormat={(k) => (k === OTHER_KEY ? OTHER_LABEL : k)}
                  />
                  {rows.map((r) => {
                    const bx = x(r.key) ?? 0;
                    const color = colorMap.get(r.key) ?? "var(--color-accent)";
                    const py = barLength((v) => y(v), r.count);
                    const cy =
                      r.comparison != null ? barLength((v) => y(v), r.comparison) : null;
                    return (
                      <g
                        key={r.key}
                        onMouseEnter={() =>
                          setHover({
                            x: bx + bandwidth / 2 + margin.left,
                            y: Math.min(py, cy ?? py) + margin.top,
                            label: r.label,
                            count: r.count,
                            comparison: r.comparison,
                          })
                        }
                        onMouseLeave={() => setHover(null)}
                      >
                        <rect
                          x={bx}
                          y={py}
                          width={Math.max(1, subWidth)}
                          height={innerHeight - py}
                          fill={grouped ? "var(--color-accent)" : color}
                        />
                        {cy != null && (
                          <rect
                            x={bx + subWidth}
                            y={cy}
                            width={Math.max(1, subWidth)}
                            height={innerHeight - cy}
                            fill="var(--color-fg-disabled)"
                            opacity={0.6}
                          />
                        )}
                      </g>
                    );
                  })}
                </>
              );
            }}
          </ChartFrame>
          {tooltip}
        </div>
      </div>
    );
  }

  const bandHeight = grouped ? rowHeight * 1.6 : rowHeight;
  const resolvedHeight = height ?? Math.max(120, rows.length * bandHeight + 40);

  return (
    <div className="flex flex-col gap-2">
      {legend}
      <div className="relative">
        <ChartFrame
          height={resolvedHeight}
          svgRef={ref}
          margin={{ top: 8, right: 52, bottom: 28, left: LABEL_COL }}
        >
          {({ innerWidth, innerHeight, margin }) => {
            const y = scaleBand()
              .domain(rows.map((r) => r.key))
              .range([0, innerHeight])
              .padding(0.25);
            const x = valueScale(innerWidth);
            const bandwidth = y.bandwidth();
            const subHeight = grouped ? bandwidth / 2 : bandwidth;

            return (
              <>
                <AxisBottom
                  scale={x as never}
                  innerWidth={innerWidth}
                  innerHeight={innerHeight}
                  ticks={4}
                  rotate={false}
                  tickFormat={(v) => fmtCount(v as number)}
                />
                {rows.map((r) => {
                  const by = y(r.key) ?? 0;
                  const bw = barLength((v) => x(v), r.count);
                  const cw =
                    r.comparison != null ? barLength((v) => x(v), r.comparison) : null;
                  const color = colorMap.get(r.key) ?? "var(--color-accent)";
                  const label = r.label.length > 22 ? r.label.slice(0, 21) + "…" : r.label;
                  return (
                    <g
                      key={r.key}
                      onMouseEnter={() =>
                        setHover({
                          x: Math.max(bw, cw ?? 0) + margin.left + 6,
                          y: by + bandwidth / 2 + margin.top,
                          label: r.label,
                          count: r.count,
                          comparison: r.comparison,
                        })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      <text
                        x={-8}
                        y={by + bandwidth / 2}
                        dy="0.32em"
                        textAnchor="end"
                        fontSize={11}
                        fill="var(--viz-ink-primary)"
                      >
                        {label}
                      </text>
                      <rect
                        x={0}
                        y={by}
                        width={Math.max(1, bw)}
                        height={subHeight}
                        fill={grouped ? "var(--color-accent)" : color}
                      />
                      {cw != null && (
                        <rect
                          x={0}
                          y={by + subHeight}
                          width={Math.max(1, cw)}
                          height={subHeight}
                          fill="var(--color-fg-disabled)"
                          opacity={0.6}
                        />
                      )}
                      <text
                        x={Math.max(bw, cw ?? 0) + 6}
                        y={by + bandwidth / 2}
                        dy="0.32em"
                        fontSize={10}
                        fill="var(--viz-ink-muted)"
                      >
                        {fmtCount(r.count)}
                        {r.comparison != null ? ` / ${fmtCount(r.comparison)}` : ""}
                      </text>
                    </g>
                  );
                })}
              </>
            );
          }}
        </ChartFrame>
        {tooltip}
      </div>
    </div>
  );
}
