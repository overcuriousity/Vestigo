/**
 * Forensic methodology documentation panel.
 *
 * Shows a human-readable, reproducible record of:
 *   - Which embedding model was used
 *   - Exactly which fields of which sources were embedded
 *   - The embedding config hash that pins this configuration
 *   - The active anomaly algorithm and its key parameters
 *
 * Intended to let analysts document and defend their analytical choices.
 */
import { useQuery } from "@tanstack/react-query";
import { similarityApi } from "@/api/similarity";
import { Info, Hash, Cpu, AlertTriangle, ShieldCheck } from "lucide-react";
import type { Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  timeline: Timeline;
}

const TOP_LEVEL_LABELS: Record<string, string> = {
  message: "Message",
  timestamp_desc: "Timestamp description",
  source_long: "Source (long)",
  display_name: "Display name",
  tags: "Parser tags",
};

function tokenLabel(token: string): string {
  if (token.startsWith("attr:")) return `attr — ${token.slice(5)}`;
  return TOP_LEVEL_LABELS[token] ?? token;
}

export function MethodologyPanel({ caseId, timelineId, timeline }: Props) {
  const { data: anomalyData } = useQuery({
    queryKey: ["anomalies", caseId, timelineId],
    queryFn: () => similarityApi.listAnomalies(caseId, timelineId, 1, 100),
    staleTime: 60_000,
    enabled: (timeline.vector_count ?? 0) > 0,
  });

  const cfg = timeline.embedding_config;
  const hasVectors = (timeline.vector_count ?? 0) > 0;
  const method = anomalyData?.method ?? "centroid-distance";
  const baselineSize = anomalyData?.baseline_size ?? 0;
  const sampleSize = anomalyData?.sample_size ?? 0;
  const configHash = anomalyData?.embedding_config_hash ?? null;

  return (
    <div className="space-y-4 text-xs">
      {/* Embedding section */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-[10px]">
          <Cpu size={11} /> Embedding
        </h4>

        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <div className="flex items-start gap-2">
            <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Model</span>
            <span className="font-mono text-[var(--color-fg-primary)] break-all">
              {timeline.embedding_model ?? "all-MiniLM-L6-v2 (default)"}
            </span>
          </div>

          {configHash && (
            <div className="flex items-start gap-2">
              <span className="text-[var(--color-fg-muted)] w-24 shrink-0 flex items-center gap-1">
                <Hash size={10} /> Config hash
              </span>
              <span className="font-mono text-[var(--color-fg-muted)] break-all text-[10px]">
                {configHash}
              </span>
            </div>
          )}

          {!hasVectors && (
            <p className="text-[var(--color-warning)] flex items-center gap-1">
              <Info size={10} /> No embeddings generated yet.
            </p>
          )}
        </div>

        {/* Field selection */}
        {cfg ? (
          <div className="space-y-2">
            {Object.entries(cfg.sources).map(([source, fields]) => (
              <div
                key={source}
                className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2.5 space-y-1.5"
              >
                <p className="font-semibold font-mono text-[var(--color-fg-primary)]">
                  {source || "(unknown source)"}
                </p>
                <div className="flex flex-wrap gap-1">
                  {(fields as string[]).map((f) => (
                    <span
                      key={f}
                      className="rounded bg-[var(--color-accent-dim)] px-1.5 py-0.5 font-mono text-[var(--color-accent)] text-[10px]"
                    >
                      {tokenLabel(f)}
                    </span>
                  ))}
                  {fields.length === 0 && (
                    <span className="text-[var(--color-warning)]">No fields selected</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : hasVectors ? (
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2.5">
            <p className="text-[var(--color-fg-muted)]">
              Legacy embedding — all fields from all sources were included.
              Re-embed with the wizard to configure per-source field selection.
            </p>
          </div>
        ) : null}
      </section>

      {/* Anomaly algorithm section */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-[10px]">
          <AlertTriangle size={11} /> Anomaly Algorithm
        </h4>
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          {method === "normal-baseline" ? (
            <>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Method</span>
                <span className="flex items-center gap-1 text-[var(--color-success)] font-medium">
                  <ShieldCheck size={11} /> Analyst-defined baseline
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Baseline</span>
                <span className="text-[var(--color-fg-primary)]">
                  {baselineSize} event{baselineSize !== 1 ? "s" : ""} marked Normal
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Scoring</span>
                <span className="text-[var(--color-fg-secondary)]">
                  Qdrant Recommendation API — negative examples only. Events
                  ranked by cosine distance from the normal set centroid.
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Distance</span>
                <span className="text-[var(--color-fg-primary)]">Cosine (L2-normalised)</span>
              </div>
            </>
          ) : (
            <>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Method</span>
                <span className="text-[var(--color-fg-primary)] font-medium">
                  Centroid distance
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Sample</span>
                <span className="text-[var(--color-fg-primary)]">
                  {sampleSize > 0
                    ? `${sampleSize.toLocaleString()} events`
                    : "up to 5 000 events"}
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Scoring</span>
                <span className="text-[var(--color-fg-secondary)]">
                  Events ranked by cosine distance from the global mean vector
                  (negated-centroid ANN query). Higher distance = more unusual.
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Distance</span>
                <span className="text-[var(--color-fg-primary)]">Cosine (L2-normalised)</span>
              </div>
            </>
          )}

          <div className="pt-1 border-t border-[var(--color-border)] flex items-start gap-1.5 text-[var(--color-fg-muted)]">
            <Info size={10} className="mt-0.5 shrink-0" />
            <span>
              Statistical outliers, not confirmed threats. Use for triage —
              rare ≠ malicious. Mark routine events as{" "}
              <strong className="text-[var(--color-fg-secondary)]">Normal</strong> in the
              timeline to refine the baseline.
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}
