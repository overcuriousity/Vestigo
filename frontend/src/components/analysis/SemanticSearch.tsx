import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { similarityApi } from "@/api/similarity";
import { Badge } from "@/components/ui/Badge";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { fmtScore, truncate } from "@/lib/format";
import { fmtTimestamp } from "@/lib/time";
import type { Event } from "@/api/types";

interface Props {
  caseId: string;
  /** Present when viewing a specific timeline; enables the scope toggle. */
  timelineId?: string;
  onSelectEvent?: (event: Event) => void;
}

export function SemanticSearch({ caseId, timelineId, onSelectEvent }: Props) {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [thisTimelineOnly, setThisTimelineOnly] = useState(!!timelineId);
  const scopeTimelineId = thisTimelineOnly ? timelineId : undefined;

  const { data, isLoading, error } = useQuery({
    queryKey: ["semantic-search", caseId, submitted, scopeTimelineId],
    queryFn: () =>
      similarityApi.semanticSearch(caseId, submitted, 15, scopeTimelineId),
    enabled: submitted.length > 0,
  });

  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide">
        Semantic Search
      </h4>

      <form
        className="flex gap-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(query.trim());
        }}
      >
        <Input
          placeholder="describe what you're looking for…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          type="submit"
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-[var(--color-border-strong)] text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)]"
        >
          <Search size={13} />
        </button>
      </form>

      {timelineId && (
        <label className="flex items-center gap-1.5 text-xs text-[var(--color-fg-muted)]">
          <input
            type="checkbox"
            checked={thisTimelineOnly}
            onChange={(e) => setThisTimelineOnly(e.target.checked)}
          />
          This timeline only
        </label>
      )}

      {isLoading && (
        <div className="flex justify-center py-4">
          <Spinner />
        </div>
      )}
      {error && (
        <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
      )}
      {data?.status === "not_embedded" && (
        <p className="text-xs text-[var(--color-fg-muted)]">
          Embeddings not yet generated for the searched sources.
        </p>
      )}
      {data?.results.map((r) => (
        <button
          key={r.event_id}
          className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-left hover:border-[var(--color-accent)]/40 transition-base"
          onClick={() => onSelectEvent?.(r.event)}
        >
          <div className="flex items-center gap-2 mb-0.5">
            <Badge variant="accent">sim {fmtScore(r.score)}</Badge>
            <span className="ml-auto text-xs text-[var(--color-fg-muted)] font-mono">
              {fmtTimestamp(r.event.timestamp)}
            </span>
          </div>
          <p className="text-xs text-[var(--color-fg-secondary)]">
            {truncate(r.event.message ?? "", 140)}
          </p>
        </button>
      ))}
    </div>
  );
}
