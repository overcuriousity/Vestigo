/**
 * EventGrid — virtualized forensic event table.
 *
 * Uses TanStack Table for column management and TanStack Virtual for row
 * virtualization (handles 100k+ rows smoothly with offset pagination).
 *
 * Row layout (Timesketch-style):
 *   [☐] [⚠🏷💬] [timestamp] [source] [message + tag chips below] … [›]
 *
 * Parser tags and user annotation tags both appear as chips under the message.
 * The annotation column shows outlier/tag/comment icons that open edit popovers.
 */
import { useMemo, useRef, useCallback, useState, useEffect, useLayoutEffect, forwardRef, useImperativeHandle } from "react";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, AlertTriangle, Tag, MessageSquare, Trash2, ArrowUp, ArrowDown, ShieldCheck } from "lucide-react";
import type { AnomalyMarker, Event, Annotation } from "@/api/types";
import { fmtTimestamp, fmtRelative, fmtTimestampFull } from "@/lib/time";
import { truncate } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/Popover";
import { Tooltip } from "@/components/ui/Tooltip";
import { useAnnotationMutations } from "@/hooks/useAnnotationMutations";
import { RETIRED_COLUMN_IDS, useUiStore } from "@/stores/ui";
import { cn } from "@/lib/cn";

// Keep in sync with --grid-row-height in index.css.
const ROW_HEIGHT_BY_DENSITY = { comfortable: 42, compact: 34 } as const;
const OVERSCAN = 10;

interface Props {
  events: Event[];
  /** Total matching event count, or `null` when unknown (e.g. after a jump-to-time seek). */
  total: number | null;
  annotations: Map<string, Annotation[]>; // eventId → annotations
  selectedIds: Set<string>;
  caseId: string;
  onToggleSelect: (id: string) => void;
  /** Toggles selection of all currently-loaded events. */
  onToggleSelectAll: () => void;
  expandedId: string | null;
  onExpand: (event: Event | null) => void;
  onLoadMore: () => void;
  /** Fetches the page immediately preceding the currently-loaded window. */
  onLoadEarlier: () => void;
  /** Whether an earlier (older/newer, depending on sort) page is known to exist. */
  hasPreviousPage: boolean;
  /** Whether a further page is known to exist — independent of `total`, which can be unknown. */
  hasNextPage: boolean;
  isFetching: boolean;
  visibleColumns: string[];
  sortDir: "asc" | "desc";
  onSortToggle: () => void;
  /** Active (not-yet-tagged) analysis findings, keyed by event ID. */
  liveAnomalies?: Map<string, AnomalyMarker[]>;
  /** Called with the timestamp of the topmost visible row whenever scroll position changes. */
  onVisibleTimestampChange?: (ts: string | null) => void;
  /** Soft visual highlight for a time window (e.g. a Frequency finding's anomalous window). */
  highlightRange?: { start: string; end: string } | null;
}

export interface EventGridHandle {
  /** Scrolls to the row closest to `ts` — prefers an exact `eventId` match when loaded. */
  scrollToTimestamp: (ts: string, eventId?: string) => void;
  scrollToIndex: (index: number) => void;
}

// ── Annotation column ────────────────────────────────────────────────────────

interface AnnotationCellProps {
  eventId: string;
  anns: Annotation[];
  caseId: string;
  sourceId: string;
  /** Active, not-yet-tagged findings for this event. */
  liveFindings?: AnomalyMarker[];
}

