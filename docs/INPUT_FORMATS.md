# Input Data Formats

TraceSignal ingests three file formats: **CSV**, **JSONL**, and **Parquet**. This document
specifies exactly what each format must contain to normalize cleanly into a TraceSignal
`Event` (`src/tracesignal/models/event.py`). Read this before writing a converter or hand-
crafting an ingest file.

All three formats normalize to the same target: one row/line/record per event, with a common
set of *known fields* mapped onto `Event` attributes, and everything else preserved verbatim
as free-form `attributes`. Nothing is ever silently dropped — unrecognized columns/keys always
survive as attributes so an examiner can inspect the original data later.

## Choosing a format

- **CSV / JSONL** — for small-to-medium timelines, hand-edited data, or output from tools that
  already produce a Timesketch-style CSV/JSONL export (e.g. `log2timeline.py -o dynamic`,
  `psort.py -o dynamic`). Parsed server-side, one row/line at a time.
- **Parquet** — for large evidence sets or custom log formats. A local converter script
  (`src/tracesignal/assets/converters/*2tracesignal.py`, downloadable from the web UI) parses
  the raw evidence file entirely on the analyst's machine and writes one columnar `.parquet`
  file; the server ingests it directly with no re-parsing, no intermediate text file touching
  disk, and no possibility of a lossy round trip through CSV escaping. See
  [`TECH_STACK.md` §3.4a](TECH_STACK.md) for why this format was chosen over CSV/JSONL for
  bulk conversion.

Format is detected by file extension: `.csv`/`.tsv` → CSV, `.jsonl`/`.ndjson`/`.json` → JSONL,
`.parquet` → Parquet (`src/tracesignal/ingestion/parser.py::detect_format`).

## The target: `Event`

Every parser — CSV, JSONL, or Parquet — ultimately produces the same fields
(`src/tracesignal/models/event.py::Event`):

| Field             | Type           | Meaning                                                            |
|-------------------|----------------|----------------------------------------------------------------------|
| `message`         | string         | Human-readable event text. Required (falls back to the raw line/record if no `message`-like field is present). |
| `timestamp`       | ISO-8601 string / Parquet timestamp | When the event occurred. Optional — unparseable or absent timestamps are stored as a sentinel and shown as `null`. |
| `timestamp_desc`  | string         | What the timestamp represents (e.g. `"HTTP Request Time"`, `"Last Written"`). Optional. |
| `artifact`        | string         | Short artifact/source type (e.g. `nginx:access`, `WEBHIST`). Optional. |
| `artifact_long`   | string         | Long-form artifact type (e.g. `web:access:request`). Optional.     |
| `display_name`    | string         | Display label for the source. Optional.                            |
| `tags`            | list of string | Free-form tags. Optional, defaults to empty.                       |
| `attributes`      | map of string → string | Every field not recognized as one of the above. This is where format-specific detail lives (IP addresses, status codes, usernames, ...). |

Forensic provenance fields (`file_hash`, `byte_offset`, `content_hash`, `parser_name`,
`parser_version`) are *not* supplied by hand — CSV/JSONL parsing derives them from the file
being read; Parquet converters compute them from the original raw evidence file. See
`docs/MODEL_REFINEMENT.md` for the full Case/Source/Timeline/Event model.

---

## CSV

Parser: `TimesketchCsvParser` (`src/tracesignal/ingestion/parser.py`). Timesketch-compatible:
a header row, then one event per data row. Delimiter is auto-detected (comma, semicolon, tab,
or pipe); quoting follows RFC 4180 with `""` as the escape for embedded quotes.

### Known columns (case-insensitive)

| Column header(s)            | Maps to           |
|------------------------------|--------------------|
| `datetime`, `timestamp`      | `timestamp`        |
| `timestamp_desc`             | `timestamp_desc`   |
| `message`                    | `message`          |
| `source`                     | `artifact`         |
| `source_long`                | `artifact_long`    |
| `display_name`               | `display_name`     |
| `tag`, `tags`                | `tags`             |

Any other column becomes an entry in `attributes`, keyed by its original header text.

### Tag encoding

A tag column may be comma-separated (`ssh,brute-force`), pipe-separated (`ssh|brute-force`),
or a JSON/Python list literal (`["ssh", "brute-force"]`). All three are accepted.

### Timestamp encoding

