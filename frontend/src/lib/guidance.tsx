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

/**
 * The suggested workflow is a genuine sequence — each step depends on the one
 * before — so the numbering encodes something true rather than decorating a list.
 */
const CASE_OVERVIEW_STEPS = [
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
  },
];

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
    body: (
      <ol className="space-y-2">
        {CASE_OVERVIEW_STEPS.map((step, i) => (
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
    ),
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
  // Both prompts restate docs/INPUT_FORMATS.md as an LLM instruction — keep
  // them in sync with that spec (and with ingestion/parquet_format.py).
  // Parquet interchange format v1: strict schema + footer metadata.
  llmPromptParquet: `Write a single-file Python 3.10+ script that converts a custom log format into a Vestigo interchange Parquet file (format version 1), following this spec exactly.

DEPENDENCY
- pyarrow is the ONLY third-party dependency. Everything else: standard library.

OUTPUT SCHEMA (exact — the server validates it and rejects mismatches)
Write batches with this pyarrow schema, one row per event:

    import pyarrow as pa
    schema = pa.schema([
        pa.field("source_file", pa.string()),
        pa.field("file_hash", pa.string()),
        pa.field("byte_offset", pa.uint64()),
        pa.field("content_hash", pa.string()),
        pa.field("message", pa.string()),
        pa.field("timestamp", pa.timestamp("ms", tz="UTC")),  # nullable
        pa.field("timestamp_desc", pa.string()),
        pa.field("artifact", pa.string()),
        pa.field("artifact_long", pa.string()),
        pa.field("display_name", pa.string()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("attributes", pa.map_(pa.string(), pa.string())),
    ])

COLUMN SEMANTICS
- source_file: name/path of the ORIGINAL raw evidence file this row came from (not the .parquet). Never null.
- file_hash: SHA-256 hex digest of that original raw evidence file. Never null.
- byte_offset: byte offset of this record within the original file (decompressed stream offset for .gz inputs). Never null.
- content_hash: SHA-256 hex digest of the original raw line/record text. Never null.
- (The four provenance columns above anchor forensic event identity — the server rejects the whole file if any row has a null in them.)
- message: human-readable one-line summary of the event (fall back to the raw line if in doubt).
- timestamp: millisecond-precision, UTC-tagged Arrow timestamp. Convert to UTC; record any input-timezone assumption in the "vestigo.timezone_assumption" footer metadata key. If a timestamp cannot be parsed, write null — do not guess and do not drop the row.
- timestamp_desc: short label for what the timestamp means, e.g. "Event Logged" ("" if absent).
- artifact: short artifact/source type, e.g. "myapp:auth" ("" if absent).
- artifact_long: long-form artifact type, e.g. "application:auth:login" ("" if absent).
- display_name: display label for the source ("" if absent).
- tags: list of strings ([] if absent).
- attributes: string-to-string map holding every format-specific field (IPs, status codes, usernames, ...) with snake_case keys. Keep each value atomic — no packed/pipe-joined values. Omit empty-string values.

REQUIRED FOOTER METADATA (schema.with_metadata({...}))
- "vestigo.format_version": "1"
- "vestigo.converter_name": a short converter identifier, e.g. "myapp2vestigo"
- "vestigo.converter_version": a version string, e.g. "1.0.0"
- "vestigo.original_files": JSON array of {"name": str, "sha256": str, "size_bytes": int, "path": str, "mtime": str}, one entry per raw input file. "path" is the absolute source path, "mtime" its ISO-8601 UTC mtime.

OPTIONAL FORENSIC FOOTER METADATA (self-documenting chain of custody; the server reads but does not require these)
- "vestigo.converted_at": ISO-8601 UTC timestamp of the conversion run.
- "vestigo.row_counts": JSON {"parsed": int, "skipped_malformed": int, "skipped_by_time": int}.
- "vestigo.timezone_assumption": free-text note on any timezone or year assumption the parser made ("" if none).
- "vestigo.parse_decisions": JSON object of format-specific parsing choices.

CLI CONVENTION
- argparse with: -i/--input (required; file, directory, or glob), -o/--output (required; .parquet path), -v/--verbose (progress to stderr).
- Exit code 0 on success, 1 on error with a clear message on stderr.

CONSTRAINTS
- Stream the input and write in record batches (pyarrow.parquet.ParquetWriter, compression="zstd") — do not hold the whole file in memory.
- Handle .gz input transparently if the source format commonly ships gzipped; byte offsets then refer to the decompressed stream.
- Never drop a line silently: rows that fail to parse should either be emitted with a best-effort message and empty fields, or counted and reported on stderr.

Here is a sample of my log format:
[PASTE A REPRESENTATIVE SAMPLE OF YOUR LOG LINES HERE]`,
  // Timesketch-compatible CSV/JSONL: lenient schema, stdlib-only script.
  llmPromptCsv: `Write a single-file Python 3.10+ script that converts a custom log format into a Timesketch-compatible timeline that Vestigo can ingest, following this spec exactly.

OUTPUT FORMAT
- Emit CSV (default) or JSONL (one JSON object per line, UTF-8), selectable with -f/--format {csv,jsonl}.
- These column headers / JSON keys are recognized (case-insensitive) and map onto the event model:
  - datetime: when the event occurred. Prefer ISO 8601 UTC, e.g. 2026-07-09T14:32:01Z. Also accepted: "YYYY-MM-DD HH:MM:SS[.ffffff]", "YYYY-MM-DD", or Unix epoch as a 10-digit (seconds), 13-digit (milliseconds), or 16/17-digit (microseconds) numeric string. Values without a timezone are assumed UTC. Emit an empty value rather than guessing when a timestamp cannot be parsed — the event is kept, just unanchored in time.
  - timestamp_desc: short label for what the timestamp means, e.g. "Event Logged".
  - message: human-readable one-line summary of the event (include the raw line if in doubt). This is the ONLY required field.
  - source: short artifact/source type, e.g. "myapp:auth".
  - source_long: long-form artifact type, e.g. "application:auth:login".
  - display_name: optional display label for the source.
  - tag: comma-separated or pipe-separated tags, e.g. "ssh,brute-force" or "ssh|brute-force" (in JSONL, "tags" as a JSON array of strings is also fine).
- Every OTHER column/key is preserved verbatim as a free-form attribute — put all format-specific fields (IPs, status codes, usernames, ...) in extra columns/keys with snake_case names. Keep each value atomic — no packed/pipe-joined fields.
- CSV specifics: header row first, comma delimiter, RFC 4180 quoting ("" escapes embedded quotes).

CLI CONVENTION
- argparse with: -i/--input (required; file, directory, or glob), -o/--output (default "-" = stdout), -f/--format {csv,jsonl} (default csv), -v/--verbose (progress to stderr).
- Exit code 0 on success, 1 on error with a clear message on stderr.

CONSTRAINTS
- Python standard library ONLY. No pip dependencies.
- Stream or buffer sensibly; handle .gz transparently if the source format commonly ships gzipped.
- Never drop a line silently: rows that fail to parse should either be emitted with a best-effort message and empty fields, or counted and reported on stderr.
- Timestamps must be converted to UTC; document any assumption about the input timezone at the top of the script.

Here is a sample of my log format:
[PASTE A REPRESENTATIVE SAMPLE OF YOUR LOG LINES HERE]`,
} as const;
