import { useMemo, useState } from "react";
import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import {
  buildSeriesColorMap,
  OTHER_COLOR,
  OTHER_KEY,
  OTHER_LABEL,
} from "@/components/viz/lib/colors";
import type { ChartValueClickHandler } from "@/components/viz/lib/interaction";
import type { FieldPivotResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const NODE_WIDTH = 12;
const NODE_GAP = 6;
const LABEL_COL = 118;
const MIN_LINK_PX = 1;

const displayLabel = (key: string) => (key === OTHER_KEY ? OTHER_LABEL : key);
const truncate = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

interface SankeyFlowProps {
  data: FieldPivotResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  onValueClick?: ChartValueClickHandler;
}

interface Node {
  key: string;
  total: number;
  y0: number;
  y1: number;
  /** Running cursor for stacking this node's ribbons during layout. */
  cursor: number;
}

interface Link {
  xKey: string;
  yKey: string;
  count: number;
  sy0: number;
  sy1: number;
  ty0: number;
  ty1: number;
}

/**
 * Two-column flow (Sankey) between two fields' top-N values — the same
 * `field-pivot` aggregation as the field×field heatmap, drawn as ribbons:
 * left nodes are X values, right nodes are Y values, ribbon thickness =
 * joint event count. A plain bipartite stacking (no crossing minimization —
 * with only two layers there is nothing to minimize); nodes keep the
 * server's count-descending order and per-axis "Other" rollups render as a
 * neutral node. Clicking a ribbon reports both field=value pairs; clicking
 * a node reports its single pair. Hand-rolled SVG — no d3-sankey dependency.
 */
export function SankeyFlow({ data, svgRef, height = 340, onValueClick }: SankeyFlowProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    label: string;
    count: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  if (data.cells.length === 0) {
    return (
      <ChartEmptyState hint="Both fields need a non-empty value on the same events for a pair to count.">
        No events with both fields set match the current filters.
      </ChartEmptyState>
    );
  }

  return (
    <div className="relative">
      <ChartFrame
        height={height}
        svgRef={ref}
        margin={{ top: 8, right: LABEL_COL, bottom: 8, left: LABEL_COL }}
      >
        {({ innerWidth, innerHeight, margin }) => (
          <SankeyBody
            data={data}
            innerWidth={innerWidth}
            innerHeight={innerHeight}
            marginLeft={margin.left}
            marginTop={margin.top}
            onValueClick={onValueClick}
            setHover={setHover}
          />
        )}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            {hover.label}
            <br />
            <strong>{fmtCount(hover.count)}</strong> events
          </>
        )}
      </ChartTooltip>
    </div>
  );
}

function SankeyBody({
  data,
  innerWidth,
  innerHeight,
  marginLeft,
  marginTop,
  onValueClick,
  setHover,
}: {
  data: FieldPivotResponse;
  innerWidth: number;
  innerHeight: number;
  marginLeft: number;
  marginTop: number;
  onValueClick?: ChartValueClickHandler;
  setHover: (
    h: { x: number; y: number; label: string; count: number } | null,
  ) => void;
}) {
  const { leftNodes, rightNodes, links } = useMemo(
    () => layoutSankey(data, innerHeight),
    [data, innerHeight],
  );
  const colorByLeft = useMemo(
    () => buildSeriesColorMap(leftNodes.map((n) => n.key)),
    [leftNodes],
  );

  const rightX = innerWidth - NODE_WIDTH;
  const midX = innerWidth / 2;

  const nodeClick =
    (field: string, key: string) =>
    (e: React.MouseEvent) => {
      if (onValueClick && key !== OTHER_KEY) {
        onValueClick({ entries: [[field, key]], clientX: e.clientX, clientY: e.clientY });
      }
    };

  return (
    <>
      {links.map((l, i) => {
        const clickable = onValueClick != null && l.xKey !== OTHER_KEY && l.yKey !== OTHER_KEY;
        const label = `${data.field_x} = ${displayLabel(l.xKey)} → ${data.field_y} = ${displayLabel(l.yKey)}`;
        return (
          <path
            key={i}
            d={ribbonPath(NODE_WIDTH, l.sy0, l.sy1, rightX, l.ty0, l.ty1, midX)}
            fill={l.xKey === OTHER_KEY ? OTHER_COLOR : (colorByLeft.get(l.xKey) ?? OTHER_COLOR)}
            fillOpacity={0.35}
            style={clickable ? { cursor: "pointer" } : undefined}
            onMouseEnter={(e) => {
              (e.currentTarget as SVGPathElement).setAttribute("fill-opacity", "0.6");
              setHover({
                x: midX + marginLeft,
                y: (l.sy0 + l.ty0) / 2 + marginTop,
                label,
                count: l.count,
              });
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as SVGPathElement).setAttribute("fill-opacity", "0.35");
              setHover(null);
            }}
            onClick={
              clickable
                ? (e) =>
                    onValueClick({
                      entries: [
                        [data.field_x, l.xKey],
                        [data.field_y, l.yKey],
                      ],
                      clientX: e.clientX,
                      clientY: e.clientY,
                    })
                : undefined
            }
          />
        );
      })}
      {leftNodes.map((n) => (
        <g key={n.key}>
          <rect
            x={0}
            y={n.y0}
            width={NODE_WIDTH}
            height={Math.max(1, n.y1 - n.y0)}
            fill={n.key === OTHER_KEY ? OTHER_COLOR : (colorByLeft.get(n.key) ?? OTHER_COLOR)}
            style={onValueClick && n.key !== OTHER_KEY ? { cursor: "pointer" } : undefined}
            onClick={nodeClick(data.field_x, n.key)}
            onMouseEnter={() =>
              setHover({
                x: NODE_WIDTH + marginLeft,
                y: n.y0 + marginTop,
                label: `${data.field_x} = ${displayLabel(n.key)}`,
                count: n.total,
              })
            }
            onMouseLeave={() => setHover(null)}
          />
          <text
            x={-6}
            y={(n.y0 + n.y1) / 2}
            dy="0.32em"
            textAnchor="end"
            fontSize={11}
            fill={n.key === OTHER_KEY ? "var(--viz-ink-muted)" : "var(--viz-ink-primary)"}
          >
            {truncate(displayLabel(n.key), 18)}
          </text>
        </g>
      ))}
      {rightNodes.map((n) => (
        <g key={n.key}>
          <rect
            x={rightX}
            y={n.y0}
            width={NODE_WIDTH}
            height={Math.max(1, n.y1 - n.y0)}
            fill="var(--viz-ink-muted)"
            style={onValueClick && n.key !== OTHER_KEY ? { cursor: "pointer" } : undefined}
            onClick={nodeClick(data.field_y, n.key)}
            onMouseEnter={() =>
              setHover({
                x: rightX + marginLeft,
                y: n.y0 + marginTop,
                label: `${data.field_y} = ${displayLabel(n.key)}`,
                count: n.total,
              })
            }
            onMouseLeave={() => setHover(null)}
          />
          <text
            x={rightX + NODE_WIDTH + 6}
            y={(n.y0 + n.y1) / 2}
            dy="0.32em"
            fontSize={11}
            fill={n.key === OTHER_KEY ? "var(--viz-ink-muted)" : "var(--viz-ink-primary)"}
          >
            {truncate(displayLabel(n.key), 18)}
          </text>
        </g>
      ))}
    </>
  );
}