Accepted timestamp formats (parsed by `_parse_timestamp` in `models/event.py`):
ISO-8601 (`2026-07-09T14:32:01Z` or with an explicit offset), `YYYY-MM-DD HH:MM:SS[.ffffff]`,
`YYYY-MM-DD`, or Unix epoch as a 10-digit (seconds), 13-digit (milliseconds), or 16/17-digit
(microseconds) numeric string. A value with no timezone is assumed UTC (a warning is logged).
An unparseable or empty value results in `timestamp: null` — the event is kept, just
unanchored in time.

### Minimal example

```csv
datetime,timestamp_desc,message,source,source_long,tag,user,src_ip
2026-07-09T14:32:01Z,Login Time,User admin logged in,AUTH,authentication:login,"ssh,success",admin,10.0.0.5
2026-07-09T14:33:47Z,Login Time,Failed password for root,AUTH,authentication:login,"ssh,failure",root,203.0.113.9
```

`user` and `src_ip` are not known columns, so they land in `attributes` as
`{"user": "admin", "src_ip": "10.0.0.5"}` and `{"user": "root", "src_ip": "203.0.113.9"}`.

### Minimal valid file (only what's required)

`message` is the only field a row needs to become a usable event — everything else is
optional:

```csv
message
System started
```

---

## JSONL

Parser: `JsonlParser` (`src/tracesignal/ingestion/parser.py`). One JSON object per line, UTF-8.
A malformed line is skipped (not fatal to the whole file) — the raw source file is untouched
so the skipped line is still recoverable by manual inspection.

### Known keys (case-insensitive)

| JSON key                      | Maps to           |
|--------------------------------|--------------------|
| `datetime`, `timestamp`        | `timestamp`        |
| `timestamp_desc`               | `timestamp_desc`   |
| `message`, `msg`               | `message`          |
| `source`                       | `artifact`         |
| `source_long`                  | `artifact_long`    |
| `display_name`                 | `display_name`     |
| `tag`, `tags`                  | `tags`             |

Any other key becomes an entry in `attributes`. `tags` may be a JSON array of strings, or a
single string (parsed with the same comma/pipe/list-literal logic as CSV).

Non-string values for known scalar fields (e.g. a numeric `timestamp`) are coerced with
`str()` before being stored, except `timestamp` itself, whose numeric/string forms are both
accepted directly by `_parse_timestamp`.

### Minimal example

```jsonl
{"datetime": "2026-07-09T14:32:01Z", "timestamp_desc": "Login Time", "message": "User admin logged in", "source": "AUTH", "source_long": "authentication:login", "tags": ["ssh", "success"], "user": "admin", "src_ip": "10.0.0.5"}
{"datetime": "2026-07-09T14:33:47Z", "timestamp_desc": "Login Time", "message": "Failed password for root", "source": "AUTH", "source_long": "authentication:login", "tags": "ssh,failure", "user": "root", "src_ip": "203.0.113.9"}
```

### Minimal valid file

```jsonl
{"message": "System started"}
```

---

## Parquet (TraceSignal interchange format, version 1)

Spec module: `src/tracesignal/ingestion/parquet_format.py`. Unlike CSV/JSONL, Parquet is not
meant to be hand-written — a local converter script parses raw evidence and writes columnar
batches with `pyarrow`. The server (`ingestion/parquet_reader.py`) validates the schema and
footer metadata, then bulk-inserts the columns with **no per-row re-parsing**. This is why the
schema below is stricter than CSV/JSONL: every column and type must match exactly.

### Required per-row schema

```python
PARQUET_EVENT_SCHEMA = pa.schema([
    pa.field("source_file", pa.string()),
    pa.field("file_hash", pa.string()),
    pa.field("byte_offset", pa.uint64()),
    pa.field("content_hash", pa.string()),
    pa.field("message", pa.string()),
    pa.field("timestamp", pa.timestamp("ms", tz="UTC")),   # nullable
    pa.field("timestamp_desc", pa.string()),
    pa.field("artifact", pa.string()),
    pa.field("artifact_long", pa.string()),
    pa.field("display_name", pa.string()),
    pa.field("tags", pa.list_(pa.string())),
    pa.field("attributes", pa.map_(pa.string(), pa.string())),
])
```

