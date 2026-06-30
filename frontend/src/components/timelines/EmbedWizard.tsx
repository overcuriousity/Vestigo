/**
 * Per-source embedding wizard.
 *
 * Loads content-aware field recommendations for the source (hybrid
 * heuristic→pairs analysis on the backend) and lets the analyst pick which
 * fields of which artifacts to embed.  Recommended fields are preselected;
 * low-signal fields (IDs, hashes, numbers, constants) are shown with the reason
 * they were skipped.  Semantically related fields are surfaced as groups.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Cpu, Check, Link2, Info } from "lucide-react";
import { sourcesApi } from "@/api/sources";
import { useJobsStore } from "@/stores/jobs";
import {
  Dialog,
  DialogContent,
  DialogTrigger,
  DialogClose,
} from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import type { EmbeddingArtifactInfo, FieldVerdict, Source } from "@/api/types";

interface Props {
  caseId: string;
  source: Source;
  /** Called after the embed job is started so parent can update. */
  onJobStarted?: (jobId: string) => void;
}

const TOP_LEVEL_LABELS: Record<string, string> = {
  message: "Message",
  timestamp_desc: "Timestamp description",
  artifact_long: "Artifact (long)",
  display_name: "Display name",
  tags: "Parser tags",
};

function tokenLabel(token: string): string {
  if (token.startsWith("attr:")) return token.slice(5);
  return TOP_LEVEL_LABELS[token] ?? token;
}

function artifactTokens(info: EmbeddingArtifactInfo): string[] {
  return [...info.top_level, ...info.attributes.map((a) => `attr:${a}`)];
}

const KIND_HINT: Record<string, string> = {
  numeric: "numeric",
  hash: "hash",
  guid: "GUID",
  id: "identifier",
  constant: "constant",
  empty: "empty",
};

