/**
 * Timeline-level embedding wizard.
 *
 * Analyses all sources in the timeline together and recommends a shared,
 * cross-source-cohesive field config.  The backend computes:
 *   1. Per-field text-signal heuristics (are values worth embedding?).
 *   2. Cross-source cohesion (do the values carry comparable meaning across
 *      sources?).  Source-specific or divergent fields are off by default to
 *      avoid the "batch effect" where distance measures source format rather
 *      than event behaviour.
 *
 * A cohesion banner at the top explains the quality of the merged embedding
 * substrate and warns when cross-source outlier detection may be unreliable.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, Check, Link2, Info, AlertTriangle, ShieldCheck } from "lucide-react";
import { timelinesApi } from "@/api/timelines";
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
import type {
  CohesionSummary,
  EmbeddingArtifactInfo,
  FieldVerdict,
  Timeline,
} from "@/api/types";

interface Props {
  caseId: string;
  timeline: Timeline;
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
  "source-specific": "source-specific",
  divergent: "divergent",
};

// ---------------------------------------------------------------------------
// Cohesion banner
// ---------------------------------------------------------------------------

const COHESION_COLORS: Record<string, string> = {
  strong: "var(--color-success)",
  moderate: "var(--color-info)",
  weak: "var(--color-warning)",
  unavailable: "var(--color-fg-muted)",
};

const COHESION_LABELS: Record<string, string> = {
  strong: "Strong",
  moderate: "Moderate",
  weak: "Weak",
  unavailable: "Unavailable",
};

function CohesionBanner({ cohesion }: { cohesion: CohesionSummary }) {
  const color = COHESION_COLORS[cohesion.level] ?? "var(--color-fg-muted)";
  const label = COHESION_LABELS[cohesion.level] ?? cohesion.level;
  const isWeak = cohesion.level === "weak";

  return (
    <div
      className="rounded border p-2.5 space-y-1.5"
      style={{ borderColor: `${color}33`, background: `${color}0d` }}
    >
      <div className="flex items-center gap-2">
        {isWeak ? (
          <AlertTriangle size={12} style={{ color }} className="shrink-0" />
        ) : cohesion.level === "strong" ? (
          <ShieldCheck size={12} style={{ color }} className="shrink-0" />
        ) : (
          <Info size={12} style={{ color }} className="shrink-0" />
        )}
        <span className="text-xs font-semibold" style={{ color }}>
          Cross-source cohesion:{" "}
          {label}
          {cohesion.mean_cohesion != null && (
            <span className="font-normal ml-1 opacity-80">
              ({cohesion.mean_cohesion.toFixed(2)})
            </span>
          )}
        </span>
        {cohesion.shared_field_count > 0 && (
          <span className="ml-auto text-xs text-[var(--color-fg-muted)]">
            {cohesion.shared_field_count} shared field
            {cohesion.shared_field_count !== 1 ? "s" : ""}
          </span>
        )}
      </div>
      <p className="text-xs text-[var(--color-fg-secondary)] leading-snug">
        {cohesion.message}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Artifact section
// ---------------------------------------------------------------------------

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
        <span className="text-xs text-[var(--color-fg-muted)]">
          {info.count.toLocaleString()} events
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {tokens.map((token) => {
          const v = verdicts.get(token);
          const on = selected.has(token);
          const skippedKind = v && !v.recommended ? KIND_HINT[v.kind] : null;
          // Show cohesion score for shared-cohesive fields.
          const cohesionHint =
            v?.kind === "shared-cohesive" && v.cohesion != null
              ? `· ${v.cohesion.toFixed(2)}`
              : null;
          const chip = (
            <button
              key={token}
              type="button"
              onClick={() => onToggle(token)}
              className={`flex items-center gap-1 rounded border px-2 py-1 text-xs transition-colors ${
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
              {cohesionHint && (
                <span className="text-[9px] opacity-50">{cohesionHint}</span>
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
          <div className="flex items-center gap-1 text-xs text-[var(--color-fg-muted)]">
            <Info size={10} /> Semantically related — toggle as a group:
          </div>
          <div className="flex flex-wrap gap-1.5">
            {info.related_groups.map((group, i) => (
              <button
                key={i}
                type="button"
                onClick={() => onToggleGroup(group)}
                className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
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

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------

export function EmbedWizard({ caseId, timeline, onJobStarted }: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Record<string, Set<string>>>({});
  const addJob = useJobsStore((s) => s.addJob);
  const qc = useQueryClient();

  const isEmbedded = timeline.is_embedded;
  const isStale = timeline.is_stale;
  // Sources get a default all-fields embedding automatically on ingest, so
  // this wizard's role is curating a better field selection, not a required
  // first step.
  const label = isEmbedded ? "Re-embed" : "Improve search quality";

  const { data, isLoading, fetchStatus } = useQuery({
    queryKey: ["timeline-embedding-fields", caseId, timeline.id],
    queryFn: () => timelinesApi.embeddingFields(caseId, timeline.id),
    enabled: open,
  });

  // Backend has a fixed execution order — sampling always precedes cohesion
  // scoring — so an elapsed-time-driven label gives an honest sense of
  // progress without needing a second round trip or a real percentage.
  const [loadingLabel, setLoadingLabel] = useState("Sampling events…");
  useEffect(() => {
    if (fetchStatus !== "fetching") return;
    const startedAt = Date.now();
    setLoadingLabel("Sampling events…");
    const interval = setInterval(() => {
      const elapsed = Date.now() - startedAt;
      if (elapsed > 6000) {
        setLoadingLabel("Still scoring — larger timelines with more sources take longer…");
      } else if (elapsed > 1500) {
        setLoadingLabel("Scoring field cohesion…");
      }
    }, 500);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Initialise selection once data arrives: stored timeline config if
  // re-embedding, otherwise the backend's cross-source recommendation.
  const initialised = Object.keys(selected).length > 0;
  if (open && data && !initialised && data.artifacts.length > 0) {
    const next: Record<string, Set<string>> = {};
    const storedCfg = timeline.embedding_config;
    const stored = storedCfg?.artifacts;
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
      return timelinesApi.embed(caseId, timeline.id, { version: 1, artifacts });
    },
    onSuccess: (result) => {
      addJob(result.job_id, `Embedding "${timeline.name}"`, [
        ["timelines", caseId],
        ["timeline", caseId, timeline.id],
      ]);
      onJobStarted?.(result.job_id);
      // Invalidate timelines so is_embedded / is_stale update.
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
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

  const totalEvents = useMemo(
    () => (data?.artifacts ?? []).reduce((n, a) => n + a.count, 0),
    [data],
  );

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogTrigger asChild>
        <Button
          variant={isStale ? "outline" : "outline"}
          size="sm"
          className={isStale ? "border-[var(--color-warning)] text-[var(--color-warning)]" : ""}
        >
          <Cpu size={13} /> {label}
          {isStale && (
            <span className="ml-1 text-[9px] opacity-80">· stale</span>
          )}
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Embedding wizard"
        description={
          timeline.source_ids.length > 1
            ? "Sources are already embedded with a default all-fields configuration. Choose a curated field selection across all sources in this timeline for higher-quality search — recommendations are based on shared content and cross-source cohesion."
            : "This source is already embedded with a default all-fields configuration. Choose a curated field selection for higher-quality search — recommendations are based on each field's content."
        }
        className="max-w-2xl"
      >
        {isLoading || !data ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12">
            <Spinner />
            <span
              role="status"
              aria-live="polite"
              className="text-[11px] text-[var(--color-fg-muted)]"
            >
              {loadingLabel}
            </span>
          </div>
        ) : data.artifacts.length === 0 ? (
          <p className="py-8 text-center text-xs text-[var(--color-fg-muted)]">
            No events found for this timeline.
          </p>
        ) : (
          <div className="space-y-4">
            {/* Cohesion banner (only for multi-source timelines) */}
            {timeline.source_ids.length > 1 && data.cohesion && (
              <CohesionBanner cohesion={data.cohesion} />
            )}

            <div className="max-h-[50vh] space-y-4 overflow-y-auto pr-1">
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
                  {label} {totalEvents.toLocaleString()} events
                </Button>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
