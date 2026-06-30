import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Tag, ShieldCheck, Info, Layers } from "lucide-react";
import { similarityApi } from "@/api/similarity";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Badge } from "@/components/ui/Badge";
import { fmtScore, truncate } from "@/lib/format";
import { fmtTimestamp } from "@/lib/time";
import type { Event } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  /** Number of sources in the timeline; toggles the per-source centering control. */
  sourceCount?: number;
  onSelectEvent?: (event: Event) => void;
}

export function AnomaliesList({ caseId, timelineId, sourceCount = 1, onSelectEvent }: Props) {
  const qc = useQueryClient();
  const [normalizePerSource, setNormalizePerSource] = useState(false);

  const isMultiSource = sourceCount > 1;

  const { data, isLoading, error } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, normalizePerSource],
    queryFn: () => similarityApi.listAnomalies(caseId, timelineId, 50, 5000, normalizePerSource),
    staleTime: 60_000,
  });

  const { mutate: tagAnomalies, isPending: isTagging } = useMutation({
    mutationFn: () => similarityApi.tagAnomalies(caseId, timelineId, 50, 5000, normalizePerSource),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations", caseId, timelineId] });
      qc.invalidateQueries({ queryKey: ["anomalies", caseId, timelineId] });
    },
  });

  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <Spinner />
      </div>
    );
  }

  if (error) {
    return (
      <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
    );
  }

  if (!data || data.status === "not_embedded") {
    return (
      <p className="text-xs text-[var(--color-fg-muted)]">
        Embeddings required for anomaly detection.
      </p>
    );
  }

  const isBaselineMode = data.method === "normal-baseline";
  const isPerSourceMode = data.method === "per-source-centroid";

  return (
    <div className="space-y-3">
      {/* Framing note — varies by active mode */}
      <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-xs text-[var(--color-fg-muted)] space-y-1">
        {isBaselineMode ? (
          <>
            <p className="flex items-center gap-1.5">
              <ShieldCheck size={11} className="text-[var(--color-success)] shrink-0" />
              <span>
                Ranked by distance from{" "}
                <strong className="text-[var(--color-fg-secondary)]">
                  {data.baseline_size} Normal event{data.baseline_size !== 1 ? "s" : ""}
                </strong>{" "}
                you marked as baseline.
              </span>
            </p>
            <p className="opacity-70">
              Mark more routine events as Normal in the timeline to refine results.
              Use for triage — proximity to your baseline ≠ confirmed threat.
            </p>
          </>
        ) : isPerSourceMode ? (
          <>
            <p className="flex items-center gap-1.5">
              <Layers size={11} className="text-[var(--color-accent)] shrink-0" />
              <span>
                Ranked by deviation from each{" "}
                <strong className="text-[var(--color-fg-secondary)]">source's own bulk</strong>{" "}
                ({data.sample_size.toLocaleString()} events sampled).
              </span>
            </p>
            <p className="flex items-center gap-1 opacity-70">
              <Info size={10} />
              Source-format differences removed. Outliers reflect behaviour, not log style.
            </p>
          </>
        ) : (
          <>
            <p className="flex items-center gap-1.5">
              <AlertTriangle size={11} className="text-[var(--color-warning)] shrink-0" />
              <span>
                Ranked by distance from the{" "}
                <strong className="text-[var(--color-fg-secondary)]">
                  statistical bulk
                </strong>{" "}
                of this timeline ({data.sample_size.toLocaleString()} events sampled).
              </span>
            </p>
            <p className="flex items-center gap-1 opacity-70">
              <Info size={10} />
              Statistically rare, not confirmed threats. Mark routine events as{" "}
              <strong className="text-[var(--color-fg-secondary)]">Normal</strong> to
              switch to analyst-defined baseline mode.
            </p>
          </>
        )}
      </div>

      {/* Per-source centering toggle — only surfaced for multi-source timelines */}
      {isMultiSource && (
        <label className="flex cursor-pointer items-center gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-xs text-[var(--color-fg-muted)] hover:border-[var(--color-border-hover)] transition-base">
          <input
            type="checkbox"
            className="accent-[var(--color-accent)]"
            checked={normalizePerSource}
            onChange={(e) => setNormalizePerSource(e.target.checked)}
          />
          <Layers size={11} className="shrink-0" />
          <span>
            <strong className="text-[var(--color-fg-secondary)]">Center per source</strong>
            {" "}— score events against their own source's bulk to remove format bias
          </span>
        </label>
      )}

      {/* Persist all as annotations */}
      <Button
        variant="outline"
        size="sm"
        className="w-full"
        disabled={isTagging}
        onClick={() => tagAnomalies()}
      >
        {isTagging ? <Spinner size={13} /> : <Tag size={13} />}
        {isTagging ? "Tagging…" : "Persist as Outlier Annotations"}
      </Button>

      {/* Results */}
      <div className="space-y-1.5">
        {data.results.map((r) => (
          <button
            key={r.event_id}
            className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2.5 text-left hover:border-[var(--color-outlier)]/40 hover:bg-[var(--color-outlier-dim)] transition-base"
            onClick={() => onSelectEvent?.(r.event)}
          >
            <div className="flex items-center gap-2 mb-1">
              <Badge variant="outlier">
                dist {fmtScore(r.details.distance)}
              </Badge>
              <span className="text-xs text-[var(--color-fg-muted)] font-mono">
                #{r.details.rank}
              </span>
              {isBaselineMode && (
                <span className="text-xs text-[var(--color-fg-muted)] opacity-60">
                  baseline
                </span>
              )}
              <span className="ml-auto text-xs text-[var(--color-fg-muted)] font-mono">
                {fmtTimestamp(r.event.timestamp)}
              </span>
            </div>
            <p className="text-xs text-[var(--color-fg-secondary)] leading-relaxed break-words">
              {truncate(r.event.message ?? "", 160)}
            </p>
          </button>
        ))}
        {data.results.length === 0 && (
          <p className="text-xs text-center text-[var(--color-fg-muted)] py-4">
            No anomalies found.
          </p>
        )}
      </div>
    </div>
  );
}