/** Closed ribbon between a left node span and a right node span — two
 * horizontal-tangent cubic Béziers, the standard Sankey link shape. */
function ribbonPath(
  x0: number,
  sy0: number,
  sy1: number,
  x1: number,
  ty0: number,
  ty1: number,
  midX: number,
): string {
  return (
    `M ${x0},${sy0}` +
    ` C ${midX},${sy0} ${midX},${ty0} ${x1},${ty0}` +
    ` L ${x1},${ty1}` +
    ` C ${midX},${ty1} ${midX},${sy1} ${x0},${sy1}` +
    ` Z`
  );
}

/** Pure bipartite stacking: node heights proportional to marginal counts
 * (server count-desc order preserved, Other last), ribbons stacked within
 * each node in the opposite axis's node order so they never cross inside a
 * node. */
function layoutSankey(data: FieldPivotResponse, innerHeight: number) {
  const hasOtherX = data.cells.some((c) => c.x === "");
  const hasOtherY = data.cells.some((c) => c.y === "");
  const xKeys = [...data.x_values, ...(hasOtherX ? [OTHER_KEY] : [])];
  const yKeys = [...data.y_values, ...(hasOtherY ? [OTHER_KEY] : [])];

  const cells = data.cells.map((c) => ({
    xKey: c.x === "" ? OTHER_KEY : c.x,
    yKey: c.y === "" ? OTHER_KEY : c.y,
    count: c.count,
  }));

  const marginal = (keys: string[], axis: "xKey" | "yKey") =>
    keys.map((key) => ({
      key,
      total: cells.reduce((sum, c) => (c[axis] === key ? sum + c.count : sum), 0),
    }));

  const stack = (totals: { key: string; total: number }[]): Node[] => {
    const grand = Math.max(
      1,
      totals.reduce((s, t) => s + t.total, 0),
    );
    const usable = Math.max(1, innerHeight - NODE_GAP * Math.max(0, totals.length - 1));
    let y = 0;
    return totals.map(({ key, total }) => {
      const h = Math.max(total > 0 ? MIN_LINK_PX : 0, (total / grand) * usable);
      const node: Node = { key, total, y0: y, y1: y + h, cursor: y };
      y += h + NODE_GAP;
      return node;
    });
  };

  const leftNodes = stack(marginal(xKeys, "xKey"));
  const rightNodes = stack(marginal(yKeys, "yKey"));
  const leftByKey = new Map(leftNodes.map((n) => [n.key, n]));
  const rightByKey = new Map(rightNodes.map((n) => [n.key, n]));
  const yOrder = new Map(yKeys.map((k, i) => [k, i]));
  const xOrder = new Map(xKeys.map((k, i) => [k, i]));

  // Stack each ribbon inside its source node in Y-node order and inside its
  // target node in X-node order — a canonical non-crossing-within-node layout.
  const ordered = [...cells].sort(
    (a, b) =>
      (xOrder.get(a.xKey) ?? 0) - (xOrder.get(b.xKey) ?? 0) ||
      (yOrder.get(a.yKey) ?? 0) - (yOrder.get(b.yKey) ?? 0),
  );
  const links: Link[] = [];
  for (const c of ordered) {
    const src = leftByKey.get(c.xKey);
    const tgt = rightByKey.get(c.yKey);
    if (!src || !tgt || c.count <= 0) continue;
    const sh = Math.max(MIN_LINK_PX, (c.count / Math.max(1, src.total)) * (src.y1 - src.y0));
    const th = Math.max(MIN_LINK_PX, (c.count / Math.max(1, tgt.total)) * (tgt.y1 - tgt.y0));
    links.push({
      xKey: c.xKey,
      yKey: c.yKey,
      count: c.count,
      sy0: src.cursor,
      sy1: Math.min(src.y1, src.cursor + sh),
      ty0: tgt.cursor,
      ty1: Math.min(tgt.y1, tgt.cursor + th),
    });
    src.cursor += sh;
    tgt.cursor += th;
  }
  return { leftNodes, rightNodes, links };
}
