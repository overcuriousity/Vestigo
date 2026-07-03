/**
 * All subtle-guidance copy in one place so wording is reviewable centrally
 * (issue #11). Keep the tone factual and muted — guidance is a whisper in the
 * margins, never a tutorial overlay.
 */

export const guidance = {
  casesPage: {
    title: "How TraceSignal is organized",
    body:
      "A case is the investigation container: it holds the original log files " +
      "(sources, hashed and immutable), the timelines composed from them, and " +
      "everything your team annotates along the way. Cases can be shared with " +
      "a team so several investigators work the same evidence. Create a case " +
      "to begin.",
  },

  caseOverview: {
    title: "Suggested workflow",
    steps: [
      {
        title: "Normalize your input data",
        body:
          "TraceSignal ingests Timesketch-compatible CSV/JSONL (and Plaso exports). " +
          "For raw logs — nginx, firewall, CloudTrail, browser history, systemd journal — " +
          "use the converter scripts available in the upload dialog. They run offline " +
          "with plain Python, no dependencies.",
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
    ],
  },

  converters: {
    hint:
      "Format not covered here? Generative LLMs are good at writing one-off " +
      "normalization scripts. Copy the prompt below, add a sample of your log " +
      "format, and you should get a converter that produces valid input.",
    // The 2timesketch converter contract, phrased as an LLM instruction.
    llmPrompt: `Write a single-file Python 3.10+ script that converts a custom log format into a Timesketch-compatible timeline, following these rules exactly:

OUTPUT FORMAT
- Emit CSV (default) or JSONL, selectable with -f/--format {csv,jsonl}.
- Every row/object MUST contain these columns, in this order first:
  datetime, timestamp_desc, message, data_type, timestamp, source, src_ip, dst_ip
  - datetime: ISO 8601 UTC with millisecond precision, e.g. 2025-01-01T12:00:00.123Z
  - timestamp_desc: short label for what the timestamp means, e.g. "Event Logged"
  - message: human-readable one-line summary of the event (include the raw line if in doubt)
  - data_type: dotted/colon-namespaced type identifier, e.g. "myapp:auth:login"
  - timestamp: same instant as datetime, as integer Unix MICROSECONDS
  - source: the input file path the row came from
  - src_ip: the single IP that originated the event, "" if unknown; never join multiple IPs in one field
  - dst_ip: the single IP the event was directed at, "" if not applicable
- Additional format-specific fields go in extra columns after the common ones (snake_case names). Keep each value atomic — no packed/pipe-joined fields.

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
  },
} as const;
