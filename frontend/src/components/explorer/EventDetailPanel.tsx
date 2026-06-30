import { useState, useRef, useCallback, useEffect } from "react";
import { X, Copy, Search, Filter, FilterX, Tag, MessageSquare, Trash2, Plus, Clock, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { fmtTimestampFull, fmtRelative } from "@/lib/time";
import { truncateHash } from "@/lib/format";
import { Tooltip } from "@/components/ui/Tooltip";
import { useAnnotationMutations } from "@/hooks/useAnnotationMutations";
import { useUiStore } from "@/stores/ui";
import { TagInput } from "@/components/explorer/TagInput";
import type { Event, Annotation } from "@/api/types";

interface Props {
  event: Event;
  annotations: Annotation[];
  caseId: string;
  sourceId: string;
  onClose: () => void;
  onFindSimilar: (event: Event) => void;
  /** Called when the user clicks filter-in or filter-out on a field row. */
  onAddFilter: (fieldKey: string, value: string, include: boolean) => void;
  /** Existing annotation-tag labels for autocomplete. */
  tagSuggestions?: string[];
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation();
        await navigator.clipboard.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
      title="Copy value"
    >
      <Copy size={11} className={copied ? "text-[var(--color-success)]" : ""} />
    </button>
  );
}

/**
 * A single attribute row with filter-in / filter-out / copy actions.
 *
 * filterKey: the field name sent to the backend filters/exclusions param.
 *   Pass null for rows that are display-only (provenance, timestamps).
 */
function FieldRow({
  label,
  value,
  mono = false,
  filterKey,
  onAddFilter,
}: {
  label: string;
  value: string | null | undefined;
  mono?: boolean;
  filterKey?: string | null;
  onAddFilter?: (fieldKey: string, value: string, include: boolean) => void;
}) {
  if (!value) return null;
  const canFilter = !!filterKey && !!onAddFilter;

  return (
    <div className="group flex items-start gap-1.5 py-1.5 border-b border-[var(--color-border-subtle)] hover:bg-[var(--color-bg-hover)] -mx-2 px-2 rounded-sm transition-base">
      <span className="w-32 shrink-0 text-xs text-[var(--color-fg-secondary)] pt-0.5 select-none">
        {label}
      </span>
      <span
        className={`flex-1 min-w-0 break-all text-xs text-[var(--color-fg-primary)] ${mono ? "font-mono" : ""}`}
      >
        {value}
      </span>

      {/* Action buttons — visible on row hover */}
      <div className="flex items-center gap-0.5 shrink-0 opacity-0 group-hover:opacity-100 transition-base">
        {canFilter && (
          <>
            <Tooltip content={`Filter IN: ${label} = ${value}`} side="top">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onAddFilter!(filterKey!, value, true);
                }}
                className="rounded p-0.5 text-[var(--color-info)] hover:bg-[var(--color-info-dim)] transition-base"
              >
                <Filter size={11} />
              </button>
            </Tooltip>
            <Tooltip content={`Filter OUT: ${label} ≠ ${value}`} side="top">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onAddFilter!(filterKey!, value, false);
                }}
                className="rounded p-0.5 text-[var(--color-danger)] hover:bg-[var(--color-danger-dim)] transition-base"
              >
                <FilterX size={11} />
              </button>
            </Tooltip>
          </>
        )}
        <CopyButton value={value} />
      </div>
    </div>
  );
}

/** Button to toggle "normal operation" annotation on an event. */
function NormalToggleButton({
  event,
  annotations,
  add,
  remove,
}: {
  event: Event;
  annotations: Annotation[];
  add: ReturnType<typeof useAnnotationMutations>["add"];
  remove: ReturnType<typeof useAnnotationMutations>["remove"];
}) {
  const normalAnn = annotations.find(
    (a) => a.annotation_type === "normal" && a.origin === "user",
  );
  const isNormal = !!normalAnn;

  const handleClick = () => {
    if (isNormal && normalAnn) {
      remove.mutate({ eventId: event.event_id, annotationId: normalAnn.id });
    } else {
      add.mutate({ eventId: event.event_id, type: "normal", content: "normal operation" });
    }
  };

  return (
    <Button
      variant={isNormal ? "accent" : "outline"}
      size="sm"
      onClick={handleClick}
      disabled={add.isPending || remove.isPending}
    >
      <ShieldCheck size={11} />
      {isNormal ? "Normal ✓" : "Mark Normal"}
    </Button>
  );
}

