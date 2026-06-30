/**
 * Forensic methodology documentation panel.
 *
 * Documents exactly what is running under the hood so analysts can defend
 * their analytical choices:
 *   - Statistical anomaly engine (value novelty + frequency detectors)
 *   - Semantic similarity search substrate (embeddings, model, field config)
 *
 * No API calls for the anomaly section — the detectors are parameter-driven
 * and work on any ingested data; the methodology is stable.
 */
import { useQuery } from "@tanstack/react-query";
import {
  Info,
  Hash,
  Cpu,
  Activity,
  ShieldCheck,
  BarChart2,
} from "lucide-react";
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

export function MethodologyPanel({ caseId: _caseId, timelineId: _timelineId, sources }: Props) {
  const hasVectors = sources.some((s) => s.vector_count > 0);

  return (
    <div className="space-y-5 text-xs">

      {/* Statistical anomaly engine */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-[10px]">
          <BarChart2 size={11} /> Statistical Anomaly Engine
        </h4>

        {/* Value novelty */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Hash size={11} /> Rare values (value_novelty)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Self-baseline (whole timeline) or temporal (baseline window vs
              detect window — analyst supplies split point).
            </Row>
            <Row label="Signal">
              Events with field values that appear ≤ rarity floor times in the
              corpus, or values absent in the baseline window but present in the
              detect window.
            </Row>
            <Row label="Score">
              −log(count / total events) — interpretable surprise score.
              Higher = rarer. Carried in{" "}
              <code className="font-mono text-[10px]">details.surprise</code>.
            </Row>
            <Row label="Fields">
              artifact, timestamp_desc, display_name (default).
              Analyst can extend to any top-level column or attributes key.
            </Row>
            <Row label="Backend">
              Pure ClickHouse GROUP BY aggregations — no embeddings or ML.
              Works immediately after ingestion.
            </Row>
          </div>
        </div>

        {/* Frequency */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Activity size={11} /> Frequency spikes (frequency)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Z-score against the series' own event-count distribution.
              Optional temporal variant: baseline/detect split.
            </Row>
            <Row label="Signal">
              Time windows where the event count per (field, value) series
              deviates more than the z-threshold standard deviations from the
              series mean. Detects both spikes and unusual silences.
            </Row>
            <Row label="Score">
              |z| — absolute z-score. Carried in{" "}
              <code className="font-mono text-[10px]">details.z_score</code>.
            </Row>
            <Row label="Bucketing">
              Same interval math as the timeline histogram: duration / bucket
              count (default 60). Groups: any top-level column or attributes key.
            </Row>
            <Row label="Backend">
              ClickHouse GROUP BY time bucket — no embeddings or ML.
            </Row>
          </div>
        </div>

        <div className="flex items-start gap-1.5 text-[10px] text-[var(--color-fg-muted)]">
          <ShieldCheck size={10} className="mt-0.5 shrink-0 text-[var(--color-success)]" />
          <span>
            Both detectors are forensically defensible: every finding carries
            the exact field, value, count, and baseline in{" "}
            <code className="font-mono">details</code>. Rare ≠ malicious — use
            for triage. Confirmed findings can be tagged as{" "}
            <strong className="text-[var(--color-fg-secondary)]">anomaly</strong>{" "}
            system annotations for case reporting.
          </span>
        </div>
      </section>

      {/* Semantic similarity search */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-[10px]">
          <Cpu size={11} /> Semantic Similarity Search
        </h4>

        {!hasVectors && (
          <p className="text-[var(--color-warning)] flex items-center gap-1">
            <Info size={10} /> No embeddings generated yet — similarity search
            unavailable.
          </p>
        )}

        {sources.map((source) => {
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
              {source.vector_count > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Vectors</span>
                  <span className="text-[var(--color-fg-primary)]">
                    {source.vector_count.toLocaleString()}
                  </span>
                </div>
              )}

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
                  All fields embedded. Re-embed with the wizard to configure
                  per-artifact field selection.
                </p>
              ) : null}

              {cfg && (
                <div className="flex items-start gap-2">
                  <span className="text-[var(--color-fg-muted)] w-24 shrink-0 flex items-center gap-1">
                    <Hash size={10} /> Config hash
                  </span>
                  <span className="font-mono text-[var(--color-fg-muted)] break-all text-[10px]">
                    {source.embedding_config_hash ?? "—"}
                  </span>
                </div>
              )}
            </div>
          );
        })}

        <div className="flex items-start gap-1.5 text-[10px] text-[var(--color-fg-muted)]">
          <Info size={10} className="mt-0.5 shrink-0" />
          <span>
            Similarity search uses cosine distance in 384-dim embedding space
            (L2-normalised). The config hash pins the field selection so results
            are reproducible across sessions.
          </span>
        </div>
      </section>
    </div>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-20 shrink-0 text-[var(--color-fg-muted)]">{label}</span>
      <span className="flex-1 text-[var(--color-fg-secondary)]">{children}</span>
    </div>
  );
}
