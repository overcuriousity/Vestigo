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

### `pcap2vestigo.py`: packet captures, optionally with HTTP reassembly

`src/vestigo/assets/converters/pcap2vestigo.py` decodes pcap/pcapng captures to one row per
packet (`network:packet:<protocol>`), down to the Ethernet/Linux-SLL/raw-IP, IPv4/IPv6 and
TCP/UDP/ICMP/ARP headers. Only a first fragment carries an L4 header, so for IPv4 and IPv6
alike a row with `fragment_offset` > 0 gets no port/sequence fields rather than invented
ones. `byte_offset`/`content_hash` follow the normal contiguous-span convention: the classic-pcap record header plus captured data, or the whole pcapng block.

`--reassemble http` adds a second row type. It is off by default.

- **What it adds.** One `network:http:transaction` row per HTTP/1.x request/response,
  reassembled from the TCP streams — **in addition to** the packet rows, never instead of
  them. Packet rows are the forensic floor and are byte-identical whether or not the flag is
  used; the transaction row is derived convenience. `artifact_long` is `web:access:request`,
  the same as an nginx access row, and `timestamp` is the request's first captured byte —
  the earliest capture time among the records that carried it, which with an out-of-order
  start is not the packet that completed the header block.
- **Fields**, deliberately spelled as `nginx2vestigo` spells them so a pcap timeline and a
  webserver-log timeline filter identically and a saved View ports across both:
  `http_method`, `http_uri`, `http_protocol`, `http_request_full`, `status_code`. Plus, from
  the reassembly itself: `http_status_reason`, `http_transaction_index` (position within the
  connection, for keep-alive pipelining), `http_request_bytes` / `http_response_bytes` (wire
  bytes), `http_request_body_bytes` / `http_response_body_bytes` (framed body, chunk framing
  excluded), `http_content_encoding` with `http_response_body_decoded_bytes` (or
  `http_response_body_decode_failed`), and `duration_ms`. Response *bodies* are never emitted
  — see `ROADMAP.md` N3 for why they cannot live in `attributes`.
- **Honest gaps.** `http_incomplete`, `http_request_missing`, `http_response_missing`,
  `reassembly_gap` (a hole the capture never recorded — the halves are never silently
  spliced) and `reassembly_truncated_capture` (a snaplen shorter than the frame). A
  transaction still open when the capture ends is emitted flagged rather than dropped.
- **Provenance is a different, explicitly non-contiguous convention**, because a transaction
  spans N packet records that are not adjacent on disk:
  - `byte_offset` = the offset of the record carrying the *request line*. A real offset, but
    the start of one packet, not of the transaction — and **not** necessarily the lowest
    contributing offset, since an out-of-order segment from later in the message can sit
    earlier in the file.
  - `content_hash` = sha256 over the ASCII tag
    `vestigo:http-transaction:<http_transaction_index>\n`, then the concatenation of every
    contributing record's raw span, in capture order (ascending offset). Deterministic and
    re-derivable by hand, but not a single `dd` span. The tag is not decoration: a
    transaction carried by a single packet would otherwise hash exactly that packet row's
    bytes at exactly that packet row's offset, and the server derives `event_id` from
    `(…, byte_offset, content_hash, …)` — two different events under one id.
  - `packet_offsets` lists those offsets (`packet_count` holds the true count;
    `packet_offsets_truncated` marks a capped list), so an examiner can reconstruct the
    exact input.
  - Every such row carries `reassembled=true`, `byte_offset_basis=request_line_record` and
    `content_hash_basis=reassembled_records`, so the two conventions are never confused by
    inspection. The footer's `vestigo.parse_decisions.reassemble` records the flag — it
    changes which rows exist — and `vestigo.row_counts` splits `packets` from
    `http_transactions`.
  - Row order: a transaction is written when its response completes, so with this flag the
    file is no longer sorted by `byte_offset`. Harmless (the server sorts on query), but a
    reader walking the file must not assume monotone offsets.
- **Limits, stated in `--help` too.** Cleartext HTTP/1.0 and 1.1 only. HTTPS is not
  decrypted, so most real traffic yields nothing; HTTP/2 and HTTP/3 are not parsed (binary
  framing plus HPACK/QPACK); a snaplen-truncated or single-direction capture cannot be
  reassembled.
