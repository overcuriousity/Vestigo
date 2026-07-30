# Input Data Formats

Vestigo ingests three file formats: **CSV**, **JSONL**, and **Parquet**. This document
specifies exactly what each format must contain to normalize cleanly into a Vestigo
`Event` (`src/vestigo/models/event.py`). Read this before writing a converter or hand-
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
  (`src/vestigo/assets/converters/*2vestigo.py`, downloadable from the web UI) parses
  the raw evidence file entirely on the analyst's machine and writes one columnar `.parquet`
  file; the server ingests it directly with no re-parsing, no intermediate text file touching
  disk, and no possibility of a lossy round trip through CSV escaping. See
  [`TECH_STACK.md` §3.4a](TECH_STACK.md) for why this format was chosen over CSV/JSONL for
  bulk conversion.

Binary evidence containers have no CSV/JSONL equivalent and are always the Parquet path:
`evtx2vestigo.py` reads binary Windows Event Logs (`.evtx`) directly, while
`evtx2timesketch.py` covers *text* exports of the same data (`wevtutil qe /f:xml`,
`evtx_dump`). Prefer the binary path — a text export re-anchors provenance to the dump file
rather than the original `.evtx`.

Format is detected by file extension: `.csv`/`.tsv` → CSV, `.jsonl`/`.ndjson`/`.json` → JSONL,
`.parquet` → Parquet (`src/vestigo/ingestion/parser.py::detect_format`).

## The target: `Event`

Every parser — CSV, JSONL, or Parquet — ultimately produces the same fields
(`src/vestigo/models/event.py::Event`):

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

CSV/JSONL inputs are decoded as UTF-8 with `errors="surrogateescape"`, so `byte_offset`
always counts the file's *real* bytes even when a line is not valid UTF-8 (a Latin-1
logfile, a truncated multi-byte sequence). Undecodable bytes are replaced with U+FFFD in
the event payload itself — the offset points at the original bytes, the stored text is
always valid UTF-8.

---

## CSV

Parser: `TimesketchCsvParser` (`src/vestigo/ingestion/parser.py`). Timesketch-compatible:
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

Parser: `JsonlParser` (`src/vestigo/ingestion/parser.py`). One JSON object per line, UTF-8.
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

## Parquet (Vestigo interchange format, version 1)

Spec module: `src/vestigo/ingestion/parquet_format.py`. Unlike CSV/JSONL, Parquet is not
meant to be hand-written — a local converter script parses raw evidence and writes columnar
batches with `pyarrow`. The server (`ingestion/parquet_reader.py`) validates the schema and
footer metadata, then bulk-inserts the columns with **no per-row re-parsing**. This is why the
schema below is stricter than CSV/JSONL: every column and type must match exactly.

### Required per-row schema

```python
PARQUET_EVENT_SCHEMA = pa.schema(
    [
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
    ]
)
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

Parquet supports arbitrary key-value footer metadata; Vestigo requires these keys
(`schema.with_metadata({...})` in pyarrow):

| Key                                | Value |
|--------------------------------------|-------|
| `vestigo.format_version`         | `"1"` |
| `vestigo.converter_name`         | Converter identifier, e.g. `"nginx2vestigo"`. Becomes the event's `parser_name`. |
| `vestigo.converter_version`      | Converter version string, e.g. `"1.0.0"`. Becomes the event's `parser_version`. Versioned **per converter** — it says nothing about which optional keys below are present. |
| `vestigo.original_files`         | JSON array of `{"name": str, "sha256": str, "size_bytes": int}` — one entry per raw input file (a directory input yields several). |

### Optional forensic footer metadata

Converters may also write additive, self-documenting chain-of-custody footer keys. The
reader does not require them (`validate_parquet_source` ignores them), but they are readable
from the Parquet footer:

| Key                            | Content                                                        |
| ------------------------------ | -------------------------------------------------------------- |
| `vestigo.converted_at`         | ISO-8601 UTC timestamp of the conversion run.                  |
| `vestigo.row_counts`           | JSON `{"parsed", "skipped_malformed", "skipped_by_time"}`.     |
| `vestigo.timezone_assumption`  | Free-text note on any timezone/year assumption.                |
| `vestigo.parse_decisions`      | JSON of format-specific parsing choices.                       |

`vestigo.original_files` entries may likewise carry `path` (absolute source path) and
`mtime` (ISO-8601 UTC); files without them remain valid.

**Probe for these keys — do not infer them from `vestigo.converter_version`.** Each
converter versions itself independently (a newly written one starts at `1.0.0` while a
long-lived one is well past it), so the version number is not a capability marker. Every
converter currently shipped in `src/vestigo/assets/converters/` writes the full set; older
files, hand-written producers, and third-party ones may not.

### Minimal example (Python / pyarrow)

```python
import pyarrow as pa
import pyarrow.parquet as pq
import json
import datetime

schema = pa.schema(
    [
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
    ]
).with_metadata(
    {
        "vestigo.format_version": "1",
        "vestigo.converter_name": "example2vestigo",
        "vestigo.converter_version": "1.0.0",
        "vestigo.original_files": json.dumps(
            [{"name": "auth.log", "sha256": "e3b0c4...", "size_bytes": 128}]
        ),
    }
)

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
`src/vestigo/assets/converters/nginx2vestigo.py` — start from it rather than from
scratch when writing a new converter.

