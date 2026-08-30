/**
 * All subtle-guidance copy in one place so wording is reviewable centrally
 * (issue #11). Keep the tone factual and muted — guidance is a whisper in the
 * margins, never a tutorial overlay.
 *
 * `guidance` is keyed by `GuidancePanel`'s `id`, and the panel takes nothing but
 * that id — it reads both title and body from here. That is deliberate: the four
 * Investigate panels used to inline their copy as JSX at the call site and never
 * touch this file, which is exactly the drift it was created to prevent. With the
 * panel owning no copy props, inlining is a type error rather than a convention.
 *
 * Bodies are JSX because the copy uses emphasis and inline code, which a plain
 * string cannot carry. Reviewing wording still means reading one file.
 */
import type { ReactNode } from "react";
import { useCapabilities } from "@/api/health";
import type { Capabilities } from "@/api/types";

/**
 * The suggested workflow is a genuine sequence — each step depends on the one
 * before — so the numbering encodes something true rather than decorating a list.
 */
const CASE_OVERVIEW_STEPS: {
  title: string;
  body: string;
  /** Only shown where this optional subsystem is configured. */
  capability?: keyof Capabilities;
}[] = [
  {
    title: "Normalize your input data",
    body:
      "Vestigo ingests Timesketch-compatible CSV/JSONL (and Plaso exports) plus " +
      "Vestigo Parquet files produced by the converter scripts in the Parser " +
      "downloads panel. Common raw formats — nginx, firewall, CloudTrail, Suricata, " +
      "pcap — convert to compact Parquet with full provenance; the remaining " +
      "stdlib-only scripts emit CSV/JSONL. All converters run offline with plain " +
      "Python.",
  },
  {
    title: "Upload & ingest",
    body:
      "Each uploaded file becomes an immutable, SHA-256-hashed source. Large files " +
      "keep ingesting in the background — the job tray in the top bar shows progress, " +
      "and events become searchable as they land.",
  },
  {
    title: "Explore timelines",
    body:
      "The default timeline always contains all sources of the case; open it to start " +
      "filtering, searching, and building histograms right away. Create additional " +
      "timelines to recombine sources into task-specific views — the timeline wizard " +
      "can merge equivalent fields from differently normalized sources (src_ip vs " +
      "ip_addr) into one canonical field.",
  },
  {
    title: "Optionally: embeddings",
    body:
      "Run the embedding wizard on a timeline to enable semantic search and " +
      "similarity analysis on top of the statistical anomaly detectors, which work " +
      "without any embedding step.",
    /** Dropped entirely where nothing can embed: the step describes a wizard
     * that renders no entry point on such an instance. */
    capability: "embeddings",
  },
];

/** A component rather than inline JSX so the embeddings step can be gated on
 * the health capability — the numbering renumbers itself when it is dropped. */
function CaseOverviewSteps() {
  const capabilities = useCapabilities();
  const steps = CASE_OVERVIEW_STEPS.filter(
    (step) => step.capability === undefined || capabilities[step.capability],
  );
  return (
    <ol className="space-y-2">
      {steps.map((step, i) => (
        <li key={step.title} className="flex gap-2">
          <span className="shrink-0 font-mono opacity-60">{i + 1}.</span>
          <span>
            <span className="font-medium text-[var(--color-fg-secondary)]">
              {step.title}.
            </span>{" "}
            {step.body}
          </span>
        </li>
      ))}
    </ol>
  );
}

// `satisfies` rather than a type annotation: the annotation would widen the keys
// to `string` and `GuidanceId` would stop constraining anything.
export const guidance = {
  "cases-page": {
    title: "How Vestigo is organized",
    body: (
      <p>
        A case is the investigation container: it holds the original log files
        (sources, hashed and immutable), the timelines composed from them, and
        everything your team annotates along the way. Cases can be shared with a
        team so several investigators work the same evidence. Create a case to
        begin.
      </p>
    ),
  },

  "case-overview": {
    title: "Suggested workflow",
    body: <CaseOverviewSteps />,
  },

  "investigate-anomalies": {
    title: "How anomaly scanning works",
    body: (
      <ol className="list-decimal space-y-1 pl-4">
        <li>
          <strong>Scope</strong> — <em>Scan all events</em> compares every event
          against the whole corpus; <em>Compare baseline</em> scores suspect
          windows against a period you declare normal (build one via{" "}
          <em>Manage baselines</em> or by dragging on the histogram).
        </li>
        <li>
          <strong>Findings</strong> — every detector's best findings in one ranked
          feed. Chips filter by detector; <strong>Advanced</strong> opens a
          detector's full view with field selection and tuning.
        </li>
        <li>
          Disposition a finding: <strong>Normal</strong> adds it to the
          known-normal list (that exact value or pattern stops surfacing in future
          scans), <strong>Dismiss</strong> hides it as noise without changing
          detection, <strong>Confirm</strong> escalates it durably.
        </li>
      </ol>
    ),
  },

  "investigate-sigma": {
    title: "How Sigma scanning works",
    body: (
      <p>
        Sigma rules are community-standard YAML signatures for suspicious log
        patterns. Running them evaluates each rule against every event in this
        timeline; matches are tagged{" "}
        <span className="font-mono">sigma: &lt;rule title&gt;</span> as system
        annotations, filterable from the Tags panel. Signature matching is
        deterministic — it complements, not replaces, the statistical anomaly
        detectors.
      </p>
    ),
  },

  "investigate-templates": {
    title: "How template browsing works",
    body: (
      <>
        <p>
          This tab <strong>collapses structurally identical log lines</strong> into
          shapes — variable parts (timestamps, IPs, UUIDs, hex, numbers) are masked,
          so 50M repeats of one routine line group into one template while a
          genuinely odd line stands out.
        </p>
        <p className="mt-1">
          <strong>Mute</strong> a template you recognize as routine noise — its
          events disappear from the grid immediately (no background job), always
          behind a visible "N routine events" count. Muted templates stay listed
          below and can be unmuted anytime.
        </p>
      </>
    ),
  },

  "investigate-patterns": {
    title: "How pattern mining works",
    body: (
      <>
        <p>
          This tab <strong>discovers repeating event sequences</strong> (motifs) —
          it needs no baseline and detects nothing by itself; it shows the log's
          routine structure so you can separate it from the interesting rest.
        </p>
        <p className="mt-1">
          <strong>Mark routine</strong> when you recognize a sequence as expected
          operations — cron jobs, heartbeats, poller loops, backup runs. Its
          occurrences collapse in the event grid behind a visible "N routine events"
          count, decluttering the timeline without hiding anything.
        </p>
      </>
    ),
  },
} satisfies Record<string, { title: string; body: ReactNode }>;

export type GuidanceId = keyof typeof guidance;

/**
 * Converter copy. Not panel content — `ParserDownloadsPanel` renders it in its
 * own box next to the download list, so it lives beside the panel registry rather
 * than inside it.
 */
export const converterCopy = {
  hint:
    "Format not covered here? Generative LLMs are good at writing one-off " +
    "normalization scripts. Copy the prompt below, add a sample of your log " +
    "format, and you should get a converter that produces valid input.",
  // The prompts themselves are rendered server-side from the data contract
  // (`GET /api/converters/prompt`, `src/vestigo/converters/prompt.py`) so the
  // copy cannot drift from `ingestion/parquet_format.py` again (#204).
} as const;
