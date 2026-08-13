# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Detectors can be muted per timeline.** Some defects belong to the evidence rather than
  to the behavior it records — a capture whose sources disagree about the clock makes
  `timestamp_order` fire on millions of true, useless findings — so a strip at the top of
  the Investigate rail takes a method out of the sweep entirely: no findings, no histogram
  or grid marks, no query issued. The mute is shared case state on the timeline
  (`PATCH .../timelines/{id}/muted-methods`, audited) rather than a browser preference, so
  the next analyst inherits the conclusion instead of rediscovering it. It is a reading
  preference and never a lock: the analysis plan does not consult it, and a muted method
  still runs from Tools when asked for by name. The rail always names how many detectors it
  is holding back, and the Tools accounting counts them apart from both "ran" and "skipped".

### Changed

- **The Tools sheet is now tabbed, Scope first, and reachable directly.** Its four sections
  were one long scroll in which a thousand-row template list buried the baseline picker —
  the control that reframes every other section — below all of it. Each section is now its
  own tab that scrolls independently, and the Investigate rail header carries a Tools
  button, so the sheet no longer has to be reached sideways through an error message or the
  skipped-methods summary. Every existing entry point still lands on its own section.

### Fixed

- The Sigma and pattern-mining guidance panels rendered twice in the Tools sheet, once from
  the sheet and once from the panel inside it.

## [1.12.0] — 2026-08-13

### Added

