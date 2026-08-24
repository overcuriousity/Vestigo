/**
 * "Add to story" — the push path that makes a story assemble itself during
 * the investigation instead of being written afterwards.
 *
 * Mounted on the analysis surfaces (Explorer filters, saved charts, event
 * detail, agent findings). Picks a story (or creates one), resolves the block
 * content, appends it, and toasts a link back. Embeds must reference
 * persisted objects, so a caller pushing live Explorer state passes
 * `resolveContent` and saves a View first.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, Plus } from "lucide-react";
import { storiesApi } from "@/api/stories";
import type { Story, StoryBlockKind } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/Popover";
import { Spinner } from "@/components/ui/Spinner";
import { toast } from "@/stores/toasts";

export interface StoryBlockDraft {
  kind: StoryBlockKind;
  content: Record<string, unknown>;
}

interface CommonProps {
  caseId: string;
  label?: string;
  className?: string;
  /** Compact icon-only trigger for dense toolbars. */
  iconOnly?: boolean;
}

/**
 * Exactly one of `content` / `resolveContent` is required, as a union rather
 * than two optionals: passing neither used to be a runtime crash on the
 * non-null assertion instead of a compile error.
 */
type Props = CommonProps &
  (
    | {
        /** Ready-made block content (chart/event pushes). */
        content: StoryBlockDraft;
        resolveContent?: never;
      }
    | {
        content?: never;
        /**
         * Content that can only be built once the target story is known —
         * e.g. an Explorer filter set, which needs a persisted View.
         */
        resolveContent: (story: Story) => Promise<StoryBlockDraft>;
      }
  );

export function AddToStoryButton({
  caseId,
  content,
  resolveContent,
  label = "Add to story",
  className,
  iconOnly = false,
}: Props) {
  const [open, setOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: stories, isLoading } = useQuery({
    queryKey: ["stories", caseId],
    queryFn: () => storiesApi.list(caseId),
    enabled: open,
  });

  const push = useMutation({
    mutationFn: async (story: Story) => {
      const draft = content ?? (await resolveContent(story));
      await storiesApi.createBlock(caseId, story.id, {
        kind: draft.kind,
        content: draft.content,
      });
      return story;
    },
    onSuccess: (story) => {
      qc.invalidateQueries({ queryKey: ["story", caseId, story.id] });
      qc.invalidateQueries({ queryKey: ["views", caseId] });
      setOpen(false);
      toast.success(`Added to “${story.title}”`, undefined, {
        label: "Open story",
        // Client-side navigation: a full reload would drop the query cache
        // and the Explorer's in-memory state the analyst just built up.
        onClick: () => navigate(`/cases/${caseId}/stories/${story.id}`),
      });
    },
    onError: (err) => toast.error("Couldn't add to story", (err as Error).message),
  });

  const createAndPush = useMutation({
    mutationFn: async (title: string) => {
      const story = await storiesApi.create(caseId, title);
      qc.invalidateQueries({ queryKey: ["stories", caseId] });
      return story;
    },
    onSuccess: (story) => {
      setNewTitle("");
      push.mutate(story);
    },
    onError: (err) => toast.error("Couldn't create story", (err as Error).message),
  });

  const busy = push.isPending || createAndPush.isPending;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={className}
          aria-label={label}
          title={label}
        >
          <BookOpenText size={13} />
          {!iconOnly && <span>Add to story</span>}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-2">
        <p className="mb-2 px-1 text-[11px] uppercase tracking-wider text-[var(--color-fg-muted)]">
          Add to story
        </p>
        {isLoading && (
          <div className="flex justify-center py-3">
            <Spinner size={14} />
          </div>
        )}
        {stories && stories.length === 0 && (
          <p className="px-1 pb-2 text-xs text-[var(--color-fg-muted)]">
            No stories yet — name one below.
          </p>
        )}
        <div className="max-h-52 space-y-0.5 overflow-y-auto">
          {(stories ?? []).map((story) => (
            <button
              key={story.id}
              type="button"
              disabled={busy}
              className="w-full truncate rounded px-2 py-1.5 text-left text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] disabled:opacity-50"
              onClick={() => push.mutate(story)}
            >
              {story.title}
            </button>
          ))}
        </div>
        <div className="mt-2 flex items-center gap-1 border-t border-[var(--color-border)] pt-2">
          <Input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="New story…"
            className="h-7 text-[11px]"
            onKeyDown={(e) => {
              if (e.key === "Enter" && newTitle.trim() && !busy) {
                createAndPush.mutate(newTitle.trim());
              }
            }}
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-1.5"
            aria-label="Create story and add"
            disabled={!newTitle.trim() || busy}
            onClick={() => createAndPush.mutate(newTitle.trim())}
          >
            {busy ? <Spinner size={11} /> : <Plus size={13} />}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
