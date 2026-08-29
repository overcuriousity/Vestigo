/**
 * The table figure's row model — one place that decides which columns show,
 * how a cell reads, which rows are highlighted and what the remainder row
 * says, shared by the SVG figure, the Story's real <table> and the CSV
 * export so the three can never disagree. `docs/VISUALIZE.md` §"Table figure".
 */
import type { FieldTableResponse } from "@/api/types";
import type { ChartConfig, TableColumn } from "./chartConfig";
import { OTHER_KEY } from "./colors";
import { fieldTokenLabel, valueLabeller } from "./fieldDisplay";

export const DEFAULT_TABLE_COLUMNS: TableColumn[] = ["count", "share", "first_seen", "last_seen"];

export const TABLE_COLUMN_LABELS: Record<"value" | TableColumn, string> = {
  value: "value",
  count: "count",
  share: "share",
  first_seen: "first seen",
  last_seen: "last seen",
  distinct_second: "distinct",
};

/** Column header for *column*, naming the second field where it applies. */
export function columnHeader(column: "value" | TableColumn, config: ChartConfig): string {
  if (column === "distinct_second" && config.fieldY)
    return `distinct ${fieldTokenLabel(config.fieldY)}`;
  if (column === "value" && config.field) return fieldTokenLabel(config.field);
  return TABLE_COLUMN_LABELS[column];
}

/** The columns the figure shows: the analyst's choice, else the defaults plus
 * `distinct_second` when a second field is set — and never `distinct_second`
 * without one, whatever the stored choice says. */
export function effectiveColumns(config: ChartConfig): TableColumn[] {
  const chosen = config.inputs.columns;
  const base =
    chosen ??
    (config.fieldY ? [...DEFAULT_TABLE_COLUMNS, "distinct_second"] : DEFAULT_TABLE_COLUMNS);
  return base.filter((c) => c !== "distinct_second" || !!config.fieldY);
}

export interface TableRowModel {
  key: string;
  value: string;
  label: string;
  cells: Record<TableColumn, string>;
  count: number;
  share: number;
  highlighted: boolean;
  isRemainder: boolean;
}

const NONE = "—";
const fmtTime = (iso: string | null) => (iso ? `${iso.slice(0, 19).replace("T", " ")}Z` : NONE);
const fmtShare = (share: number) => `${(share * 100).toFixed(1)}%`;
const fmtInt = (n: number) => n.toLocaleString("en-US");

const remainderLabel = (n: number) => `Remainder (${fmtInt(n)} more value${n === 1 ? "" : "s"})`;

export function tableRowModels(
  data: FieldTableResponse,
  config: ChartConfig,
  highlight: string[],
): TableRowModel[] {
  const labelOf = config.field ? valueLabeller(config.field) : (v: string) => v;
  const lit = new Set(highlight);
  const rows: TableRowModel[] = data.rows.map((r) => ({
    key: r.value,
    value: r.value,
    label: labelOf(r.value),
    cells: {
      count: fmtInt(r.count),
      share: fmtShare(r.share),
      first_seen: fmtTime(r.first_seen),
      last_seen: fmtTime(r.last_seen),
      distinct_second: r.distinct_second == null ? NONE : fmtInt(r.distinct_second),
    },
    count: r.count,
    share: r.share,
    highlighted: lit.has(r.value),
    isRemainder: false,
  }));
  if (data.remainder) {
    rows.push({
      key: OTHER_KEY,
      value: "",
      label: remainderLabel(data.remainder.distinct_values),
      cells: {
        count: fmtInt(data.remainder.count),
        share: fmtShare(data.remainder.share),
        first_seen: NONE,
        last_seen: NONE,
        distinct_second: NONE,
      },
      count: data.remainder.count,
      share: data.remainder.share,
      highlighted: false,
      isRemainder: true,
    });
  }
  return rows;
}

const csvCell = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);

/** Raw values (a share is `0.4545…`, not `45.5%`), so the file computes. */
export function tableCsv(
  data: FieldTableResponse,
  config: ChartConfig,
  captionLines: string[],
): string {
  const columns = effectiveColumns(config);
  const raw = (r: FieldTableResponse["rows"][number], c: TableColumn): string => {
    switch (c) {
      case "count":
        return String(r.count);
      case "share":
        return String(r.share);
      case "first_seen":
        return r.first_seen ?? "";
      case "last_seen":
        return r.last_seen ?? "";
      case "distinct_second":
        return r.distinct_second == null ? "" : String(r.distinct_second);
    }
  };
  const lines = captionLines.map((l) => `# ${l}`);
  lines.push(["value", ...columns].map(csvCell).join(","));
  for (const r of data.rows)
    lines.push([r.value, ...columns.map((c) => raw(r, c))].map(csvCell).join(","));
  if (data.remainder) {
    const rem = data.remainder;
    lines.push(
      [
        `Remainder (${rem.distinct_values} more value${rem.distinct_values === 1 ? "" : "s"})`,
        ...columns.map((c) =>
          c === "count" ? String(rem.count) : c === "share" ? String(rem.share) : "",
        ),
      ]
        .map(csvCell)
        .join(","),
    );
  }
  return lines.join("\n") + "\n";
}