- **The Investigate surface is now a findings rail plus one overlay sheet.** Findings are
  grouped by the *kind of claim* they make — named techniques, statistical outliers ("odd,
  not necessarily bad"), exploration ("leads, not verdicts") — which is the one thing the
  old panel never said: a Sigma hit and a rare value are not the same assertion. Presets
  filter the feed by the question being asked rather than by detector. The rail is the only
  fixed-width surface the analysis flow spends; everything else opens as an absolutely
  positioned sheet, so detail can be wide without ever narrowing the event grid. The sheet
  has three modes — a finding, a method, and Tools — and sizes to its content rather than
  the viewport.
- **A finding's sheet states the claim in prose, names its subject, and shows the query
  shape behind it.** Every number in the sentence comes from the payload; the field and
  value that triggered the finding are the first thing on the surface; the SQL sketch is
  labeled as a teaching aid rather than a transcript, because the detectors do not return
  their compiled statement and presenting one anyway would be a claim we cannot point at
  code for. The method's parameters are editable in place and re-run from there, which is
  what keeps the analysis gate advice rather than a lock in the UI as well as the API.
- **An analysis gate.** `GET /api/cases/{id}/timelines/{id}/analysis/plan` answers, per
  method and without scanning a single event, whether that method *can* produce a finding
  on this data — from the per-source field-stats cache plus one timestamp-range probe. A
  method is marked `not_applicable` only when it structurally cannot score, never when it
  looks unpromising, and it stays runnable on request: the plan is advice plus an audit
  record. Each verdict carries the arithmetic behind it ("no field parses as numeric (0 of
  19 sampled)"), so it is a claim an analyst can check and argue with.
- **A fingerprint-keyed cache for findings.** The key covers every input that can change an
  answer, and sources are immutable, so a cache hit is proof the answer still holds —
  deliberately no TTL. It is purged with the source or case it derives from, and is
  distinct from `DetectorRun`, which remains the forensic diary of what an analyst ran.
- **Verdicts record the comparison they were reached under.** Dispositions carry an
  `analysis_scope`, and `confirmed` is the one kind whose identity includes it: escalating a
  finding against the February baseline and again against March are two claims, not one
  deduplicated row. Findings badge verdicts reached under the scope on screen and mark
  verdicts reached elsewhere as such, instead of silently presenting one as the other.
- **A scope-change dialog that states its consequences in numbers** — how many methods will
  re-run and how many verdicts were reached under the scope being left — and a baseline
  builder reachable by marking a range directly on the histogram.
- **The evtx converter now detects silent parser attrition.** pyevtx-rs skips a damaged
  record and ends a broken chunk without raising, so counting exceptions reported a clean
  run over lossy evidence. The converter reconciles scanned record headers against records
  returned, per chunk, and reports the difference per file and in
  `vestigo.parse_decisions.scan_unresolved_records`. Detection, not recovery: the missing
  records are counted, not restored.

### Changed

- **The rail's ranked feed carries a display floor for the two band methods.** Findings
  rotate method-by-method so every method has a row near the top, which means a value one
  band width outside its learned band would otherwise sit above one tens of band widths
  out. Numeric-range and entropy findings below 2× band leave the feed; the group's count
  still includes them and a row says how many, one click from showing them. Presentation
  only — the methods still return everything, and methods with a threshold of their own
  (frequency's z, the q-gated two-window methods) keep it in the run where it belongs.
- **`sequence_novelty` and `entropy` are no longer gated off where they can still score.**
  Sequence novelty required three distinct series values; two yield eight distinct trigrams
  and a rare one scores fine, so every ordinary two-source timeline had quietly lost n-gram
  novelty. Entropy shared charset's enum-like gate, which only holds for charset.

### Fixed

- A `confirmed` verdict is deduplicated by the comparison it was reached under — frame and
  baseline id — rather than by the whole scope object, so renaming a baseline no longer
  splits one claim into two rows with two system annotations.
- The analysis plan's timestamp probe reads the raw sort-key column and widens its span by
  the declared clock offsets, instead of aggregating an expression that could not use the
  index — an unbudgeted full read of the case on every scope change.
- `delete_source` purges the analysis cache, which held event ids, field values and message
  templates from the deleted source.
- A detector's cached answer records the *mode* it ran in (`analysis_mode`), which the
  runner had been overwriting with the requested method id.
- The Investigate rail no longer claims "no findings under this scope" for a preset whose
  methods all need setup and never ran, and the routine-collapse chip no longer announces
  that zero events are hidden.
- Out-of-band evidence strips label the value at its marker rather than mid-track, where it
  read as sitting inside the band — the exact misread the to-scale marker exists to prevent.

### Dependencies

- Backend: fastapi 0.140.13 → 0.141.1, uvicorn 0.51.0 → 0.52.2, qdrant-client 1.18.0 →
  1.19.0, pysigma 1.4.0 → 1.5.0, pydantic-ai-slim 2.19.0 → 2.29.0, typer 0.27.0 → 0.27.1,
  h2 4.3.0 → 4.4.1, ruff 0.16.0 → 0.16.2.
- Frontend: vite 8.1.5 → 8.2.0, jsdom 29.1.1 → 30.0.1, oxlint 1.75.0 → 1.77.0,
  `@tanstack/react-virtual` 3.14.8 → 3.14.9, the Radix checkbox and toast primitives, the
  React type packages, `@vitejs/plugin-react` 6.0.4 → 6.0.5, and a transitive nanoid
  advisory (GHSA-2v37-7h3g-55p8). The container's Node base image goes 24-alpine →
  25-alpine.
- **`@tanstack/react-table` 8.21.3 → 9.0.0, migrated onto the new feature API** rather
  than its v8 compatibility shim. `EventGrid` now declares the three features it actually
  uses — column sizing, column resizing, column visibility — and subscribes to the single
  state slice it reads instead of to the whole table state. Row selection, sorting,
  filtering and expansion stay deliberately unregistered: this grid does all four against
  the server, and enabling the features would keep a second, empty copy of that state
  beside the real one. Two things moved and would have failed silently: the live resize
  state is `columnResizing.isResizingColumn` (was `columnSizingInfo`), so a column's width
  would have stopped being persisted on drag-release, and `ColumnDef`/`Header` take the
  feature set as their first type argument. `src/test/eventGridResize.test.tsx` covers the
  resize gesture end to end, since neither failure is visible to a type check.

## [1.11.0] — 2026-08-07

### Added

- **An `empty` field match mode** — a fourth mode beside exact, wildcard and regex that
  asks whether a field has a value at all. Include means "is empty", exclude means "has
  a value". It rides the existing per-key mode maps, so URLs, saved views, export,
  bulk-annotate and the histogram all inherit it, and the filter rail renders it as a
  static `(empty)` label rather than a value input the backend would ignore. The
  predicate is `ifNull(<col>, '') = ''`: an absent `attributes` map key already reads as
  `''` in ClickHouse, the cast covers non-String columns, and the coalesce stops a NULL
  from evaluating to NULL and dropping the row from *both* sides of the filter.
  Whitespace-only values count as values — `' '` is what the source recorded, and
  folding it into "absent" would make the filter lie about the evidence.
- **Drag grid columns by their headers to reorder them.** Visible columns were already
  an ordered array driving render order, so a drag rewrites it into the same
  per-timeline override a manual column choice writes, under the precedence rules that
  already existed. The checkbox, annotation and expand columns stay pinned outside the
  sortable set, and the pointer sensor takes an 8px activation distance so clicking the
  timestamp sort button still sorts.
- **Saved views carry the grid column layout.** A view now records the columns it was
  saved with, inside its already-opaque JSON payload — no migration, no backend change.
  Views saved before this leave the current layout untouched, which falls out of the
  undefined case rather than needing a version flag. The agent's finding cards and story
  pushes deliberately omit the key: neither speaks for a grid.
- **Search and delete in the saved-views list.** Case-insensitive substring search
  appears once the list passes five entries (five rows are faster to scan than to
  filter), and each row gains a delete behind a confirmation, since deleting an
  unreferenced view is not undoable. The `DELETE` endpoint and its client method already
  existed and had simply never been called from the UI.
- **Resume an enrichment run that was interrupted before its results were saved.** The
  enrichers dialog now surfaces any run whose staged results were never applied — how
  many events across how many sources, and how long ago — with a **Resume** button that
  applies them without re-scanning anything. Previously the only recovery was startup
  reconciliation, so a run orphaned while the app stayed up (see the ClickHouse fix
  below) was unrecoverable short of restarting the app. Recovery is deliberately manual
  — no timer, no reconnect hook — so every partition rewrite traces to a named actor;
  the request and its outcome are both audited (`enricher.resume_requested`,
  `enricher.job_recovered` with `trigger`, `enricher.resume_failed`).

### Fixed

- **Two enrichment applies could still meet over one source.** The run route read its
  conflict check ~65 lines above the claim it belonged to, with three awaits in between,
  and discarded the claim's return value — so the loser of a race spawned its job anyway,
  and, holding no run slot, its *live* marker read as dead to the enrichers dialog, which
  then offered Resume on a running job. Separately, startup reconciliation (which runs
  after the app is serving) never claimed a slot at all, so the dialog offered Resume for
  a marker it was in the middle of applying. Because the ClickHouse scratch tables are
  keyed on the job id alone while the apply lock is per source, the two applies could
  finalize one source's partition from another's rows — a `mapUpdate` matching nothing
  plus stale-key stripping that removes derived keys that were there. The claim is now
  the authoritative check everywhere, and reconciliation holds the slot for the duration
  of its apply.
- **The interrupted-run banner tells the truth after a half-finished resume.** Its
  partial-coverage caveat compared the durable "sources fully staged" count against the
  count of sources *still* staged — and a resume deletes staged rows as it applies them,
  so the comparison inverted exactly when the caveat applied. The API now reports
  `partial_sources` as a set difference. A marker with nothing left staged also no longer
  claims "0 events across 0 sources were enriched but never written"; it says the run
  left nothing pending and that resuming clears the record.
- **The enrichers dialog admits when a run is in flight.** A live run's marker is hidden
  on purpose, which left "Run now" enabled during any run in progress — including a
  startup recovery still applying — with a 409 as the only feedback. The listing now
  reports the running job and the buttons disable on it, server-derived so it survives a
  page reload.
- **Auto field selection is reproducible.** The novelty-field recommender ranked
  recommended-first, then by coverage — and stopped there. Coverage ties are the normal
  case (every field of one source covers that source's events), so the tie order came
  from a ClickHouse `GROUP BY` that does not promise a stable row order. The detectors
  that auto-pick fields take the top 1 (`value_novelty`) or top 2 (`value_combo`) of
  that list, so the same timeline could be scanned on different fields each time it was
  opened. The ranking now breaks ties on the field token, and the inventory query orders
  its `LIMIT` by `cov_count DESC, key ASC` so truncation is stable too.
- **The enrichment partition rewrite no longer raises insert parallelism.**
  `VESTIGO_ENRICHMENT_APPLY_MAX_INSERT_THREADS` was removed rather than re-defaulted:
  ClickHouse's default of 0 means a single-threaded `INSERT SELECT`, so shipping 2 gave
  each thread its own squashing buffer on the exact query that OOM-killed a host — more
  write-side memory, presented as a guardrail.
  `VESTIGO_ENRICHMENT_APPLY_INSERT_BLOCK_BYTES` now defaults to 64 MiB (it previously
  shipped ClickHouse's own 256 MiB default, so it bounded nothing) and is no longer
  marked restart-required, because the apply reads it per call.
- **The enrichers dialog clears its own interrupted-run banner.** A resume's partition
  rewrite runs after the request returns, so the marker was still present at the only
  refetch the dialog did — leaving "an earlier run was interrupted" on screen, with Run
  now disabled, until the dialog was closed and reopened. The tracked job now invalidates
  the enrichers list on completion, and Resume stays disabled until that job is terminal
  instead of re-arming into a 409 against the analyst's own resume.
- **One interrupted run, one job id.** The dialog surfaced the oldest stale marker while
  the run route's 409 named the newest, so with more than one marker an analyst could
  resume the job they were shown and be blocked again by a job nobody mentioned. Oldest
  wins everywhere now. The auto-trigger's skip log also no longer reports a healthy
  in-flight run as needing a resume.
- **A ClickHouse OOM-kill during enrichment (`exited with code 137`).** The enrichment
  partition rewrite carried the heavy-scan memory settings but never took a slot in the
  admission gate that every detector scan takes, so it stacked on top of a full set of
  admitted scans — each holding its own per-query cap — and the kernel killed
  clickhouse-server mid-apply. Note the symptom: exit 137 is SIGKILL from *outside*, not
  a ClickHouse memory error, so nothing appears in the server's own log. It now holds a
  gate slot across the whole rewrite including the `REPLACE PARTITION` swap, which
  queues merges on freshly written parts that the per-query cap never covered, and its
  write side is bounded separately (`VESTIGO_ENRICHMENT_APPLY_INSERT_BLOCK_BYTES`, the
  block squash floor, set below ClickHouse's default) because the scan settings bound a
  read while the rewrite also materializes a full copy of the partition.
  - **Operators on a full-docker stack should also set memory ceilings.** The automatic
    scan budget is detected in the *app* container and sizes itself as though ClickHouse
    owned the machine, which is wrong as soon as they are separate containers. The
    reference `docker-compose.yml` now carries **commented** `mem_limit` values sized
    for a 32 GiB host and a new `deploy/clickhouse/memory.xml.example` drop-in — nothing
    changes on upgrade, you have to opt in. A worked example and the relationship
    between the three ceilings are in `docs/DEPLOYMENT.md` §"Resource sizing".
- **Editing a scan-guardrail setting in the admin console appeared to work and did
  nothing.** The SETTINGS clause is built once at import and the admission semaphore is
  shared by value across the scan modules, so no `VESTIGO_STAT_SCAN_*` edit could reach
  the running process. They are now declared restart-required and labelled as such,
  rather than silently accepting an edit with no effect.
- **Starting a fresh enrichment run no longer strands an interrupted run's results.**
  The run route consulted neither the durable job marker nor the staged rows, so "Run
  now" minted a new job id, re-scanned everything, and orphaned the earlier run's output
  permanently — output that is not even recomputable once the enricher's data version
  has changed, since it is stamped with a pinned config hash. Both the manual route and
  the post-ingest auto-trigger now refuse (409) or skip while an unfinished run exists,
  pointing at Resume; the conflict checks run before the "already enriched, nothing to
  do" short-circuit, which would otherwise have been a misleading answer.
- **One failing enricher eligibility check no longer blanks the whole enrichers dialog.**
  The checks were gathered without `return_exceptions`, so an unreachable ClickHouse
  emptied the list — precisely when an analyst needs to see the resume banner. Failed
  checks now render with their error.
- **"Force re-run" survives a page refresh.** It was gated on component state that only
  appeared after a skipped run in the same dialog session, making the documented
  recovery path unreachable after any reload. It is now a standing affordance. Relatedly,
  "Run now" is no longer disabled by a negative eligibility result — that is a heuristic
  sample scan and must never be the thing that locks an analyst out of running.
- **Deleting a saved view that a story embeds no longer breaks that story's export.** A
  `view_ref` block resolves its view live at render and export time; deleting a
  referenced view froze a `resolution.error` in place of the evidence the block was
  built on — the quieter and worse outcome for a forensic report. Deletion is now
  conditional: an unreferenced view is removed, a referenced one is hidden via a new
  nullable `views.deleted_at` and disappears from every list an analyst sees while the
  story keeps working, then is hard-deleted once the last reference goes. The sweep runs
  after every block delete, block content update and story delete, so no path can forget
  it.
- **The event-grid header scrolls with its columns.** It was a sibling of the scroll
  container, so scrolling right moved the columns out from under it. The same fix sizes
  rows to the content rather than the viewport, which had been cutting their background,
  hover state and borders off at the right edge whenever columns overflowed.
- Several defects found reviewing the above: `specToEventFilters` dropped the `empty`
  mode while keeping its placeholder value, so a finding card's "open in Explorer" ran
  an exact match against the literal empty string and excluded the very NULL rows the
  agent had counted; the click-to-filter reducer had the same blind spot from the other
  direction; an `empty` mode naming a key absent from `filters` validated and was then
  ignored, answering with the whole timeline; `delete_view`'s count-then-delete could
  race a `view_ref` write and recreate the dangling reference the design exists to
  prevent; the virtualizer's scroll margin used `offsetTop` against an unpositioned
  ancestor and put the rendered window hundreds of pixels off; and `viewMatchesFilters`
  compared a key `filtersToViewPayload` never emits, so every explorer-saved view was
  unreusable and each story push minted a duplicate.

### Changed

- **Vendored `*2timesketch` converters re-synced to upstream 1.1.0 (`1bbe64f`).** The only
  behavior change is in `pcap2timesketch.py`, which gains the `--reassemble http` flag
  back-ported from `pcap2vestigo`: one `network:http:transaction` row per reassembled
  HTTP/1.x request/response, emitted in addition to the packet rows, with the same field
  names (`http_method`, `http_uri`, `status_code`, …), the same client/server orientation of
  `src_ip`/`dst_ip`, and the same per-flow caps against hostile captures. Two deliberate
  differences from the native converter, both following the vendored suite's own
  conventions:
  - **No per-row provenance.** No `*2timesketch` script emits `byte_offset`/`content_hash`
    (those columns exist because the Vestigo Parquet schema requires them), so the
    transaction-hash machinery is not ported — `--report`'s audit report remains that
    suite's provenance layer. Rows keep `packet_count`, `reassembled`, `http_incomplete`,
    `reassembly_gap` and `reassembly_truncated_capture`.
  - **Output stays globally time-sorted.** A transaction row is stamped with its request's
    first captured byte but produced when the response completes, which would break the
    k-way merge's per-stream ordering invariant. Each capture file therefore contributes a
    second, lazily evaluated stream that re-reads the file, keeps only the derived rows and
    sorts them (spilling to temporary JSONL past 200k rows) before the merge. Cost: an
    enabled capture is read twice.
  Also fixed upstream, since the reassembler depends on it: IPv6 fragments now report
  `fragment_offset`, and a non-first fragment's payload is no longer decoded as a transport
  header (which invented ports, a sequence number and a phantom flow). `pcap2vestigo` had
  both fixes already.

- **`pcap2vestigo` 1.5.0: IPv6 `fragment_offset` is now reported in bytes**, as IPv4's always
  was — the wire field counts 8-byte units, and reporting the raw unit count left one column
  carrying two meanings depending on IP version. Only non-first fragments are affected
  (internally the value is used as an `== 0` test, so no decoding behavior changes), and the
  vendored `pcap2timesketch` was written this way from the start.

## [1.10.0] — 2026-08-06

### Added

- **`pcap2vestigo --reassemble http` — HTTP/1.x transactions from a packet capture.**
  Off by default. With it, the converter reassembles TCP streams (ISN tracking,
  sequence-ordered buffering, retransmit and overlap handling, gap tolerance, FIN/RST
  teardown, LRU/idle eviction) and frames HTTP/1.0 and 1.1 on top — chunked encoding,
  keep-alive pipelining, `Expect: 100-continue`, `Content-Encoding` (stdlib zlib) — emitting
  one `network:http:transaction` row per request/response **in addition to** the per-packet
  rows. Packet rows are the forensic floor and are byte-identical with and without the flag;
  the transaction row is derived convenience. Still stdlib-only: pyarrow remains the
  converter's single dependency. Transaction rows reuse `nginx2vestigo`'s field names
  (`http_method`, `http_uri`, `http_protocol`, `http_request_full`, `status_code`), so a
  pcap timeline and a webserver-log timeline filter identically and saved Views port across
  both.
  - **A reassembled row's provenance is deliberately not a contiguous span.** It carries
    `byte_offset` = the record holding the request line, `content_hash` = sha256 over the
    tag `vestigo:http-transaction:<index>` and then the concatenated contributing records in
    capture order (the tag is what keeps a transaction carried by a single packet from
    hashing that packet row's own bytes at that packet row's own offset), and
    `packet_offsets` /
    `packet_count` so an examiner can reconstruct the exact input — plus `reassembled`,
    `byte_offset_basis` and `content_hash_basis` so the two conventions are never confused
    by inspection. The flag is recorded in `vestigo.parse_decisions`: it changes which rows
    exist. Rows are no longer strictly ordered by `byte_offset`, since a transaction is
    written when its response completes.
  - **Stated limits** (in `--help`): no HTTPS — nothing is decrypted, so most real traffic
    yields nothing — no HTTP/2 or HTTP/3, and nothing useful from a snaplen-truncated or
    single-direction capture; such transactions come out flagged `http_incomplete` /
    `reassembly_gap` / `reassembly_truncated_capture`, or not at all.
  - **Bounded against hostile captures**, which is the routine case for incident evidence:
    caps on per-stream buffered bytes, concurrently tracked flows (LRU eviction), header
    count and size, framed and decompressed body size, and held out-of-order segments.
    Breaching a cap kills that one flow — its packet rows survive — never the run.

- **ASN enricher — "who operates this IP" on every timeline.** A second enricher
  (`enrichers/asn.py`) resolves IP-shaped attribute values to the announcing AS number and
  organization via an admin-uploaded MaxMind GeoLite2-ASN database, writing
  `<attr>:asn_number` / `<attr>:asn_org` siblings through the same staging and atomic
  partition-apply machinery GeoIP uses. The upload flow, availability checks and admin UI
  needed no changes — they were already generic over `Enricher.asset_spec`. The Explorer
  merges the operator into the existing GeoIP cell tooltip (e.g. "Frankfurt, Germany —
  AS12345 Example Hosting"), and shows an "AS" marker when only ASN output exists. Deliberate
  scope: GeoLite2-ASN is *not* whois — no netname, registrant, abuse contact or allocation
  dates — and the enricher says so in its name and description. Like the ingestion
  converters, the module is self-contained by design: it mirrors `geoip.py`'s MaxMind
  mechanics rather than importing a shared base, so each enricher stays an independent
  reference plugin.

### Changed

- **IP eligibility now covers IPv6 for both enrichers.** The eligibility regex (an
  re2-compatible gate pushed into ClickHouse; `enrich_value` still validates via stdlib
  `ipaddress`) was widened from IPv4-only to IPv4+IPv6 — pcap timelines are exactly where
  IPv6 turns up. The pattern is an input to GeoIP's `config_hash()`, so sources enriched
  before this change report a new hash and are offered re-enrichment; re-running is safe
  (the apply strips and rewrites the enricher's derived keys). The IPv6 arms deliberately
  require either all eight groups or a literal `::`, so MAC addresses (`00:1a:2b:3c:4d:5e`)
  and bare times (`13:45:02`) do not read as eligible — otherwise a pcap/DHCP/ARP timeline
  containing neither an IPv4 nor an IPv6 field would be offered enrichment and auto-run a
  full-timeline scan that can only produce zero rows.
- **An IPv4-only MaxMind database no longer fails an enrichment run.** `maxminddb` raises a
  bare `ValueError` (not `AddressNotFoundError`) for an IPv6 lookup against a database whose
  metadata says `ip_version == 4`, and the job loop re-raises anything else — so with IPv6
  eligibility, one IPv6-shaped value would have failed the whole job for an operator whose
  uploaded `.mmdb` carries only IPv4. Both enrichers now check the database's address family
  first and treat such a lookup as an ordinary miss.

### Fixed

- **A single-packet HTTP transaction no longer collides with the packet row it came from.**
  When a transaction's whole exchange fit in one record, its `content_hash` covered exactly
  that record's bytes at exactly that record's `byte_offset` — the pair the server derives
  `event_id` from — so two different events landed under one id, and an annotation keyed by
  that id hit both. The hash now carries a transaction tag ahead of the records; it stays
  re-derivable by hand, and the tests re-derive it.
- **A body delimited only by connection close is bounded like every other body.** HTTP/1.0
  (or `Connection: close` with no `Content-Length`) hit the one framing branch that checked
  no cap, and the per-stream limit could not cover it, so a single large download grew the
  converter's memory to the size of the response. It now takes the same verdict as an
  over-long `Content-Length`: that flow dies, its packet rows survive, the run continues.
- **Requests waiting for a response are capped.** A single-direction capture — the case
  `--help` already warned yields flagged-or-nothing — queued one unanswered request per
  request for the length of the run. The overflow is emitted as an `http_response_missing`
  transaction rather than dropped: evidence that arrived should not vanish because its
  answer did not.
- **Pipelined responses are framed against their own request.** A `HEAD` and a `GET`
  answered within one packet framed the second response with the first's method, reporting a
  body of zero bytes and desyncing everything behind it.
- **A transaction is stamped with the request's first captured byte**, as the format
  documentation always said — not with whichever packet completed the header block, which
  with an out-of-order start is a later packet and, when one arrival drives both directions,
  could even be the peer's. `duration_ms` inherited the same skew.
- **A non-first IPv6 fragment is no longer decoded as TCP.** IPv4 had the guard; IPv6 handed
  the fragment's body bytes to the L4 decoder, inventing ports and a sequence number — and,
  under `--reassemble`, a phantom flow built from payload. IPv6 packet rows now carry
  `fragment_offset` as IPv4 rows already did.
- **Records consumed before a skipped capture gap stay in the provenance.** They contributed
  bytes to the message but were dropped from `packet_offsets` without a trace, which is
  exactly the silent loss that list exists to prevent.

## [1.9.1] — 2026-08-02

### Changed

- **The demo case's story now demonstrates what a story can hold.** It shipped as fourteen
  markdown blocks and nothing else — it reads well, but it is the only worked example most
  users see, and it quietly taught that a Vestigo story is a text editor. The narrative now
  uses all four block kinds: the spray's first failed logon, the encoded PowerShell, the
  persistence service install and the first upload chunk are frozen `event_ref` blocks with
  captions; the matching saved filter sets are embedded as `view_ref` blocks; and three
  charts are embedded where the shape of the data carries the argument. The demo case also
  now creates four saved charts of its own, so the Visualization page has content on a first
  login instead of an empty canvas.

  The seed runs every block through the same content and referent-scope validation the HTTP
  router and the agent go through, and a new test resolves the seeded story into a real
  export snapshot and asserts no block fails to resolve — a broken embed in the shipped
  example would otherwise surface only as a frozen `resolution.error` in someone's first
  report.

## [1.9.0] — 2026-08-02

### Added

- **A worked demo case, seeded for every new user.** A new account's first screen used to
  be an empty case list, which is the worst possible introduction to a tool whose whole
  argument is detection-as-workflow. Every user now finds a fabricated investigation
  waiting: 251k events across four sources over 30 days, with a quiet intrusion buried in
  it, plus the analyst artifacts that show what working the case looks like — annotations,
  saved views, a baseline definition, a Sigma rule and a story. It is *generated* per user
  from code that ships with the app, not shipped as a data file, so it costs nothing in the
  repo and nothing on an airgapped host. One per account: deleting it is final, and
  `POST /api/demo/seed` is the way back. Off with `VESTIGO_DEMO_CASE_ENABLED=0`.
  `tests/test_demo_detector_coverage_clickhouse.py` asserts every shipped analysis tool
  still finds something in it, so retuning a detector cannot quietly hollow the demo out.

- **A timeline opens on columns its own data justifies.** Every timeline used to open on
  timestamp/artifact/message regardless of what it held; the fields that would say something
  — `user`, `src_ip`, `event_id`, whatever this corpus has — were a popover away and only if
  you knew to look. A pure scorer over the existing per-source field-stats cache now
  recommends a column set per timeline (no new ClickHouse scans on the common path), stored
  on `Timeline.recommended_columns` and shared by everyone with access. A per-user column
  choice in the browser always outranks it, and the grid renders defaults immediately rather
  than blocking on the job. "Suggest with AI" adds one typed LLM call that may only reorder
  and select from the scorer's own candidates; it is an explicit per-(user, timeline) opt-in
  behind a disclosure naming the endpoint, the model and what is sent, so no automatic
  trigger — ingest, timeline creation, the CLI, the demo build — ever causes egress. Every
  run writes a `timeline.recommend_columns` audit row.

- **A derived `annotated` tag.** Any event carrying an annotation — a human tag or comment,
  an agent proposal, or a detector finding — also carries the tag `annotated`, filterable
  alongside parser tags and analyst tags in the same panel. It is computed at read time
  rather than stored, so it cannot disagree with the annotations it describes: delete the
  last annotation and the tag goes with it, with no write path to maintain.

### Changed

- **The frontend design system now has a ratchet.** Seven dead `var(--…)` references had
  compiled, typechecked, linted and passed tests, because nothing checked. Undefined custom
  properties are now a hard failure at zero across every `.ts`/`.tsx` under `src/`, and
  arbitrary `text-[Npx]` plus raw `<button>` outside `components/ui/` are budgeted per file
  in a list that only ever falls — exceeding an entry fails, and so does beating one without
  lowering it. Also lands a guidance registry and actionable empty states.

- **README reordered** so the tool comes before the comparison to prior art.

- **Dependencies updated.** FastAPI 0.139.2 → 0.140.13, pydantic-ai-slim 2.17 → 2.19, React
  and React DOM 19.2.7 → 19.2.8, lucide-react 1.26 → 1.27, `@tanstack/react-virtual` 3.14.8,
  six Radix primitives, the frontend build image to `node:24-alpine`, and the
  `docker/login-action` / `github/codeql-action` workflow actions.

- **The frontend build image tracks Node LTS, and CI builds on the same major.** The bot's
  bump landed on `node:25-alpine`, an odd-numbered release that goes end-of-life mid-2026 —
  a poor floor for an image operators install and then never update on an isolated host.
  Pinned to `node:24-alpine` instead, with `ci.yml` and `release.yml` moved from Node 22 to
  24 so the runtime that tests the frontend is the one that builds the shipped image.
  Workflow actions are pinned by major throughout again (`github/codeql-action@v4`,
  `docker/login-action@v4`), so a patch-level fix no longer waits for a bot PR.

### Fixed

- **A timeline's canonical mapped fields now render as columns.** Field mappings resolved in
  filters, aggregations and detectors but never in the rows themselves, so a canonical field
  like `ip_address` — offered by the column picker, since discovery already substitutes it —
  drew an empty column on every row, and the column recommender excluded mapped fields
  outright rather than suggest one that looks broken. The presented page now carries each
  canonical field alongside the raw keys it was coalesced from, by the same rule the filter
  SQL applies, so a rendered value and a filter on that field cannot disagree; the
  recommender scores the canonical field in place of the raw spellings, counting a source
  toward it whichever spelling that source uses. Resolution reads a key stored under the
  canonical name itself first — validation rejects such a mapping, but only against a known
  inventory, and a source ingested afterwards can introduce that key — so an ingested value
  is what both the filter and the row carry rather than data the filter silently ignores.
  Each row declares which of its attributes came from the mapping, and the event detail
  panel marks exactly those as mapped and names the raw fields behind them — the value is a
  timeline view, not something the source file carried. Exports still carry raw attributes
  only.

- **`GET /api/cases/` no longer runs `alembic upgrade head` on every request.** The endpoint
  the UI hits most re-ran the migration machinery per call — a connection, a version-table
  check and a migration lock on the hot path — where the startup lifespan already does it
  once.

- **OIDC authorization codes no longer reach the system journal.** Uvicorn's access log
  writes the full request target, and the callback carries `code` and `state` as query
  parameters, so every SSO login logged a live credential in the clear. Sensitive parameter
  *values* are now redacted while the path and parameter names survive, so an operator can
  still see that a callback carried a code. The audit trail was already clean.

- **OIDC discovery follows redirects**, which a Nextcloud IdP needs; it previously answered
  every SSO click with a 500.

- **A saved chart is now the slice it was built over.** `SavedChart.config` held chart shape
  only, so a chart built with Explorer filters active redrew over the whole timeline
  everywhere it was reused — the story block, the frozen export, the "Open in Visualize"
  link — showing precisely the data the analyst had excluded. The filters travel with the
  chart now, including the scopings only an agent's `ChartSpec` can carry (a detector run,
  an explicit event set, routine collapse), so the card, the export and the rail agree by
  construction. Charts saved before this have no filters to recover; re-save them from the
  filtered view.

- **A story's chart block opens *that* chart.** "Open in Visualize" navigated to the right
  timeline and then drew a default chart with the preset picker open, because the Visualize
  page reads its entire state from `c_*` URL params and the link carried none of them. It
  now names the saved chart instead of describing it — `?c_chart=<id>`, resolved against
  storage — so the shape *and* the filters come back, including the three narrowings that
  have no URL form at all and would otherwise have widened an agent-scoped chart to the
  whole timeline in silence. The rail's load button uses the same reference, and editing
  either half spells the chart out in full and drops it. A link to a deleted chart says so.
  While the URL names a chart, none of the page's own defaulting effects may write it back:
  the field default, the scale probe, the time-field suggestion and the metric clamp all
  stand down. Any one of them firing would have counted as the analyst taking the chart
  over — rewriting the URL as `c_*` params seconds after the link opened, and dropping the
  three narrowings on the way out.

- **`read_story` no longer cuts the analyst's report unmarked.** Markdown blocks were
  truncated at 1600 characters — 0.6% of what a write accepts — with no marker, so the agent
  summarized half a paragraph believing it had read the block. Markdown now has its own
  per-block and per-response budget, every cut carries `truncated` and the real
  `text_length`, and short blocks are charged only what they hold.

## [1.8.6] — 2026-07-30

### Added

- **`evtx2vestigo`: binary Windows Event Log converter.** Parses `.evtx` containers
  directly (file or directory) instead of a text export, so `file_hash` anchors to the
  original evidence. `byte_offset` is a real offset into the `.evtx` and `content_hash`
  covers that same raw record span, so `dd`+`sha256sum` reproduces it without Vestigo;
  where a damaged chunk forces a substitute, the row says so itself in
  `byte_offset_basis` and `content_hash_basis` rather than leaving a record id to pass
  for an offset.
  Each 64 KiB chunk is handed to the parser as a complete, checksum-valid one-chunk
  document, which recovers records the whole-file path loses at the first damaged chunk;
  record offsets are resolved per chunk *and per occurrence within a chunk*, so a record
  id repeated in a re-chunked or partially overwritten log still yields distinct offsets
  either way. No value is lost to a name collision: repeated `<Data Name="X">` elements
  are numbered rather than overwritten, and where a converter-derived key (`host`, `user`,
  `src_ip`, `Map*`) collides with a native field of the same name, the native value moves
  to a numbered spelling instead of disappearing. Attribute names are Sigma-canonical
  (`EventID` as an unpadded string, `Channel`, `Provider_Name`, native `EventData` names),
  so community Windows rules compile to exactly the predicate they look like with no
  field translation. The EvtxECmd map corpus (468 maps,
  [EricZimmerman/evtx](https://github.com/EricZimmerman/evtx), MIT) is embedded for event
  descriptions; `--no-maps` opts out. Requires `pyarrow` and `evtx`.

- **Tool calls in the agent panel are expandable.** Every tool row now unfolds to the
  exact arguments the agent sent and what the tool returned, persisted rows and live
  stream alike. (#203)
- **The agent panel shows which Explorer view the agent sees.** A persistent bar names
  the filters inherited as context, and each sent message is stamped with the filter
  snapshot the agent received with it, so a mid-investigation filter change is visible
  in the transcript instead of silently shifting the agent's ground. (#205)
- **Charset detector: per-identifier scoping.** `group_field` learns one alphabet per
  value of a second field (e.g. per host), retiring the merged-alphabet caveat.
  Suppressions stay keyed on `(field, value)` and apply across groups. Group count costs
  rows, not queries: the per-group alphabets travel into one scan per field as array
  parameters, with `LIMIT … BY grp` keeping each group's budget.

  The two skip guards mean opposite things and are treated as such. An alphabet over
  5,000 characters means the *question* does not apply — a novel character carries no
  signal in free text — so that group is dropped and named in `warnings`. Fewer than 20
  distinct values means not enough *evidence*, which does not exonerate the group, so it
  is scored against a fallback reference: events outside the suspect windows in temporal
  mode, or the merged whole-scope alphabet in self-baseline mode — exactly what the field
  was measured against before grouping, so enabling grouping never narrows coverage.
  "Absent from the baseline window" is the zero case of the same condition and takes the
  same route. Every finding records which reference scored it and how much evidence its
  own group contributed (`details.group_basis`,
  `details.group_baseline_distinct_values`, both shown on the row). The fallback learn is
  a whole-scope scan, so it runs only when some group needs one — a bounded probe over
  the suspect windows decides that far more cheaply. A non-string `group_field` is
  refused with 422. (D14)
- **Sequence detectors: `max_gap_seconds`.** `sequence_novelty` and `sequence_motif`
  break an n-gram when consecutive events are farther apart than the bound, retiring
  the manufactured-sequence caveat. Unset keeps the pre-1.8.6 behavior bit-identical.
  (D14)

### Security

- **`react-router-dom` upgraded 7.18.1 → 7.18.2**, picking up the 7.x backport of the RSC
  CSRF fix for GHSA-qwww-vcr4-c8h2. No source changes. Vestigo was never exposed — the
  advisory covers the unstable RSC APIs with server actions, and Vestigo is a SPA with no
  `unstable_*` imports — so this is defense in depth. Note that security tooling may keep
  flagging the advisory: its published range (`>= 7.12.0, < 8.3.0`) was never amended to
  exclude 7.18.2. Do not "fix" it by downgrading; `npm audit fix --force` installs 7.11.0.

### Fixed

- **A grouped charset run no longer scans `events` once per group.** With a high-cardinality
  `group_field` that was one heavy scan per group per field; it is now one scan per field.
  (D14)
- **A tool row in the agent panel no longer formats its payload while collapsed.** A
  `<details>` renders its children regardless of state, so every row was stringifying its
  full tool result on every panel render. (#203)
- **A streamed tool result is only paired on a non-empty `tool_call_id`**, so a provider
  that emitted an empty id could not splash one result across every unkeyed row. (#203)
- **An orphaned tool result passes silently instead of rendering as a call.** A result row
  whose call row is missing from the transcript found no open call to pair with and fell
  through to the call branch, drawing an argument-less tool row for a call the agent never
  made. `tool_result` settles it — the server writes args on a call row and a result on a
  result row, never both. (#203)
- **`view_filters` is bounded (16 KiB serialized, 422 above it).** It is persisted per user
  message, so an unvalidated client dict could grow the transcript at will. (#205)
- **`evtx2vestigo`: a named `DataN` field no longer collides with an unnamed positional
  one.** The named value keeps the plain key — that is what Sigma rules address — and the
  positional value moves to `DataN_pos`, decided from the record as a whole so it does not
  depend on which element the writer emitted first.
- **`evtx2vestigo`: no `EventData`/`UserData` element overwrites another.** A repeated
  `<Data Name="X">` — which EVTX permits — silently kept only the last value. The first
  occurrence now keeps the plain spelling and the rest are numbered in document order
  (`X_2`, `X_3`, …), probing for a free key so a record carrying a literal `X_2` does not
  collapse into it.
- **A grouped charset run's `warnings` describe what it actually did.** Groups routed to
  the fallback were reported as "not evaluated", and a field whose fallback could not be
  learned reported absent groups as unevaluated even when none existed. Warnings now name
  the groups, separate "no baseline values" from "too few baseline values", and state
  which guard the fallback itself tripped. Every one of those warnings names its *field*:
  the same group can be thin for one field and absent from the baseline window for
  another, and a merged count left no way to tell which. A run that hits the grouped
  scan's 5,000-row ceiling says so too — that ceiling orders by novelty across all groups,
  so hitting it drops whole low-novelty groups and a silent result would read as "these
  are the groups with novel characters". (D14)
- **`max_gap_seconds` measures elapsed seconds, not second boundaries crossed.** The gap
  used ClickHouse `dateDiff`, which reports 2 for a 1.2 s step that straddles two
  boundaries — so a bound of 1 s broke bursts whose steps were barely over a second. It
  uses `age` now. (D14)
- **A tool row in the agent panel pairs on `tool_call_id` rather than on whether the row
  carries arguments.** A zero-argument call persisted with `null` args read as a result
  row, consumed its sibling's pending entry and folded a result onto the wrong call. The
  row is also keyed by call identity now, so its expanded state cannot follow a position
  in the transcript, and its open state has a single owner instead of a native toggle and
  a React handler racing on the same click. (#203)
- **The downloadable converter LLM prompts match the data contract again.** The Parquet
  prompt documented the pre-1.3.0 footer: it omitted the forensic metadata keys
  (`converted_at`, `row_counts`, `timezone_assumption`, `parse_decisions`) and the
  `path`/`mtime` provenance fields, and sent timezone assumptions into a script comment
  the server never reads. The CSV/JSONL prompt now mentions pipe-separated tags. (#204)

- **The login-backoff tracker's entry cap is now an actual bound.** `LoginBackoff` pruned
  expired entries when full, then inserted unconditionally — so when pruning could free
  nothing, because all `max_entries` keys were simultaneously locked into the future, the
  in-memory table grew past its cap for as long as the flood sustained those locks.
  Pruning now falls back to evicting the entry whose lock expires soonest, and a key that
  is already tracked skips the bound check entirely. Failed-login throttling itself is
  unchanged. Eviction necessarily discards the evicted key's failure count — the entry is
  the slot being freed — so that key gets `login_backoff_threshold` unthrottled attempts
  before a lock re-arms. Reaching that path costs an attacker ~50k requests at the
  defaults, and the freed key is whichever lock was closest to expiring anyway.

## [1.8.5] — 2026-07-29

### Fixed

- **Exported events carry real hashes again, not a Python `bytes` repr.** `content_hash`,
  `file_hash` and `embedding_config_hash` are ClickHouse `FixedString(64)` columns and
  arrive as NUL-padded `bytes`; the row normalizer decoded `datetime` and `UUID` but not
  these, so both the CSV and the JSONL export stringified them through `repr` and every
  exported hash read `b'028ab6…'` — a value that can never compare equal to the SHA-256 of
  the evidence it exists to verify. The Explorer's event responses shipped an unset
  `embedding_config_hash` as 64 literal NUL characters for the same reason. All three read
  paths (query, similarity, fetch-by-id) now decode and strip through one idempotent helper.
  Re-export any file whose hashes were meant to be checked against the original.

- **Inserting a view, chart or event block no longer leaves the story view unclickable.**
  Every modal Radix layer sets `pointer-events: none` on `<body>` and restores the value it
  captured on mount. `BlockPicker` opened a modal dialog from inside a modal dropdown's
  `onSelect`, so the dialog captured the menu's own `"none"` as its "original" and put it
  back when it closed — with no layer left open. The page kept rendering and polling
  throughout, which is why it read as a freeze rather than a lock; text blocks, the only
  item that opens no dialog, were unaffected. The menu is now non-modal, so the dialog is
  the only layer managing the lock, and a test fails if the menu ever locks `<body>` again.

- **"Add at top" adds at the top.** `after_block_id: null` deliberately means *append* on
  create and *top of document* on move, so the editor's top inserter had no way to say
  "top" — a block above everything has no anchor to name — and sent the append. Create now
  takes an explicit `at_top`, rejected with 422 alongside `after_block_id` so the contract
  is visible in the OpenAPI schema; the default append that "Add to story" and the agent's
  `propose_story_block` rely on is unchanged. Both top-of-document paths share one
  position calculation instead of the move path owning a private copy.

- **Exported stories draw their charts.** (#197) `ChartFrame` starts at zero width, learns
  its real width from a `ResizeObserver` and renders nothing until it has one — and the
  export runs through `renderToStaticMarkup`, which runs no effects and has no
  `ResizeObserver`, so every chart block emitted an empty `<div>` and nothing errored. The
  snapshot renderer now supplies a starting width (848px, the article width minus its
  gutters) that a live `ResizeObserver` still overrides, so on-screen charts are unchanged.
  Exports stay real `<svg>`: selectable text, no resolution ceiling, still self-contained.

- **A bundle built with podman now installs on a docker host.** Podman stores a locally
  built, unqualified image as `localhost/vestigo-app:<tag>` and saves it under that name;
  docker `load` keeps the name verbatim but resolves the bare `vestigo-app:<tag>` the
  compose file and the installer asked for to `docker.io/library/vestigo-app` — so the
  install aborted with `missing image(s) after load` for the image whose load the log had
  just reported, on an intact bundle with a matching checksum. Only docker targets saw it:
  podman resolves the same short name to `localhost/`. The app image is now written fully
  qualified in the builder, the compose file, and the installer's check, and a test fails
  if any of the three drifts. Existing bundles install after one
  `docker tag localhost/vestigo-app:<tag> vestigo-app:<tag>`; `docs/DEPLOYMENT.md`
  §Troubleshooting records it.

- **Opening a story that contains a text block no longer freezes the story view.** (#193)
  The block reported edit mode upward through an effect that depended on the callback's
  identity, and the editor passed a new callback on every render and never reused its
  edit-state `Set` — so each render caused the next, at roughly 700 a second, for a
  single block and with no interaction at all. React reports that as a console error
  rather than throwing, which is why it looked like a hang and not a crash. Both halves
  are fixed, so either one alone keeps the view responsive. Deleting a block while it
  was being edited also left the "your draft is kept" notice up permanently; it now
  clears with the block.

- **A story's embedded view rows are windowed.** A view block put up to 200 rows into a
  short scroller and rebuilt every one of them on every render, for every such block in
  the story. Only the visible rows are built now. The count beneath the table still
  describes the whole embedded set — the same set the export snapshot renders — and
  cells that no longer fit show their full text on hover.

- **The agent's story-block proposals now appear as cards while the turn is running.**
  An agent turn that proposed blocks showed bare `propose_story_block` tool rows instead,
  and kept showing them after the turn until the panel was remounted. The chat panel
  decided what a tool call renders as in five independent per-tool lists — the persisted
  transcript, the two live stream folds, the proposals refetch, and the tool selector's
  warning — and the tool had been added to one. Disabling `propose_story_block` or
  `propose_chart` in the tool selector also removed their cards with no warning. All five
  now derive from one map, so a proposal tool is wired into every path or none.

- **The airgapped installer no longer trusts a container engine that says "loaded" and
  means "registered".** `docker load` writes an image's metadata before unpacking its
  layers and exits 0 even when every layer fails — so on a host that cannot mount
  overlay, four images arrived that `docker image inspect` was perfectly happy with and
  no container could start from. `install.sh` believed them, copied the payload over a
  running install and started a stack in which nothing ran. It now reads the engine's
  output and treats an unpack error as fatal, and proves each image is *usable* by
  preparing a throwaway container from it rather than only checking that it exists.
  A host that cannot unpack is now reported as such, with the engine's own error quoted,
  before anything on the install is touched.

- **The bundle no longer carries a compose file that `docker compose` auto-discovers.**
  It shipped as `docker-compose.yml`, and the compose project name is pinned, so a
  command run from the extracted bundle rather than the install directory targeted the
  real stack with no `.env` beside it. It travels as `compose.airgap.yml` and only
  `install.sh` puts the canonical name in the install directory.

- **`docs/DEPLOYMENT.md` gains a container-install troubleshooting section** for the two
  host problems that look like bundle problems: Docker's containerd image store (the
  default since Docker 28) failing to mount overlay inside an unprivileged LXC guest,
  and runc being unable to mount `/proc` when that guest is not allowed to nest.

## [1.8.4] — 2026-07-27

### Added

- **Airgapped installation now covers the container path, not just the native one.**
  "Airgapped" previously meant the `uv` install: the image build pulled `node:22-alpine`
  and `python:3.13-slim` unconditionally, so building on an isolated host failed at DNS —
  and the obvious retry, `docker compose up -d`, silently restarted the *old* image,
  which is indistinguishable from a successful deploy. `scripts/airgap-bundle.sh` now
  produces a single verifiable tarball on the connected side (application image, all
  three backing-service images, compose file, `.env.example`, `nginx-tls.conf`,
  checksums, installer), and `deploy/airgap/install.sh` is the whole far side: it
  verifies its own checksums, loads and confirms every referenced image *before*
  touching the running stack, creates `.env` only when there is none so an upgrade keeps
  the operator's configuration, and installs into a stable directory (`/opt/vestigo`, or
  `--dir`/`VESTIGO_INSTALL_DIR`) so a new bundle upgrades the existing stack instead of
  standing up a second empty one beside it. `--app-only` upgrades the application alone.
  The image build takes `--build-arg FRONTEND_STAGE=frontend-prebuilt`, which sources
  `frontend/dist` from the build context through a `FROM scratch` stage so the node base
  image is never resolved. See `docs/DEPLOYMENT.md` §Airgapped installation, now a
  runbook covering build, carry, install, upgrade, back up, roll back and diagnose.

### Fixed

- **One malformed agent tool argument no longer takes down the whole app.** A provider
  returned a `propose_chart` spec as a JSON *string*; the chart card's shape check ran
  `'chart_type' in spec` against it and threw, and with no error boundary anywhere in the
  frontend that unmounted **every** route — Explorer, Cases, Admin — not just the card.
  Tool arguments are stored verbatim as the model emitted them, so the row was permanent
  and every re-render of that conversation hit it. Render failures are now contained at
  three levels (the page inside the app shell, the router, and each agent card that draws
  model-authored JSON), a contained failure shows a notice naming what could not be
  displayed, and the rest of the page keeps working. **Rebuild the frontend when
  upgrading** — the crash was in shipped JS, and a stale `frontend/dist` hides the fix.

- **A nested tool argument handed over as JSON text is now accepted, not rejected.** Some
  providers stringify nested object arguments; only the top level of a tool call's
  arguments is parsed for us, so the inner value arrived as text and failed validation on
  tools the model was otherwise using correctly — every filtered query in the toolset
  takes a nested filter spec. Both sides now normalize: the agent's tool models parse a
  stringified value at any position that can only have meant an object (never a free-text
  field, so a search for `{"a": 1}` still searches for that text), and the frontend does
  the same when reading stored calls back. Previously a stringified comparison layer was
  silently dropped from a chart, and a stringified filter map produced filters that were
  wrong rather than absent.

### Changed

- **Chart cards in agent conversations predating per-call tool ids.** Where two or more
  chart proposals were in flight without ids, the card and its validation result could
  only be paired by order — and a call is recorded *before* its validation runs, so a
  successful result could pop a *rejected* spec and draw a chart contradicting its own
  title. Such batches now render no card; the transcript still records every call. Single
  proposals are unaffected, as are all conversations since ids were added. A missing card
  is recoverable; a wrong one, read as evidence, is not.

  **This applies retroactively**: cards are rendered from the stored transcript on every
  open, so an affected conversation that showed a batch of chart cards yesterday will
  show none of them after this upgrade. Nothing was deleted — the calls, their arguments
  and their results are all still in the transcript, and the charts can be rebuilt from
  the Visualize page. What is gone is the claim that a given card belonged to a given
  proposal, which was never something the stored rows could support.

## [1.8.3] — 2026-07-27

### Added

- **Every setting is editable in the admin console** (`Administration → Settings`),
  stored in the database and applied without a restart. Configuration now resolves per
  field: an environment variable pins the field (shown read-only, with its variable
  name), otherwise the stored override applies, otherwise the built-in default. Values
  are validated against the same rules the environment layer gets, before anything is
  written; a stored value that a later version rejects is ignored with a warning rather
  than blocking startup. Bootstrap configuration stays environment-only —
  `VESTIGO_POSTGRES_URL`, `VESTIGO_ENVIRONMENT`, `VESTIGO_LOG_LEVEL`, the
  `VESTIGO_ADMIN_*` seed, and the data directories. Secrets are never returned by the
  API and can be refused database storage entirely with `VESTIGO_SECRETS_MODE=env-only`.
  Console-stored secrets live in the metadata database in plaintext — treat Postgres
  backups accordingly. The CLI reads the same layer, so a console-tuned value applies to
  `vestigo ingest` and `vestigo embed` too. See `docs/DEPLOYMENT.md`.

- **`VESTIGO_TRANSFER_ENABLED`** — master switch for case export/import. When off, the
  feature is absent from the UI and starting an export or import answers 503. An archive
  a previous export already produced stays downloadable; it is single-use and swept from
  disk shortly after.

### Changed

- **Unconfigured subsystems are now hidden consistently.** `/api/health` reports a
  `capabilities` map (embeddings, agent, MCP, OIDC, enrichers, Sigma, case transfer) and
  the UI renders no entry point for an unavailable one — previously only the AI agent
  behaved this way, while embeddings left a disabled embed wizard and a Similarity tab
  that could only fail. The agent's two embedding-backed tools are likewise removed from
  its tool server instead of answering with an error. The map requires a session:
  an anonymous `GET /api/health` still answers with liveness, version and `oidc_enabled`,
  which is what the login page needs.

- **`.env.example` no longer pins settings by accident.** Variables that only restated
  their own default are commented out, since a set variable now makes that field
  read-only in the console. Connection strings and the admin bootstrap are unchanged.

## [1.8.2] — 2026-07-27

### Fixed

- **A case import can no longer be started twice** ([#184]). The Import button
  stayed enabled for the entire upload, because the import dialog only recorded
  the job id once the upload promise resolved — on a multi-GB archive that left
  a minutes-long window in which a second click started a second import of the
  same file. Submission is now blocked synchronously, so a double-click cannot
  slip through before the button re-renders as disabled. Every transfer in the
  app got the same guard, including the source upload, where a second click
  previously cost a full duplicate upload of a file the server would then
  discard as a duplicate hash.

- **Every large file transfer reports progress and can be cancelled** ([#183]).
  Uploads and downloads alike showed a disabled button and nothing else, so a
  multi-GB transfer was indistinguishable from a hang. All of them now report
  bytes moved with throughput and a time estimate, and can be stopped in
  flight:

  - **Uploading a log source** — the largest routine transfer in the app, and
    previously the blindest: the ingest job that the job tray shows does not
    exist until the whole file has landed, so on a multi-GB source everything
    up to that point was silent.
  - **Case export and import.** Both now also name the server-side phase —
    "Verifying archive integrity", "Packing events", "Restoring original source
    files" — with a percentage for the phases that process many items. Sealing
    and hashing the finished archive shows a moving bar rather than a stalled
    one; it counts no items, so there is no percentage to show.
  - **Exporting events as CSV/JSONL.** The response is streamed with no length
    known in advance, so this reports bytes received without a percentage. The
    dialog also no longer claims there is "no memory limit": the server streams
    it, but the browser holds the whole file until the download finishes, and
    for a very large export a case archive is the better tool.
  - **Uploading an enricher asset** (GeoIP and similar, hundreds of MB).

  Cancelling is safe everywhere it is offered: the server streams an upload to
  a temporary file and creates rows and jobs only once all of it has arrived,
  so a cancelled upload leaves nothing behind, and a cancelled export download
  leaves the archive on the server for a retry. A running import is tracked in
  the job tray, so closing the dialog no longer hides it.

- **Re-selecting the same file after a failed case import works.** The import
  dialog never cleared its file input, so picking the same archive again fired
  no change event and the button looked dead. Fixed for every file picker in the
  app (case import, source upload, Sigma rule upload, admin enricher assets),
  which now share one implementation — that also means files dropped onto the
  source-upload zone are checked against the accepted types, as they always
  were when picked through the file dialog. Keyboard users get one tab stop per
  picker instead of two.

- **The Visualize page states the filters it inherits.** It charts exactly what
  the Explorer grid is showing, but said so only in the caption underneath the
  chart, so a chart of one narrow slice looked identical to a chart of the whole
  timeline — a real risk for a figure exported into a report. The active filters
  now appear above the chart as removable chips, with an explicit "No filters —
  charting the whole timeline" when there are none, a one-click way to clear
  them, and a link back to the Explorer. Removing an exclusion, tag or
  time-range chip in the comparison-layer editor also works now; those chips
  were inert.

- **"Clear all filters" only appears when something is actually filtered.** The
  Explorer's filter rail and toolbar decided this by counting any non-empty
  member of the filter state, so a sort order or a leftover match-mode setting
  was enough to offer to clear filters on an unfiltered view. All three
  surfaces — rail, toolbar and the new Visualize bar — now share one definition
  of "filtered", matching exactly what the filter chips render.

## [1.8.1] — 2026-07-26

### Fixed

- **Large id filters no longer fail with a ClickHouse 500** ([#181]). Any filter
  resolving to a large Postgres-side event-id list — `annotated=`, `ids=`, tag
  include/exclude — bound the whole list as a single `Array(String)` query
  parameter. Past roughly 3,300 ids the driver form-encodes that parameter and
  ClickHouse's form parser rejects the oversized field (`code: 1000, HTML Form
  Exception: Field value too long`), so a case became progressively
  un-filterable as tagging grew and agent bulk-tagging hit the wall quickly.
  Membership lists past a threshold now travel as ClickHouse **external data** —
  a multipart file part with a 1 GiB ceiling instead of a 128 KiB field cap —
  and filter with `IN (SELECT * FROM …)`, which builds a hash set rather than
  scanning a constant array per row. Applied to every large-list filter, not
  only the reported one. A filter that still overflows now answers **413** with
  an actionable message instead of a raw ClickHouse error, on the Explorer and
  on streaming exports alike.

- **Byte offsets are correct for sources containing invalid UTF-8** ([#156],
  [#161]). Offsets were measured over text decoded with `errors="replace"`;
  because the replacement character re-encodes to three bytes, every
  `byte_offset` after the first undecodable byte was wrong and the
  event-to-source-byte provenance invariant silently broke on real-world logs
  (a Latin-1 logfile, a truncated multi-byte sequence). Offsets are now measured
  over the file's real bytes, while the stored event text keeps the same U+FFFD
  substitution as before — the offset points at the original bytes, the stored
  text is always valid UTF-8. See `docs/INPUT_FORMATS.md`.

  **Upgrade note:** `byte_offset` contributes to an event's derived id, so
  re-ingesting a file that contains invalid UTF-8 produces different event ids
  than a previous ingest of that same file. Already-ingested data is unaffected —
  ids are derived once at ingest and nothing recomputes them. See
  `docs/DEPLOYMENT.md` § Stability & upgrades.

- **The Parquet event-id identity invariant is enforced under `python -O`**. It
  was guarded by a bare `assert`, which the optimizer strips — turning a broken
  identity into silent evidence corruption rather than a loud failure. It is now
  a descriptive error, and the linter rejects new bare asserts in production
  code.

- **Case and source deletion no longer block the event loop** ([#155]). The
  synchronous Qdrant calls in the delete cascade now run on a worker thread,
  matching the ClickHouse deletes beside them.

- **Job status responses can no longer tear mid-serialization** ([#157]). A
  worker thread updating a job's progress while a polling request serialized it
  could change the payload mid-encode. `progress` and `result` are now
  snapshotted under a per-job lock.

### Changed

- Streaming exports and paginated reads that carry a large filter upload the
  filter's value list once per read rather than once per batch, and identical
  lists referenced by two predicates share a single upload.

[#184]: https://github.com/overcuriousity/Vestigo/issues/184
[#183]: https://github.com/overcuriousity/Vestigo/issues/183
[#181]: https://github.com/overcuriousity/Vestigo/issues/181
[#156]: https://github.com/overcuriousity/Vestigo/issues/156
[#161]: https://github.com/overcuriousity/Vestigo/issues/161
[#155]: https://github.com/overcuriousity/Vestigo/issues/155
[#157]: https://github.com/overcuriousity/Vestigo/issues/157

## [1.8.0] — 2026-07-26

### Added

- **Stories** — a per-case block document where the investigation's narrative
  and its evidence live together, so the report assembles itself while the work
  happens instead of being written afterwards (roadmap Phase 3 Step 3 / W7;
  design in `docs/superpowers/specs/2026-07-26-w7-stories-design.md`, reference
  in `docs/STORIES.md`). A story is an ordered list of blocks —
  `markdown | view_ref | chart_ref | event_ref` — and embeds stay **live** while
  an analyst writes, so the document tracks the data as ingestion and detection
  progress. "Add to story" buttons on the Explorer filter rail, the saved-chart
  rail, event detail and agent finding cards push evidence in without leaving
  the analysis surface; a push carrying live filter state saves a View first, so
  an embed always references a persisted object.

  **Export freezes a point-in-time snapshot.** `POST .../exports` resolves every
  block server-side — view queries through the same path the Explorer uses,
  charts through the shared `execute_chart_spec` — and stores the bundle with a
  SHA-256 over its canonical JSON. That snapshot is the authoritative record;
  the browser then renders it to a standalone HTML document (styles inlined, no
  network access at all) and uploads it once. Exports are immutable: the
  artifact seals exactly once and deletion is admin-only. Per-block resolution
  is individually wrapped, so a view deleted before the export freezes as a
  visible `resolution.error` rather than vanishing, and one bad block never
  fails an export. Truncation is always stated — a report showing 200 of 14203
  rows says which it is. An export whose HTML upload fails stays usable as JSON
  and can be re-rendered from its stored snapshot ("Render HTML"), so a
  transport failure never costs the attestation.

  **Collaborative at block granularity.** Every block carries an optimistic
  `version`; a stale write returns 409 with the winning row and the editor keeps
  the local draft, offering load-theirs or overwrite. Other analysts' changes
  arrive by polling. No CRDT and no WebSockets — the same call the streaming
  milestone already made for the live Explorer. Block **delete** carries the
  same guard (`?version=N`, 409 when stale): deleting a block a collaborator
  has meanwhile rewritten is the one loss the version cannot undo afterwards,
  so it is not the mutation that skips the check.

  **Agent parity from day one.** The phase spec had deferred agent-authored
  stories; that deferral was rescinded during the design round, on the standing
  principle that the agent can do what an analyst can do. `list_stories` and
  `read_story` are read tools (also on the external `/mcp` endpoint), and
  `propose_story_block` drafts a block through the existing propose→confirm
  machinery — the analyst's confirm is the write, and the block lands with
  `origin: agent`. A chart may be proposed with an inline spec, which is saved
  as a chart and embedded in one step. Block edit/move/delete and export stay
  analyst-only: parity covers analytical contribution, not document arrangement
  or the attestation act.

### Changed

- `AgentProposal` gained `kind`/`payload` (migration `0017`) so annotation and
  story-block proposals share one decide path and its 409 idempotency backbone;
  pre-existing rows read as `kind="annotation"`.
- Chart execution and chart rendering each moved behind one shared seam —
  `agent/chart_exec.py::execute_chart_spec` server-side, `viz/ChartCanvas`'s
  `ChartMarks` client-side — so a chart is validated and drawn identically in
  the Visualize page, an agent proposal card, a story block and an exported
  report. No behavior change to `propose_chart`.
- Deleting a **case** that carries sealed story exports now requires an
  administrator, and the destroyed exports' hashes go into the `case.delete`
  audit record. Deleting a single export, or a story carrying any, was already
  admin-only because an export is an immutable attestation; the case cascade
  takes the same rows, so without the same gate it was the way around both.

### Security

- The story-export HTML artifact is authored entirely by the client and served
  back from the app's own origin. `Content-Disposition: attachment` already kept
  a browser from rendering it there; the download now also sends
  `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`, so
  that defense is not one header deep. Every UI path treats the response as a
  download, so nothing changes for users.
- `VESTIGO_STORY_EXPORT_MAX_SNAPSHOT_BYTES` is now enforced *during* export
  resolution rather than over the finished bundle. Measuring only at the end
  bounded what got stored while still materializing an arbitrarily large bundle
  first — the worst legal case under the default caps held hundreds of thousands
  of frozen rows in memory, plus a second copy as the serialized string, before
  anything rejected them. Resolution now stops at the block that crosses the
  ceiling, and the 413 names it.

## [1.7.0] — 2026-07-25

### Added

- **Case export/import (`.vestigo` archive)** — any case leaves the instance as
  a single versioned zip and comes back intact, on the same or a different
  instance (roadmap Milestone 9 / X1; design in
  `docs/superpowers/specs/2026-07-24-case-export-import-design.md`). The archive
  carries every case-scoped Postgres entity (including audit rows), all
  ClickHouse events as per-source Arrow IPC, and — behind an explicit
  `include_blobs` flag — the original source-file blobs, with a SHA-256 per
  member verified before import writes anything. `POST
  /api/cases/{case_id}/export` is MANAGE-gated and audited (`case.export`);
  `POST /api/cases/import` is open to any authenticated user and restores as a
  new case owned by the importer with no other grants (audited `case.import`).
  Import remaps every Postgres id through an in-memory old→new map while event
  ids are preserved verbatim, so annotation→event cross-references survive;
  unknown usernames fall back to the importer with a warning; secrets (tokens,
  passwords, enricher API keys) are never exported. Frontend: export button on
  the case card, import dialog on the case list, both on the existing
  job-polling pattern.

  Because an uploaded archive is untrusted input from any authenticated user,
  the reader treats sizes as load-bearing: every member's declared size is
  cross-checked against the zip directory, every read is bounded by it, and
  `VESTIGO_TRANSFER_MAX_EXPANDED_BYTES` (default 200 GiB, `0` disables) caps
  the total uncompressed size before a single member is read — a decompression
  bomb can otherwise exhaust memory or disk inside a small upload. Because a
  total says nothing about any *one* member, `VESTIGO_TRANSFER_MAX_METADATA_BYTES`
  (default 2 GiB, `0` disables) additionally caps each `postgres/*` member, and
  the importer streams every one of them row by row rather than materializing
  it — peak memory scales with the largest single row, not the largest entity.
  Event Arrow members are checked against the current event schema before
  reaching ClickHouse. Reads are also restricted to manifest-listed members, so
  an archive missing an entity stream fails instead of silently restoring a
  case without it. In-flight archives live under `VESTIGO_TRANSFER_TEMP_PATH`
  (default `data/transfer`), created `0700` (and repaired to it) and refused if
  owned by another user or not a real directory; they are deleted on download,
  expired after 24h by the next export, and cleared at startup — a sweep that
  removes only Vestigo's own archives and job directories, never anything else
  it finds under the configured path. Restored events have their embedding
  markers blanked and the import warns that vectors need re-embedding, since
  Qdrant data is not portable.

  Export and import do their hashing, zipping and archive verification off the
  event loop, so a multi-GiB transfer no longer stalls the rest of the API for
  its duration. A failed import removes the blobs it had already written to the
  instance-global retention directory, leaving nothing untracked behind, and
  the frontend holds the import dialog open to show the importer's warnings
  instead of navigating past them.

  Restored `audit_log` rows keep the actor, action and timestamp the archive
  asserted — that is what makes an exported chain of custody worth having —
  but nothing on the importing instance vouches for them. Every imported row
  therefore carries `detail.imported` (import job id, importing user, source
  case id) and is badged **imported** in the admin audit view, so a forged
  archive can never read as locally recorded activity. Their `target_id` is
  remapped along with everything else, so a restored audit trail still points
  at the entities it describes. `VESTIGO_TRANSFER_MAX_CONCURRENT` (default 2,
  `0` disables) caps in-flight transfers instance-wide; the count and the job
  creation happen under one lock, so simultaneous requests cannot both slip
  past a cap of 1, and an import over the cap is rejected with 429 before its
  upload is accepted (and its temp file removed if a slot fills while the body
  streams). Blob members no source in the archive references are ignored rather
  than written to the instance-global retention directory.
- **`clickhouse` pytest marker** — registered in `pyproject.toml` and applied to
  all eleven `tests/*_clickhouse.py` files, so `pytest -m clickhouse` selects
  the dev-stack tests and a ClickHouse-less run can no longer pass them
  silently (roadmap Milestone 2 residue).

## [1.6.1] — 2026-07-24

### Fixed

- **Agent context-window overflow** — an analyst investigation died silently
  when a 76k-token request passed a 49k budget against a 65k-context local
  model (the provider 400 surfaced as a dead turn), and the retry then re-ran
  the full orientation sweep three times. Root causes and fixes:
  - **The budget never counted the advertised tool schemas.** 30 tools with
    `FilterSpec` inlined ~14 times cost ~38.8k chars (~12.9k tokens) per
    request and ride outside `messages`, invisible to the window processor.
    `budget_for` now reserves them, measured per-scope by
    `schema_chars_for_scope` (`disabled_tools` changes the advertised set).
  - **The estimator assumed chars/4.** Real tool payloads (escaped JSON,
    base64 params, dotted-quad IPs, UUIDs) measured 2.35 chars/token on the
    overflow. The default is now 3.0, and `calibrate_chars_per_token` learns
    the true ratio from the provider's own error body (clamped to 1.5–5.0),
    persisted per conversation and reused by later turns — no tokenizer,
    airgapped-safe. The retry budget and the persisted `role="window"` row
    carry the ratio actually used, so a reduced request stays reproducible.
  - **Overflow retry trusted a budget already known to be wrong.** The
    provider-reported window now wins over a blind 0.6 shrink; the shrink
    remains only as the no-hint fallback.
  - **Empty-list filters answered full-size.** `{"src_ip": []}` behaved like
    an absent filter, so a model that kept "narrowing" kept getting the whole
    unfiltered timeline. `FilterSpec` now rejects empty value lists with an
    actionable message naming `field_terms`.
  - **Duplicate calls and runaway totals inside one request.** A per-request
    guard (`_RequestGuardToolset`) collapses identical
    `(tool, canonical-args)` calls to a `{"duplicate_of": …}` back-reference —
    safe under pydantic-ai's parallel tool execution — and caps one request's
    total tool-return bytes at half the budget. Both actions are counted on
    the persisted window row and named in the chat's window marker.
  - **Failed turns froze the conversation list.** Every appended message now
    bumps the conversation's `updated_at`, so a conversation whose turns all
    failed no longer sorts as if abandoned.
- **Config guard-rail for the overflow shape**: explicit `tool_fidelity=full`
  against a `context_window` below 100k is flagged in the server log, in the
  admin agent-settings `warnings` array, and as a warning box on the admin
  agent page. Advisory only — the operator keeps every override.

## [1.6.0] — 2026-07-23

### Added

- **Statistical visualization depth** — the visualization stack was audited
  against a data-analysis-and-visualization lecture set and every identified
  gap closed except geographic charts (deferred with its blockers named in
  `ROADMAP.md`). Five new capabilities, each available to the analyst and to
  the AI agent from the same legality table:
  - **Correlation matrix** (`corr`): pairwise Pearson and Spearman across 2–8
    numeric fields as a lower-triangle diverging grid with the coefficient
    printed in every cell, and a click-through to the pair's scatter plot.
    New `field_correlation` aggregation, endpoint and agent tool. Correlations
    are **pairwise-complete** — each pair reports the `n` it was computed over,
    so a field with sparse numeric coverage shrinks only the pairs it takes
    part in.
  - **Grouped box/violin plots**: `box`/`violin` now accept an optional
    categorical `field_y`, giving one distribution per top group. Per-group
    quantiles are binned over the *global* value range so the silhouettes are
    directly comparable; groups outside the top-N are reported as omitted and
    never merged into an "Other" box.
  - **Waffle chart**: shares of a whole as a 10×10 grid of countable cells,
    allocated by largest remainder so the cells sum to exactly 100 and no
    existing category ever rounds away to zero. More categories than cells
    (one cell each is the floor) fold into the `Other` row rather than
    overflowing the grid.
  - **Scatter statistics**: Pearson r, Spearman ρ, Kendall τ-b, their
    p-values, a least-squares regression line with R², and Shapiro–Wilk
    normality checks that decide which coefficient the panel recommends.
    `recommendation_basis` says whether that recommendation follows a
    normality verdict or is the conservative default because normality could
    not be tested — an untested fallback is never presented as a finding.
- **`vestigo.stats`** — a pure-Python inference module (regularized incomplete
  beta, Student-t survival, correlation p-values, Kendall τ-b in O(n log n)
  via Knight's method, Shapiro–Wilk after Royston 1995, the Freedman–Diaconis
  bin rule), pinned against
  scipy-computed reference constants. scipy is deliberately not a dependency;
  everything ClickHouse has an aggregate for (`corr`, `rankCorr`,
  `simpleLinearRegression`, `skewPop`, quantiles) is computed there over the
  full filtered data, and the response labels which numbers came from a sample.
- **Teaching explainers throughout the visualization UI** — every statistic
  and chart concept carries a popover with what it is, how to read it, when to
  distrust it, and its formula; every chart type carries a one-line reading
  aid. The "when to distrust it" section is mandatory (enforced by test): a
  statistic explained without its failure mode teaches overconfidence.

### Changed

- **Histograms** default to Freedman–Diaconis bin widths (manual override
  retained) and gained a density curve, mean/median markers, and skewness with
  its plain-language reading. The response reports `bin_rule`
  (`fd` / `fd_fallback` / `manual`), `bin_count_clamped` and `bin_width`, and
  the caption states exactly which of them produced the bins — a fixed
  fallback for a distribution with no interquartile spread is never captioned
  as Freedman–Diaconis.
- **Box and violin plots** can overlay a uniform sample of the raw values as
  a jittered strip — a violin drawn without its points implies data it never
  measured. Both the jitter and the sample itself are deterministic, so an
  SVG/PNG export reproduces exactly what was on screen and a rerun of the same
  query redraws the same points. In grouped mode the violin widths encode each
  group's distribution *shape* (relative frequency within the group), not its
  size; the caption says so, and each group's n is on its tooltip.
- **Line charts** mark their actually-measured buckets, so the line is no
  longer read as an assertion about values between them.
- **Pie charts** warn when they stop being readable — more than four slices,
  or two slices within 10% of each other — and offer bar or waffle instead.
  Advisory, never a refusal, and `propose_chart` applies the identical rule.
- `propose_chart` gained `fields` (correlation matrix) and the
  `groups`/`show_points`/`show_density` options; `field_y` is now optional on
  box/violin. New agent data tools: `field_correlation`,
  `field_numeric_grouped`.
- Chart captions — which are also the SVG/PNG export captions — carry the new
  truthfulness lines: bin rule, skewness reading, grouped-distribution
  omissions, point-overlay sample size, correlation basis, the
  correlation-is-not-causation caveat, and — where the sample is large enough
  for it to matter — the caveat that Shapiro–Wilk's power grows with n.
- **Chart samples are reproducible.** Every sampling path (scatter points,
  box/violin strips, grouped strips) draws in a stable hash order over
  `event_id` instead of `ORDER BY rand()`, so identical filters redraw
  identical points across reruns, restarts and replicas — the same
  requirement that already governed the jitter. Costs no extra scan.
- **Kendall τ-b is computed in O(n log n)** (Knight's method) rather than over
  all pairs, which took ~17 s at the API's 20 000-point scatter ceiling and
  ~1 s at the UI default — on every scatter render, inside a request holding a
  heavy-scan slot. Shapiro–Wilk is capped at the 5 000 points its
  approximation covers instead of silently returning nothing past it.
- **Grouped distributions run their scans in two parallel waves** rather than
  four sequential ones, halving the wall clock of a grouped box/violin on a
  large timeline.
- The correlation matrix fades cells whose coefficient is not distinguishable
  from zero (p ≥ 0.05) or rests on fewer than 30 pairwise-complete events, and
  puts both p-values in the tooltip — full-strength colour on a coefficient
  the data cannot support reads as a finding.
- The `field_correlation` agent tool now rejects a field list that is too long
  or repeats a token instead of silently truncating it, and grouped charts
  warn when the grouping field's cardinality suggests an identifier rather
  than a grouping variable.

## [1.5.0] — 2026-07-22

### Added

- **Sliding context window for the AI agent** (`src/vestigo/agent/window.py`).
  Applied before *every* model request — mid-turn included — via pydantic-ai's
  `ProcessHistory` capability: oldest tool-result contents are replaced by
  visible elision stubs until the estimated prompt fits the budget, then whole
  oldest turns are replaced by a marker pair, and — last resort — the newest
  request's oversized results are truncated to a leading slice, the one case
  neither other pass can touch. Deterministic (pure function of
  history + budget, so replays reproduce it exactly), transparent to the model
  (the system prompt explains recovery via `get_event` / narrower re-runs),
  and applied at send time only — the stored transcript stays complete.
  Driven by `context_window`; with it unset, a provider overflow enables the
  window reactively and re-runs the turn once — the budget comes from the
  window the provider names in its error body when present (OpenAI /
  Anthropic / llama.cpp phrasings), else from the estimated pre-turn history
  size. A budget learned that way is reused by the conversation's next turn
  (`PostgresStore.get_last_window_budget`), so an unconfigured deployment pays
  the failed round trip once rather than every turn. Every reduced turn —
  finished, stopped, or interrupted — persists a `role="window"` transcript
  row plus an `agent.window` audit row, carrying the turn's single largest
  reduction.

### Removed

- **LLM history compaction** (`agent/compaction.py`) and the
  `compact_threshold` setting (env `VESTIGO_AGENT_COMPACT_THRESHOLD`, admin
  field, DB column — migration `0015`). The summarizer ran on the same
  possibly-small investigation model and its output was nondeterministic;
  the window's turn-dropping covers its niche.
- **The fidelity overflow ladder** (drop a tier, re-run the whole turn). The
  static `tool_fidelity` setting (`full`/`message`/`minimal`/`auto`) stays —
  it still shapes tool results up front; overflow handling is now the
  window's job alone. Historical `compaction`/`fidelity` transcript rows from
  both retired mechanisms still render read-only in the agent panel.

## [1.4.5] — 2026-07-22

### Fixed

- **"Locate this event in the timeline" scrolls again — and now surfaces
  events the current view hides (#150).** After routine-collapse became
  auto-on-with-mutes (#147), locate stopped scrolling: it seeded the query
  cache under a hardcoded `{}` key while the live events query is keyed on
  `effectiveFilters` (which carries `collapseRoutine`), so the anchor page
  landed in a cache entry the grid never read. Locate now keeps the active
  filters and seeds the *current* key, so the seed can't drift from the live
  query. If the target would otherwise be hidden by the current view (a
  routine/mute collapse or an active filter) it is force-included at its
  correct position and rendered visually distinct as "normally hidden". The
  same seek path drove the "preserve scroll position when adding a filter"
  soft-anchor, which silently reset the grid to the top under collapse for the
  same reason — both now compose the seed key the way the live query does:
  URL-round-tripped filters through the shared `computeEffectiveFilters`
  helper, never a hand-rolled object. Analysis-panel jump-to-time shares the
  new behaviour.

  Follow-up review hardened the same seam:
  - **Applying an agent finding keeps your scroll position.** The finding's
    filter set carries `ids`/`collapseRoutine`, which the URL deliberately
    drops and which are set in the same React batch as the filter change, so
    the soft anchor seeded a key the grid never read and the timeline snapped
    to the top. The overlays are now threaded into `setFilters` explicitly.
  - **"Locate" no longer loses its target to a late soft anchor.** Both paths
    now seed the same query key, so a scroll-preserving fetch still in flight
    could land after the located page and overwrite it.
  - **The "normally hidden" marker expires with the view that hid it** —
    revealing routine events (or any other overlay change) clears it instead of
    leaving a row asserting something no longer true — and it stays visible
    while the located row is expanded or selected, which is exactly when a jump
    leaves it.
  - Clearing your last filter chip now preserves scroll position like every
    other filter change, and the "back to filtered view" breadcrumb describes
    what actually produces it (a context query, not a jump).

## [1.4.4] — 2026-07-21

### Fixed

- **Agent chart proposals no longer vanish when the model batches tool calls.**
  A model issuing parallel `propose_chart` calls (Kimi does this routinely)
  persists N call rows followed by N result rows, but the agent panel paired
  them through a single buffer that assumed call→result adjacency — so a batch
  of 14 validated charts rendered as one card, and even that one carried the
  wrong title. Tool call and result rows now persist the provider's
  `tool_call_id` (migration `0014`) and the panel pairs by it, with FIFO
  fallback for conversations recorded before the migration. A chart that fails
  validation consumes only its own slot instead of shifting its batch
  siblings, in both the live stream and the reloaded transcript.

## [1.4.3] — 2026-07-21

### Fixed

- **Muting a template now actually hides its events.** A mute was recorded
  correctly — it appeared under "Muted templates" with its count — but the grid
  kept showing every one of its events, because collapsing them was a separate
  toggle in the top bar that muting never switched on. A mute is a filter, so it
  now applies the moment you make it, which is what the tab always claimed. The
  toggle is now a *reveal*: press it to see the routine events again
  temporarily. The next mute re-applies collapse, so revealing once cannot
  quietly disable every mute you make afterwards.
- **"Select all matching → Tag" no longer tags events you cannot see.** With
  routine events collapsed, bulk-tagging the current filter wrote annotations to
  the muted events as well — records attached to events that were never on
  screen, while the confirmation dialog counted only the visible ones. The bulk
  action now covers exactly the set the grid displays. Exports and histograms
  were already correct.
- **Charts now respect muted templates too.** Every visualization endpoint
  (top values, timeseries, punchcard, pivot, scatter, compare) silently ignored
  the collapse flag the frontend was already sending, so a chart could disagree
  with the grid it sat next to — the histogram modal's top-value list included
  events its own histogram hid. The Visualize page, which cannot inherit the
  flag from the URL, now derives collapse from the mute list itself, shows a
  visible "routine events collapsed" indicator, and offers the same temporary
  reveal as the Explorer.
- **No more flash of muted events on load.** The Explorer and Visualize pages
  fired their first data query before the mute list had loaded, briefly showing
  (and needlessly computing) the uncollapsed event set, then refetching. Both
  now wait for the mute list — one small metadata read — before the first
  fetch.

## [1.4.2] — 2026-07-21

### Added

- **Tool-result detail is now an agent setting (`tool_fidelity`).** How much of
  each event record the agent gets back from searches, similarity lookups and
  anomaly findings — `full` (the whole event), `message` (the one line that
  distinguishes a succeeded login from a failed one), `minimal` (just the
  identity fields), or `auto` (derive it from the configured context window:
  100k and up gets `full`, 32k and up `message`, anything smaller `minimal`, and
  an unconfigured window `message`).
  The default is `full`: an unset context window means the operator has declared
  no constraint, which is assumed to be a cloud model with room. Admins running
  a small local model should set `message` or `auto`. `get_event` always answers
  in full — it is the escape hatch the reduced results point at.
  **Note for `/mcp` users:** the setting applies to the external transport too,
  so setting anything but `full` changes what existing MCP clients receive from
  `search_events`, `semantic_search`, `similar_events` and `run_anomaly_detector`
  — each such result names its tier in a `fidelity` field.
- **An overflow now costs a slower turn, not a shallower one.** When a turn
  overflows the model's context window, the agent first re-runs it handing the
  model less of each event record — no summarizer call, and unlike compaction
  it works on a single broad turn, which has no older turns to fold. It is
  skipped when the turn fetched no event records, since there would be nothing
  to give up; only once it is exhausted does the agent compact. Each such drop
  is recorded the way a compaction is — a message row in the conversation and an
  audit entry — so it survives a reload and reaches the JSON export, and each
  tool result records the detail level that produced it. An exported
  conversation states every degradation that was applied to it.

### Security

- **Path traversal in the frontend catch-all (unauthenticated arbitrary file
  read).** The route that serves the built SPA joined the request path onto
  `frontend/dist` and let the filesystem resolve it, so a request line carrying
  a literal `..` — which neither uvicorn nor Starlette normalizes — returned any
  file readable by the service account, including the deployment's own `.env`.
  The route is unauthenticated by design (the browser needs the app shell before
  login), so this was reachable by anyone who could reach the port. Candidates
  are now resolved and required to sit inside `frontend/dist`, which also stops
  a symlink pointing out of it. **Anyone who exposed vestigo-web to an untrusted
  network should check their proxy access logs for request paths containing `..`
  and rotate the secrets in `.env` if there is any doubt.**

### Fixed

- **Agent turns no longer die on a LiteLLM context overflow.** The overflow
  heuristic did not recognise LiteLLM's "exceeds the available context size"
  phrasing, so an overflow against a proxied local model skipped the
  compact-and-retry escalation entirely and surfaced as a generic model error,
  losing the turn.
- **A single broad investigation turn no longer overflows a small model.** Each
  anomaly finding handed to the agent embedded the full resolved example event
  (~85% of the finding's size); a "find anomalies and visualise" ask that ran
  seven detectors in one turn piled up ~18k tokens of duplicated event bodies
  and overflowed a 64k model — a case compaction cannot fix, since there is only
  one turn to fold. The agent's copy of a finding now carries the example's
  `event_id` and its `message` line — the part that distinguishes a succeeded
  login from a failed one — instead of the whole event, with `get_event` for the
  full record and a note saying so; and the bulk `list_annotations` scan
  truncates long bodies harder than the per-event detail tool. The persisted
  detector run and the Analysis page keep the full data. On the turn that
  failed, this cut the tool payload from ~34k to ~16k tokens.
- **The agent gets more than one attempt to correct a rejected tool call.** Tool
  legality errors name the legal alternative and exist to be acted on, but the
  retry budget was one, so a second wrong guess killed the whole turn. A
  `propose_chart` call asking for a `heatmap` with two fields did exactly that.
  The budget is now three, and that particular rejection names the fix
  (`chart_type="pivot"` is the field × field heatmap; `heatmap` is one field over
  time) rather than only listing the two-field chart types.
- **A turn that ends early says why.** Exhausting a tool's retries or the turn's
  step budget surfaced as "Agent turn failed — see server logs", which does not
  tell the analyst whether to rephrase, narrow the question, or call an admin.
  Both now end with a named error (`tool_retry_exhausted`, `turn_limit_reached`)
  carrying the underlying reason.
- **A reduced tool result no longer claims to have dropped something it kept.**
  An anomaly finding whose example event could not be resolved, or held nothing
  but a short message, still came back with "call get_event for the full
  record" attached — an untruth in an exported conversation. The note now
  appears only when the detail level actually removed something.
- **A degraded turn is legible in the case record.** A turn re-run at a lower
  detail level re-executes its tools, so one analyst question could leave
  several identical detector runs on the Analysis page with nothing to tell them
  apart; re-runs now carry the attempt that produced them. The estimate that
  decides whether to summarize older turns also ignores token counts measured
  before a detail drop, the way it already ignored counts measured before a
  summarization — they describe a request the conversation no longer sends.

## [1.4.1] — 2026-07-20

### Changed

- **The agent fits a small context window again** — tool definitions are resent
  to the model on every request, and they had grown to roughly half of a 32k
  local-model window before the conversation even started. They are now
  advertised in a compact form (~52% smaller) with no loss of guidance: the
  shared filter/chart field documentation moved into the system prompt, where
  it is sent once instead of once per tool. Nothing about what the agent can
  do, or how strictly its arguments are checked, has changed.
- **Tabular tool results are compact** — search hits, value distributions,
  pivots, comparisons, detector findings and time series are handed to the
  model with their column names stated once instead of repeated on every row,
  and a time series no longer repeats its time axis per series (−84% on a full
  one). Every value is preserved exactly; this is a reshaping, not a summary,
  so results stay reproducible. Because results are replayed on every later
  turn, this compounds over a long investigation.
- **The agent's metadata list tools are capped** at 200 rows (baselines, saved
  views, annotations, dispositions, Sigma rules and runs). They were unbounded,
  so a long-running case could push an arbitrarily large payload into the
  conversation history. Each one now reports how many rows it returned
  alongside how many exist, so a capped list can never be mistaken for a
  complete one.
- **The external `/mcp` tool surface changes shape with it.** Clients of the
  `/mcp` endpoint get the same slimmed schemas and the same column-header-once
  results as the built-in agent, rather than a second encoding maintained in
  parallel. The server's MCP `instructions` now carry the filter/chart field
  reference and the result-format legend, so an external client has everything
  it needs to read either. Any client that parsed the old row-per-dict results
  needs updating; Vestigo has no external MCP consumers in the field, so this
  is called out for completeness rather than as a migration.

### Added

- **Core / All presets in the agent tool selector** — "Core" keeps the
  eleven tools an investigation cycle actually needs and turns off the rest,
  cutting the per-request tool overhead to about a fifth of the full catalog.
  Useful when running a small local model. Disabled tools are removed from the
  request entirely, so this reclaims context rather than just tidying the list.
- **Stop a running agent turn** — a turn that is still running when you close
  the panel or navigate away is now visible when you come back, with a Stop
  button that actually cancels it server-side instead of only dropping your
  own stream. Whatever the agent had already written is kept, marked
  `[stopped]`, and who stopped it is recorded in the audit trail.
- **Agent tool selection stays editable** — the tool popover no longer
  disappears once a conversation starts; changing it now adjusts that
  conversation (from the next turn onward) and is written to the audit trail.
- **Resizable agent panel** — drag its left edge, same as the Investigate and
  event-detail panels. The width persists.
- **Model picker in the agent admin settings** — once the API base URL and key
  are set, the model field becomes a dropdown populated from the endpoint's own
  model listing instead of a name typed from memory. Free-text entry remains the
  fallback when an endpoint offers no listing, and stays available for models a
  listing omits.
- **Save an agent finding as a View** — finding cards get a save action
  alongside "Apply to Explorer", so a filter set worth keeping lands in the
  left-hand Views panel instead of dying with the conversation.

## [1.4.0] — 2026-07-20

### Added

- **Log template clustering**: structurally identical log lines (variable
  timestamps/IPs/UUIDs/hex/numbers masked) are grouped into shapes, browsable in
  a new Templates tab (under Patterns) — mute a routine shape to collapse its
  events out of the grid immediately, always behind a visible count. Field is
  filterable in the grid via the new `template_id` facet.
- **Agent chart proposals**: the agent can now explore data through the same
  charts as the Visualize page (per-value time series, punch card, field×field
  pivot, scatter, two-layer compare) and propose one as a live chart card in
  the chat — "Open in Visualize" jumps to the full page with the same chart,
  "Save" writes a saved chart credited to the analyst. The agent never writes
  a chart itself.
- **Agent auto-compaction**: configurable model context window
  (`VESTIGO_AGENT_CONTEXT_WINDOW` / admin UI); long conversations are summarized
  before they overflow, with the summary shown in chat and the exact
  pre-compaction history preserved on an append-only, audited record. Provider
  context-overflow errors now compact-and-retry once, then fail with a specific,
  friendly message instead of a generic one.
- **Per-tool enable/disable, three layers**: admins can hard-disable individual
  agent tools globally (applies to the in-app agent and the external `/mcp`
  endpoint); users can set personal defaults and adjust the tool set per
  conversation.
- **Persistent OPSEC notice**: the agent panel always shows where evidence data
  goes — the configured API endpoint URL and model — in its empty state, with
  no dismiss, so it is visible before every first message. Tool selection for a
  new chat sits next to the input as a popover.
- **Thinking content**: the model's reasoning segments are streamed, persisted,
  and rendered as collapsible blocks in the chat.
- **Conversation JSON export**: download any agent thread as JSON — every
  message, tool call with arguments and results, thinking content, token usage,
  compaction records, and the raw provider-wire history.

## [1.3.0] — 2026-07-19

### Added

- **AI investigation agent** (`docs/AGENT.md`) — optional, off-by-default assistant
  embedded in the Explorer. It drives the iterative analysis loop (search, aggregate,
  run detectors, refine) in its own sandbox and hands results back as **findings**:
  filter-set cards the analyst applies with one click — the agent never mutates the
  analyst's view. Conversations, every tool call with exact arguments, and the
  replayable runtime history persist in Postgres; every tool call is audited.
- **Propose→confirm writes**: the agent never writes annotations itself.
  `propose_annotation` records a proposal; an analyst confirms or rejects in the UI.
  Confirming re-resolves events against the current scope and writes annotations with
  `origin="agentic-analysis"`, credited to the confirming analyst and audited.
- **Full read parity**: tools for events, aggregations, histograms, similarity /
  semantic search, all statistical detectors (with tuning parameters), detector
  baselines, dispositions, saved views, annotations, and Sigma rules/runs.
- **External `/mcp` endpoint** (`VESTIGO_MCP_ENABLED`, default off) — the identical
  scoped tool server over streamable HTTP for external MCP clients, authenticated by
  per-timeline scoped tokens (`vgo_…`, shown once at creation). Scope comes from the
  token, never from the client.
- **Admin agent settings page** — DB-backed runtime configuration with per-field
  env-pinning (`VESTIGO_AGENT_*` always wins, pinned fields shown disabled with a
  badge), masked API key, endpoint test button, and per-provider reasoning-effort
  translation (`off`–`max`, incl. an experimental Kimi mapping).
- **Token-usage metering** — measured per turn from the runtime (never estimated;
  `NULL` when the endpoint reports nothing), shown as per-message chips and a running
  conversation total.
- **`VESTIGO_AGENT_SECRET_MODE=env-only`** — refuses DB storage of the LLM API key and
  ignores any previously stored one, making `VESTIGO_AGENT_API_KEY` the only source.
- Explorer: agent-provenance badge on annotations; usernames resolve to display names
  everywhere names render.

### Changed

- `docs/CONCEPT.md` refreshed to match the shipped product: statistical detector suite,
  Sigma, and the agent in the vision; corrected Qdrant collection naming; out-of-scope
  list rewritten (streaming ingest, correlation rules, and Stories are now roadmap
  milestones).

## [1.2.1] — 2026-07-19

### Changed

- **Dependency roundup** — all 20 open Dependabot PRs merged and lockfiles fully
  refreshed. Backend: fastapi 0.139.2, clickhouse-connect 1.5.0, typer 0.27.0,
  geoip2 5.3.0, ruff 0.15.22, plus all transitive updates via `uv lock --upgrade`.
  Frontend: vite 8.1.5, tailwindcss 4.3.3, oxlint 1.74.0, @types/node 26,
  Radix UI patch releases, @tanstack/react-virtual 3.14.6, lucide-react 1.25.0,
  @fontsource/inter + jetbrains-mono 5.3.0. CI: docker/* actions and
  actions/setup-node major bumps. Full backend + frontend suites green on the
  upgraded set.
- Frontend `package.json` version now tracks the app version (was stale at 1.1.2).

## [1.2.0] — 2026-07-19

### Added

- **Sigma rule runner** (`docs/ANOMALY_DETECTION.md` §13) — deterministic signature
  matching of community-standard [Sigma](https://github.com/SigmaHQ/sigma) YAML rules
  over ClickHouse, deliberately separate from the statistical detectors. Rules come
  from an admin-managed offline directory (`VESTIGO_SIGMA_RULES_PATH`, a file drop —
  no restart needed, unchanged files reuse a per-file parse cache) and per-case
  uploads. Every hit is written as `Annotation(origin=system, annotation_type="sigma")`
  whose `sigma: <rule title>` label joins the unified tag filter panel.
- **Custom pySigma → ClickHouse backend**: one boolean SQL expression per rule.
  Sigma-spec case-insensitive matching (`ILIKE` with `*`/`?` wildcards), `|cased`,
  `|re` (RE2), `|cidr` (guarded `isIPAddressInRange`), numeric comparisons, null/missing
  semantics, field-less keywords over `search_blob`. Field names resolve through
  ruleset `vestigo-fieldmap.yml` → timeline canonical mappings → raw-attribute
  fallback (tracked and flagged in the UI). All values pass through an audited,
  adversarially-tested literal-quoting boundary.
- **Streamed, reproducible runs**: background job per timeline; per rule, hits stream
  under the shared heavy-scan gate through a bounded queue (no hit cap, no in-memory
  hit list) into batched annotation writes; re-runs are idempotent per rule and
  preserve confirmed findings. Persistent `sigma_runs` records (Alembic `0006`)
  snapshot each rule's YAML content hash, exact compiled SQL, match count, and status.
- **Sigma tab** in the Investigate panel: rule picker with level/logsource badges,
  YAML upload, run launch into the job tray, run history with per-rule status,
  compiled-SQL view, fallback-field warnings, and filter-grid-by-rule.
- Config: `VESTIGO_SIGMA_RULES_PATH`, `VESTIGO_SIGMA_ANNOTATION_BATCH_SIZE`.
  Deps: `pysigma`, explicit `pyyaml` (offline — no Sigma code path touches the network).

## [1.1.0] — 2026-07-13

### Added

- **Repeating-sequence (motif) mining** — new `sequence_motif` detector
  (`docs/ANOMALY_DETECTION.md` §12): per source, time-ordered n-grams of one field's
  values that *recur* are ranked by support × cadence regularity (median gap, CV,
  Greenwood spacing test). Mode-less — needs no baseline, runs right after ingestion;
  optional `start`/`end` scope. Tunables: `VESTIGO_STAT_MOTIF_MIN_SUPPORT`,
  `VESTIGO_STAT_MOTIF_MAX_CANDIDATES`, `VESTIGO_STAT_MOTIF_CADENCE_TOP_K`.
- **Routine suppression** — new disposition `kind="routine"`: a motif marked routine has
  its occurrences materialized (ClickHouse `motif_occurrences` table, auto-created) so the
  event grid, histogram, and export can collapse them via `collapse_routine`. The response
  always reports `routine_collapsed_count` — collapse is explicit, never silent. Routine is
  presentation-only: detectors keep scoring and it never enters the reproducibility hash.
- **Patterns tab** in the Investigate panel: motif list with support, period, regularity
  bar and per-source cadence; Mark routine / unmark; Explorer collapse toggle with an
  always-visible collapsed-count banner.
- **Unified findings feed** — the Anomalies tab now opens with one cross-detector ranked
  inbox (per-detector rank interleave, raw score with its unit per row, detector chips as
  filters), built from the detector sweep the count badges already paid for.

### Changed

- The 11 per-detector views moved under a collapsed **Advanced** expander, grouped
  Values / Volume & timing / Sequences. The dense baseline/suspect-window builder moved
  from the inline flow into an overlay drawer (FrameBar → *Manage baselines*; histogram
  mark-mode opens it automatically).

## [1.0.0] — 2026-07-12

First stable release. Everything below is new in 1.0.0.

### Renamed

The project was renamed **TraceSignal → Vestigo** ahead of 1.0 (*vestigo*, Latin:
"I follow the tracks"). For anyone upgrading a pre-release deployment:

- CLI entry points: `tsig` → `vestigo`, `tsig-web` → `vestigo-web`.
- Environment variables: `TS_*` → `VESTIGO_*` (e.g. `TS_POSTGRES_URL` →
  `VESTIGO_POSTGRES_URL`).
- Default backing-store names changed to `vestigo` (PostgreSQL database/user, ClickHouse
  database, Qdrant collection prefix). Existing deployments keep their data by pinning the
  old names via `VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_DATABASE`, and
  `VESTIGO_QDRANT_COLLECTION_PREFIX`.
- Converter scripts: `*2tracesignal.py` → `*2vestigo.py`. Parquet footer metadata keys
  moved from `tracesignal.*` to `vestigo.*`; the server still reads files produced by
  pre-rename converters.

### Ingestion

- Streaming parsers for Plaso CSV/JSONL and generic Timesketch-compatible CSV/JSONL —
  constant-memory, tens-of-GB capable, with per-record byte offsets and content hashes.
- Every ingested file (Source) is SHA-256 hashed and retained content-addressed.
- Vestigo Parquet interchange format v1: downloadable client-side converter scripts
  (nginx, filterlog, suricata, cloudtrail, pcap — plus vendored stdlib-only Timesketch
  converters for apache, browser, cowrie, evtx, journal, syslog) emit typed columnar
  Parquet that the server bulk-inserts via Arrow record batches, with forensic provenance
  anchored to the original raw evidence file.
- CLI ingestion (`vestigo ingest`) streams straight from disk with progress/ETA and
  per-user attribution; upload size cap (`VESTIGO_MAX_UPLOAD_BYTES`) with mid-stream 413.
- Optional per-source enrichers with recorded provenance, force re-run recovery, and
  upgrade guards.

### Explorer

- Virtualized ELK-like event grid over ClickHouse: resizable/pickable columns, density
  modes, light/dark themes, keyset pagination.
- Full filter model (field, value, time range, tags, annotations), saved Views per
  timeline, indexed full-text search, time histogram with brush zoom and event markers.
- Context query around any event; per-source clock-skew correction; column stats and
  field inventory backed by a per-source field-stats cache.

### Anomaly detection

- Statistical detectors run directly against ClickHouse, all SQL-explainable, each with
  self-baseline and temporal (baseline/suspect window) modes where applicable:
  value novelty, frequency (z-score spikes/silences), value combinations,
  timestamp order, charset, numeric range, entropy, interval periodicity
  (cadence breaks + beaconing), sequence novelty (n-grams), proportion shift
  (G-test with BH-FDR), and value distribution drift (KS / G-test).
- Embedding pipeline: user-triggered jobs embed events into Qdrant (local models,
  offline-capable); semantic search and nearest-neighbor similarity; embedding wizard
  with content-aware field recommendation.
- Triage workflow: unified disposition taxonomy, dismissals, Investigate panel bundling
  detectors with shared baseline configuration.

### Visualization

- Visualize page: time histogram, comparison histogram, punch card, pivot, Sankey and
  scatter charts, click-to-filter, saved charts — with scan guardrails at 300M-row scale.

### Platform

- Session-cookie auth with optional OIDC SSO, case-level RBAC, teams, audit trail.
- Alembic-managed PostgreSQL schema with automatic migration on startup (pre-Alembic
  databases are auto-adopted).
- Airgapped/offline by default (`VESTIGO_ALLOW_ONLINE` gates all network paths except
  the deliberately independent OIDC).
- Typer CLI mirroring the API for scriptable/offline use; reference `docker-compose.yml`
  for the three backing services (PostgreSQL, ClickHouse, Qdrant).
- Container images published to `ghcr.io/overcuriousity/vestigo`.

[1.0.0]: https://github.com/overcuriousity/Vestigo/releases/tag/v1.0.0
