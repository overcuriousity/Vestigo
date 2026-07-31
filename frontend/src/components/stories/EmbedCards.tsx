/**
 * Read-only cards for a story's embed blocks (view / chart / event).
 *
 * Each card resolves its own reference through the existing API modules —
 * there is no story-specific data endpoint, so an embedded view is the same
 * query the Explorer runs and an embedded chart is drawn by the shared
 * `ChartCanvas`. A reference whose target was deleted renders an explicit
 * placeholder: the block stays, visibly unresolved, rather than vanishing.
 */
import { useEffect, useMemo, useRef } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertTriangle, ExternalLink } from "lucide-react";
import { eventsApi } from "@/api/events";
import { savedChartsApi } from "@/api/viz";
import { viewsApi } from "@/api/views";
import type { Event, StoryBlockOf } from "@/api/types";
import { ChartCanvas } from "@/components/viz/ChartCanvas";
import { chartConfigToParams, parseStoredChartConfig } from "@/components/viz/lib/chartConfig";
import { Spinner } from "@/components/ui/Spinner";
import { filtersToParams, viewPayloadToFilters } from "@/lib/queryParams";
import { fmtNum } from "@/lib/format";
import { fmtTimestamp } from "@/lib/time";
import { useTimelineForSource } from "@/hooks/useTimelineForSource";

/**
 * A block whose target is genuinely gone.
 *
 * Only for a *successful* lookup that found nothing. A failed request is a
 * different statement — see `LookupFailed`. Telling an analyst that a live
 * object was deleted because a request 500'd is worse than saying nothing.
 */
function Unresolved({ what }: { what: string }) {
  return (
    <p className="flex items-center gap-1.5 text-xs text-[var(--color-warning)]">
      <AlertTriangle size={12} /> {what} was deleted — this block no longer resolves.
    </p>
  );
}

/** A block whose target could not be looked up (network, 5xx, permissions). */
function LookupFailed({ what, error }: { what: string; error: unknown }) {
  return (
    <p className="flex items-start gap-1.5 text-xs text-[var(--color-danger)]">
      <AlertTriangle size={12} className="mt-0.5 shrink-0" />
      <span>
        {what} could not be loaded: {(error as Error)?.message ?? "unknown error"}
      </span>
    </p>
  );
}

function OpenLink({ to, label }: { to: string; label: string }) {
  return (
    <Link
      to={to}
      className="flex shrink-0 items-center gap-1 text-[11px] text-[var(--color-accent)] hover:underline"
    >
      {label} <ExternalLink size={10} />
    </Link>
  );
}

/** Height of one preview row, in px — fixed, so the rows can be virtualized. */
const ROW_HEIGHT = 22;
const OVERSCAN = 8;

/** One truncated preview cell, with the untruncated text on hover. */
function Cell({ value }: { value: unknown }) {
  const text = value == null ? "" : typeof value === "string" ? value : String(value);
  return (
    <span
      role="cell"
      title={text || undefined}
      className="truncate px-2 text-[var(--color-fg-secondary)]"
    >
      {text}
    </span>
  );
}

/**
 * The embedded view's rows, windowed.
 *
 * A view block embeds up to `display.limit` rows (200 by default, capped at
 * VIEW_BLOCK_ROW_CAP server-side) into a 320px-tall scroller — so the card
 * used to build every one of those rows on every render of the story, for
 * every view block in it, to show about a dozen. Virtualizing costs nothing
 * semantically: the row count and the "N of M rows shown" line below still
 * describe the full embedded set, which is also what the export snapshot
 * renders (`stories/export.py` builds that independently of this preview).
 *
 * Fixed row height is what makes the windowing simple, and it is why cells
 * truncate rather than wrap here. The Explorer is where a long message is
 * meant to be read — that is what "Open in Explorer" above is for.
 */