/** Inline add-annotation form (tag or comment). */
function AddAnnotationForm({
  type,
  onSubmit,
  onCancel,
  isPending,
  suggestions = [],
}: {
  type: "tag" | "comment";
  onSubmit: (content: string) => void;
  onCancel: () => void;
  isPending: boolean;
  suggestions?: string[];
}) {
  const [value, setValue] = useState("");
  return (
    <div className="flex items-center gap-1.5 mt-2">
      {type === "tag" ? (
        <TagInput
          autoFocus
          value={value}
          onChange={setValue}
          onSubmit={onSubmit}
          onCancel={onCancel}
          suggestions={suggestions}
          isPending={isPending}
          className="flex-1"
        />
      ) : (
        <Input
          autoFocus
          placeholder="your comment…"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && value.trim()) onSubmit(value.trim());
            if (e.key === "Escape") onCancel();
          }}
          className="flex-1 h-7 text-xs"
        />
      )}
      {type === "comment" && (
        <>
          <Button
            variant="accent"
            size="sm"
            disabled={!value.trim() || isPending}
            onClick={() => value.trim() && onSubmit(value.trim())}
          >
            {isPending ? <Spinner size={12} /> : "Add"}
          </Button>
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        </>
      )}
    </div>
  );
}

export function EventDetailPanel({
  event,
  annotations,
  caseId,
  sourceId,
  onClose,
  onFindSimilar,
  onAddFilter,
  tagSuggestions = [],
}: Props) {
  const [addMode, setAddMode] = useState<"tag" | "comment" | null>(null);
  const { add, remove } = useAnnotationMutations(caseId, sourceId);

  // ── Resize drag ────────────────────────────────────────────────────────
  const { detailPanelWidth, setDetailPanelWidth } = useUiStore();
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragState.current = { startX: e.clientX, startWidth: detailPanelWidth };
  }, [detailPanelWidth]);

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragState.current) return;
      const delta = dragState.current.startX - e.clientX;
      const newWidth = Math.max(280, Math.min(800, dragState.current.startWidth + delta));
      setDetailPanelWidth(newWidth);
    }
    function onMouseUp() {
      dragState.current = null;
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [setDetailPanelWidth]);

  const userAnnotations = annotations.filter((a) => a.origin === "user");
  const systemAnnotations = annotations.filter((a) => a.origin === "system");

  function handleAdd(content: string) {
    if (!addMode) return;
    add.mutate(
      { eventId: event.event_id, type: addMode, content },
      { onSuccess: () => setAddMode(null) },
    );
  }

  return (
    <div
      className="relative flex h-full flex-col border-l border-[var(--color-border)] bg-[var(--color-bg-surface)] shrink-0"
      style={{ width: detailPanelWidth }}
    >
      {/* Drag handle — left edge */}
      <div
        onMouseDown={onDragStart}
        className="absolute left-0 top-0 h-full w-1 cursor-col-resize opacity-0 hover:opacity-100 hover:bg-[var(--color-accent)] transition-opacity z-10"
        style={{ marginLeft: -2 }}
      />
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-3 py-2">
        <h3 className="flex-1 text-sm font-semibold text-[var(--color-fg-primary)]">
          Event Detail
        </h3>
        <Tooltip content="Find similar events (vector search)">
          <Button variant="ghost" size="icon" onClick={() => onFindSimilar(event)}>
            <Search size={14} />
          </Button>
        </Tooltip>
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X size={14} />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {/* Message — filterable on click */}
        <div className="mb-3 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3">
          <div className="flex items-center justify-between mb-1">
            <p className="text-xs text-[var(--color-fg-secondary)]">Message</p>
            {event.message && (
              <div className="flex items-center gap-0.5">
                <Tooltip content="Filter IN: message contains this text">
                  <button
                    onClick={() => onAddFilter("q", event.message, true)}
                    className="rounded p-0.5 text-[var(--color-info)] hover:bg-[var(--color-info-dim)] transition-base"
                  >
                    <Filter size={11} />
                  </button>
                </Tooltip>
                <CopyButton value={event.message} />
              </div>
            )}
          </div>
          <p className="text-sm text-[var(--color-fg-primary)] break-words leading-relaxed">
            {event.message || "—"}
          </p>
        </div>

        {/* Timestamps */}
        <div className="mb-3">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Timestamps
          </p>
          <FieldRow
            label="timestamp"
            value={fmtTimestampFull(event.timestamp)}
            mono
            filterKey={null}
          />
          <FieldRow
            label="timestamp_desc"
            value={event.timestamp_desc}
            filterKey="timestamp_desc"
            onAddFilter={onAddFilter}
          />
          <FieldRow
            label="ingest_time"
            value={fmtRelative(event.ingest_time)}
            filterKey={null}
          />
        </div>

        {/* Artifact */}
        <div className="mb-3">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Artifact
          </p>
          <FieldRow
            label="artifact"
            value={event.artifact}
            mono
            filterKey="artifact"
            onAddFilter={onAddFilter}
          />
          <FieldRow
            label="artifact_long"
            value={event.artifact_long}
            mono
            filterKey="artifact_long"
            onAddFilter={onAddFilter}
          />
          <FieldRow
            label="display_name"
            value={event.display_name}
            filterKey="display_name"
            onAddFilter={onAddFilter}
          />
        </div>

        {/* Parser tags */}
        {(event.tags ?? []).length > 0 && (
          <div className="mb-3">
            <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Parser Tags
            </p>
            <div className="flex flex-wrap gap-1">
              {(event.tags ?? []).map((t) => (
                <button
                  key={t}
                  className="group/tag flex items-center gap-1"
                  onClick={() => onAddFilter("tag", t, true)}
                  title={`Filter IN: tag = ${t}`}
                >
                  <Badge variant="default" className="hover:border-[var(--color-info)] transition-base">
                    {t}
                  </Badge>
                </button>
              ))}
            </div>
            <p className="mt-1 text-[11px] text-[var(--color-fg-muted)]">
              Click a tag to filter
            </p>
          </div>
        )}

        {/* Attributes — every row has filter-in / filter-out */}
        {Object.keys(event.attributes ?? {}).length > 0 && (
          <div className="mb-3">
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Attributes
              <span className="ml-2 normal-case font-normal text-[var(--color-fg-muted)] text-[11px] opacity-60">
                hover to filter
              </span>
            </p>
            {Object.entries(event.attributes ?? {}).map(([k, v]) => (
              <FieldRow
                key={k}
                label={k}
                value={v}
                mono
                filterKey={k}
                onAddFilter={onAddFilter}
              />
            ))}
          </div>
        )}

        {/* Annotations — editable */}
        <div className="mb-3">
          <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Annotations
          </p>

          {/* User annotations — deletable */}
          {userAnnotations.length === 0 && addMode === null && (
            <p className="text-xs text-[var(--color-fg-muted)] mb-2">None</p>
          )}
          {userAnnotations.map((a) => (
            <div
              key={a.id}
              className="group/ann mb-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2.5 py-2 text-xs"
            >
              <div className="flex items-start gap-1.5">
                {a.annotation_type === "tag" ? (
                  <Tag size={11} className="shrink-0 mt-0.5 text-[var(--color-accent)]" />
                ) : (
                  <MessageSquare size={11} className="shrink-0 mt-0.5 text-[var(--color-info)]" />
                )}
                <span className="flex-1 text-[var(--color-fg-primary)] break-all leading-snug">{a.content}</span>
                <Tooltip content="Delete annotation" side="top">
                  <button
                    onClick={() =>
                      remove.mutate({ eventId: event.event_id, annotationId: a.id })
                    }
                    disabled={remove.isPending}
                    className="shrink-0 rounded p-0.5 opacity-0 group-hover/ann:opacity-100 text-[var(--color-fg-muted)] hover:text-[var(--color-danger)] transition-base"
                  >
                    <Trash2 size={11} />
                  </button>
                </Tooltip>
              </div>
              <Tooltip content={fmtTimestampFull(a.created_at)} side="bottom">
                <p className="mt-1 flex items-center gap-1 text-[11px] text-[var(--color-fg-muted)]">
                  <Clock size={8} />
                  {a.created_by ?? "anonymous"} · {fmtRelative(a.created_at)}
                </p>
              </Tooltip>
            </div>
          ))}

          {/* System annotations (anomalies) — read-only */}
          {systemAnnotations.map((a) => (
            <div
              key={a.id}
              className="mb-1 rounded border border-[var(--color-anomaly)]/30 bg-[var(--color-anomaly-dim)] px-2.5 py-1.5 text-xs"
            >
              <span className="font-medium text-[var(--color-anomaly)]">
                ⚠ {a.annotation_type}:
              </span>{" "}
              <span className="text-[var(--color-fg-primary)]">{a.content}</span>
            </div>
          ))}

          {/* Inline add form */}
          {addMode !== null ? (
            <AddAnnotationForm
              type={addMode}
              onSubmit={handleAdd}
              onCancel={() => setAddMode(null)}
              isPending={add.isPending}
              suggestions={tagSuggestions}
            />
          ) : (
            <div className="flex flex-wrap gap-1.5 mt-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAddMode("tag")}
              >
                <Plus size={11} />
                <Tag size={11} />
                Tag
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAddMode("comment")}
              >
                <Plus size={11} />
                <MessageSquare size={11} />
                Comment
              </Button>
              <NormalToggleButton
                event={event}
                annotations={annotations}
                add={add}
                remove={remove}
              />
            </div>
          )}
        </div>

        {/* Provenance — display-only, no filter buttons */}
        <div className="mb-3">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Provenance
          </p>
          <FieldRow label="event_id" value={event.event_id} mono filterKey={null} />
          <FieldRow label="source_id" value={event.source_id} mono filterKey={null} />
          <FieldRow
            label="content_hash"
            value={truncateHash(event.content_hash, 24)}
            mono
            filterKey={null}
          />
          <FieldRow
            label="file_hash"
            value={truncateHash(event.file_hash, 24)}
            mono
            filterKey={null}
          />
          <FieldRow label="parser" value={event.parser_name} mono filterKey={null} />
          <FieldRow label="source_file" value={event.source_file} mono filterKey={null} />
          <FieldRow
            label="byte_offset"
            value={String(event.byte_offset)}
            mono
            filterKey={null}
          />
          {event.embedding_model && (
            <FieldRow label="embed_model" value={event.embedding_model} mono filterKey={null} />
          )}
        </div>
      </div>
    </div>
  );
}