export function EmbedWizard({ caseId, source, onJobStarted }: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Record<string, Set<string>>>({});
  const addJob = useJobsStore((s) => s.addJob);
  const label = (source.vector_count ?? 0) > 0 ? "Re-embed" : "Embed";

  const { data, isLoading } = useQuery({
    queryKey: ["embedding-fields", caseId, source.id],
    queryFn: () => sourcesApi.embeddingFields(caseId, source.id),
    enabled: open,
  });

  // Initialise selection once data arrives: stored config if re-embedding,
  // otherwise the recommended preselection.
  const initialised = Object.keys(selected).length > 0;
  if (open && data && !initialised && data.artifacts.length > 0) {
    const next: Record<string, Set<string>> = {};
    const stored = source.embedding_config?.artifacts;
    for (const art of data.artifacts) {
      next[art.artifact] = new Set(
        stored?.[art.artifact] ?? art.recommended,
      );
    }
    setSelected(next);
  }

  const embedMutation = useMutation({
    mutationFn: () => {
      const artifacts: Record<string, string[]> = {};
      for (const [art, tokens] of Object.entries(selected)) {
        if (tokens.size > 0) artifacts[art] = Array.from(tokens);
      }
      return sourcesApi.embed(caseId, source.id, { version: 1, artifacts });
    },
    onSuccess: (result) => {
      addJob(result.job_id, `Embedding "${source.name}"`);
      onJobStarted?.(result.job_id);
      handleOpen(false);
    },
  });

  function handleOpen(v: boolean) {
    setOpen(v);
    if (!v) setSelected({});
  }

  function toggle(artifact: string, token: string) {
    setSelected((prev) => {
      const cur = new Set(prev[artifact] ?? []);
      if (cur.has(token)) cur.delete(token);
      else cur.add(token);
      return { ...prev, [artifact]: cur };
    });
  }

  function toggleGroup(artifact: string, group: string[]) {
    setSelected((prev) => {
      const cur = new Set(prev[artifact] ?? []);
      const allOn = group.every((t) => cur.has(t));
      for (const t of group) {
        if (allOn) cur.delete(t);
        else cur.add(t);
      }
      return { ...prev, [artifact]: cur };
    });
  }

  const totalSelected = useMemo(
    () => Object.values(selected).reduce((n, s) => n + s.size, 0),
    [selected],
  );

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Cpu size={13} /> {label}
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Embedding wizard"
        description="Choose which fields of which artifacts to embed. Recommendations are based on each field's content."
        className="max-w-2xl"
      >
        {isLoading || !data ? (
          <div className="flex justify-center py-12">
            <Spinner />
          </div>
        ) : data.artifacts.length === 0 ? (
          <p className="py-8 text-center text-xs text-[var(--color-fg-muted)]">
            No events found for this source.
          </p>
        ) : (
          <div className="space-y-4">
            <div className="max-h-[55vh] space-y-4 overflow-y-auto pr-1">
              {data.artifacts.map((art) => (
                <ArtifactSection
                  key={art.artifact}
                  info={art}
                  selected={selected[art.artifact] ?? new Set()}
                  onToggle={(t) => toggle(art.artifact, t)}
                  onToggleGroup={(g) => toggleGroup(art.artifact, g)}
                />
              ))}
            </div>

            <div className="flex items-center justify-between border-t border-[var(--color-border)] pt-3">
              <span className="text-xs text-[var(--color-fg-muted)]">
                {totalSelected} field{totalSelected === 1 ? "" : "s"} selected
                across {data.artifacts.length} artifact
                {data.artifacts.length === 1 ? "" : "s"}
              </span>
              <div className="flex gap-2">
                <DialogClose asChild>
                  <Button variant="ghost" size="sm">
                    Cancel
                  </Button>
                </DialogClose>
                <Button
                  variant="accent"
                  size="sm"
                  disabled={totalSelected === 0 || embedMutation.isPending}
                  onClick={() => embedMutation.mutate()}
                >
                  {embedMutation.isPending ? (
                    <Spinner size={13} />
                  ) : (
                    <Check size={13} />
                  )}
                  {label} {source.event_count.toLocaleString()} events
                </Button>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ArtifactSection({
  info,
  selected,
  onToggle,
  onToggleGroup,
}: {
  info: EmbeddingArtifactInfo;
  selected: Set<string>;
  onToggle: (token: string) => void;
  onToggleGroup: (group: string[]) => void;
}) {
  const verdicts = useMemo(() => {
    const m = new Map<string, FieldVerdict>();
    for (const v of info.field_analysis) m.set(v.token, v);
    return m;
  }, [info.field_analysis]);

  const tokens = artifactTokens(info);

  return (
    <div className="rounded border border-[var(--color-border)] p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="font-mono text-xs font-semibold text-[var(--color-fg-primary)]">
          {info.artifact || "(no artifact)"}
        </span>
        <span className="text-[10px] text-[var(--color-fg-muted)]">
          {info.count.toLocaleString()} events
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {tokens.map((token) => {
          const v = verdicts.get(token);
          const on = selected.has(token);
          const skippedKind = v && !v.recommended ? KIND_HINT[v.kind] : null;
          const chip = (
            <button
              key={token}
              type="button"
              onClick={() => onToggle(token)}
              className={`flex items-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors ${
                on
                  ? "border-[var(--color-accent)] bg-[var(--color-accent)]/15 text-[var(--color-fg-primary)]"
                  : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:border-[var(--color-fg-muted)]"
              }`}
            >
              {on && <Check size={10} />}
              {tokenLabel(token)}
              {skippedKind && (
                <span className="text-[9px] opacity-60">· {skippedKind}</span>
              )}
            </button>
          );
          return v?.reason ? (
            <Tooltip key={token} content={v.reason}>
              {chip}
            </Tooltip>
          ) : (
            chip
          );
        })}
      </div>

      {info.related_groups.length > 0 && (
        <div className="mt-2.5 space-y-1 border-t border-[var(--color-border)] pt-2">
          <div className="flex items-center gap-1 text-[10px] text-[var(--color-fg-muted)]">
            <Info size={10} /> Semantically related — toggle as a group:
          </div>
          <div className="flex flex-wrap gap-1.5">
            {info.related_groups.map((group, i) => (
              <button
                key={i}
                type="button"
                onClick={() => onToggleGroup(group)}
                className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-[10px] text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
              >
                <Link2 size={9} />
                {group.map(tokenLabel).join(" + ")}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
