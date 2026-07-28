import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, ChevronLeft } from "lucide-react";
import { storiesApi } from "@/api/stories";
import { ExportsTab } from "@/components/stories/ExportsTab";
import { StoryEditor } from "@/components/stories/StoryEditor";
import { useInvalidateStory, useStory } from "@/components/stories/useStory";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { fmtRelative } from "@/lib/time";

export function StoryEditorPage() {
  const { caseId, storyId } = useParams<{ caseId: string; storyId: string }>();
  const qc = useQueryClient();
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const [tab, setTab] = useState<"editor" | "exports">("editor");

  const { data, isLoading, error } = useStory(caseId, storyId);
  const invalidateStory = useInvalidateStory(caseId, storyId);

  const renameStory = useMutation({
    mutationFn: (title: string) => storiesApi.update(caseId!, storyId!, { title }),
    onSuccess: () => {
      invalidateStory();
      qc.invalidateQueries({ queryKey: ["stories", caseId] });
    },
  });

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-[var(--color-danger)]">
        {error ? (error as Error).message : "Story not found"}
      </div>
    );
  }

  const story = data.story;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-6 py-8">
        <Link
          to={`/cases/${caseId}/stories`}
          className="mb-4 inline-flex items-center gap-1 text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
        >
          <ChevronLeft size={12} /> All stories
        </Link>

        <div className="mb-6 flex items-start gap-4">
          <BookOpenText size={26} className="mt-1 shrink-0 text-[var(--color-accent)]" />
          <div className="min-w-0 flex-1">
            <Input
              className="border-transparent bg-transparent px-0 text-lg font-semibold text-[var(--color-fg-primary)] focus:border-[var(--color-border)]"
              value={titleDraft ?? story.title}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={() => {
                const next = (titleDraft ?? "").trim();
                if (next && next !== story.title) renameStory.mutate(next);
                setTitleDraft(null);
              }}
              maxLength={255}
              aria-label="Story title"
            />
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
              {story.description ? `${story.description} · ` : ""}
              Updated {fmtRelative(story.updated_at)} by {story.updated_by}
            </p>
          </div>
        </div>

        <div className="mb-4 flex gap-1 border-b border-[var(--color-border)]">
          {(["editor", "exports"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={`-mb-px border-b-2 px-3 py-1.5 text-xs capitalize transition-base ${
                tab === t
                  ? "border-[var(--color-accent)] text-[var(--color-fg-primary)]"
                  : "border-transparent text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]"
              }`}
            >
              {t}
            </button>
          ))}
        </div>

        {tab === "editor" ? (
          <StoryEditor caseId={caseId!} storyId={storyId!} />
        ) : (
          <ExportsTab caseId={caseId!} storyId={storyId!} />
        )}
      </div>
    </div>
  );
}
