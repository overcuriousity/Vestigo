/**
 * The table figure, twice over one row model (`lib/tableRows.ts`):
 * `TableFigure` draws an <svg> so the page and the PNG/SVG export treat it
 * like every other figure; `TableHtml` is a real <table> for a Story
 * snapshot and the HTML export, where a table should be a table.
 */
import type { JSX } from "react";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import type { FieldTableResponse } from "@/api/types";
import type { ChartConfig, TableColumn } from "@/components/viz/lib/chartConfig";
import { columnHeader, effectiveColumns, tableRowModels } from "@/components/viz/lib/tableRows";

const VALUE_COL_FRACTION = 0.34;
const NUMERIC: Set<string> = new Set(["count", "share", "distinct_second"]);

interface Props {
  data: FieldTableResponse;
  config: ChartConfig;
  highlight: string[];
  svgRef?: React.RefObject<SVGSVGElement | null>;
  rowHeight?: number;
}

const inkFor = (isRemainder: boolean) =>
  isRemainder ? "var(--viz-ink-secondary)" : "var(--viz-ink-primary)";

export function TableFigure({
  data,
  config,
  highlight,
  svgRef,
  rowHeight = 22,
}: Props): JSX.Element {
  const ref = useChartRef(svgRef);
  const columns = effectiveColumns(config);
  const rows = tableRowModels(data, config, highlight);
  const maxCount = Math.max(1, ...rows.filter((r) => !r.isRemainder).map((r) => r.count));
  const height = rowHeight * (rows.length + 1) + 16;
  return (
    <ChartFrame height={height} margin={{ top: 4, right: 8, bottom: 4, left: 8 }} svgRef={ref}>
      {({ innerWidth: width }) => {
        const valueW = Math.max(80, width * VALUE_COL_FRACTION);
        const colW = columns.length ? (width - valueW) / columns.length : 0;
        const x = (i: number) => valueW + i * colW;
        const y = (r: number) => rowHeight * (r + 1);
        const cellX = (c: TableColumn, i: number) => x(i) + (NUMERIC.has(c) ? colW - 4 : 4);
        const anchor = (c: TableColumn) => (NUMERIC.has(c) ? "end" : "start");
        return (
          <g fontSize={12}>
            <text x={0} y={y(0) - 6} fill="var(--viz-ink-secondary)" fontWeight={600}>
              {columnHeader("value", config)}
            </text>
            {columns.map((c, i) => (
              <text
                key={c}
                x={cellX(c, i)}
                y={y(0) - 6}
                textAnchor={anchor(c)}
                fill="var(--viz-ink-secondary)"
                fontWeight={600}
              >
                {columnHeader(c, config)}
              </text>
            ))}
            <line x1={0} x2={width} y1={y(0)} y2={y(0)} stroke="var(--color-border)" />
            {rows.map((r, ri) => (
              <g key={r.key} transform={`translate(0,${y(ri)})`}>
                {r.highlighted && (
                  <rect
                    data-highlight-band
                    x={0}
                    y={2}
                    width={width}
                    height={rowHeight - 4}
                    fill="var(--color-accent-dim)"
                  />
                )}
                <text
                  x={0}
                  y={rowHeight - 7}
                  fill={inkFor(r.isRemainder)}
                  fontStyle={r.isRemainder ? "italic" : undefined}
                >
                  {r.label}
                </text>
                {columns.map((c, i) => (
                  <g key={c}>
                    {c === "count" && !r.isRemainder && (
                      <rect
                        data-cell-bar
                        x={x(i) + 2}
                        y={4}
                        height={rowHeight - 8}
                        width={Math.max(0, (colW - 8) * (r.count / maxCount))}
                        fill="var(--color-accent)"
                        opacity={0.35}
                      />
                    )}
                    <text
                      x={cellX(c, i)}
                      y={rowHeight - 7}
                      textAnchor={anchor(c)}
                      fill={inkFor(r.isRemainder)}
                      fontStyle={r.isRemainder ? "italic" : undefined}
                      className="tabular-nums"
                    >
                      {r.cells[c]}
                    </text>
                  </g>
                ))}
              </g>
            ))}
          </g>
        );
      }}
    </ChartFrame>
  );
}

export function TableHtml({
  data,
  config,
  highlight,
}: Omit<Props, "svgRef" | "rowHeight">): JSX.Element {
  const columns = effectiveColumns(config);
  const rows = tableRowModels(data, config, highlight);
  const th = "border-b border-[var(--color-border)] px-1.5 py-0.5 font-semibold";
  return (
    <table data-testid="table-figure-html" className="w-full text-xs">
      <thead>
        <tr>
          <th className={`${th} text-left`}>{columnHeader("value", config)}</th>
          {columns.map((c) => (
            <th key={c} className={`${th} ${NUMERIC.has(c) ? "text-right" : "text-left"}`}>
              {columnHeader(c, config)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr
            key={r.key}
            data-highlighted={r.highlighted ? "true" : undefined}
            data-remainder={r.isRemainder ? "true" : undefined}
            className={
              r.highlighted
                ? "bg-[var(--color-accent-dim)]"
                : r.isRemainder
                  ? "italic text-[var(--color-fg-secondary)]"
                  : undefined
            }
          >
            <td className="px-1.5 py-0.5">{r.label}</td>
            {columns.map((c) => (
              <td key={c} className={`px-1.5 py-0.5 tabular-nums ${NUMERIC.has(c) ? "text-right" : ""}`}>
                {r.cells[c]}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
