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
import { ChevronRight, AlertTriangle, Tag, MessageSquare, Trash2, ArrowUp, ArrowDown, ShieldCheck } from "lucide-react";
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
import { RETIRED_COLUMN_IDS } from "@/stores/ui";
import { cn } from "@/lib/cn";

const ROW_HEIGHT = 34; // px — compact forensic density (chips inline with message)
const OVERSCAN = 10;

interface Props {
  events: Event[];
  total: number;
  annotations: Map<string, Annotation[]>; // eventId → annotations
  selectedIds: Set<string>;
  caseId: string;
  timelineId: string;
  onToggleSelect: (id: string) => void;
  /** Toggles selection of all currently-loaded events. */
  onToggleSelectAll: () => void;
  expandedId: string | null;
  onExpand: (event: Event | null) => void;
  onLoadMore: () => void;
  isFetching: boolean;
  visibleColumns: string[];
  sortDir: "asc" | "desc";
  onSortToggle: () => void;
}

// ── Annotation column ────────────────────────────────────────────────────────

interface AnnotationCellProps {
  eventId: string;
  anns: Annotation[];
  caseId: string;
  sourceId: string;
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
            <span className="ml-0.5 text-[11px] font-mono">{userTags.length}</span>
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
                  <span className="flex-1 text-[11px] text-[var(--color-accent)] font-medium">{t.content}</span>
                  <Tooltip content={fmtTimestampFull(t.created_at)} side="top">
                    <span className="text-[11px] text-[var(--color-fg-muted)] whitespace-nowrap">
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
            <span className="ml-0.5 text-[11px] font-mono">{userComments.length}</span>
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
                    <p className="mt-1 text-[11px] text-[var(--color-fg-muted)]">
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
  const hasAnomaly = props.anns.some((a) => a.annotation_type === "anomaly");
  return (
    <div
      className="flex items-center gap-0.5"
      onClick={(e) => e.stopPropagation()}
    >
      {hasAnomaly ? (
        <Tooltip content="System-detected anomaly" side="top">
          <span className="p-1 text-[var(--color-anomaly)]">
            <AlertTriangle size={13} />
          </span>
        </Tooltip>
      ) : (
        <span className="p-1 w-[29px]" /> /* placeholder to keep layout stable */
      )}
      <NormalToggle {...props} />
      <TagPopover {...props} />
      <CommentPopover {...props} />
    </div>
  );
}

// ── Main grid ────────────────────────────────────────────────────────────────

export function EventGrid({
  events,
  total,
  annotations,
  selectedIds,
  caseId,
  timelineId,
  onToggleSelect,
  onToggleSelectAll,
  expandedId,
  onExpand,
  onLoadMore,
  isFetching,
  visibleColumns,
  sortDir,
  onSortToggle,
}: Props) {
  const parentRef = useRef<HTMLDivElement>(null);

  const columns = useMemo<ColumnDef<Event>[]>(() => {
    const cols: ColumnDef<Event>[] = [
      // Checkbox
      {
        id: "_select",
        size: 36,
        header: () => {
          const allChecked = events.length > 0 && selectedIds.size === events.length;
          const indeterminate = selectedIds.size > 0 && selectedIds.size < events.length;
          return (
            <input
              type="checkbox"
              ref={(el) => { if (el) el.indeterminate = indeterminate; }}
              checked={allChecked}
              onChange={onToggleSelectAll}
              className="h-3.5 w-3.5 cursor-pointer rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
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
            sourceId={row.original.source_id}
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
            Timestamp
            {sortDir === "desc" ? <ArrowDown size={10} /> : <ArrowUp size={10} />}
          </button>
        ),
        size: 170,
        cell: ({ row }) => (
          <span className="font-mono text-xs text-[var(--color-fg-secondary)]">
            {fmtTimestamp(row.original.timestamp)}
          </span>
        ),
      },
      artifact: {
        id: "artifact",
        header: "Artifact",
        size: 140,
        cell: ({ row }) => {
          const value = row.original.artifact || row.original.source_file || null;
          return (
            <span className="font-mono text-xs truncate text-[var(--color-info)]">
              {value ?? "—"}
            </span>
          );
        },
      },
      artifact_long: {
        id: "artifact_long",
        header: "Artifact Long",
        size: 180,
        cell: ({ row }) => (
          <span className="font-mono text-xs truncate text-[var(--color-info)]">
            {row.original.artifact_long ?? "—"}
          </span>
        ),
      },
      source_id: {
        id: "source_id",
        header: "Source ID",
        size: 160,
        cell: ({ row }) => (
          <span className="font-mono text-xs truncate text-[var(--color-fg-secondary)]">
            {row.original.source_id}
          </span>
        ),
      },
      timestamp_desc: {
        id: "timestamp_desc",
        header: "Time Desc",
        size: 140,
        cell: ({ row }) => (
          <span className="text-xs truncate text-[var(--color-fg-secondary)]">
            {row.original.timestamp_desc ?? "—"}
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
          return (
            <div className="flex items-center gap-1 min-w-0">
              <span className="text-xs text-[var(--color-fg-primary)] truncate leading-none shrink">
                {truncate(row.original.message, 300)}
              </span>
              {parserTags.slice(0, 3).map((t) => (
                <Badge key={t} variant="muted" className="text-[10px] py-0 px-1 leading-none shrink-0">
                  {t}
                </Badge>
              ))}
              {userTags.map((t) => (
                <Badge key={t.id} variant="accent" className="text-[10px] py-0 px-1 leading-none shrink-0">
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
  }, [visibleColumns, selectedIds, annotations, expandedId, onToggleSelect, onToggleSelectAll, events, caseId, timelineId, sortDir, onSortToggle]);

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
              className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)] select-none"
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
            const eventAnns = annotations.get(event.event_id) ?? [];
            const hasAnomaly = eventAnns.some(
              (a) => a.annotation_type === "anomaly",
            );
            const hasNormal = eventAnns.some(
              (a) => a.annotation_type === "normal" && a.origin === "user",
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
                    ? "bg-[var(--color-bg-active)] border-[var(--color-accent)]/40"
                    : isSelected
                      ? "bg-[var(--color-accent-dim)]"
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
          {events.length.toLocaleString()} of {total.toLocaleString()} events loaded
          {events.length >= total && total > 0 && " · all loaded"}
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
