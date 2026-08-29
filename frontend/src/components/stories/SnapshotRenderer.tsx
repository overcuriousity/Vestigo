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
import { ChartMarks } from "@/components/viz/ChartCanvas";
import { ChartStaticWidthContext } from "@/components/viz/primitives/chartStaticWidth";
import { snapshotToChartResult } from "@/components/viz/chartFetch";
import { parseStoredChartConfig } from "@/components/viz/lib/chartConfig";
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

type SnapshotOf<K extends SnapshotBlock["kind"]> = Extract<SnapshotBlock, { kind: K }>;

/** A resolved block: `data` is non-null exactly when `resolution.error` is. */
type Resolved<K extends SnapshotBlock["kind"]> = SnapshotOf<K> & {
  data: NonNullable<SnapshotOf<K>["data"]>;
};

function MarkdownSnapshot({ block }: { block: Resolved<"markdown"> }) {
  const text = block.data.text ?? "";
  return (
    <div className="story-md text-sm [&>*+*]:mt-2 [&_code]:font-mono [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_ol]:list-decimal [&_ol]:pl-5 [&_table]:w-full [&_td]:border [&_td]:px-1.5 [&_td]:py-0.5 [&_th]:border [&_th]:px-1.5 [&_th]:py-0.5 [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function ViewSnapshot({ block }: { block: Resolved<"view_ref"> }) {
  const data = block.data;
  const columns = data.columns?.length ? data.columns : null;
  const name = block.ref.name ?? "View";

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

function ChartSnapshot({ block }: { block: Resolved<"chart_ref"> }) {
  const data = block.data;
  const config = parseStoredChartConfig(data.config);
  if (!config || !data.resolved || data.chart == null) {
    return <Unresolved error={`chart “${data.name}” could not be redrawn from the snapshot`} />;
  }
  // The server records the aggregation it ran; `snapshotToChartResult` rebuilds
  // the discriminated payload the mark dispatch expects — including the
  // histogram reshaping the live path applies before any mark sees it.
  const result = snapshotToChartResult(
    data.resolved.data_kind,
    data.resolved.compare_mode,
    config,
    data.chart,
  );
  if (!result) {
    return (
      <Unresolved
        error={`chart “${data.name}” froze an aggregation this build does not know how to draw (${data.resolved.data_kind})`}
      />
    );
  }

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-[var(--color-fg-secondary)]">{data.name}</p>
      <ChartMarks
        config={config}
        data={result}
        opts={resolveChartOptions(config)}
        compareOn={data.resolved.compare_mode !== "off"}
        tableAs="html"
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

function EventSnapshot({ block }: { block: Resolved<"event_ref"> }) {
  const data = block.data;
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

/**
 * Provenance marker on an agent-authored block.
 *
 * The exported HTML is the artifact that leaves the tool and gets attached to
 * a report, so "which paragraphs the AI wrote" has to be legible in it — the
 * editor shows the badge, and a reader of the export is exactly who needs it.
 * Analyst-authored blocks are unmarked: the default is a human wrote it.
 */
function OriginBadge({ block }: { block: SnapshotBlock }) {
  if (block.origin !== "agent") return null;
  return (
    <p className="mb-1 inline-block rounded border border-[var(--color-accent)]/40 bg-[var(--color-accent)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[var(--color-accent)]">
      agent-authored
    </p>
  );
}

function BlockBody({ block }: { block: SnapshotBlock }) {
  if (block.resolution.error || block.data == null) {
    return <Unresolved error={block.resolution.error ?? "no data was captured"} />;
  }
  switch (block.kind) {
    case "markdown":
      return <MarkdownSnapshot block={block as Resolved<"markdown">} />;
    case "view_ref":
      return <ViewSnapshot block={block as Resolved<"view_ref">} />;
    case "chart_ref":
      return <ChartSnapshot block={block as Resolved<"chart_ref">} />;
    case "event_ref":
      return <EventSnapshot block={block as Resolved<"event_ref">} />;
  }
}

function Block({ block }: { block: SnapshotBlock }) {
  return (
    <>
      <OriginBadge block={block} />
      <BlockBody block={block} />
    </>
  );
}

/**
 * Chart width for the exported document: the `max-w-4xl` article (56rem =
 * 896px) minus its `p-6` gutters (24px a side).
 *
 * Needed because this tree is rendered with `renderToStaticMarkup`, which
 * runs no effects — `ChartFrame`'s ResizeObserver never fires, so without a
 * pinned width every chart stayed behind its `width > 0` gate and the export
 * shipped prose with no diagrams (issue #197). A `ResizeObserver` still wins
 * where there is one, so this only decides the static case.
 */
const EXPORT_CHART_WIDTH = 848;

export function SnapshotRenderer({ snapshot }: { snapshot: StorySnapshot }) {
  return (
    <ChartStaticWidthContext.Provider value={EXPORT_CHART_WIDTH}>
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
    </ChartStaticWidthContext.Provider>
  );
}