- **Bounded against hostile captures** — the routine case, since this is incident evidence:
  per-stream buffered bytes, concurrently tracked flows (LRU eviction), header count and
  size, framed and decompressed body size (compression bombs), held out-of-order segments,
  and framed requests still waiting for a response. Breaching a cap kills that one flow,
  never the run, and its packet rows survive — except the request queue, whose overflow is
  emitted as an `http_response_missing` transaction rather than discarded. A body delimited
  only by connection close (HTTP/1.0, or `Connection: close` with no `Content-Length`) is
  bounded by the same framed-body cap as a declared `Content-Length`, so a very large such
  download is refused rather than buffered.

The vendored `pcap2timesketch.py` carries the same flag (upstream 1.1.0), emitting the same
transaction fields into CSV/JSONL. It has no per-row provenance — no `*2timesketch` script
does; `--report` is that suite's provenance layer — and it keeps its globally time-sorted
output by reading each capture file a second time to sort the derived rows before merging.

### `timesketch2parquet.py`: converting existing CSV/JSONL

If you already have a Timesketch-compatible CSV or JSONL file, don't hand-write a converter —
`src/vestigo/assets/converters/timesketch2parquet.py` reads exactly the CSV/JSONL formats
described above and re-emits them as an interchange Parquet file, so a very large existing
timeline can be uploaded as a single fast columnar file instead of parsed row-by-row server
side.

## Generated converters (the model writes the script)

When an operator has switched on `converter_generation_enabled` **and** an agent endpoint is
configured and reachable (`docs/AGENT.md`), the upload dialog gains a second mode — *Let AI
write the converter* — and the CLI a matching `vestigo convert-ingest <path> -c <case>`. Both
run the same job (`src/vestigo/converters/job.py`): the analyst uploads any plain-text,
time-annotated file, and Vestigo has the configured model write a converter to the Parquet
contract above, runs it in a guarded subprocess on the server, validates the output, repairs
on failure, and ingests the resulting Parquet through the normal path. Nothing in the data
model changes: **the produced Parquet is the source**, with `parser = <name>@<version>` from
its footer, retained and deduplicated exactly like a Parquet the analyst converted locally.
The differences are `sources.converter_script_id`, which points at the script, and
`sources.converter_input_hash`, the raw file's SHA-256 — a converter's Parquet is not
byte-stable across runs (it stamps `converted_at`), so the source-level hash check cannot see
a repeat; the convert endpoint refuses the *same saved script over the same raw file* with a
409 that names the source it already produced, before any work happens, and the job checks
again before it starts (the CLI and a concurrent submit reach it without the endpoint's
check) — with a partial unique index on `(case_id, converter_script_id,
converter_input_hash)` as the backstop under both, so a race that slips past every pre-check
registers as a duplicate rather than a second copy of the evidence. Another script, or a
fresh generation, over the same raw file is allowed. When a run's Parquet nevertheless
matches an existing source byte for byte, the job completes as a duplicate and the job tray
says so instead of a bare "completed".

### What leaves the host

Exactly this, and the dialog says so before the analyst confirms: a bounded excerpt of the raw
file (`converter_sample_bytes`, default 64 KiB — the head, a middle window and the tail, with
absolute line numbers), the filename, its size, line count and mtime, the version — and,
once known, the name — the script must declare, and the analyst's optional hint. No case, source, timeline or user identifier,
no key, no host name. Reusing a saved converter sends nothing. `tests/test_converter_prompt.py`
asserts the rendered prompt against this list.

The mtime is the **evidence file's own** — the browser sends `File.lastModified`, the CLI
uses the file's `stat`, a regeneration replays the stored `converter_scripts.raw_mtime` — and
it is also stamped onto the private copy the script sees on `-i`, so a year taken from the
mtime and the `original_files[].mtime` a script records are the evidence's, never the upload
time. A raw API client that sends none gets "mtime unknown" in the task header, and the
prompt tells the model to fall back to the newest full date in the sample, else the current
year, and to say which in `timezone_assumption`.

### The loop