function TagPopover({
  eventId,
  anns,
  caseId,
  sourceId,
}: AnnotationCellProps) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const { add, remove } = useAnnotationMutations(caseId, sourceId);
  const userTags = anns.filter((a) => a.annotation_type === "tag" && a.origin === "user");

  function submit() {
    const tag = value.trim();
    if (!tag) return;
    if (userTags.some((t) => t.content === tag)) { setValue(""); return; }
    add.mutate(
      { eventId, type: "tag", content: tag },
      { onSuccess: () => setValue("") },
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip content={userTags.length > 0 ? `${userTags.length} tag(s) — click to edit` : "Add tag"} side="top">
        <PopoverTrigger
          onClick={(e: React.MouseEvent) => e.stopPropagation()}
          className={cn(
            "rounded p-1 transition-base",
            userTags.length > 0
              ? "text-[var(--color-accent)]"
              : "text-[var(--color-fg-muted)] opacity-0 group-hover:opacity-100 hover:text-[var(--color-accent)]",
          )}
        >
          <Tag size={13} />
          {userTags.length > 0 && (
            <span className="ml-0.5 text-xs font-mono">{userTags.length}</span>
          )}
        </PopoverTrigger>
      </Tooltip>
      <PopoverContent side="bottom" align="start" className="w-60 p-2.5">
        <div onClick={(e) => e.stopPropagation()}>
          {userTags.length > 0 && (
            <div className="mb-2 space-y-1">
              {userTags.map((t) => (
                <div
                  key={t.id}
                  className="group/tag flex items-center gap-1 min-w-0 rounded bg-[var(--color-accent-dim)] px-2 py-1"
                >
                  <Tag size={9} className="shrink-0 text-[var(--color-accent)]" />
                  <Tooltip content={`${t.created_by ? t.created_by + " · " : ""}${fmtRelative(t.created_at)} — ${fmtTimestampFull(t.created_at)}`} side="top">
                    <span className="flex-1 min-w-0 truncate text-xs text-[var(--color-accent)] font-medium cursor-default">
                      {t.content}
                    </span>
                  </Tooltip>
                  <button
                    onClick={() => remove.mutate({ eventId, annotationId: t.id })}
                    className="shrink-0 opacity-0 group-hover/tag:opacity-100 text-[var(--color-fg-muted)] hover:text-[var(--color-danger)] transition-base"
                  >
                    <Trash2 size={9} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-1.5">
            <Input
              autoFocus
              placeholder="new tag…"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
                if (e.key === "Escape") { setOpen(false); setValue(""); }
              }}
              className="flex-1"
            />
            <Button
              variant="accent"
              size="sm"
              disabled={!value.trim() || add.isPending}
              onClick={submit}
            >
              {add.isPending ? <Spinner size={11} /> : <Tag size={11} />}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function CommentPopover({
  eventId,
  anns,
  caseId,
  sourceId,
}: AnnotationCellProps) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const { add, remove } = useAnnotationMutations(caseId, sourceId);
  const userComments = anns.filter((a) => a.annotation_type === "comment" && a.origin === "user");

  function submit() {
    if (!value.trim()) return;
    add.mutate(
      { eventId, type: "comment", content: value.trim() },
      { onSuccess: () => setValue("") },
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip content={userComments.length > 0 ? `${userComments.length} comment(s) — click to view` : "Add comment"} side="top">
        <PopoverTrigger
          onClick={(e: React.MouseEvent) => e.stopPropagation()}
          className={cn(
            "rounded p-1 transition-base",
            userComments.length > 0
              ? "text-[var(--color-info)]"
              : "text-[var(--color-fg-muted)] opacity-0 group-hover:opacity-100 hover:text-[var(--color-info)]",
          )}
        >
          <MessageSquare size={13} />
          {userComments.length > 0 && (
            <span className="ml-0.5 text-xs font-mono">{userComments.length}</span>
          )}
        </PopoverTrigger>
      </Tooltip>
      <PopoverContent side="bottom" align="start" className="w-72 p-2.5">
        <div onClick={(e) => e.stopPropagation()}>
          {userComments.length > 0 && (
            <div className="mb-2 space-y-1">
              {userComments.map((c) => (
                <div
                  key={c.id}
                  className="group/comment rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1.5 text-xs"
                >
                  <div className="flex items-start gap-1.5">
                    <span className="flex-1 text-[var(--color-fg-primary)] break-words leading-snug">{c.content}</span>
                    <button
                      onClick={() => remove.mutate({ eventId, annotationId: c.id })}
                      className="shrink-0 opacity-0 group-hover/comment:opacity-100 text-[var(--color-fg-muted)] hover:text-[var(--color-danger)] transition-base mt-0.5"
                    >
                      <Trash2 size={10} />
                    </button>
                  </div>
                  <Tooltip content={fmtTimestampFull(c.created_at)} side="bottom">
                    <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                      {c.created_by ?? "anonymous"} · {fmtRelative(c.created_at)}
                    </p>
                  </Tooltip>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-1.5">
            <Input
              autoFocus
              placeholder="new comment…"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
                if (e.key === "Escape") { setOpen(false); setValue(""); }
              }}
              className="flex-1"
            />
            <Button
              variant="accent"
              size="sm"
              disabled={!value.trim() || add.isPending}
              onClick={submit}
            >
              {add.isPending ? <Spinner size={11} /> : <MessageSquare size={11} />}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Mark-normal toggle button: adds/removes a "normal" user annotation. */
function NormalToggle({ eventId, anns, caseId, sourceId }: AnnotationCellProps) {
  const { add, remove } = useAnnotationMutations(caseId, sourceId);
  const normalAnn = anns.find((a) => a.annotation_type === "normal" && a.origin === "user");
  const isNormal = !!normalAnn;

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isNormal && normalAnn) {
      remove.mutate({ eventId, annotationId: normalAnn.id });
    } else {
      add.mutate({ eventId, type: "normal", content: "normal operation" });
    }
  };

  return (
    <Tooltip
      content={isNormal ? "Marked Normal — click to unmark" : "Mark as Normal operation"}
      side="top"
    >
      <button
        onClick={handleClick}
        className={cn(
          "rounded p-1 transition-base",
          isNormal
            ? "text-[var(--color-success)]"
            : "text-[var(--color-fg-muted)] opacity-0 group-hover:opacity-100 hover:text-[var(--color-success)]",
        )}
      >
        <ShieldCheck size={13} />
      </button>
    </Tooltip>
  );
}

/** Combined annotation column: anomaly indicator + normal toggle + tag popover + comment popover. */
function AnnotationCell(props: AnnotationCellProps) {
  const persistedAnomalies = props.anns.filter((a) => a.annotation_type === "anomaly");
  // Once tagged, the persisted annotation is the durable record — suppress
  // the live (still-active, not-yet-saved) copy so the tooltip doesn't list
  // the same finding twice.
  const liveFindings = persistedAnomalies.length > 0 ? [] : (props.liveFindings ?? []);
  const hasAnomaly = persistedAnomalies.length > 0 || liveFindings.length > 0;
  const tooltipLines = [
    ...persistedAnomalies.map((a) => a.content),
    ...liveFindings.map((f) => `${f.detail} (not yet tagged)`),
  ];
  return (
    <div
      className="flex items-center gap-0.5"
      onClick={(e) => e.stopPropagation()}
    >
      {hasAnomaly ? (
        <Tooltip content={tooltipLines.join(" · ")} side="top">
          <span className="p-1 text-[var(--color-anomaly)]">
            <AlertTriangle size={13} />
          </span>
        </Tooltip>
      ) : (
        <span className="p-1 w-[13px]" /> /* placeholder matching the icon's own box, to keep layout stable */
      )}
      <NormalToggle {...props} />
      <TagPopover {...props} />
      <CommentPopover {...props} />
    </div>
  );
}

// ── Main grid ────────────────────────────────────────────────────────────────

export const EventGrid = forwardRef<EventGridHandle, Props>(function EventGrid({
  events,
  total,
  annotations,
  selectedIds,
  caseId,
  onToggleSelect,
  onToggleSelectAll,
  expandedId,
  onExpand,
  onLoadMore,
  onLoadEarlier,
  hasPreviousPage,
  hasNextPage,
  isFetching,
  visibleColumns,
  sortDir,
  onSortToggle,
  liveAnomalies,
  onVisibleTimestampChange,
  highlightRange,
}, ref) {
  const parentRef = useRef<HTMLDivElement>(null);
  const density = useUiStore((s) => s.density);
  const ROW_HEIGHT = ROW_HEIGHT_BY_DENSITY[density];

  const columns = useMemo<ColumnDef<Event>[]>(() => {
    const cols: ColumnDef<Event>[] = [
      // Checkbox
      {
        id: "_select",
        size: 44,
        enableResizing: false,
        header: () => {
          const allChecked = events.length > 0 && selectedIds.size === events.length;
          const indeterminate = selectedIds.size > 0 && selectedIds.size < events.length;
          return (
            <input
              type="checkbox"
              ref={(el) => { if (el) el.indeterminate = indeterminate; }}
              checked={allChecked}
              onChange={onToggleSelectAll}
              className="h-4 w-4 cursor-pointer rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
              onClick={(e) => e.stopPropagation()}
              title={allChecked ? "Deselect all" : "Select all loaded"}
            />
          );
        },
        cell: ({ row }) => (
          <input
            type="checkbox"
            checked={selectedIds.has(row.original.event_id)}
            onChange={() => onToggleSelect(row.original.event_id)}
            className="h-4 w-4 cursor-pointer rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      // Annotation column — outlier indicator + tag/comment popovers
      {
        id: "_annotations",
        size: 104,
        enableResizing: false,
        header: () => null,
        cell: ({ row }) => (
          <AnnotationCell
            eventId={row.original.event_id}
            anns={annotations.get(row.original.event_id) ?? []}
            caseId={caseId}
            sourceId={row.original.source_id}
            liveFindings={liveAnomalies?.get(row.original.event_id)}
          />
        ),
      },
    ];

    const colDefs: Record<string, ColumnDef<Event>> = {
      timestamp: {
        id: "timestamp",
        header: () => (
          <button
            onClick={onSortToggle}
            className="flex items-center gap-1 hover:text-[var(--color-fg-primary)] transition-base"
            title={sortDir === "desc" ? "Newest first — click for oldest first" : "Oldest first — click for newest first"}
          >
            Timestamp (UTC)
            {sortDir === "desc" ? <ArrowDown size={10} /> : <ArrowUp size={10} />}
          </button>
        ),
        size: 170,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) => (
          <span className="font-mono text-sm leading-snug text-[var(--color-fg-secondary)]">
            {fmtTimestamp(row.original.timestamp)}
          </span>
        ),
      },
      artifact: {
        id: "artifact",
        header: "Artifact",
        size: 140,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) => {
          const value = row.original.artifact || row.original.source_file || null;
          return (
            <span className="font-mono text-sm leading-snug truncate text-[var(--color-info)]">
              {value ?? "—"}
            </span>
          );
        },
      },
      artifact_long: {
        id: "artifact_long",
        header: "Artifact Long",
        size: 180,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) => (
          <span className="font-mono text-sm leading-snug truncate text-[var(--color-info)]">
            {row.original.artifact_long ?? "—"}
          </span>
        ),
      },
      source_id: {
        id: "source_id",
        header: "Source ID",
        size: 160,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) => (
          <span className="font-mono text-sm leading-snug truncate text-[var(--color-fg-secondary)]">
            {row.original.source_id}
          </span>
        ),
      },
      timestamp_desc: {
        id: "timestamp_desc",
        header: "Time Desc",
        size: 140,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) => (
          <span className="text-sm leading-snug truncate text-[var(--color-fg-secondary)]">
            {row.original.timestamp_desc ?? "—"}
          </span>
        ),
      },
      display_name: {
        id: "display_name",
        header: "Display Name",
        size: 160,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) => (
          <span className="text-sm leading-snug truncate text-[var(--color-fg-secondary)]">
            {row.original.display_name ?? "—"}
          </span>
        ),
      },
      message: {
        id: "message",
        header: "Message",
        size: 999, // flex
        enableResizing: false,
        cell: ({ row }) => {
          const anns = annotations.get(row.original.event_id) ?? [];
          const parserTags = row.original.tags;
          const userTags = anns.filter(
            (a) => a.annotation_type === "tag" && a.origin === "user",
          );
          return (
            <div className="flex items-center gap-1 min-w-0">
              <span className="text-sm text-[var(--color-fg-primary)] truncate leading-snug shrink">
                {truncate(row.original.message, 300)}
              </span>
              {parserTags.slice(0, 3).map((t) => (
                <Badge key={t} variant="muted" className="text-xs py-0.5 px-1.5 shrink-0">
                  {t}
                </Badge>
              ))}
              {userTags.map((t) => (
                <Badge key={t.id} variant="accent" className="text-xs py-0.5 px-1.5 shrink-0">
                  {t.content}
                </Badge>
              ))}
            </div>
          );
        },
      },
      // Keep as optional column for power users; tags also appear inline in message
      tags: {
        id: "tags",
        header: "Parser Tags",
        size: 120,
        minSize: 60,
        maxSize: 600,
        cell: ({ row }) =>
          (row.original.tags ?? []).length > 0 ? (
            <span className="flex flex-wrap gap-0.5">
              {row.original.tags.slice(0, 3).map((t) => (
                <Badge key={t} variant="muted">
                  {t}
                </Badge>
              ))}
            </span>
          ) : null,
      },
    };

    for (let colId of visibleColumns) {
      if (colId === "tags" || colId === "_annotations") continue;
      colId = RETIRED_COLUMN_IDS[colId] ?? colId;
      const def = colDefs[colId];
      if (def) {
        cols.push(def);
      } else {
        // Dynamic attribute column
        cols.push({
          id: colId,
          header: colId,
          size: 160,
          minSize: 60,
          maxSize: 600,
          cell: ({ row }) => (
            <span className="font-mono text-sm leading-snug truncate text-[var(--color-fg-secondary)]">
              {row.original.attributes[colId] ?? "—"}
            </span>
          ),
        });
      }
    }

    // Expand toggle
    cols.push({
      id: "_expand",
      size: 38,
      enableResizing: false,
      header: () => null,
      cell: ({ row }) => (
        <ChevronRight
          size={13}
          className={cn(
            "text-[var(--color-fg-muted)] transition-transform duration-150",
            expandedId === row.original.event_id && "rotate-90",
          )}
        />
      ),
    });

    return cols;
  }, [visibleColumns, selectedIds, annotations, expandedId, onToggleSelect, onToggleSelectAll, events, caseId, sortDir, onSortToggle, liveAnomalies]);

  const columnWidths = useUiStore((s) => s.columnWidths);
  const setColumnWidth = useUiStore((s) => s.setColumnWidth);
  // Seeded once from the persisted store; live-updated during drags via
  // onColumnSizingChange, then flushed back to the store on drag-end below.
  const [columnSizing, setColumnSizing] = useState<Record<string, number>>(
    () => ({ ...columnWidths }),
  );

  const table = useReactTable({
    data: events,
    columns,
    getCoreRowModel: getCoreRowModel(),
    enableColumnResizing: true,
    columnResizeMode: "onChange",
    state: { columnSizing },
    onColumnSizingChange: setColumnSizing,
  });

  // Persist a column's width once per drag gesture (on release), not per
  // pixel of movement, to avoid hammering localStorage during onChange.
  const prevResizingColRef = useRef<string | false>(false);
  const resizingColumnId = table.getState().columnSizingInfo.isResizingColumn;
  useEffect(() => {
    const wasResizing = prevResizingColRef.current;
    if (wasResizing && !resizingColumnId) {
      const finalWidth = columnSizing[wasResizing];
      if (finalWidth != null) setColumnWidth(wasResizing, finalWidth);
    }
    prevResizingColRef.current = resizingColumnId;
  }, [resizingColumnId, columnSizing, setColumnWidth]);

  const rows = table.getRowModel().rows;

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();
  const totalHeight = rowVirtualizer.getTotalSize();

  // Report the timestamp of the topmost visible row so the histogram can show
  // a "current position" indicator. Guarded against redundant calls.
  const lastReportedTsRef = useRef<string | null>(null);
  const reportVisibleTimestamp = useCallback(() => {
    if (!onVisibleTimestampChange) return;
    const el = parentRef.current;
    const ts =
      el && rows.length > 0
        ? (rows[Math.min(rows.length - 1, Math.max(0, Math.floor(el.scrollTop / ROW_HEIGHT)))]
            ?.original.timestamp ?? null)
        : null;
    if (ts !== lastReportedTsRef.current) {
      lastReportedTsRef.current = ts;
      onVisibleTimestampChange(ts);
    }
  }, [rows, onVisibleTimestampChange]);

  useEffect(() => {
    reportVisibleTimestamp();
  }, [reportVisibleTimestamp]);

  // Prepending earlier events shifts every existing row's index by the
  // prepended count — the virtualizer's scrollOffset doesn't auto-adjust,
  // which would otherwise cause a visible jump. Capture an anchor right
  // before requesting the earlier page, then correct scrollTop once the new
  // rows land (row height is fixed, so this is exact, cheap arithmetic).
  const prependAnchorRef = useRef<{ scrollTop: number; firstEventId: string } | null>(null);

  const handleLoadEarlier = useCallback(() => {
    const el = parentRef.current;
    const firstEventId = events[0]?.event_id;
    if (el && firstEventId) {
      prependAnchorRef.current = { scrollTop: el.scrollTop, firstEventId };
    }
    onLoadEarlier();
  }, [events, onLoadEarlier]);

  useLayoutEffect(() => {
    const anchor = prependAnchorRef.current;
    const el = parentRef.current;
    if (!anchor || !el) return;
    const newIndex = events.findIndex((e) => e.event_id === anchor.firstEventId);
    if (newIndex > 0) {
      el.scrollTop = anchor.scrollTop + newIndex * ROW_HEIGHT;
    }
    prependAnchorRef.current = null;
  }, [events]);

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (el && !isFetching) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
      if (nearBottom) {
        onLoadMore();
      }
      const nearTop = el.scrollTop < 200;
      if (nearTop && hasPreviousPage) {
        handleLoadEarlier();
      }
    }
    reportVisibleTimestamp();
  }, [isFetching, onLoadMore, hasPreviousPage, handleLoadEarlier, reportVisibleTimestamp]);

  useImperativeHandle(
    ref,
    () => ({
      scrollToIndex: (index: number) => {
        rowVirtualizer.scrollToIndex(index, { align: "center" });
      },
      scrollToTimestamp: (ts: string, eventId?: string) => {
        if (events.length === 0) return;
        if (eventId) {
          const exact = events.findIndex((e) => e.event_id === eventId);
          if (exact >= 0) {
            rowVirtualizer.scrollToIndex(exact, { align: "center" });
            return;
          }
        }
        // Events are sorted by timestamp in `sortDir` order — binary-search
        // for the first row at or past the target.
        const targetTime = new Date(ts).getTime();
        let lo = 0;
        let hi = events.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          const midTime = new Date(events[mid].timestamp ?? 0).getTime();
          const pastTarget = sortDir === "desc" ? midTime <= targetTime : midTime >= targetTime;
          if (pastTarget) hi = mid;
          else lo = mid + 1;
        }
        rowVirtualizer.scrollToIndex(lo, { align: "center" });
      },
    }),
    [events, sortDir, rowVirtualizer],
  );

  return (
    <div className="flex flex-1 min-w-0 flex-col h-full">
      {/* Header row */}
      <div className="flex shrink-0 border-b border-[var(--color-border)] bg-[var(--color-bg-surface)]">
        {table.getHeaderGroups().map((hg) =>
          hg.headers.map((h) => (
            <div
              key={h.id}
              className="relative px-[var(--grid-cell-x)] py-2 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)] select-none"
              style={{
                width: h.column.id === "message" ? undefined : h.getSize(),
                flex: h.column.id === "message" ? "1 1 0" : `0 0 ${h.getSize()}px`,
              }}
            >
              {flexRender(h.column.columnDef.header, h.getContext())}
              {h.column.getCanResize() && (
                <div
                  onMouseDown={(e) => { e.stopPropagation(); h.getResizeHandler()(e); }}
                  onTouchStart={(e) => { e.stopPropagation(); h.getResizeHandler()(e); }}
                  onClick={(e) => e.stopPropagation()}
                  className="absolute right-0 top-0 h-full w-1 cursor-col-resize select-none touch-none opacity-0 hover:opacity-100 hover:bg-[var(--color-accent)] transition-opacity z-10"
                  style={{ marginRight: -2 }}
                />
              )}
            </div>
          )),
        )}
      </div>

      {/* Virtualized body */}
      <div
        ref={parentRef}
        className="flex-1 overflow-auto"
        onScroll={handleScroll}
      >
        <div style={{ height: totalHeight, position: "relative" }}>
          {virtualItems.map((vItem) => {
            const row = rows[vItem.index];
            const event = row.original;
            const isExpanded = expandedId === event.event_id;
            const isSelected = selectedIds.has(event.event_id);
            const eventAnns = annotations.get(event.event_id) ?? [];
            const hasAnomaly =
              eventAnns.some((a) => a.annotation_type === "anomaly") ||
              (liveAnomalies?.get(event.event_id)?.length ?? 0) > 0;
            const hasNormal = eventAnns.some(
              (a) => a.annotation_type === "normal" && a.origin === "user",
            );
            const inHighlightRange = (() => {
              if (!highlightRange || !event.timestamp) return false;
              const t = new Date(event.timestamp).getTime();
              const start = new Date(highlightRange.start).getTime();
              const end = new Date(highlightRange.end).getTime();
              return (
                Number.isFinite(t) &&
                Number.isFinite(start) &&
                Number.isFinite(end) &&
                t >= start &&
                t <= end
              );
            })();

            return (
              <div
                key={vItem.key}
                style={{
                  position: "absolute",
                  top: vItem.start,
                  left: 0,
                  right: 0,
                  height: ROW_HEIGHT,
                }}
                onClick={() => onExpand(isExpanded ? null : event)}
                className={cn(
                  "flex items-center border-b border-[var(--color-border-subtle)] cursor-pointer transition-base group",
                  isExpanded
                    ? "bg-[var(--color-bg-active)] border-[var(--color-accent)]/40"
                    : isSelected
                      ? "bg-[var(--color-accent-dim)]"
                      : inHighlightRange
                        ? "bg-[var(--color-accent)]/10 hover:bg-[var(--color-bg-hover)]"
                        : "hover:bg-[var(--color-bg-hover)]",
                  hasAnomaly && !isSelected && !isExpanded &&
                    "border-l-2 border-l-[var(--color-anomaly)]/50",
                  hasNormal && !hasAnomaly && !isSelected && !isExpanded &&
                    "border-l-2 border-l-[var(--color-success)]/50",
                )}
              >
                {row.getVisibleCells().map((cell) => (
                  <div
                    key={cell.id}
                    className="px-[var(--grid-cell-x)] truncate"
                    style={{
                      width:
                        cell.column.id === "message"
                          ? undefined
                          : cell.column.getSize(),
                      // Fixed-width columns must never shrink below their set size —
                      // flex's default flex-shrink:1 would otherwise squeeze icon
                      // buttons into each other on a narrow viewport instead of the
                      // row simply overflowing (handled by the container's scroll).
                      flex:
                        cell.column.id === "message"
                          ? "1 1 0"
                          : `0 0 ${cell.column.getSize()}px`,
                      minWidth: 0,
                      // allow message column to overflow for flex layout
                      overflow: cell.column.id === "message" ? "hidden" : undefined,
                    }}
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>

      {/* Footer */}
      <div className="flex shrink-0 items-center justify-between border-t border-[var(--color-border)] bg-[var(--color-bg-surface)] px-4 py-1.5 text-xs text-[var(--color-fg-muted)]">
        <span>
          {total != null
            ? `${events.length.toLocaleString()} of ${total.toLocaleString()} events loaded`
            : `${events.length.toLocaleString()} events loaded`}
          {!hasNextPage && " · all loaded"}
        </span>
        {hasNextPage && (
          <button
            className="text-[var(--color-accent)] hover:underline transition-base"
            onClick={onLoadMore}
          >
            Load more
          </button>
        )}
      </div>
    </div>
  );
});
