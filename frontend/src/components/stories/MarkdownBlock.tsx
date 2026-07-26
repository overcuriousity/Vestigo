import { useEffect, useRef, useState } from "react";
import type { StoryBlock } from "@/api/types";
import { Markdown } from "@/components/agent/Markdown";
import { Button } from "@/components/ui/Button";

interface Props {
  block: StoryBlock;
  /** Save the draft against the given base version. */
  onSave: (text: string, version: number) => void;
  /**
   * The winning block after a 409 (someone else saved first), or null when
   * there is no open conflict. While set, the draft is kept and the user
   * chooses.
   */
  conflict: StoryBlock | null;
  onResolveConflict: (choice: "theirs" | "mine") => void;
  /** Report enter/leave of edit mode so polling never clobbers a draft. */
  onEditingChange: (editing: boolean) => void;
}

/**
 * A markdown block: rendered GFM at rest, plain textarea while editing.
 * Click to edit; blur or Ctrl/Cmd-S saves. No WYSIWYG by design.
 */
export function MarkdownBlock({ block, onSave, conflict, onResolveConflict, onEditingChange }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const text = (block.content.text as string) ?? "";

  useEffect(() => {
    onEditingChange(editing);
  }, [editing, onEditingChange]);

  const startEdit = () => {
    setDraft(text);
    setEditing(true);
  };

  const save = () => {
    setEditing(false);
    if (draft !== text) onSave(draft, block.version);
  };

  if (conflict) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-[var(--color-warning)]">
          Changed by {conflict.updated_by} while you were editing.
        </p>
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2">
          <p className="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-fg-muted)]">
            Their version
          </p>
          <Markdown content={(conflict.content.text as string) ?? ""} />
        </div>
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2">
          <p className="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-fg-muted)]">
            Your draft
          </p>
          <Markdown content={draft} />
        </div>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => onResolveConflict("theirs")}>
            Take theirs
          </Button>
          <Button
            size="sm"
            variant="danger"
            onClick={() => {
              // Resave the kept draft on top of the winning version.
              onSave(draft, conflict.version);
              onResolveConflict("mine");
            }}
          >
            Overwrite with mine
          </Button>
        </div>
      </div>
    );
  }

  if (editing) {
    return (
      <textarea
        ref={textareaRef}
        className="min-h-32 w-full resize-y rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] p-2 font-mono text-xs text-[var(--color-fg-primary)] outline-none focus:border-[var(--color-accent)]"
        value={draft}
        autoFocus
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            save();
          }
          if (e.key === "Escape") {
            setEditing(false);
          }
        }}
      />
    );
  }

  return (
    <div
      role="button"
      tabIndex={0}
      title="Click to edit"
      className="cursor-text text-sm"
      onClick={startEdit}
      onKeyDown={(e) => {
        if (e.key === "Enter") startEdit();
      }}
    >
      {text.trim() ? (
        <Markdown content={text} />
      ) : (
        <span className="text-xs italic text-[var(--color-fg-muted)]">Empty — click to write</span>
      )}
    </div>
  );
}
