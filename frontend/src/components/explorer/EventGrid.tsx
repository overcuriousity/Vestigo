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
import { useMemo, useRef, useCallback, useState } from "react";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, AlertTriangle, Tag, MessageSquare, Trash2 } from "lucide-react";
import type { Event, Annotation } from "@/api/types";
import { fmtTimestamp, fmtRelative, fmtTimestampFull } from "@/lib/time";
import { truncate } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/Popover";
import { Tooltip } from "@/components/ui/Tooltip";
import { useAnnotationMutations } from "@/hooks/useAnnotationMutations";
import { cn } from "@/lib/cn";

const ROW_HEIGHT = 52; // px — room for message + tag chips below
const OVERSCAN = 10;

interface Props {
  events: Event[];
  total: number;
  offset: number;
  annotations: Map<string, Annotation[]>; // eventId → annotations
  selectedIds: Set<string>;
  caseId: string;
  timelineId: string;
  onToggleSelect: (id: string) => void;
  expandedId: string | null;
  onExpand: (event: Event | null) => void;
  onLoadMore: () => void;
  isFetching: boolean;
  visibleColumns: string[];
}

// ── Annotation column ────────────────────────────────────────────────────────

interface AnnotationCellProps {
  eventId: string;
  anns: Annotation[];
  caseId: string;
  timelineId: string;
}

function TagPopover({
  eventId,
  anns,
  caseId,
  timelineId,
}: AnnotationCellProps) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const { add, remove } = useAnnotationMutations(caseId, timelineId);
  const userTags = anns.filter((a) => a.annotation_type === "tag" && a.origin === "user");

  function submit() {
    if (!value.trim()) return;
    add.mutate(
      { eventId, type: "tag", content: value.trim() },
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
            <span className="ml-0.5 text-[10px] font-mono">{userTags.length}</span>
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
                  className="group/tag flex items-center gap-1.5 rounded bg-[var(--color-accent-dim)] px-2 py-1"
                >
                  <Tag size={9} className="shrink-0 text-[var(--color-accent)]" />
                  <span className="flex-1 text-[10px] text-[var(--color-accent)] font-medium">{t.content}</span>
                  <Tooltip content={fmtTimestampFull(t.created_at)} side="top">
                    <span className="text-[9px] text-[var(--color-fg-muted)] whitespace-nowrap">
                      {t.created_by ? `${t.created_by} · ` : ""}{fmtRelative(t.created_at)}
                    </span>
                  </Tooltip>
                  <button
                    onClick={() => remove.mutate({ eventId, annotationId: t.id })}
                    className="opacity-0 group-hover/tag:opacity-100 text-[var(--color-fg-muted)] hover:text-[var(--color-danger)] transition-base"
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
              className="flex-1 h-7 text-xs"
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
  timelineId,
}: AnnotationCellProps) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const { add, remove } = useAnnotationMutations(caseId, timelineId);
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
            <span className="ml-0.5 text-[10px] font-mono">{userComments.length}</span>
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
                    <p className="mt-1 text-[9px] text-[var(--color-fg-muted)]">
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
              className="flex-1 h-7 text-xs"
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

/** Combined annotation column: outlier indicator + tag popover + comment popover. */
function AnnotationCell(props: AnnotationCellProps) {
  const hasOutlier = props.anns.some((a) => a.annotation_type === "outlier");
  return (
    <div
      className="flex items-center gap-0.5"
      onClick={(e) => e.stopPropagation()}
    >
      {hasOutlier ? (
        <Tooltip content="System-flagged outlier" side="top">
          <span className="p-1 text-[var(--color-outlier)]">
            <AlertTriangle size={13} />
          </span>
        </Tooltip>
      ) : (
        <span className="p-1 w-[29px]" /> /* placeholder to keep layout stable */
      )}
      <TagPopover {...props} />
      <CommentPopover {...props} />
    </div>
  );
}

// ── Main grid ────────────────────────────────────────────────────────────────