1. **Sample.** Binary files are refused up front (NUL bytes / mostly non-printable).
2. **Generate.** One typed model call (`converters/generator.py`) returns `{name, artifact,
   script}`, under `converter_generation_timeout_seconds` (default 180) for the whole
   round — availability probe, config resolution and the model request together. A model
   too slow to finish inside it fails every attempt identically, so this is a setting, not
   a constant: raise it for a large local model rather than cutting the attempt count. The system prompt is rendered from `ingestion/parquet_format.py`
   (`converters/prompt.py`), so it cannot drift from the contract, and it states what the
   harness enforces so the model optimises for the checks that actually run.
3. **Static check.** `converters/runner.py::check_script` allow-lists imports — the
   standard library minus a deny-list (`socket`, `subprocess`, `multiprocessing`, `ctypes`,
   `http`, `urllib`, `importlib`, `threading`, `shutil`, `tempfile`, `builtins`, `runpy`,
   the import-by-string and loader modules `pydoc`/`pkgutil`/`zipimport`/`site`, the
   deserialisers `pickle`/`marshal`/`shelve`, `gc`/`inspect`, `logging.config`,
   `unittest.mock` and friends) plus `pyarrow`/`numpy`, which is the prompt's own "pyarrow +
   stdlib" contract enforced — resolves import aliases (`import os as o; o.system(...)` reads
   as `os.system`), refuses `from <module> import *`, allows a module bound by `import` only
   as the receiver of an attribute access (`x = os`, `f(os)`, `[os]` are refused, so a denied
   attribute cannot be reached through a rebinding), and rejects `exec`/`eval`/`compile`/
   `__import__`/`globals`/`locals`/`vars` wherever they are *named* (not only called),
   `sys.modules`/`sys.path`/frame access, `getattr`-family calls on a module or with a
   dunder-name string, the dunder attributes that walk from any object back to the builtins
   or a frame (`__dict__`, `__subclasses__`, `__bases__`, `__mro__`, `__globals__`,
   `__code__`, …), and the destructive or process-spawning attributes
   (`os.system/popen/exec*/spawn*/fork/kill/remove/rename/...`, and
   `unlink`/`rmdir`/`chmod`/`chown` on any receiver, `pathlib.Path` included), read or
   called. A violation costs an attempt. Still a static, best-effort guard — a determined
   script can look for another path through the object graph: what stands behind it is the
   runner (below), the fact that the script only ever sees a private *copy* of its input,
   and the dedicated app user (`docs/DEPLOYMENT.md`).
