import { useMemo, useState } from "react";
import { scaleLinear, scaleTime } from "d3-scale";
import { line as d3line, area as d3area, curveMonotoneX } from "d3-shape";
import { max as d3max, bisector } from "d3-array";
import { utcFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { buildSeriesColorMap } from "@/components/viz/lib/colors";
import { valueLabeller } from "@/components/viz/lib/fieldDisplay";
import type { ChartValueClickHandler } from "@/components/viz/lib/interaction";
import { svgLocalPoint } from "@/components/viz/lib/pointer";
import type { FieldTimeseriesResponse } from "@/api/types";

const fmtCount = formatNum(",d");
// utcFormat, not timeFormat — bucket starts are UTC instants and the tooltip
// says "UTC"; timeFormat would silently render them in the browser's zone.
const fmtTick = utcFormat("%b %d %H:%M");
const fmtFull = utcFormat("%Y-%m-%d %H:%M:%S UTC");
const bisectDate = bisector((d: Date) => d).left;

interface LineChartProps {
  data: FieldTimeseriesResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** "overlay" (default) draws independent lines; "stacked" draws cumulative
   * areas — reads as composition of the total rather than per-series shape. */
  seriesMode?: "overlay" | "stacked";
  showLegend?: boolean;
  /** Mark the actual measured buckets. Graphical integrity (Tufte): the line
   * between two points asserts values that were never measured — markers show
   * where the data really is. Auto-suppressed on dense series. */
  showPoints?: boolean;
  /** Click-to-filter: clicking a legend entry reports its field=value pair. */
  onValueClick?: ChartValueClickHandler;
}

/**
 * Multi-series line chart — per-value event counts over time, restricted to
 * the top values (see `EventQueryService.field_value_timeseries`). A
 * crosshair + tooltip shows every series' value at the hovered bucket, per
 * the dataviz skill's line-chart interaction default.
 */
export function LineChart({
  data,
  svgRef,
  height = 260,
  seriesMode = "overlay",
  showPoints = true,
  showLegend = true,
  onValueClick,
}: LineChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const ref = useChartRef(svgRef);
  const isEmpty = data.series.length === 0 || data.series[0].buckets.length === 0;
  const stacked = seriesMode === "stacked";

  const dates = useMemo(
    () => (isEmpty ? [] : data.series[0].buckets.map((b) => new Date(b.start))),
    [isEmpty, data.series],
  );
  // Stacked offsets: series i's band sits on the sum of series 0..i-1 —
  // same order as the legend so bands and labels read top-down consistently.
  const stackBase = useMemo(() => {
    if (!stacked) return [];
    const base: number[][] = [];
    let running = dates.map(() => 0);
    for (const s of data.series) {
      base.push(running);
      running = running.map((v, i) => v + (s.buckets[i]?.count ?? 0));
    }
    return base;
  }, [stacked, dates, data.series]);
  const maxCount = useMemo(
    () =>
      stacked
        ? Math.max(
            1,
            ...dates.map((_, i) =>
              data.series.reduce((sum, s) => sum + (s.buckets[i]?.count ?? 0), 0),
            ),
          )
        : Math.max(1, d3max(data.series, (s) => d3max(s.buckets, (b) => b.count) ?? 0) ?? 0),
    [stacked, dates, data.series],
  );
  // Keyed on the canonical value, never the display label: buildSeriesColorMap
  // guarantees a value keeps its colour across chart types (bar/pie/line all
  // build from the same field_terms order), so keying on a label would
  // reshuffle colours on a bar↔line switch for labelled fields only.
  const colorMap = useMemo(
    () => buildSeriesColorMap(data.series.map((s) => s.value)),
    [data.series],
  );
  const labelOf = valueLabeller(data.field);

  if (isEmpty) {
    return (
      <ChartEmptyState
        hint="Events without a usable timestamp are excluded from time-based charts. Try a Top-values (bar) chart, or widen the time range."
      >
        No values over time here.
      </ChartEmptyState>
    );
  }

  return (
    <div className="relative flex flex-col gap-2">
      <ChartFrame height={height} svgRef={ref}>
        {({ innerWidth, innerHeight, margin }) => {
          const x = scaleTime()
            .domain([dates[0], dates[dates.length - 1]])
            .range([0, innerWidth]);
          const y = scaleLinear().domain([0, maxCount]).nice().range([innerHeight, 0]);
          const lineGen = d3line<{ start: string; count: number }>()
            .curve(curveMonotoneX)
            .x((d) => x(new Date(d.start)))
            .y((d) => y(d.count));
          const areaGen = d3area<{ date: Date; y0: number; y1: number }>()
            .curve(curveMonotoneX)
            .x((d) => x(d.date))
            .y0((d) => y(d.y0))
            .y1((d) => y(d.y1));

          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={(v) => fmtCount(v)} />
              <AxisBottom
                scale={x}
                innerWidth={innerWidth}
                innerHeight={innerHeight}
                tickFormat={(v) => fmtTick(v as Date)}
              />
              {stacked
                ? data.series.map((s, si) => (
                    <path
                      key={s.value}
                      d={
                        areaGen(
                          s.buckets.map((b, i) => ({
                            date: dates[i],
                            y0: stackBase[si][i],
                            y1: stackBase[si][i] + b.count,
                          })),
                        ) ?? undefined
                      }
                      fill={colorMap.get(s.value) ?? "var(--color-accent)"}
                      fillOpacity={0.75}
                      stroke={colorMap.get(s.value) ?? "var(--color-accent)"}
                      strokeWidth={0.75}
                    />
                  ))
                : data.series.map((s) => (
                    <g key={s.value}>
                      <path
                        d={lineGen(s.buckets) ?? undefined}
                        fill="none"
                        stroke={colorMap.get(s.value) ?? "var(--color-accent)"}
                        strokeWidth={1.75}
                      />
                      {/* Markers only while they stay legible — past ~120
                          buckets per series they merge into the line anyway. */}
                      {showPoints &&
                        s.buckets.length <= 120 &&
                        s.buckets.map((b, i) => (
                          <circle
                            key={i}
                            cx={x(new Date(b.start))}
                            cy={y(b.count)}
                            r={2}
                            fill={colorMap.get(s.value) ?? "var(--color-accent)"}
                            pointerEvents="none"
                          />
                        ))}
                    </g>
                  ))}
              {hoverIdx != null && (
                <line
                  x1={x(dates[hoverIdx])}
                  x2={x(dates[hoverIdx])}
                  y1={0}
                  y2={innerHeight}
                  stroke="var(--viz-axis)"
                  strokeWidth={1}
                  strokeDasharray="2,2"
                />
              )}
              {/* Full-height hover strip drives the crosshair + tooltip. */}
              <rect
                x={0}
                y={0}
                width={innerWidth}
                height={innerHeight}
                fill="transparent"
                onMouseMove={(e) => {
                  const local = svgLocalPoint(e, margin);
                  if (!local) return;
                  const target = x.invert(local.x);
                  let idx = bisectDate(dates, target, 1);
                  idx = Math.min(dates.length - 1, Math.max(0, idx));
                  if (
                    idx > 0 &&
                    target.getTime() - dates[idx - 1].getTime() < dates[idx].getTime() - target.getTime()
                  ) {
                    idx -= 1;
                  }
                  setHoverIdx((prev) => (prev === idx ? prev : idx));
                }}
                onMouseLeave={() => setHoverIdx(null)}
              />
            </>
          );
        }}
      </ChartFrame>
      {showLegend && (
        <Legend
          entries={data.series.map((s) => ({
            // `key` is mandatory, not decorative: Legend reports
            // `e.key ?? e.label` to onEntryClick, so omitting it would make a
            // relabelled series ("Mon") filter on its display text, which
            // matches nothing.
            key: s.value,
            label: labelOf(s.value),
            color: colorMap.get(s.value) ?? "var(--color-accent)",
          }))}
          onEntryClick={
            onValueClick
              ? (key, e) =>
                  onValueClick({
                    entries: [[data.field, key]],
                    clientX: e.clientX,
                    clientY: e.clientY,
                  })
              : undefined
          }
        />
      )}
      {hoverIdx != null && (
        <ChartTooltip
          x={80}
          y={16}
          visible
        >
          <div>{fmtFull(dates[hoverIdx])}</div>
          {data.series.map((s) => (
            <div key={s.value}>
              <span style={{ color: colorMap.get(s.value) }}>●</span> {labelOf(s.value)}:{" "}
              <strong>{fmtCount(s.buckets[hoverIdx]?.count ?? 0)}</strong>
            </div>
          ))}
        </ChartTooltip>
      )}
    </div>
  );
}
