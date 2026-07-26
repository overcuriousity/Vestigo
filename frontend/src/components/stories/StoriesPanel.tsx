import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { BookOpenText, ChevronRight } from "lucide-react";
import { storiesApi } from "@/api/stories";
import { fmtRelative } from "@/lib/time";

/** Compact case-overview entry point into the Stories feature area. */
export function StoriesPanel({ caseId }: { caseId: string }) {
  const { data: stories } = useQuery({
    queryKey: ["stories", caseId],
    queryFn: () => storiesApi.list(caseId),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
          Stories
        </h2>
        <Link
          to={`/cases/${caseId}/stories`}
          className="flex items-center gap-1 text-xs text-[var(--color-accent)] hover:underline"
        >
          All stories <ChevronRight size={12} />
        </Link>
      </div>
      {stories && stories.length === 0 && (
        <p className="rounded-lg border border-dashed border-[var(--color-border)] px-5 py-4 text-center text-xs text-[var(--color-fg-muted)]">
          No stories yet — the report that writes itself during the investigation.{" "}
          <Link
            to={`/cases/${caseId}/stories`}
            className="text-[var(--color-accent)] hover:underline"
          >
            Create one
          </Link>
          .
        </p>
      )}
      {stories && stories.length > 0 && (
        <div className="space-y-2">
          {stories.slice(0, 3).map((story) => (
            <Link
              key={story.id}
              to={`/cases/${caseId}/stories/${story.id}`}
              className="flex items-center gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-5 py-2.5 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-elevated)] transition-base"
            >
              <BookOpenText size={14} className="shrink-0 text-[var(--color-info)] opacity-70" />
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-[var(--color-fg-primary)]">
                {story.title}
              </span>
              <span className="shrink-0 text-xs text-[var(--color-fg-muted)]">
                {fmtRelative(story.updated_at)}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
