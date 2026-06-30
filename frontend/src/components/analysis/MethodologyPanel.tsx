/**
 * Forensic methodology documentation panel.
 *
 * Shows a human-readable, reproducible record of:
 *   - Which embedding model was used per source
 *   - Exactly which fields of which artifacts were embedded
 *   - The embedding config hash that pins this configuration
 *   - The active anomaly algorithm and its key parameters
 *
 * Intended to let analysts document and defend their analytical choices.
 */
import { useQuery } from "@tanstack/react-query";
import { similarityApi } from "@/api/similarity";
import { Info, Hash, Cpu, AlertTriangle, ShieldCheck } from "lucide-react";
import type { Source } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  sources: Source[];
}

const TOP_LEVEL_LABELS: Record<string, string> = {
  message: "Message",
  timestamp_desc: "Timestamp description",
  artifact_long: "Artifact (long)",
  display_name: "Display name",
  tags: "Parser tags",
};

function tokenLabel(token: string): string {
  if (token.startsWith("attr:")) return `attr — ${token.slice(5)}`;
  return TOP_LEVEL_LABELS[token] ?? token;
}

export function MethodologyPanel({ caseId, timelineId, sources }: Props) {
  const hasVectors = sources.some((s) => s.vector_count > 0);

  const { data: anomalyData } = useQuery({
    queryKey: ["anomalies", caseId, timelineId],
    queryFn: () => similarityApi.listAnomalies(caseId, timelineId, 1, 100),
    staleTime: 60_000,
    enabled: hasVectors,
  });

  const method = anomalyData?.method ?? "centroid-distance";
  const baselineSize = anomalyData?.baseline_size ?? 0;
  const sampleSize = anomalyData?.sample_size ?? 0;
  const configHash = anomalyData?.embedding_config_hash ?? null;

  return (
    <div className="space-y-4 text-xs">
      {/* Embedding section — one block per source */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-[10px]">
          <Cpu size={11} /> Embedding
        </h4>

        {!hasVectors && (
          <p className="text-[var(--color-warning)] flex items-center gap-1">
            <Info size={10} /> No embeddings generated yet.
          </p>
        )}

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

        {sources.map((source) => {
          // Fall back to the old "sources" key for configs written before the rename.
          const rawCfg = source.embedding_config;
          const cfg = rawCfg
            ? { ...rawCfg, artifacts: rawCfg.artifacts ?? rawCfg.sources }
            : undefined;

          return (
            <div
              key={source.id}
              className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2"
            >
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Source</span>
                <span className="font-mono text-[var(--color-fg-primary)] break-all text-[10px]">
                  {source.name}
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Model</span>
                <span className="font-mono text-[var(--color-fg-primary)] break-all">
                  {source.embedding_model ?? "all-MiniLM-L6-v2 (default)"}
                </span>
              </div>

              {cfg ? (
                <div className="space-y-1.5">
                  {Object.entries(cfg.artifacts).map(([artifact, fields]) => (
                    <div
                      key={artifact}
                      className="rounded border border-[var(--color-border)] px-2.5 py-2 space-y-1"
                    >
                      <p className="font-semibold font-mono text-[var(--color-fg-primary)]">
                        {artifact || "(unknown artifact)"}
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
              ) : source.vector_count > 0 ? (
                <p className="text-[var(--color-fg-muted)]">
                  Legacy embedding — all fields from all artifacts were included.
                  Re-embed with the wizard to configure per-artifact field selection.
                </p>
              ) : null}
            </div>
          );
        })}
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
                  Nearest-normal max-similarity. Each candidate is scored by its
                  cosine similarity to the closest baseline event; the least
                  similar (highest distance) are ranked as outliers.
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
