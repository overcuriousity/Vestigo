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
          "TraceSignal ingests Timesketch-compatible CSV/JSONL (and Plaso exports) plus " +
          "TraceSignal Parquet files produced by the converter scripts in the Parser " +
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
    ],
  },

  converters: {
    hint:
      "Format not covered here? Generative LLMs are good at writing one-off " +
      "normalization scripts. Copy the prompt below, add a sample of your log " +
      "format, and you should get a converter that produces valid input.",
    // Both prompts restate docs/INPUT_FORMATS.md as an LLM instruction — keep
    // them in sync with that spec (and with ingestion/parquet_format.py).
    // Parquet interchange format v1: strict schema + footer metadata.
    llmPromptParquet: `Write a single-file Python 3.10+ script that converts a custom log format into a TraceSignal interchange Parquet file (format version 1), following this spec exactly.

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
- timestamp: millisecond-precision, UTC-tagged Arrow timestamp. Convert to UTC; document any input-timezone assumption at the top of the script. If a timestamp cannot be parsed, write null — do not guess and do not drop the row.
- timestamp_desc: short label for what the timestamp means, e.g. "Event Logged" ("" if absent).
- artifact: short artifact/source type, e.g. "myapp:auth" ("" if absent).
- artifact_long: long-form artifact type, e.g. "application:auth:login" ("" if absent).
- display_name: display label for the source ("" if absent).
- tags: list of strings ([] if absent).
- attributes: string-to-string map holding every format-specific field (IPs, status codes, usernames, ...) with snake_case keys. Keep each value atomic — no packed/pipe-joined values. Omit empty-string values.

REQUIRED FOOTER METADATA (schema.with_metadata({...}))
- "tracesignal.format_version": "1"
- "tracesignal.converter_name": a short converter identifier, e.g. "myapp2tracesignal"
- "tracesignal.converter_version": a version string, e.g. "1.0.0"
- "tracesignal.original_files": JSON array of {"name": str, "sha256": str, "size_bytes": int}, one entry per raw input file

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
    llmPromptCsv: `Write a single-file Python 3.10+ script that converts a custom log format into a Timesketch-compatible timeline that TraceSignal can ingest, following this spec exactly.

OUTPUT FORMAT
- Emit CSV (default) or JSONL (one JSON object per line, UTF-8), selectable with -f/--format {csv,jsonl}.
- These column headers / JSON keys are recognized (case-insensitive) and map onto the event model:
  - datetime: when the event occurred. Prefer ISO 8601 UTC, e.g. 2026-07-09T14:32:01Z. Also accepted: "YYYY-MM-DD HH:MM:SS[.ffffff]", "YYYY-MM-DD", or Unix epoch as a 10-digit (seconds), 13-digit (milliseconds), or 16/17-digit (microseconds) numeric string. Values without a timezone are assumed UTC. Emit an empty value rather than guessing when a timestamp cannot be parsed — the event is kept, just unanchored in time.
  - timestamp_desc: short label for what the timestamp means, e.g. "Event Logged".
  - message: human-readable one-line summary of the event (include the raw line if in doubt). This is the ONLY required field.
  - source: short artifact/source type, e.g. "myapp:auth".
  - source_long: long-form artifact type, e.g. "application:auth:login".
  - display_name: optional display label for the source.
  - tag: comma-separated tags, e.g. "ssh,brute-force" (in JSONL, "tags" as a JSON array of strings is also fine).
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
  },
} as const;
