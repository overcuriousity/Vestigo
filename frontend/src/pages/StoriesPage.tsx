import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, Plus, Trash2 } from "lucide-react";
import { storiesApi } from "@/api/stories";
import { casesApi } from "@/api/cases";
import type { Story } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogClose, DialogContent, DialogTrigger } from "@/components/ui/Dialog";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { fmtRelative } from "@/lib/time";

function CreateStoryDialog({ caseId }: { caseId: string }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const qc = useQueryClient();

  const { mutate, isPending, error, reset } = useMutation({
    mutationFn: () => storiesApi.create(caseId, title.trim(), description.trim() || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stories", caseId] });
      setOpen(false);
    },
  });

  const openChange = (next: boolean) => {
    setOpen(next);
    if (next) {
      setTitle("");
      setDescription("");
      reset();
    }
  };

  return (
    <Dialog open={open} onOpenChange={openChange}>
      <DialogTrigger asChild>
        <Button variant="accent" size="sm">
          <Plus size={14} /> New Story
        </Button>
      </DialogTrigger>
      <DialogContent
        title="New Story"
        description="A story is a living report: markdown narrative with embedded views, charts and events."
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
              Title <span className="text-[var(--color-danger)]">*</span>
            </label>
            <Input
              placeholder="e.g. Intrusion report"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              autoFocus
              maxLength={255}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">Description</label>
            <Input
              placeholder="What this story covers…"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={4096}
            />
          </div>
          {error && (
            <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={isPending || title.trim().length === 0}
              onClick={() => mutate()}
            >
              {isPending ? "Creating…" : "Create Story"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function DeleteStoryDialog({ caseId, story }: { caseId: string; story: Story }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const { mutate, isPending, error } = useMutation({
    mutationFn: () => storiesApi.delete(caseId, story.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stories", caseId] });
      setOpen(false);
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Delete story ${story.title}`}
          className="opacity-0 group-hover:opacity-100"
        >
          <Trash2 size={14} className="text-[var(--color-danger)]" />
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Delete Story"
        description="Deletes the story, its blocks and its exports. Referenced views, charts and events are untouched."
      >
        <p className="text-sm text-[var(--color-fg-secondary)]">
          Delete <span className="font-medium">{story.title}</span>? This cannot be undone.
        </p>
        {error && (
          <p className="mt-2 text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <DialogClose asChild>
            <Button variant="ghost" size="sm">
              Cancel
            </Button>
          </DialogClose>
          <Button variant="danger" size="sm" disabled={isPending} onClick={() => mutate()}>
            {isPending ? "Deleting…" : "Delete"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function StoriesPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const { data: case_ } = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => casesApi.get(caseId!),
    enabled: !!caseId,
  });
  const {
    data: stories,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["stories", caseId],
    queryFn: () => storiesApi.list(caseId!),
    enabled: !!caseId,
  });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        <div className="mb-8 flex items-start gap-4">
          <BookOpenText size={28} className="mt-0.5 shrink-0 text-[var(--color-accent)]" />
          <div className="flex-1">
            <h1 className="text-xl font-semibold text-[var(--color-fg-primary)]">Stories</h1>
            <p className="mt-1 text-sm text-[var(--color-fg-muted)]">
              {case_ ? (
                <>
                  Living reports for{" "}
                  <Link
                    to={`/cases/${caseId}`}
                    className="text-[var(--color-accent)] hover:underline"
                  >
                    {case_.name}
                  </Link>
                  . Write narrative, embed live views, charts and events; export a
                  point-in-time snapshot when the report is due.
                </>
              ) : (
                "Living reports: narrative plus live embedded evidence."
              )}
            </p>
          </div>
          <CreateStoryDialog caseId={caseId!} />
        </div>

        {isLoading && (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        )}
        {error && (
          <p className="text-sm text-[var(--color-danger)]">{(error as Error).message}</p>
        )}
        {stories && stories.length === 0 && (
          <p className="py-8 text-center text-sm text-[var(--color-fg-muted)]">
            No stories yet. Create one, then push views, charts and events into it from the
            Explorer and Visualize pages.
          </p>
        )}
        {stories && stories.length > 0 && (
          <div className="space-y-2">
            {stories.map((story) => (
              <div
                key={story.id}
                className="group flex items-center gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-5 py-3 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-elevated)] transition-base"
              >
                <BookOpenText
                  size={16}
                  className="shrink-0 text-[var(--color-info)] opacity-70"
                />
                <Link to={`/cases/${caseId}/stories/${story.id}`} className="min-w-0 flex-1">
                  <span className="font-medium text-[var(--color-fg-primary)] truncate">
                    {story.title}
                  </span>
                  {story.description && (
                    <p className="mt-0.5 truncate text-xs text-[var(--color-fg-muted)]">
                      {story.description}
                    </p>
                  )}
                  <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                    Updated {fmtRelative(story.updated_at)} by {story.updated_by}
                  </p>
                </Link>
                <DeleteStoryDialog caseId={caseId!} story={story} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