4. **Sample run.** The script runs on the head excerpt, staged under the upload's own
   basename and — for a `.gz` upload — re-gzipped, so `-i` looks exactly as it will in the
   full run (a script that handles `.gz` by suffix behaves the same in both phases, and the
   `source_file` it records is the evidence file's name, never the retention hash). The
   uploaded filename is reduced to a basename before it touches any path. The run is
   `python -I` with a scrubbed environment, a private working directory, `RLIMIT_AS`
   (`converter_run_memory_mb`, ≥ 2048 — pyarrow's floor), `RLIMIT_CPU`, `RLIMIT_FSIZE`
   (`converter_run_output_mb`) and `RLIMIT_NOFILE` — applied by a bootstrap *inside* the
   child before it execs the script, not by `preexec_fn` — in its own session, and a 60 s
   wall clock; the group is killed on the deadline even if the script closed its stderr.
5. **Validate** (`converters/validate.py`). Enforced: schema and required footer keys
   (`validate_parquet_source`), `converter_version == "<n>.0.0"` for the version the task named,
   `converter_name` equal to the script row's name (so the footer identity that becomes
   `Source.parser` cannot drift from the row), `original_files[0].sha256` equal to the
   harness's own hash of the input, ≥ 1 row, no null
   in the four provenance columns, ≥ 50 % of rows not marked `attributes.parse_status =
   "unparsed"`, ≥ 50 % of rows with a timestamp. Reported only: the time range and whether
   `byte_offset` is monotonic per file. The validator streams the Parquet in Arrow batches —
   memory stays bounded however large the converter's output is.
6. **Repair.** On failure the model gets the previous script, the structured report (failed
   checks with three offending rows each) and the stderr tail, and is asked for a complete
   replacement. Up to `converter_max_attempts` rounds (default 4).
7. **Full run** on the whole raw file (the job's private copy) with
   `converter_run_timeout_seconds` (default 600) — no repair here: a script that passed the sample and fails the whole file met a
   format change the sample did not show, and sending more evidence than disclosed is not
   the harness's call. The analyst regenerates with a hint instead. In both phases the
   script sees a private read-only *copy* of its input, never a link to the retained
   evidence: a script that reopens `-i` for writing alters only its own copy, and the
   retention file's mode and bytes stay what its hash says.
8. **Ingest.** The Parquet goes through `register_source_for_ingest` and the same ingestion
   job as an upload. If that ingest fails, the script keeps its `working` status (its output
   validated) but the failure is recorded as an `ingest` attempt and a `converter.run` audit
   row, so the trail never asserts a conversion whose source does not exist.

### What is kept

`converter_scripts` (one row per script; case-bound; a regeneration is a new row with
`parent_id`, never an edit): the name and version, the status (`generating` · `working` ·
`failed`), the source code, the model and endpoint used, `prompt_hash` (sha256 of exactly what
was sent, plus the system-prompt version), the sample's hash **and** the sample text itself,
the raw file's hash and mtime (it is retained content-addressed like any source so
regeneration needs no re-upload — and the retention store counts that row, and a converted
source's `converter_input_hash`, as owners, so an unrelated source's rollback never unlinks a
converter's raw input), the hint, and `attempts` — every generation (including a first draft
discarded because its proposed name already existed at a higher version), sample run, full
run and ingest failure with elapsed time, exit code, stderr tail, the validation report, the
script's hash and, for the entries that sent a prompt, that prompt's hash. The row's own
`prompt_hash`/`model` follow the attempt whose draft is the stored `source_code` — a repair
round's prompt differs from the first draft's, and the download header must name the prompt
that produced the code under it. Attempts are appended under a row lock, so two jobs
re-running one saved script cannot overwrite each other's trail.

The raw file is retained only once a row is about to reference it (right before the script
row is inserted, or before the produced source is registered), and a job that fails before
that point takes back the copy it made if nothing references it — a model endpoint that is
down, a file that is not text, or a mistyped script id must not leave a plaintext copy of the
evidence under `data/sources/` that no row names and nothing sweeps.

Every failure is on the trail. Model errors before a row exists are buffered and flushed onto
the row once there is one; if every attempt died in the model call after the excerpt was
sent, a `failed` row named from the file (`auth_log2vestigo`) carries them and is regenerable
with a hint; if the endpoint was unreachable before the first prompt went out, nothing left
the host and only the audit row and the job error say so. A failure after the full run
passed (the Parquet footer refused, the retention volume full, the database) is an `ingest`
attempt and a `converter.run` audit row, and the script keeps its `working` status — its
output validated. A row is `generating` only while its job runs: any failure the job sees
flips it to `failed`, and startup reconciliation does the same for a row a restart orphaned
(jobs are in-memory), recording the interruption as an attempt — before the app accepts a
request, so it can never touch a live generation. A script never shows as generating
forever. Deleting the case deletes its scripts. Audit rows: `converter.generate`,
`converter.run`, `converter.regenerate`. Case export carries the rows and the raw files
(`transfer/`); an archive from an older version simply has none.

A downloaded script (`GET /api/cases/{id}/converters/{sid}/download`, or the panel's Download)
is prefixed with a comment header naming the case, version, status, model, prompt and sample
hashes and the raw input, so provenance travels with the file. Scripts remain listable and
downloadable after the switch is turned off; only starting new work is refused (503).

### Reuse and regeneration

The upload dialog offers every `working` script in the case under *Reuse a converter*: the
saved script runs on the new file with no model call — and so needs only the operator switch,
not a reachable model (`converter_reuse` capability, `503` otherwise): an airgapped site that
imported a case with vetted converters converts with them without ever configuring a model,
and the dialog then offers *Use a saved converter* alone. *Regenerate* (panel or
`POST …/regenerate`) writes version *n+1* from the retained raw file plus a hint — for the
"almost right" case — and, like a fresh generation, needs the model. Editing in the browser is deliberately not offered: download, fix
locally, and upload the Parquet is the path for anything a hint cannot express.

The copy-paste prompts in the downloads panel are rendered by the same module
(`GET /api/converters/prompt`), so a hand-run converter is written to the same contract.