| Column           | Required? | Notes |
|-------------------|-----------|-------|
| `source_file`     | yes, non-null | Name/path of the **original raw evidence file** this row came from (not the `.parquet` file itself). |
| `file_hash`       | yes, non-null | SHA-256 hex digest of that original raw evidence file. |
| `byte_offset`     | yes, non-null | Byte offset of this record within the original file (decompressed stream offset for `.gz` inputs). |
| `content_hash`    | yes, non-null | SHA-256 hex digest of the original raw line/record text. |
| `message`         | yes           | Same meaning as CSV/JSONL `message`. |
| `timestamp`       | no (nullable) | Millisecond-precision, UTC-tagged Arrow timestamp. Unparseable timestamps are the converter's problem to resolve into `null`, not the server's. |
| `timestamp_desc`  | no (`""` if absent) | Same meaning as CSV/JSONL. |
| `artifact`        | no (`""` if absent) | Same meaning as CSV/JSONL `source`. |
| `artifact_long`   | no (`""` if absent) | Same meaning as CSV/JSONL `source_long`. |
| `display_name`    | no (`""` if absent) | Same meaning as CSV/JSONL. |
| `tags`            | no (`[]` if absent) | List of strings. |
| `attributes`      | no (`{}` if absent) | String-to-string map. Empty-string values should be omitted by the converter — the server strips them anyway, but writing them bloats the file. |

`file_hash`, `byte_offset`, `content_hash`, and `source_file` together anchor forensic event
identity (`derive_event_id` in `models/event.py`) — they must never be null. The server
rejects the whole file if any row has a null provenance column.

### Required footer metadata

Parquet supports arbitrary key-value footer metadata; TraceSignal requires these keys
(`schema.with_metadata({...})` in pyarrow):

| Key                                | Value |
|--------------------------------------|-------|
| `tracesignal.format_version`         | `"1"` |
| `tracesignal.converter_name`         | Converter identifier, e.g. `"nginx2tracesignal"`. Becomes the event's `parser_name`. |
| `tracesignal.converter_version`      | Converter version string, e.g. `"1.0.0"`. Becomes the event's `parser_version`. |
| `tracesignal.original_files`         | JSON array of `{"name": str, "sha256": str, "size_bytes": int}` — one entry per raw input file (a directory input yields several). |

### Minimal example (Python / pyarrow)

```python
import pyarrow as pa
import pyarrow.parquet as pq
import json
import datetime

schema = pa.schema([
    pa.field("source_file", pa.string()),
    pa.field("file_hash", pa.string()),
    pa.field("byte_offset", pa.uint64()),
    pa.field("content_hash", pa.string()),
    pa.field("message", pa.string()),
    pa.field("timestamp", pa.timestamp("ms", tz="UTC")),
    pa.field("timestamp_desc", pa.string()),
    pa.field("artifact", pa.string()),
    pa.field("artifact_long", pa.string()),
    pa.field("display_name", pa.string()),
    pa.field("tags", pa.list_(pa.string())),
    pa.field("attributes", pa.map_(pa.string(), pa.string())),
]).with_metadata({
    "tracesignal.format_version": "1",
    "tracesignal.converter_name": "example2tracesignal",
    "tracesignal.converter_version": "1.0.0",
    "tracesignal.original_files": json.dumps(
        [{"name": "auth.log", "sha256": "e3b0c4...", "size_bytes": 128}]
    ),
})

row = {
    "source_file": "auth.log",
    "file_hash": "e3b0c4...",
    "byte_offset": 0,
    "content_hash": "9f86d0...",
    "message": "User admin logged in",
    "timestamp": datetime.datetime(2026, 7, 9, 14, 32, 1, tzinfo=datetime.timezone.utc),
    "timestamp_desc": "Login Time",
    "artifact": "AUTH",
    "artifact_long": "authentication:login",
    "display_name": "",
    "tags": ["ssh", "success"],
    "attributes": {"user": "admin", "src_ip": "10.0.0.5"},
}

batch = pa.RecordBatch.from_pydict({k: [v] for k, v in row.items()}, schema=schema)
with pq.ParquetWriter("example.parquet", schema, compression="zstd") as writer:
    writer.write_batch(batch)
```

For a real, streaming, forensically-complete implementation see
`src/tracesignal/assets/converters/nginx2tracesignal.py` — start from it rather than from
scratch when writing a new converter.

### `timesketch2parquet.py`: converting existing CSV/JSONL

If you already have a Timesketch-compatible CSV or JSONL file, don't hand-write a converter —
`src/tracesignal/assets/converters/timesketch2parquet.py` reads exactly the CSV/JSONL formats
described above and re-emits them as an interchange Parquet file, so a very large existing
timeline can be uploaded as a single fast columnar file instead of parsed row-by-row server
side.