export function EventGrid({
  events,
  total,
  offset,
  annotations,
  selectedIds,
  caseId,
  timelineId,
  onToggleSelect,
  expandedId,
  onExpand,
  onLoadMore,
  isFetching,
  visibleColumns,
}: Props) {
  const parentRef = useRef<HTMLDivElement>(null);

  const columns = useMemo<ColumnDef<Event>[]>(() => {
    const cols: ColumnDef<Event>[] = [
      // Checkbox
      {
        id: "_select",
        size: 36,
        header: () => null,
        cell: ({ row }) => (
          <input
            type="checkbox"
            checked={selectedIds.has(row.original.event_id)}
            onChange={() => onToggleSelect(row.original.event_id)}
            className="h-3.5 w-3.5 cursor-pointer rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      // Annotation column — outlier indicator + tag/comment popovers
      {
        id: "_annotations",
        size: 88,
        header: () => null,
        cell: ({ row }) => (
          <AnnotationCell
            eventId={row.original.event_id}
            anns={annotations.get(row.original.event_id) ?? []}
            caseId={caseId}
            timelineId={timelineId}
          />
        ),
      },
    ];

    const colDefs: Record<string, ColumnDef<Event>> = {
      timestamp: {
        id: "timestamp",
        header: "Timestamp",
        size: 170,
        cell: ({ row }) => (
          <span className="font-mono text-xs text-[var(--color-fg-secondary)]">
            {fmtTimestamp(row.original.timestamp)}
          </span>
        ),
      },
      source: {
        id: "source",
        header: "Source",
        size: 140,
        cell: ({ row }) => (
          <span className="font-mono text-xs truncate text-[var(--color-info)]">
            {row.original.source ?? "—"}
          </span>
        ),
      },
      display_name: {
        id: "display_name",
        header: "Display Name",
        size: 160,
        cell: ({ row }) => (
          <span className="text-xs truncate text-[var(--color-fg-secondary)]">
            {row.original.display_name ?? "—"}
          </span>
        ),
      },
      message: {
        id: "message",
        header: "Message",
        size: 999, // flex
        cell: ({ row }) => {
          const anns = annotations.get(row.original.event_id) ?? [];
          const parserTags = row.original.tags;
          const userTags = anns.filter(
            (a) => a.annotation_type === "tag" && a.origin === "user",
          );
          const hasTags = parserTags.length > 0 || userTags.length > 0;
          return (
            <div className="flex flex-col justify-center gap-0.5 min-w-0 py-0.5">
              <span className="text-xs text-[var(--color-fg-primary)] truncate leading-tight">
                {truncate(row.original.message, 300)}
              </span>
              {hasTags && (
                <div className="flex flex-wrap gap-0.5">
                  {parserTags.slice(0, 5).map((t, i) => (
                    <Badge key={i} variant="muted" className="text-[9px] py-0 leading-tight">
                      {t}
                    </Badge>
                  ))}
                  {userTags.map((t) => (
                    <Badge key={t.id} variant="accent" className="text-[9px] py-0 leading-tight">
                      {t.content}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          );
        },
      },
      // Keep as optional column for power users; tags also appear inline in message
      tags: {
        id: "tags",
        header: "Parser Tags",
        size: 120,
        cell: ({ row }) =>
          row.original.tags.length > 0 ? (
            <span className="flex flex-wrap gap-0.5">
              {row.original.tags.slice(0, 3).map((t, i) => (
                <Badge key={i} variant="muted">
                  {t}
                </Badge>
              ))}
            </span>
          ) : null,
      },
    };

    for (const colId of visibleColumns) {
      // Retired column IDs that have been superseded
      if (colId === "tags" || colId === "_annotations") continue;
      const def = colDefs[colId];
      if (def) {
        cols.push(def);
      } else {
        // Dynamic attribute column
        cols.push({
          id: colId,
          header: colId,
          size: 160,
          cell: ({ row }) => (
            <span className="font-mono text-xs truncate text-[var(--color-fg-secondary)]">
              {row.original.attributes[colId] ?? "—"}
            </span>
          ),
        });
      }
    }

    // Expand toggle
    cols.push({
      id: "_expand",
      size: 32,
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
  }, [visibleColumns, selectedIds, annotations, expandedId, onToggleSelect, caseId, timelineId]);

  const table = useReactTable({
    data: events,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const rows = table.getRowModel().rows;

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();
  const totalHeight = rowVirtualizer.getTotalSize();

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el || isFetching) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
    if (nearBottom && events.length < total) {
      onLoadMore();
    }
  }, [isFetching, events.length, total, onLoadMore]);

  return (
    <div className="flex flex-1 min-w-0 flex-col h-full">
      {/* Header row */}
      <div className="flex shrink-0 border-b border-[var(--color-border)] bg-[var(--color-bg-surface)]">
        {table.getHeaderGroups().map((hg) =>
          hg.headers.map((h) => (
            <div
              key={h.id}
              className="px-2.5 py-2 text-[10px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)] select-none"
              style={{
                width: h.column.id === "message" ? undefined : h.getSize(),
                flex: h.column.id === "message" ? "1 1 0" : undefined,
              }}
            >
              {flexRender(h.column.columnDef.header, h.getContext())}
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
            const hasOutlier = (annotations.get(event.event_id) ?? []).some(
              (a) => a.annotation_type === "outlier",
            );

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
                    ? "bg-[var(--color-bg-active)] border-[var(--color-accent)] border-opacity-40"
                    : isSelected
                      ? "bg-[var(--color-accent-dim)]"
                      : "hover:bg-[var(--color-bg-hover)]",
                  hasOutlier && !isSelected && !isExpanded &&
                    "border-l-2 border-l-[var(--color-outlier)] border-l-opacity-50",
                )}
              >
                {row.getVisibleCells().map((cell) => (
                  <div
                    key={cell.id}
                    className="px-2 truncate"
                    style={{
                      width:
                        cell.column.id === "message"
                          ? undefined
                          : cell.column.getSize(),
                      flex: cell.column.id === "message" ? "1 1 0" : undefined,
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
          Showing {events.length.toLocaleString()} of {total.toLocaleString()} events
          {offset > 0 ? ` (offset ${offset.toLocaleString()})` : ""}
        </span>
        {events.length < total && (
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
}
