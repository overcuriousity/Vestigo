/**
 * SnapshotRenderer — draws a story export snapshot and nothing else.
 *
 * Every value comes from the frozen bundle the server resolved at export
 * time: no hooks that fetch, no `useQuery` anywhere in this subtree. That is
 * what makes the exported HTML self-contained and the report reproducible —
 * and it is asserted by a test that fails if a render triggers a request.
 *
 * Blocks the server could not resolve (a view deleted before the export)
 * render as a visible "unresolved at export" panel rather than disappearing:
 * a gap in a forensic report has to be legible.
 */
import { AlertTriangle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { SnapshotBlock, StorySnapshot } from "@/api/types";
import { ChartMarks, type ChartResult } from "@/components/viz/ChartCanvas";
import { parseStoredChartConfig } from "@/components/viz/lib/chartConfig";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import { resolveChartOptions } from "@/components/viz/lib/chartOptions";
import { fmtNum } from "@/lib/format";
import { fmtTimestamp } from "@/lib/time";

function Unresolved({ error }: { error: string }) {
  return (
    <p className="flex items-start gap-1.5 rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 p-2 text-xs text-[var(--color-warning)]">
      <AlertTriangle size={12} className="mt-0.5 shrink-0" />
      <span>unresolved at export: {error}</span>
    </p>
  );
}

function MarkdownSnapshot({ block }: { block: SnapshotBlock }) {
  const text = (block.data?.text as string) ?? "";
  return (
    <div className="story-md text-sm [&>*+*]:mt-2 [&_code]:font-mono [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_ol]:list-decimal [&_ol]:pl-5 [&_table]:w-full [&_td]:border [&_td]:px-1.5 [&_td]:py-0.5 [&_th]:border [&_th]:px-1.5 [&_th]:py-0.5 [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function ViewSnapshot({ block }: { block: SnapshotBlock }) {
  const data = block.data as {
    rows: Record<string, unknown>[];
    row_count_total: number;
    rows_included: number;
    truncated: boolean;
    columns: string[] | null;
  };
  const columns = data.columns?.length ? data.columns : null;
  const name = (block.ref.name as string) ?? "View";

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-[var(--color-fg-secondary)]">{name}</p>
      <div className="overflow-x-auto rounded border border-[var(--color-border)]">
        <table className="w-full text-[11px]">
          <thead className="bg-[var(--color-bg-elevated)] text-left text-[var(--color-fg-muted)]">
            <tr>
              <th className="px-2 py-1 font-medium">Timestamp</th>
              {columns ? (
                columns.map((c) => (
                  <th key={c} className="px-2 py-1 font-medium">
                    {c}
                  </th>
                ))
              ) : (
                <th className="px-2 py-1 font-medium">Message</th>
              )}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, i) => {
              const attrs = (row.attributes ?? {}) as Record<string, string>;
              return (
                <tr
                  key={(row.event_id as string) ?? i}
                  className="border-t border-[var(--color-border)]/60 align-top"
                >
                  <td className="whitespace-nowrap px-2 py-1 font-mono text-[var(--color-fg-muted)]">
                    {row.timestamp ? fmtTimestamp(row.timestamp as string) : "—"}
                  </td>
                  {columns ? (
                    columns.map((c) => (
                      <td key={c} className="px-2 py-1">
                        {attrs[c] ?? ""}
                      </td>
                    ))
                  ) : (
                    <td className="px-2 py-1">{(row.message as string) ?? ""}</td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {/* Truncation is stated, never implied: a report that shows 200 of
          14203 rows must say which it is. */}
      <p className="text-[10px] text-[var(--color-fg-muted)]">
        {data.truncated
          ? `${data.rows_included} of ${fmtNum(data.row_count_total)} rows shown`
          : `${data.rows_included} row${data.rows_included === 1 ? "" : "s"}`}
      </p>
    </div>
  );
}

function ChartSnapshot({ block }: { block: SnapshotBlock }) {
  const data = block.data as {
    name: string;
    config: Record<string, unknown>;
    resolved: { data_kind: string; compare_mode: string } | null;
    warnings: string[];
    chart: unknown;
  };
  const config = parseStoredChartConfig(data.config);
  if (!config || !data.resolved || data.chart == null) {
    return <Unresolved error={`chart “${data.name}” could not be redrawn from the snapshot`} />;
  }
  const meta = CHART_META[config.chartType];
  // The server records the aggregation it ran; rebuild the discriminated
  // payload the mark dispatch expects around the frozen result.
  const kind =
    data.resolved.data_kind === "numeric" && meta.acceptsSecondField && config.fieldY
      ? "numeric_grouped"
      : data.resolved.data_kind;
  const result = {
    kind,
    compare: data.resolved.compare_mode !== "off",
    data: data.chart,
  } as unknown as ChartResult;

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-[var(--color-fg-secondary)]">{data.name}</p>
      <ChartMarks
        config={config}
        data={result}
        opts={resolveChartOptions(config)}
        compareOn={data.resolved.compare_mode !== "off"}
      />
      {data.warnings?.length > 0 && (
        <ul className="list-disc pl-4 text-[10px] text-[var(--color-fg-muted)]">
          {data.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EventSnapshot({ block }: { block: SnapshotBlock }) {
  const data = block.data as { event: Record<string, unknown>; caption: string | null };
  const ev = data.event;
  return (
    <div className="space-y-1 rounded border border-[var(--color-border)] p-2">
      <div className="flex items-center gap-2 text-[11px] text-[var(--color-fg-muted)]">
        <span className="font-mono">
          {ev.timestamp ? fmtTimestamp(ev.timestamp as string) : "no timestamp"}
        </span>
        <span className="truncate">{(ev.source_file as string) ?? ""}</span>
      </div>
      <p className="break-words font-mono text-xs">{(ev.message as string) ?? ""}</p>
      {data.caption && (
        <p className="text-xs italic text-[var(--color-fg-secondary)]">{data.caption}</p>
      )}
    </div>
  );
}

function Block({ block }: { block: SnapshotBlock }) {
  if (block.resolution.error || block.data == null) {
    return <Unresolved error={block.resolution.error ?? "no data was captured"} />;
  }
  switch (block.kind) {
    case "markdown":
      return <MarkdownSnapshot block={block} />;
    case "view_ref":
      return <ViewSnapshot block={block} />;
    case "chart_ref":
      return <ChartSnapshot block={block} />;
    case "event_ref":
      return <EventSnapshot block={block} />;
  }
}

export function SnapshotRenderer({ snapshot }: { snapshot: StorySnapshot }) {
  return (
    <article className="mx-auto max-w-4xl space-y-4 p-6 text-[var(--color-fg-primary)]">
      <header className="space-y-1 border-b border-[var(--color-border)] pb-3">
        <h1 className="text-xl font-semibold">{snapshot.story.title}</h1>
        <p className="text-xs text-[var(--color-fg-muted)]">
          Exported {fmtTimestamp(snapshot.story.exported_at)} by {snapshot.story.exported_by} ·
          case <span className="font-mono">{snapshot.story.case_id}</span>
        </p>
      </header>
      {snapshot.blocks.map((block) => (
        <section key={block.id}>
          <Block block={block} />
        </section>
      ))}
    </article>
  );
}