### `evtx2vestigo.py`: binary Windows Event Logs

`src/vestigo/assets/converters/evtx2vestigo.py` parses the `.evtx` container itself (a file
or a directory of them) rather than a text export. It is the only converter needing a second
dependency — `pip install pyarrow evtx` — because binary EVTX parsing is not something the
standard library can do.

What is specific to it:

- **`byte_offset` is a real offset into the original `.evtx`.** The parser exposes no file
  offset, so the converter walks the EVTX chunk structure itself to locate each record.
  `content_hash` is the sha256 of that same raw record span, so
  `dd bs=1 skip=<byte_offset> count=<record_size>` against the original file reproduces it
  with no Vestigo tooling. The `record_size` attribute carries the span length.
  If the scan cannot locate a record, `byte_offset` degrades to the record id and the hash
  covers the rendered XML instead. A record id is indistinguishable from a real offset by
  inspection, so the row states both substitutions itself:
  `byte_offset_basis=record_id` and `content_hash_basis=rendered_xml` (and no
  `record_size`). Those rows are never mistaken for `dd`-reproducible ones;
  `vestigo.parse_decisions.byte_offset_fallback_rows` counts them.
  Because a substituted record id occupies the same column as a real offset, `byte_offset`
  is **not** monotone within an evtx source and a value in one row may numerically equal a
  genuine offset in another. Event identity still holds — those rows hash different bytes,
  so `content_hash` differs — but a reader ordering or ranging on `byte_offset` must filter
  on `byte_offset_basis` first.
  Offsets are scanned per chunk *and per occurrence within a chunk*, so a record id
  duplicated across chunks or repeated inside one (both routine in a re-chunked or
  partially overwritten log) still yields distinct offsets — two records can never collapse
  onto one forensic identity. The footer's `chunk_scan` note reports how many repeats were
  seen, counting each extra occurrence wherever it fell.
- **Damage stays local.** Each 64 KiB chunk is parsed in isolation, so one corrupt chunk
  costs only that chunk instead of aborting the rest of the file. Each chunk is handed to
  the parser as a complete, checksum-valid one-chunk EVTX document.
- **Junk input is reported, not absorbed.** A directory is triage output, so a file whose
  magic is not EVTX (a text export saved under the wrong extension, a zero-byte placeholder)
  is named on stderr and skipped; a file that parses to zero records is warned about too. A
  single named file still fails hard.
- **Attribute names are Sigma-canonical** — see
  [`ANOMALY_DETECTION.md`](ANOMALY_DETECTION.md) §Sigma. Unnamed `<Data>` elements become
  `Data1`, `Data2`, … by position. A record that carries *both* a named `Data1` and unnamed
  positional elements gives the plain key to the named one — that is the name a Sigma rule
  addresses — and the positional value moves to `Data1_pos` rather than being overwritten.
  This is decided from the record as a whole, so it does not depend on which element the
  writer emitted first.
- **No element overwrites another.** EVTX permits a repeated `<Data Name="X">` within one
  `<EventData>` (and `<UserData>` nesting can put two same-named tags on one key). The first
  occurrence keeps the plain spelling Sigma addresses; the rest are numbered in document
  order — `X_2`, `X_3`, … — probing for a free key so a record that also carries a literal
  `X_2` field does not collapse into it. Losing evidence to a name collision with nothing on
  the row to say so is the one outcome the rule exists to prevent.
  The rule also covers collisions between Windows' vocabulary and Vestigo's. The converter
  derives `host`, `user`, `src_ip`, `src_port`, `MapDescription` and the `Map*` properties
  under its own spellings, and those have to win the plain key because the platform reads
  them by name (the GeoIP enricher wants `src_ip`) — so here it is the *native* value that
  steps aside to `host_2`, `user_2`, … rather than being overwritten. Same for the
  `EventData_`-prefixed form a field colliding with a `<System>` name gets: a record
  carrying a literal `EventData_Channel` alongside an `EventData` `Channel` keeps both.
- **EvtxECmd maps are embedded.** The community map corpus from
  [EricZimmerman/evtx](https://github.com/EricZimmerman/evtx) (MIT) is compiled into the
  script by `scripts/vendor_evtx_maps.py` and supplies `MapDescription` plus the `Map*`
  attributes the `message` is rendered from. The corpus commit is recorded in
  `vestigo.parse_decisions`. `--no-maps` skips it entirely and emits only what is literally
  in the record.
- **Not resolved:** `%%1833`-style message-table references stay as-is (rendering them needs
  the originating host's WEVT templates), and `.evtx.gz` is not accepted.

### `timesketch2parquet.py`: converting existing CSV/JSONL

If you already have a Timesketch-compatible CSV or JSONL file, don't hand-write a converter —
`src/vestigo/assets/converters/timesketch2parquet.py` reads exactly the CSV/JSONL formats
described above and re-emits them as an interchange Parquet file, so a very large existing
timeline can be uploaded as a single fast columnar file instead of parsed row-by-row server
side.
