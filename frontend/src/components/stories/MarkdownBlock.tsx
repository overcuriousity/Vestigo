import { useEffect, useRef, useState } from "react";
import type { StoryBlockOf } from "@/api/types";
import { Markdown } from "@/components/agent/Markdown";
import { Button } from "@/components/ui/Button";

interface Props {
  block: StoryBlockOf<"markdown">;
  /** Save the draft against the given base version. */
  onSave: (text: string, version: number) => void;
  /**
   * The winning block after a 409 (someone else saved first), or null when
   * there is no open conflict. While set, the draft is kept and the user
   * chooses.
   */
  conflict: StoryBlockOf<"markdown"> | null;
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
  /**
   * The block as it stood when this edit began. `block` is a live prop from
   * the story poll, so reading `block.version` at save time would send
   * whatever version the last poll happened to fetch — including a
   * collaborator's. The server's check would then pass and their edit would
   * be destroyed with no 409 and no conflict UI. Since a paragraph routinely
   * takes longer to write than the poll interval, that is the common case,
   * not the rare one.
   */
  const [base, setBase] = useState<{ version: number; text: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const text = block.content.text ?? "";

  /**
   * Report edit mode on the transition only — never on the parent re-rendering.
   *
   * `onEditingChange` is an inline closure in StoryEditor, so it is a new
   * function on every render of the story. Depending on it here meant the
   * effect fired on identity rather than on `editing`, called back into the
   * parent's state setter, and re-rendered the story — which allocated the
   * next closure. That loop froze the whole story view (issue #193). The ref
   * keeps the latest callback without making it a dependency.
   */
  const onEditingChangeRef = useRef(onEditingChange);
  // Kept current in an effect rather than during render: a render that never
  // commits (concurrent React can throw one away) must not leave the ref
  // pointing into a discarded tree. Declared first, so it has already run by
  // the time the effect below fires in the same commit.
  useEffect(() => {
    onEditingChangeRef.current = onEditingChange;
  });
  useEffect(() => {
    onEditingChangeRef.current(editing);
    // Report `false` on unmount too: a block deleted mid-edit otherwise stays
    // in the parent's editingIds forever, which leaves the "your draft is
    // kept" banner up with no block left to justify it.
    return () => onEditingChangeRef.current(false);
  }, [editing]);

  const startEdit = () => {
    setBase({ version: block.version, text });
    setDraft(text);
    setEditing(true);
  };

  const save = () => {
    setEditing(false);
    if (base && draft !== base.text) onSave(draft, base.version);
    setBase(null);
  };

  const cancel = () => {
    setEditing(false);
    setBase(null);
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
          <Markdown content={conflict.content.text ?? ""} />
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
            // Escape throws the draft away, so only do it silently when
            // there is nothing to throw away.
            if (base && draft !== base.text) {
              if (!window.confirm("Discard your unsaved changes to this block?")) return;
            }
            cancel();
          }
        }}
      />
    );
  }

  return (
    // Not role="button": the rendered markdown can contain its own links, and
    // nesting interactives inside a button is both invalid and unusable with
    // a screen reader. The click target is a convenience; the explicit Edit
    // button below is the accessible path.
    <div className="group/md relative cursor-text text-sm" onClick={startEdit}>
      {text.trim() ? (
        <Markdown content={text} />
      ) : (
        <span className="text-xs italic text-[var(--color-fg-muted)]">Empty — click to write</span>
      )}
      <button
        type="button"
        aria-label="Edit this text block"
        className="absolute right-0 top-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-[10px] text-[var(--color-fg-muted)] opacity-0 transition-opacity group-hover/md:opacity-100 focus-visible:opacity-100"
        onClick={(e) => {
          e.stopPropagation();
          startEdit();
        }}
      >
        Edit
      </button>
    </div>
  );
}
