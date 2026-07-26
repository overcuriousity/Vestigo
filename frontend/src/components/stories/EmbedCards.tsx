/**
 * Read-only cards for a story's embed blocks (view / chart / event).
 *
 * Each card resolves its own reference through the existing API modules —
 * there is no story-specific data endpoint, so an embedded view is the same
 * query the Explorer runs and an embedded chart is drawn by the shared
 * `ChartCanvas`. A reference whose target was deleted renders an explicit
 * placeholder: the block stays, visibly unresolved, rather than vanishing.
 */
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink } from "lucide-react";
import { eventsApi } from "@/api/events";
import { savedChartsApi } from "@/api/viz";
import { viewsApi } from "@/api/views";
import type { StoryBlockOf } from "@/api/types";
import { ChartCanvas } from "@/components/viz/ChartCanvas";
import { parseStoredChartConfig } from "@/components/viz/lib/chartConfig";
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
          <div className="max-h-80 overflow-auto rounded border border-[var(--color-border)]">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-[var(--color-bg-elevated)] text-left text-[var(--color-fg-muted)]">
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
                {rows.map((ev) => (
                  <tr
                    key={ev.event_id}
                    className="border-t border-[var(--color-border)]/60 align-top"
                  >
                    <td className="whitespace-nowrap px-2 py-1 font-mono text-[var(--color-fg-muted)]">
                      {ev.timestamp ? fmtTimestamp(ev.timestamp) : "—"}
                    </td>
                    {columns ? (
                      columns.map((c) => (
                        <td key={c} className="px-2 py-1 text-[var(--color-fg-secondary)]">
                          {ev.attributes?.[c] ?? ""}
                        </td>
                      ))
                    ) : (
                      <td className="px-2 py-1 text-[var(--color-fg-secondary)]">{ev.message}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
  const config = chart ? parseStoredChartConfig(chart.config) : null;

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
          to={`/cases/${caseId}/timelines/${timelineId}/visualize`}
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
