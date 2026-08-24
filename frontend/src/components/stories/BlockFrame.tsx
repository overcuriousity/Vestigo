import type { CSSProperties, ReactNode } from "react";
import { Bot, GripVertical, Trash2 } from "lucide-react";
import type { StoryBlock } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { fmtRelative } from "@/lib/time";

interface Props {
  block: StoryBlock;
  children: ReactNode;
  onDelete: () => void;
  /** dnd-kit drag-handle listeners/attributes; spread onto the grip. */
  handleProps?: Record<string, unknown>;
  /** dnd-kit sortable node ref + transform style. */
  containerRef?: (node: HTMLElement | null) => void;
  style?: CSSProperties;
  dragging?: boolean;
  /** Extra header controls (per-kind display options, open-in links). */
  headerExtra?: ReactNode;
  title?: ReactNode;
}

/**
 * Shared chrome around every story block: drag grip, origin badge, author
 * line, delete. Content (markdown, embed card) renders inside.
 */
export function BlockFrame({
  block,
  children,
  onDelete,
  handleProps,
  headerExtra,
  title,
  containerRef,
  style,
  dragging = false,
}: Props) {
  return (
    <div
      ref={containerRef}
      style={style}
      className={`group/block rounded-lg border bg-[var(--color-bg-surface)] transition-base hover:border-[var(--color-border-strong)] ${
        dragging
          ? "border-[var(--color-accent)] opacity-60"
          : "border-[var(--color-border)]"
      }`}
    >
      <div className="flex items-center gap-2 border-b border-[var(--color-border)]/60 px-3 py-1.5">
        <button
          type="button"
          aria-label="Drag to reorder"
          // focus-visible matters as much as hover here: dnd-kit's keyboard
          // sensor lives on this button, so without it a keyboard user tabs
          // onto an invisible control and cannot see where they are.
          className="cursor-grab rounded p-0.5 text-[var(--color-fg-muted)] opacity-0 transition-opacity hover:bg-[var(--color-bg-hover)] group-hover/block:opacity-100 focus-visible:opacity-100"
          {...(handleProps ?? {})}
        >
          <GripVertical size={13} />
        </button>
        {title && (
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-[var(--color-fg-secondary)]">
            {title}
          </span>
        )}
        {!title && <span className="flex-1" />}
        {headerExtra}
        {block.origin === "agent" && (
          <Badge variant="accent" className="flex items-center gap-1">
            <Bot size={9} /> agent
          </Badge>
        )}
        <span className="text-[10px] text-[var(--color-fg-muted)]">
          {block.updated_by} · {fmtRelative(block.updated_at)}
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Delete block"
          className="h-6 w-6 opacity-0 group-hover/block:opacity-100 focus-visible:opacity-100"
          onClick={onDelete}
        >
          <Trash2 size={12} className="text-[var(--color-danger)]" />
        </Button>
      </div>
      <div className="px-4 py-3">{children}</div>
    </div>
  );
}