function RowPreview({ rows, columns }: { rows: Event[]; columns: string[] | null }) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
  });
  const virtualItems = virtualizer.getVirtualItems();

  // Same remount guard the Explorer grid carries: if the scroller's height
  // settles after react-virtual's ResizeObserver first looks, the initial
  // paint yields no virtual items and the card would stay blank until the
  // analyst scrolled. Cheap at a fixed row height, and the condition cannot
  // hold after a successful measure, so it cannot loop.
  useEffect(() => {
    if (rows.length > 0 && virtualItems.length === 0) virtualizer.measure();
  }, [rows.length, virtualItems.length, virtualizer]);

  const gridTemplate = `9.5rem repeat(${columns ? columns.length : 1}, minmax(0, 1fr))`;

  return (
    // Divs, not a <table>, because virtualization needs absolutely positioned
    // rows — so the table roles are spelled out by hand. `aria-rowcount` is
    // the whole embedded set rather than what is windowed into the DOM, and
    // each row carries its true index, which is how a screen reader is told
    // it is reading a window rather than the lot.
    <div
      role="table"
      aria-label="Embedded view rows"
      aria-rowcount={rows.length + 1}
      className="rounded border border-[var(--color-border)] text-[11px]"
    >
      <div role="rowgroup">
        <div
          role="row"
          aria-rowindex={1}
          className="grid gap-0 border-b border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-left font-medium text-[var(--color-fg-muted)]"
          style={{ gridTemplateColumns: gridTemplate }}
        >
          <span role="columnheader" className="truncate px-2 py-1">
            Timestamp
          </span>
          {(columns ?? ["Message"]).map((c) => (
            <span role="columnheader" key={c} className="truncate px-2 py-1">
              {c}
            </span>
          ))}
        </div>
      </div>
      <div ref={parentRef} role="rowgroup" className="max-h-80 overflow-auto">
        {/* presentation: a layout box between rowgroup and row would otherwise
            break the table's ownership chain. */}
        <div
          role="presentation"
          className="relative w-full"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualItems.map((v) => {
            const ev = rows[v.index];
            return (
              <div
                key={ev.event_id}
                role="row"
                aria-rowindex={v.index + 2}
                className="absolute left-0 top-0 grid w-full items-center border-t border-[var(--color-border)]/60"
                style={{
                  height: ROW_HEIGHT,
                  transform: `translateY(${v.start}px)`,
                  gridTemplateColumns: gridTemplate,
                }}
              >
                <span
                  role="cell"
                  className="truncate px-2 font-mono text-[var(--color-fg-muted)]"
                >
                  {ev.timestamp ? fmtTimestamp(ev.timestamp) : "—"}
                </span>
                {/* `title` on every cell: rows are a fixed height so the text
                    truncates, and a log line is exactly the thing an analyst
                    needs to read in full. Hover restores it without giving up
                    the windowing; "Open in Explorer" is the full-size path. */}
                {columns ? (
                  columns.map((c) => (
                    <Cell key={c} value={ev.attributes?.[c] ?? ""} />
                  ))
                ) : (
                  <Cell value={ev.message} />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function ViewBlockCard({
  block,
  caseId,
}: {
  block: StoryBlockOf<"view_ref">;
  caseId: string;
}) {
  const { view_id: viewId, timeline_id: timelineId } = block.content;
  const display = block.content.display ?? {};
  const limit = display.limit ?? 200;

  const viewQuery = useQuery({
    queryKey: ["views", caseId],
    queryFn: () => viewsApi.list(caseId),
  });
  const view = viewQuery.data?.find((v) => v.id === viewId);
  const filters = view ? viewPayloadToFilters(view.filter) : null;

  const rowsQuery = useQuery({
    // The view's filter payload is part of the key: editing a View's filters
    // elsewhere has to invalidate this card, and the ids alone never change
    // when it does.
    queryKey: ["story-view-rows", caseId, timelineId, viewId, limit, view?.filter ?? null],
    queryFn: () => eventsApi.list(caseId, timelineId, { ...filters!, limit }),
    enabled: !!filters,
  });

  if (viewQuery.isLoading) return <Spinner size={14} />;
  if (viewQuery.isError) {
    return <LookupFailed what="The referenced view" error={viewQuery.error} />;
  }
  if (!view) return <Unresolved what="The referenced view" />;

  const rows = rowsQuery.data?.events ?? [];
  const total = rowsQuery.data?.total ?? null;
  const columns = display.columns?.length ? display.columns : null;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-[var(--color-fg-secondary)]">
          {view.name}
        </span>
        <OpenLink
          to={`/cases/${caseId}/timelines/${timelineId}?${filtersToParams(filters!).toString()}`}
          label="Open in Explorer"
        />
      </div>
      {rowsQuery.isLoading && <Spinner size={14} />}
      {rowsQuery.isError && (
        <p className="text-xs text-[var(--color-danger)]">
          {(rowsQuery.error as Error).message}
        </p>
      )}
      {rowsQuery.data && (
        <>
          <RowPreview rows={rows} columns={columns} />
          <p className="text-[10px] text-[var(--color-fg-muted)]">
            {total != null && total > rows.length
              ? `${rows.length} of ${fmtNum(total)} rows shown`
              : `${rows.length} row${rows.length === 1 ? "" : "s"}`}
          </p>
        </>
      )}
    </div>
  );
}

export function ChartBlockCard({
  block,
  caseId,
}: {
  block: StoryBlockOf<"chart_ref">;
  caseId: string;
}) {
  const { chart_id: chartId, timeline_id: timelineId } = block.content;

  const chartsQuery = useQuery({
    queryKey: ["viz-saved-charts", caseId, timelineId],
    queryFn: () => savedChartsApi.list(caseId, timelineId),
  });
  const chart = chartsQuery.data?.charts.find((c) => c.id === chartId);
  // Memoized because ChartCanvas puts this object in a query key: re-parsing
  // it on every render handed the key a new object each time, so every render
  // of the story re-hashed the whole config.
  const config = useMemo(
    () => (chart ? parseStoredChartConfig(chart.config) : null),
    [chart],
  );

  if (chartsQuery.isLoading) return <Spinner size={14} />;
  if (chartsQuery.isError) {
    return <LookupFailed what="The referenced chart" error={chartsQuery.error} />;
  }
  if (!chart) return <Unresolved what="The referenced chart" />;
  if (!config) {
    return (
      <p className="text-xs text-[var(--color-warning)]">
        “{chart.name}” was saved with an incompatible config version and cannot be drawn.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-[var(--color-fg-secondary)]">
          {chart.name}
        </span>
        <OpenLink
          to={`/cases/${caseId}/timelines/${timelineId}/visualize?${chartConfigToParams(
            config,
          ).toString()}`}
          label="Open in Visualize"
        />
      </div>
      <ChartCanvas caseId={caseId} timelineId={timelineId} config={config} />
    </div>
  );
}

export function EventBlockCard({
  block,
  caseId,
}: {
  block: StoryBlockOf<"event_ref">;
  caseId: string;
}) {
  const { event_id: eventId, source_id: sourceId } = block.content;
  const caption = block.content.caption ?? null;

  const timelinesQuery = useTimelineForSource(caseId, sourceId);
  const timelineId = timelinesQuery.timelineId;

  const eventQuery = useQuery({
    queryKey: ["story-event", caseId, timelineId, eventId],
    queryFn: () => eventsApi.getById(caseId, timelineId!, eventId),
    enabled: !!timelineId,
  });

  if (timelinesQuery.isLoading) return <Spinner size={14} />;
  if (timelinesQuery.isError) {
    return <LookupFailed what="The referenced event" error={timelinesQuery.error} />;
  }
  if (!timelineId) {
    return <Unresolved what="The event's source" />;
  }
  if (eventQuery.isLoading) return <Spinner size={14} />;
  if (eventQuery.isError) {
    return <LookupFailed what="The referenced event" error={eventQuery.error} />;
  }
  if (!eventQuery.data) return <Unresolved what="The referenced event" />;

  const ev = eventQuery.data;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px] text-[var(--color-fg-muted)]">
          {ev.timestamp ? fmtTimestamp(ev.timestamp) : "no timestamp"}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-[var(--color-fg-muted)]">
          {ev.source_file}
        </span>
        <OpenLink
          to={`/cases/${caseId}/timelines/${timelineId}?event_id=${encodeURIComponent(eventId)}`}
          label="Open in Explorer"
        />
      </div>
      <p className="break-words font-mono text-xs text-[var(--color-fg-primary)]">{ev.message}</p>
      {caption && <p className="text-xs italic text-[var(--color-fg-secondary)]">{caption}</p>}
    </div>
  );
}
