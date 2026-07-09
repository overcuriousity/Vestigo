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
import {
  Info,
  Hash,
  Layers,
  Cpu,
  Activity,
  Percent,
  Rewind,
  Ruler,
  Shuffle,
  Timer,
  Type,
  ShieldCheck,
  BarChart2,
} from "lucide-react";
import type { Source, Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  timeline: Timeline | undefined;
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

export function MethodologyPanel({
  caseId: _caseId,
  timelineId: _timelineId,
  timeline,
  sources,
}: Props) {
  const hasVectors = sources.some((s) => s.vector_count > 0);

  return (
    <div className="space-y-5 text-xs">

      {/* Statistical anomaly engine */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-xs">
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
              detect window — defaults to timeline midpoint as split).
            </Row>
            <Row label="Signal">
              Events with field values that appear ≤ rarity floor times in the
              corpus, or values absent in the baseline window but present in the
              detect window.
            </Row>
            <Row label="Score">
              −log(count / total events) — interpretable surprise score.
              Higher = rarer. Carried in{" "}
              <code className="font-mono text-xs">details.surprise</code>.
            </Row>
            <Row label="Fields">
              Auto-selected by cardinality: constant and near-unique (identifier)
              fields are skipped; moderate-cardinality categoricals are
              recommended. Analyst can override via the Fields picker — any
              top-level column or <code className="font-mono text-xs">attr:key</code> is accepted.
            </Row>
            <Row label="Backend">
              Pure ClickHouse GROUP BY aggregations — no embeddings or ML.
              Works immediately after ingestion.
            </Row>
          </div>
        </div>

        {/* Value combo */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Layers size={11} /> Value combos (value_combo)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              The multi-field extension of rare values — group by two or more
              fields together and score each surviving combination by the same
              surprise formula. Self-baseline or temporal, same as rare values.
            </Row>
            <Row label="Signal">
              Combinations that are rare (self-baseline) or first-seen in the
              detect window (temporal) — even when each field's individual
              values are common. E.g. (action, hour) = (login_ok, 03:00).
            </Row>
            <Row label="Score">
              −log(count / total events), over the combination's count. Carried
              in <code className="font-mono text-xs">details.surprise</code>.
            </Row>
            <Row label="Fields">
              Auto mode combines the two highest-coverage recommended fields
              (no pairwise enumeration — that would be one query per pair).
              Analyst can pick 2–4 explicit fields.
            </Row>
            <Row label="Backend">
              ClickHouse GROUP BY over the field expressions — exact match, no
              ML.
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
              Z-score against the series' own event-count distribution, using
              leave-one-out mean/std (each window is scored against the rest of
              the series, excluding itself, so one spike can't inflate its own
              baseline). Optional temporal variant: baseline/detect split, mean
              and std computed from the baseline window only.
            </Row>
            <Row label="Signal">
              Time windows where the event count per (field, value) series
              deviates more than the z-threshold standard deviations from the
              series mean. Detects both spikes and unusual silences.
            </Row>
            <Row label="Score">
              |z| — absolute z-score. Carried in{" "}
              <code className="font-mono text-xs">details.z_score</code>.
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

        {/* Timestamp order */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Rewind size={11} /> Timestamp order (timestamp_order)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Mode-less (sequential) — no baseline/detect split. Each event's
              timestamp is compared to its immediate predecessor in record
              order via a ClickHouse window function (lagInFrame).
            </Row>
            <Row label="Signal">
              Events whose parsed timestamp runs backwards relative to the
              previous record, by at least the minimum-jump threshold. Indicates
              log tampering, clock resets, or interleaved multi-writer logs.
            </Row>
            <Row label="Order">
              Record position = byte offset in the source file (monotonic per
              file), then line number and event id as tie-breaks — not the
              parsed timestamp. Comparison uses the predecessor, not a running
              maximum, so one future-dated outlier flags two boundaries instead
              of cascading over every later event.
            </Row>
            <Row label="Score">
              Backwards jump in seconds. Carried in{" "}
              <code className="font-mono text-xs">details.skew_seconds</code>.
            </Row>
            <Row label="Backend">
              ClickHouse window function over (source_id, byte_offset). NULL
              timestamps are excluded.
            </Row>
          </div>
        </div>

        {/* Numeric range */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Ruler size={11} /> Numeric range (numeric_range)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Self-baseline uses a Tukey IQR fence [q1−1.5·IQR, q3+1.5·IQR] over
              the whole corpus; temporal learns the exact min/max of the
              baseline window. Fields are selected by parsing values as numbers
              (toFloat64OrNull ≥ 90% parse rate) — never by field meaning.
            </Row>
            <Row label="Signal">
              Numeric values falling outside the learned band. Findings group by
              distinct value, ranked by how far outside they fall.
            </Row>
            <Row label="Score">
              Distance outside the band ÷ band width. Carried with the band
              bounds in <code className="font-mono text-xs">details</code>.
            </Row>
            <Row label="Backend">
              ClickHouse quantile()/min()/max() over toFloat64OrNull — no ML.
              Fields with fewer than 20 numeric baseline samples are skipped.
            </Row>
          </div>
        </div>

        {/* Charset novelty */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Type size={11} /> Charset novelty (charset)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Per field, learns a reference character set over distinct values.
              Self-baseline ("rare-chars") treats characters appearing in ≤ 3
              distinct values as rare and flags values containing them;
              temporal learns the baseline window's character set and flags
              detect-window values with never-seen characters.
            </Row>
            <Row label="Signal">
              Null bytes, unicode homoglyphs, injection metacharacters —
              detected purely by character identity, never by what a value
              means. Fields with fewer than 20 distinct baseline values or an
              alphabet over 5000 characters (free text in large scripts) are
              skipped.
            </Row>
            <Row label="Score">
              Sum over the value's novel characters of −log(values-with-char /
              distinct-values) — value_novelty's surprise family. Novel
              characters and their codepoints are carried in{" "}
              <code className="font-mono text-xs">details</code>.
            </Row>
            <Row label="Backend">
              ClickHouse extractAll + array functions over distinct values — no
              ML.
            </Row>
          </div>
        </div>

        {/* Entropy outliers */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Shuffle size={11} /> Entropy outliers (entropy)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Shannon character entropy of each distinct value, compared to a
              Tukey fence [q1−1.5·IQR, q3+1.5·IQR] over the field's entropy
              distribution — the whole corpus in self-baseline mode, the
              baseline window in temporal mode.
            </Row>
            <Row label="Signal">
              Above-band values look random (DGA domains, encoded payloads,
              keys); below-band values look degenerate (padding, repeated
              characters). Purely syntactic — computed from character
              frequencies, never from what a value means.
            </Row>
            <Row label="Score">
              Distance outside the band ÷ band width, like numeric range.
              Entropy, band, and quartiles are carried in{" "}
              <code className="font-mono text-xs">details</code>.
            </Row>
            <Row label="Backend">
              ClickHouse array functions over distinct values — no ML. Values
              shorter than 6 characters are excluded; fields with fewer than
              20 qualifying baseline values are skipped.
            </Row>
          </div>
        </div>

        {/* Proportion shift */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Percent size={11} /> Proportion shift (proportion_shift)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Temporal-only — compares each value's <em>share</em> of events
              between the baseline window and each suspect window with a 2×2
              G-test (log-likelihood ratio). All tests in a run — every field ×
              value × window — are corrected together with Benjamini–Hochberg
              FDR, so of everything flagged at most ~q (default 5%) is expected
              false.
            </Row>
            <Row label="Signal">
              Values whose proportion rose or fell significantly — including
              values that vanish from a suspect window entirely (a maximal
              "down"). Because it tests shares, not counts, a global volume
              change alone flags nothing. First-seen values are excluded by
              construction; Rare values owns those.
            </Row>
            <Row label="Score">
              The G statistic (evidence strength).{" "}
              <code className="font-mono text-xs">details.q_value</code> is the
              FDR-adjusted p-value; a finding needs q ≤ threshold <em>and</em> a
              share change of at least the minimum ratio (default 2×) — on large
              timelines significance without magnitude is noise.
            </Row>
            <Row label="Fields">
              Same categorical auto-selection as rare values (identifier-like
              fields have no repeating shares to test); override via the Fields
              picker.
            </Row>
            <Row label="Backend">
              One ClickHouse GROUP BY per field with per-window counts; the
              test runs in Python (exact df=1 chi² via erfc — no scipy, fully
              offline). Events cluster in bursts, so q-values are a ranking
              aid, not an exact false-positive probability.
            </Row>
          </div>
        </div>

        {/* Interval cadence */}
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <p className="flex items-center gap-1.5 font-medium text-[var(--color-fg-primary)]">
            <Timer size={11} /> Interval cadence (interval_periodicity)
          </p>
          <div className="space-y-1.5 text-[var(--color-fg-muted)]">
            <Row label="Method">
              Temporal-only — measures the inter-arrival gaps of each value
              inside each window. A value that was <em>regular</em> in the
              baseline gets a Poisson-rate likelihood-ratio test on its arrival
              rate (window durations as exposures); a value that was{" "}
              <em>bursty</em> gets Greenwood's spacing statistic on how evenly
              its window arrivals are spread. All tests share one
              Benjamini–Hochberg correction.
            </Row>
            <Row label="Signal">
              A broken heartbeat — a value arriving every ~60 s that goes
              missing or silent in the suspect window (this subsumes per-value
              silence), or accelerates — and its inverse, beaconing: a value
              that was irregular in the baseline but arrives suspiciously
              evenly in the window (C2 callbacks). Purely temporal: it never
              reads what a value means.
            </Row>
            <Row label="Score">
              −log10 of the p-value (comparable across the two tests).{" "}
              <code className="font-mono text-xs">details.q_value</code> is the
              FDR-adjusted p-value; each direction adds an effect floor — a
              rate must change ≥ the minimum ratio, and beaconing needs a tight
              window CV covering a real span fraction — so significance alone
              never flags.
            </Row>
            <Row label="Fields">
              Same categorical auto-selection as rare values; override via the
              Fields picker.
            </Row>
            <Row label="Backend">
              One ClickHouse GROUP BY per field: inter-arrival deltas via{" "}
              <code className="font-mono text-xs">lagInFrame</code> partitioned
              by (value, window) so a gap never straddles a window edge, then
              the tests run in Python (no scipy). The Poisson-rate test is
              conservative for genuinely periodic values (their counts vary
              less than Poisson), so a flagged deficit is at least as
              significant.
            </Row>
          </div>
        </div>

        <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)]">
          <ShieldCheck size={10} className="mt-0.5 shrink-0 text-[var(--color-success)]" />
          <span>
            All detectors are forensically defensible: every finding carries
            the exact field/value/count/baseline (or timestamps and skew) in{" "}
            <code className="font-mono">details</code>. Rare ≠ malicious — use
            for triage. Confirmed findings can be tagged as{" "}
            <strong className="text-[var(--color-fg-secondary)]">anomaly</strong>{" "}
            system annotations for case reporting.
          </span>
        </div>
      </section>

      {/* Semantic similarity search */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wide text-xs">
          <Cpu size={11} /> Semantic Similarity Search
        </h4>

        {!hasVectors && (
          <p className="text-[var(--color-warning)] flex items-center gap-1">
            <Info size={10} /> No embeddings generated yet — similarity search
            unavailable.
          </p>
        )}

        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-2">
          <div className="flex items-start gap-2">
            <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Model</span>
            <span className="font-mono text-[var(--color-fg-primary)] break-all">
              {timeline?.embedding_model ?? "all-MiniLM-L6-v2 (default)"}
            </span>
          </div>

          {sources.map((source) => (
            <div key={source.id} className="flex items-start gap-2">
              <span className="text-[var(--color-fg-muted)] w-24 shrink-0">Source</span>
              <span className="min-w-0 flex-1">
                <span className="font-mono text-[var(--color-fg-primary)] break-all text-xs">
                  {source.name}
                </span>
                {source.vector_count > 0 && (
                  <span className="ml-2 text-[var(--color-fg-muted)]">
                    {source.vector_count.toLocaleString()} vectors
                  </span>
                )}
              </span>
            </div>
          ))}

          {timeline?.embedding_config ? (
            <div className="space-y-1.5">
              {Object.entries(timeline.embedding_config.artifacts).map(([artifact, fields]) => (
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
                        className="rounded bg-[var(--color-accent-dim)] px-1.5 py-0.5 font-mono text-[var(--color-accent)] text-xs"
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
            <p className="text-[var(--color-fg-muted)]">
              All fields embedded. Re-embed with the wizard to configure
              per-artifact field selection.
            </p>
          ) : null}
        </div>

        <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)]">
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
