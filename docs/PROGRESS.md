# Vestigo Implementation Progress

Last updated: 2026-08-07 (session 157 — HTTP reassembly back-ported to the vendored `pcap2timesketch`).

Append-only session log, newest entry on top. Older sessions are archived:
[1–70](./archive/PROGRESS_SESSIONS_01-70.md), [71–100](./archive/PROGRESS_SESSIONS_71-100.md).

## Session 157 — 2026-08-07: `--reassemble http` back-ported upstream and re-vendored

**Why.** Sessions 155–156 built HTTP/1.x reassembly in the native `pcap2vestigo` only. The
vendored `*2timesketch` suite is the permanent stdlib-only alternative
(`ROADMAP.md` §"Vendored converter ports stay demand-driven"), and a capability gap this
large between the two pcap converters is a reason to pick the native one for the wrong
reason — pyarrow availability, not analysis need. Ported upstream, then re-vendored.

**Upstream** (`github.com/overcuriousity/2timesketch`, commit `1bbe64f`, suite version
1.0.0 → 1.1.0): `pcap2timesketch.py --reassemble http`, the same reassembler
(ISN tracking, sequence-ordered buffering, retransmit/overlap/gap handling, chunked and
`until_close` framing, pipelining, HEAD/204/304/1xx/101/CONNECT rules, LRU and idle flow
eviction, per-flow caps) emitting one `network:http:transaction` row per request/response on
top of the packet rows. Two decisions where the suites legitimately differ:

- **No per-row provenance.** `byte_offset`/`content_hash` exist in the `*2vestigo` converters
  because the Parquet interchange schema mandates them; no `*2timesketch` script emits any
  per-row provenance, and `packet_offsets` would have been the only byte offset in the whole
  suite — dangling, since its packet rows carry none to join against. `--report`'s
  `AuditReport` (input paths + input sha256 + output sha256) stays that suite's provenance
  layer. The transaction-tag hashing from session 156 is therefore not ported; `packet_count`
  and the flags are.
- **The globally time-sorted output guarantee is preserved.** That guarantee is what the
  vendored converter has over the native one (which lets the server sort on query), and a
  transaction row breaks it: stamped with the request's first captured byte, produced when
  the response completes. So each file contributes a *second*, lazily evaluated stream —
  re-read the file, keep only derived rows, sort them, spill to temporary JSONL past 200k
  rows — which enters the existing k-way merge as an ordinary sorted stream. Cost, stated in
  `--help`: an enabled capture file is read twice.

Also fixed upstream because the reassembler depends on it: IPv6 fragments now report
`fragment_offset` (in bytes, as for IPv4) and a non-first fragment's payload is no longer
decoded as a transport header — doing so invented ports, a sequence number, and a phantom
flow built from body bytes. `pcap2vestigo` fixed both in session 156 — and, aligned with the port, now reports IPv6
`fragment_offset` in bytes rather than raw 8-byte units (`pcap2vestigo` 1.5.0): one column
carrying two meanings depending on IP version was the kind of thing an analyst finds out
the hard way.

**Vestigo side.** Re-vendored the whole suite at `1bbe64f`
(`scripts/vendor_converters.py`; every file's header and `__version__` move to
`1.1.0+vendored.1bbe64fbcf04`, only `pcap2timesketch.py` changes behavior), and updated its
`manifest.json` description to name the flag. Verified the vendored single-file script
against `tests/data/sample_http.pcap`: same two transactions as `pcap2vestigo`, field for
field, including `packet_count` (5 and 2) and timestamps; packet rows byte-identical with
and without the flag; output time-sorted. Upstream has no test suite, so the behavioral
checks (pipelined HEAD+GET framing, IPv6 fragment, single-direction incomplete, chunked +
gzip, CSV column shape, non-HTTP capture unchanged) were run as a one-off script rather than
committed — `tests/test_pcap_converter.py` continues to cover the same ground for the native
converter, which is where the logic is maintained.

## Session 156 — 2026-08-06: PR #238 review fixes (`pcap2vestigo` reassembler)

**Why.** A review of session 155's work found seven defects, three of them reproduced by
driving the reassembler directly. All are fixed here; nothing was deferred.

- **Unbounded `until_close` bodies (memory).** `length` and chunked framing both checked
  `_REASM_MAX_BODY_BYTES`; the connection-close-delimited branch checked nothing, and
  `_REASM_MAX_STREAM_BYTES` could not save it — `_take` moves bytes *out* of the direction's
  buffer and into the message, so `buffered` stays near zero while the body grows. One
  HTTP/1.0 download was enough to grow the body and its retained record blobs to the size of
  the response. It now takes the same verdict as an over-long `Content-Length`: the flow
  dies, its packet rows survive.
- **A transaction row could collide with a packet row's `event_id`.** With exactly one
  contributing record, the transaction hashed that record's bytes at that record's offset —
  the identical `(byte_offset, content_hash)` pair the packet row carries, and the server
  derives `event_id` from those. Two different events landed under one id (the events table
  is a plain `MergeTree`, so nothing deduplicated them), and any annotation keyed by id hit
  both. Reproduced by the PR's own orphan-request test. `content_hash` now covers a
  `vestigo:http-transaction:<index>\n` tag ahead of the records — still re-derivable by hand,
  which the provenance test asserts, and `docs/INPUT_FORMATS.md` states the tag.
- **Pipelined responses were framed against the wrong request.** `messages()` is a generator,
  so its `peer_method` argument was frozen at generator-creation time; the caller's update
  between responses was dead code. `HEAD /a` + `GET /b` answered in one packet framed the
  `GET` response as bodyless and desynced the stream behind it. `peer_method` is now a
  callable the framer asks at each message start, which is the only shape that is correct
  for a generator resumed N times.
- **The unanswered-request queue had no cap.** A single-direction capture — the exact case
  `--help` says yields flagged-or-nothing — queued a request (with its records) per request,
  forever. Capped at `_REASM_MAX_PENDING_REQUESTS`; the overflow is *emitted* as an
  `http_response_missing` transaction rather than dropped, because evidence that arrived
  should not vanish because its answer did not.
- **`timestamp` was the packet that completed the header block**, not the request's first
  captured byte as `INPUT_FORMATS.md` promised — off by a packet with an out-of-order start,
  and worse across directions, since `_pump` drives both from one arrival and a server
  message framed during a client packet inherited the client's clock. Each message now
  carries the earliest capture time among the records attributed to it; `duration_ms`
  inherited the same skew and is fixed with it.
- **IPv6 had no first-fragment guard.** IPv4 refuses to decode L4 out of a non-first
  fragment; IPv6 handed the fragment's body bytes straight to the TCP decoder, inventing
  ports and a sequence number — and, under `--reassemble`, a phantom flow. Guarded, and
  `fragment_offset` is now on IPv6 packet rows as it already was on IPv4 ones.
- **Gap forwarding silently dropped provenance.** Records wholly inside a skipped span end at
  or below the new base, so `_take` never attributed them and they left `packet_offsets`
  without a trace — the comment claimed the opposite. They are credited at the jump.

Each fix carries a regression test; all seven fail against the pre-fix converter. The
`manifest.json` size/sha256 for the converter were refreshed. Version stays 1.4.0 — it has
not shipped.

## Session 155 — 2026-08-06: `pcap2vestigo --reassemble http` (Milestone 9 N2)

**Why.** Second half of the analyst feedback that opened Milestone 9: a pcap timeline shows
packets, and an analyst working an intrusion wants the HTTP transaction — "what did this
host request, and what came back". Reconstructing that by hand from packet rows is exactly
the work a converter should do once.

**What shipped** (`assets/converters/pcap2vestigo.py`, 1.3.0 → 1.4.0):

- **A real reassembler, stdlib-only.** No dpkt/scapy — pyarrow stays the converter's single
  dependency, so it remains a standalone download. `_HttpDirection` does ISN tracking,
  sequence-ordered buffering with 32-bit wrap handling, retransmit drop and overlap trim
  (first writer wins, matching what the receiver saw), out-of-order hold-and-drain, and
  gap forwarding; `_HttpFlow` pairs the directions and queues requests against responses;
  `_HttpReassembler` owns the flow table, teardown (FIN/RST), LRU and idle eviction.
  On top: HTTP/1.x framing with chunked encoding, keep-alive pipelining, `Expect:
  100-continue` (an interim 1xx does not close a transaction), HEAD (no body whatever
  `Content-Length` claims), 101/CONNECT upgrades (stop parsing — the rest is not HTTP), and
  `Content-Encoding` via stdlib zlib.
- **Additive, never substitutive.** Transaction rows join the per-packet rows; a test asserts
  the packet rows are byte-identical with and without the flag. Packet rows are the forensic
  floor — the derived row is convenience, and convenience must not be able to delete
  evidence.
- **Field names are an API.** `http_method`, `http_uri`, `http_protocol`,
  `http_request_full` and `status_code` are spelled exactly as `nginx2vestigo` spells them,
  so a pcap timeline and a webserver-log timeline filter identically and a saved View ports
  across both. This is why the row is `artifact_long: web:access:request` too.
- **A stated new provenance convention.** The module's guarantee that `content_hash` covers
  a contiguous `byte_offset`-anchored span cannot hold for a transaction spanning N
  non-adjacent records, so the convention is documented rather than quietly broken:
  `byte_offset` = the record carrying the request line, `content_hash` = sha256 over the
  concatenated contributing records in capture order, `packet_offsets`/`packet_count` to
  reconstruct the input, and `reassembled` / `byte_offset_basis` / `content_hash_basis` on
  every such row so the two conventions are never confused by inspection. The flag is in
  `vestigo.parse_decisions` because it changes *which rows exist*, and `vestigo.row_counts`
  now splits `packets` from `http_transactions`. A test re-derives the hash from the listed
  offsets against the raw capture, which is the only proof that "re-derivable by hand" is
  true. Note the anchor is not the lowest contributing offset: the fixture sends the
  request's tail first, so an out-of-order segment sits earlier in the file.
- **Hostile input is the routine case** — this is incident evidence. Caps on per-stream
  buffered bytes (payload *and* the retained record bytes, which are the dominant cost since
  a request's records are held until its response completes), concurrent flows (LRU),
  header count/size, framed and decompressed body size, and held out-of-order segments.
  Breaching a cap kills one flow; its packet rows survive and the run continues.
- **Limits stated in `--help`**, because unstated each arrives as a bug report: no HTTPS
  (so most real traffic yields nothing), no HTTP/2 or /3, nothing useful from a
  snaplen-truncated or single-direction capture. Incomplete transactions come out flagged
  (`http_incomplete`, `reassembly_gap`, `reassembly_truncated_capture`) rather than dropped
  or silently spliced.

**Decisions worth recording.**

- `status_code`, not the roadmap's provisional `http_status` — nginx parity was the stated
  rationale for reusing field names, and it beats internal consistency with a not-yet-built
  N3. N3's tier 1 was edited accordingly.
- N2 emits the status but stops short of the rest of N3's tier-1 metadata (host,
  content-type, user-agent, referer, body sha256). Status is not optional scope creep: a
  request-only row is not a *transaction*.
- Row order is no longer file order under the flag (a transaction is written when its
  response completes). Harmless — the server sorts on query — but the docstring claimed
  otherwise, so it now says so.
- One packet record straddling a message boundary is attributed to *both* messages. It
  genuinely contributed to both, and dropping either attribution would make a
  `content_hash` non-re-derivable.

**Tests.** `sample_http.pcap` joins the committed fixtures — a keep-alive connection whose
request line arrives in the *second* segment sent, with a retransmitted body segment and a
chunked response, plus a pipelined second transaction. The edge cases (gzip, HEAD,
100-continue, orphan request, non-HTTP traffic, a capture gap, header flood, absurd
`Content-Length`, compression bomb, flow-table bound, parallel parity, `--help` limits)
build their own captures from the same builders instead of committing a fixture each.
`gen_pcap_fixtures.py` no longer writes on import for that reason.

`CONVERTER_VERSION` bumped and `manifest.json` regenerated (`test_converters_api.py` asserts
its sha256 and size).

## Session 154 — 2026-08-06: two review findings on the IPv6 eligibility widening

**Why.** Review of PR #237 (session 153's work) found two real defects, both introduced by
the IPv6 half rather than by the ASN enricher itself. Fixed in place on the branch — 1.10.0
is not tagged yet, so the `CHANGELOG.md` entry absorbed them rather than getting a patch
release.

- **An IPv4-only `.mmdb` could fail an entire enrichment job.** `maxminddb` raises a plain
  `ValueError` — not `AddressNotFoundError` — when asked for an IPv6 address in a database
  whose metadata says `ip_version == 4`, and `enrichers/jobs.py::_build_rows` deliberately
  re-raises everything else so partial results are never silently produced. Before the
  widening no IPv6 value ever reached the reader; after it, the first IPv6-shaped attribute
  value would kill the run for any operator on an IPv4-only database. Fixed at the lookup
  rather than at install: rejecting such a database at upload would refuse a legitimate
  install for an unrelated reason, whereas "this database has no answer for this address
  family" is exactly a miss. Both enrichers now check `reader.metadata().ip_version` when
  the parsed address is v6.

- **The IPv6 arm matched MAC addresses and bare times.** `(?:[0-9a-fA-F]{0,4}:){2,7}...`
  accepts `00:1a:2b:3c:4d:5e` and `13:45:02`. `enrich_value` rejected them via `ipaddress`
  so no wrong data could be written — but `check_eligibility` pushes the pattern into
  ClickHouse, so a pcap/DHCP/ARP/Windows-network timeline carrying MAC or time-of-day
  fields and no IP field reported as *eligible*, and with `auto_run_default` both enrichers
  auto-ran a full-timeline scan guaranteed to produce nothing. The arm is now split in two:
  the uncompressed form requires all eight groups, the compressed form requires a literal
  `::`. re2 has no lookarounds, so this is the tightening that fits. Regression test covers
  both the rejected non-addresses and the IPv6 forms that must keep matching.

- The third finding (a local `docker-compose.yml` edit in the working tree) was an obsolete
  dev tweak, stashed rather than committed.

## Session 153 — 2026-08-05: N1 shipped — ASN enricher and IPv6 eligibility

**Why.** First item off Milestone 9 (analyst feedback from a pcap timeline): "who operates
this IP" on every timeline, not only pcap ones. The framework needed no work — upload flow,
`derived_suffixes`, and the per-source apply lock were already generic — so the session was
the enricher module itself plus two deliberate decisions the roadmap had flagged.

- **`enrichers/asn.py`, self-contained by design.** The roadmap had advised extracting a
  shared `MaxMindEnricher` base from `geoip.py`; that was deliberately declined at plan
  review. Enrichers are modular reference plugins (like the ingestion converters) and are
  meant to stay independent, so `asn.py` mirrors the MaxMind mechanics (spawn/pin with
  `MODE_FD`, sidecar identity, flavor-checked install) instead of importing them.
  `ROADMAP.md`'s Milestone 9 records the divergence so a future reader doesn't "fix" it.

- **ASN, never whois.** Output is the announcing AS number and organization
  (`<attr>:asn_number`, `<attr>:asn_org`) — usually the hosting provider, which is what the
  analyst asked for. `display_name`, `description`, and the module docstring state what the
  database does *not* carry (netname, registrant, abuse contact, allocation dates); RIR
  whois remains A8's external-MCP path.

- **IPv6 landed in the same commit, for both enrichers.** GeoLite2 City and ASN both carry
  IPv6 networks, and pcap timelines are where IPv6 turns up, so the eligibility regex became
  a combined IPv4+IPv6 re2 pattern (`IP_REGEX`; still a gate, `enrich_value` validates via
  `ipaddress`). Accepted cost: GeoIP's `config_hash()` changed, so previously enriched
  sources report a new hash and are offered re-enrichment. Recorded in `CHANGELOG.md`.

- **Explorer merges both enrichers into one cell decoration.** `getAttributeDecoration`
  returns the first matching decorator, so the GeoIP decorator now appends the operator to
  its tooltip ("Frankfurt, Germany — AS12345 Example Hosting") and falls back to an "AS"
  marker when only ASN output exists. No admin-UI work: the dialog and config page were
  already registry-driven.

- **Tests mirror the GeoIP suite for ASN** (`tests/test_enrichers.py`,
  `tests/test_admin_enrichers_api.py`) using the same fake-`Reader` pattern; frontend label
  merge covered in `enrichment.test.ts`. 61 backend + 13 frontend tests pass.

## Session 152 — 2026-08-05: pcap-timeline feedback triaged into a network-evidence milestone

**Why.** An analyst working a pcap-derived timeline asked for two things: reassembled HTTP
payload from the capture, and IP addresses enriched with the operating provider. No code
changed this session — the work was establishing what each request actually costs against the
shipped code, and splitting the one that turned out to be two requests wearing one coat.

- **Milestone 9 added to `ROADMAP.md`** with three items (`N` prefix, previously unused). N1 is
  the ASN enricher, N2 the pcap HTTP reassembly, N3 the payload-handling tiers. N1 and N2 enter
  the priority list at 2 and 5.

- **The enricher framework needs no work to take a second enricher.** Verified: the asset
  upload flow is generic over `Enricher.asset_spec`, `derived_suffixes` comes from the
  registry, and the per-`(case_id, source_id)` apply lock in `enrichers/jobs.py` was written
  precisely so two enrichers cannot clobber each other's `REPLACE PARTITION`. `geoip2` already
  ships and its reader has `.asn()`. The one real change is extracting the MaxMind
  pinning/sidecar mechanics out of `geoip.py` into a shared base rather than copying them.

- **ASN is not whois, and the roadmap now says so at the item.** GeoLite2-ASN gives the
  announcing AS number and organization; netname, registrant and abuse contact need RIR whois,
  which needs the network, which is A8's external-MCP path. Recorded so the gap is not
  rediscovered as a defect.

- **Reassembly's real cost is provenance, not parsing.** The converter's docstring guarantees
  `content_hash` covers a contiguous `byte_offset`-anchored span on disk; a reassembled HTTP
  transaction spans N non-contiguous packet records, so N2 has to state a new convention rather
  than inherit the old one. Filed with the shape of that convention.

- **The payload request splits at the storage layer.** Metadata, body hashes and local file
  extraction are converter work and land in N3. Rendering images inline and serving download
  links needs a blob store, and there is none: events are `Map(String, String)` and
  `source_retention_path` retains the uploaded `.parquet`, not the pcap. Base64 bodies in
  `attributes` are additionally ruled out because `search_blob` concatenates every attribute
  value under an ngram bloom index, so bodies would degrade query selectivity timeline-wide.
  That half is now cross-referenced from E4, whose blob store plus hex/text/image viewers is
  the same component.

## Session 151 — 2026-08-02: the demo story now demonstrates a story

**Why.** The seeded demo case shipped a story made of fourteen markdown blocks and nothing
else. It reads well, but it is the only worked example most users ever see, and it silently
taught that a Vestigo story is a text editor — no embedded view, no chart, no frozen event.
The demo case also created no saved charts at all, so the Visualization page was empty on a
first login.

- **Four saved charts** (`demo/metadata.py::CHARTS`): failed logons over the month (time,
  filtered to 4625), where the traffic goes (bar on `host`), upload sizes (histogram on
  `bytes_out`), activity by hour and weekday (punchcard). Written as stored `ChartConfig`s —
  the frontend's camelCase `v: 1` shape — and parsed through the export path's
  `_stored_chart_to_spec` in a test, because a config in the wrong shape draws nothing,
  silently, in every consumer.

- **The story uses all four block kinds.** `STORY_BLOCKS` became a tuple of `DemoBlock`s that
  name their referents by view/chart/timeline *name*; `resolve_story_blocks` swaps in the real
  ids at seed time. Each claim that rests on evidence is now followed by the evidence: the
  spray's first 4625 and the encoded PowerShell as `event_ref`s, the matching saved filter set
  as a `view_ref`, the shape as a `chart_ref`.

- **The seed applies the same two gates every other write path applies** —
  `validate_block_content` then `validate_block_scope` — so a mistyped referent fails the
  build instead of shipping as a frozen `resolution.error` in someone's first export.

- **`first_event_id` is now shared** by the annotations and the story's event blocks; the
  annotation resolver lost its inline copy of the query.

- Tests: `test_story_embeds_resolve_into_a_snapshot` seeds the case and resolves the real
  export snapshot against live ClickHouse, asserting every block resolves with data;
  `test_story_has_a_narrative_arc` now requires the block-kind set to be complete.

## Session 150 — 2026-08-02: sixth review pass on PR 230, and the 1.9.0 cut

**Why.** A last read of the release PR as a whole — the column subsystem, the access-log
redaction, the saved-chart filter persistence — rather than of the branch that had just landed.
Four small things and two records to correct before tagging.

- **A recompute with no ready sources no longer discards a good suggestion.**
  `run_column_recommendation_job` wrote `insufficient` unconditionally when a timeline had no
  ready source. But "no ready sources" is also what a re-ingest or a briefly detached source
  looks like from inside the job, so a timeline that had a perfectly good recommendation
  dropped the whole case's grid back to the built-in defaults until the next successful run.
  A stored answer with columns is now left byte-for-byte alone — including its
  `generated_at`, which is when those columns were actually derived — with one exception: a
  `running` placeholder is settled, since this job holds the `_ACTIVE` claim and a stored
  `running` can therefore only be a dead job's.

- **The CLI opens its ClickHouse client only once there is a timeline to score.** A source
  belonging to no timeline paid a connection, and a blip on it printed
  `WARNING: column suggestion skipped` after an ingest that had entirely succeeded.

- **`update_timeline_recommended_columns` says what an empty dict means.** It coerces falsy to
  `NULL`, which is right — there is no payload without a `status` — but the coercion was
  invisible at the call site.

- **The one read-endpoint-that-writes is now a written-down exception.**
  `_settle_dead_recommendations` relabels a dead `running` payload from a `require_case_read`
  endpoint, which is correct (a read-only member has no other way to stop a timeline claiming
  to be thinking forever) and is exactly the kind of thing that spreads by precedent.
  `CLAUDE.md` now carries the rule and the exception together.

- **1.9.0 is dated 2026-08-02**, the day it was cut, not the day the branch opened.

**Verified.** `uv run pytest` — full suite green, `ruff check .` clean. Frontend unchanged this
session; its suite and typecheck re-run before the merge.

## Session 149 — 2026-08-02: canonical mapped fields reach the event projection

**Why.** The one known correctness hole in the release that ships the column recommender:
`score_columns` excluded both the raw keys a timeline's `field_mappings` consume *and* the
canonical names they define, on the grounds that neither renders. That was true, and it hid a
live defect one layer up — the picker already lists canonical names (discovery substitutes
them via `apply_mappings_to_attribute_keys`), so an analyst could select `ip_address` today
and get a column of em dashes, frozen into story view blocks along with everything else.

- **The root cause was the projection, not the recommender.** `mapping_coalesce_expr` had
  exactly one call site — the field-expression resolver that serves WHERE clauses and every
  aggregation. The grid's SELECT is a fixed column list ending in the whole `attributes` map,
  so nothing ever resolved a mapping into a row. `project_mapped_fields` is the Python twin of
  that SQL, applied to the presented page: first raw key in mapping order whose value is
  neither absent nor `''`, which is what `coalesce(nullif(...), ...)` means. Computing it from
  the same rule as the filter is the point — a live ClickHouse test asserts the rows whose
  projected value equals `10.0.0.1` are exactly the rows a filter on that field returns.

- **Precedence starts at the canonical name itself.** `validate_field_mappings` rejects a
  canonical name that shadows a raw attribute key, but only when the inventory is known — a
  source ingested after the mapping was saved introduces that key with nobody checking. The
  projection never overwrites a stored key, so the filter had to agree: `mapping_coalesce_expr`
  now reads `attributes[canonical]` as its first coalesce term. For every well-formed mapping
  that term is always `''` and nothing changes; in the shadowed case it is the difference
  between a filter that matches what the grid shows and one that ignores it.

- **The derived value lands in `attributes`, and the detail panel says so.** Injecting it
  there is why the grid, the story snapshot renderer and the embed cards needed no change at
  all — but it also makes a synthesized key indistinguishable from an ingested one, and
  `field_mappings.py` is explicit that a mapping is a per-timeline view. Storage stays
  untouched; the *presented* row does not, so `EventDetailPanel` marks a canonical field as
  mapped and names the raw fields behind it. Which keys those are comes from the row itself —
  each projected event carries `mapped_fields`, the canonical names *that row* got from the
  mapping — not from the mapping dict alone, so a key the source file did carry under a
  canonical name is never badged with a provenance it does not have. The same branch fixes
  its allowlist token:
  marking a canonical field's value normal used to write `attr:ip_address`, and `attr:`
  bypasses mappings, so the entry keyed on a field no source has and could never match.

- **Export deliberately stays raw.** `iter_events` does not project — a CSV/JSONL export
  carries what was ingested. `event_ref` story blocks likewise: a frozen single event is
  source-scoped and carries no timeline, so there is no mapping to apply.

- **The recommender now scores the canonical field.** Merged from its raws: a source counts
  once toward breadth if it carries *any* of them, which is the whole point — each spelling
  alone looks partial on a merged timeline. Coverage is capped at the source's event count, so
  a source carrying both spellings cannot double-count the events that set both — an upper
  bound, not a dedupe, since per-source stats hold no per-event overlap; the cap keeps `fill`
  inside [0, 1] and exact for the one-spelling-per-source case that is essentially all of
  them. The uniqueness ratio is read off a single spelling's own coverage, since pairing the
  max-distinct count with that summed coverage would halve the ratio and let a per-row-unique
  column through. A canonical name that also exists as a real attribute key is folded into the
  canonical field rather than scored separately: resolution reads that stored key first, so it
  is one well-defined column and its statistics belong to it.

**Verified.** `uv run pytest` — full suite green. Frontend: 783 tests across 92 files, `tsc -b
--noEmit` clean, `oxlint src` clean, `ruff check .` clean. The live-ClickHouse mapping tests
ran against the dev stack. The new detail-panel test was confirmed to fail against the
pre-fix component before being kept.

## Session 148 — 2026-08-01: the fifth review pass on PR 230

**Why.** A fresh read of the 1.9.0 release PR, this time over the column-suggestion feature
as a whole rather than the `?c_chart=` path the previous passes kept circling. Two real
findings, both about *when* things run rather than what they compute, plus the minor cleanups.

- **A dismissal could race the opt-in and record the opposite consent.** The Explorer treats
  closing the AI disclosure as "no thanks" and writes `false`; Cancel was disabled while the
  confirm was pending, but Escape, the overlay and the X were not, so a close landing between
  the opt-in write and its response fired a second `PUT /auth/me/preferences` with the
  opposite answer. Last write wins, and the stored consent could contradict the request that
  had already gone out — on the one record whose whole job is saying what the analyst
  authorized. `ColumnAdvisorNotice` now refuses every close while pending, and
  `closeAdvisorOffer` refuses too: the same lock on both sides, so a future dialog that
  forgets cannot reintroduce it.

- **An ingest burst silently dropped every source but the first.** `_ACTIVE` collapsed
  concurrent triggers for one timeline into the running job, and the docstring called them
  "identical jobs" — they were not. The holder read its source list before those sources
  became ready, so four files landing in parallel left the timeline recommended from one of
  them until the next ingest or a manual re-suggest. Collapsed triggers now mark the timeline
  in `_DIRTY` and the holder re-runs once for the whole burst, after releasing its claim. The
  re-run never carries `use_llm`: it answers an ingest, not a person, and a burst must not
  turn one opted-in "Suggest with AI" into repeated egress.

- **A redaction list nothing enforced.** `_SECRET_QUERY_PARAMS` only scrubs the parameter
  names it is told about, and the obligation to extend it lived in a comment. A test now walks
  the app's own OpenAPI schema and fails when a route declares a query parameter whose name
  reads like a credential and is not on the list — name-shaped, so it catches exactly what a
  reviewer reading the name would have caught, which is how the OIDC code reached the journal
  in the first place.

- **Minor.** The suggestion poll widened from a flat 3 s to 3/10/30 s by job age (~200 requests
  over the ten-minute staleness window was buying nothing); "Reset to defaults" now says
  "Reset to suggested" when that is what it restores; a comment duplicated across an earlier
  review pass in `ColumnPicker` was folded into one.

**Verified.** `uv run pytest` — 2214 passed. Frontend: 781 tests across 91 files, `tsc -b
--noEmit` clean, `oxlint src` clean. `ruff check .` clean. The disclosure test was confirmed to
fail against the pre-fix component before being kept.

## Session 147 — 2026-08-01: the third review pass on PR 230

**Why.** A third read of the 1.9.0 release PR. Four findings, one of them a blocker that
sessions 145 and 146 both walked past — each pass tightened `?c_chart=`'s *success* path and
neither asked what the page does when the fetch behind it fails.

- **A failed saved-charts fetch suspended the Visualize page indefinitely.** `chartRefBroken`
  was gated on `isSuccess`, so an errored chart list left the reference neither live nor
  broken: every defaulting effect stayed suppressed *and* `scopeReady` stayed false. No chart
  drew, no notice appeared, and nothing on screen explained either — a blank page whose only
  exit was editing the URL. A reference now settles on error too and falls through to the
  params, which is the same graceful degradation a deleted chart already got.

- **The broken-reference notice lasted about one frame.** Fixing the above exposed it: the
  moment a reference settles as broken the defaulting effects write the default chart into the
  URL, and `chartConfigToParams` drops `c_chart` with the rest of its namespace — so the
  *derived* "this reference is broken" stopped being true one tick after it became true, and
  the message blinked out with it. Both older notices (deleted chart, unreadable chart) had
  the same hole and passed their tests only by racing them. Latched in state now, like
  `droppedScope`, and cleared when the URL names a chart again.

- **A take-over rebuilt the query string from scratch.** `takeOver` composed
  `chartConfigToParams(config, filtersToParams(filters))`, which is a *fresh*
  `URLSearchParams` — so the first config or filter edit silently dropped every param outside
  the two namespaces this page owns. The helper it replaced preserved them by construction, by
  mutating a copy. Nothing writes such a param today, which is exactly why the loss would have
  gone unnoticed until something did. `chartUrlParams` rewrites `c_*` and the filter keys and
  carries the rest through; `loadSavedChart` clears the same two namespaces and no more.
  `FILTER_PARAM_KEYS` names the filter namespace explicitly, because `filtersToParams` writes
  only what is set and so cannot tell "cleared filter" from "not ours" — with a test that
  populates every member of `EventFilters` and asserts the set is exactly what gets written.

- **A failed spawn wedged a timeline until the next restart.** `start_column_recommendation`
  claims `_ACTIVE` before spawning, and the claim is released by the job's own `finally` —
  which a coroutine that was never scheduled never reaches. `_recommendation_is_dead` reads an
  active claim as proof the job is alive, so the leak is not a slow job: the timeline reports
  `running` for the life of the process *and* every later recommendation for it is skipped as
  a duplicate. The claim is handed back on a failed spawn, the job is marked failed rather
  than left at `queued`, and the test proves the *next* attempt is not turned away.

Also: `_settle_dead_recommendations` now states why it writes from a `require_case_read`
endpoint, since a read-only member is the one caller who cannot repair the row any other way,
and a test locks the bound that makes it safe — relabel only, same columns, no audit row.

Backend 2205 passed, frontend 759 across 90 files, typecheck/lint/format clean.

## Session 146 — 2026-08-01: the second review pass on PR 230

**Why.** A fresh read of the 1.9.0 release PR after session 145's fixes. Nothing blocking;
six real findings, one of them a correction to a claim this document makes.

- **Editing a chart under `?c_chart=` widened it in silence.** Session 145 stopped the
  *automatic* writes from dropping the reference, which was the blocker. But the analyst's own
  edit still goes through `takeOver`, which spells the chart into `c_*` params — and those
  cannot carry `ids`, `anomalyRunId` or `collapseRoutine`. Changing the metric on an
  agent-scoped chart therefore still turns 47 events into the whole timeline. That is
  unavoidable (the URL is the record now) but it must not be *silent*, which is the whole
  argument of the feature. `unrepresentableFilterMembers` names what a write would lose, and
  the page says so where it already reports a broken reference.

- **`collapseRoutine` was captured for agent charts and dropped for analyst ones.** The rail
  was passed the raw URL filters, on the reasoning that collapse derives from live
  dispositions and the page re-derives it. Only the *page* re-derives it: `ChartBlockCard` and
  the export resolver draw a saved chart's stored filters verbatim. So a chart saved with
  routine collapse on was frozen as its uncollapsed superset — the release's own "a saved
  chart is the slice it was built over" claim, missed for the one narrowing an analyst toggles
  by hand. The rail now gets the resolved set.

- **Uniqueness was judged on the worst source instead of the best-evidenced one.**
  `_uniqueness_ratio` took `max()` over the per-source ratios, so a single 60-event source
  where `user` happens to be per-row-unique vetoed a field that groups cleanly across three
  5000-event ones — and did so *more* often the more sources a timeline merges, which is
  backwards for a scorer that weights breadth highest. It now reads the ratio off the source
  with the most values.

- **The timelines list settled stale recommendations one round trip at a time.** Split into a
  pure `_recommendation_is_dead` predicate and a batched
  `PostgresStore.settle_running_recommendations`, so the common case (nothing stale) touches
  the database not at all and a restart that orphaned a dozen costs one statement.

- **The AI opt-in cap was a wall with no way past it.** At 500 opted-in timelines every
  further opt-in 400'd, and a consent that cannot be recorded is a disclosure dialog that
  reappears forever — which is how people learn to click through it unread. Over the cap the
  oldest entries are now evicted (FIFO on JSON insertion order, which is the opt-in order);
  nothing from the current request is ever the thing dropped.

- **The access-log redactor's survival was untested.** It works only because `dictConfig`
  clears a configured logger's *handlers* and not its *filters* — a CPython implementation
  detail, not a contract, and the difference between "OIDC codes are redacted" and "they are
  back in the journal after a dependency bump". A test now runs uvicorn's own `LOGGING_CONFIG`
  and asserts the filter is still attached and still redacting.

Also: `chartConfigToStored` clears `filters` before writing it, so a future `ChartConfig.filters`
cannot ride the spread into storage and be read back as a slice nobody chose; and the PR
description's "`node:25-alpine`" was corrected to the `node:24-alpine` the Dockerfile and
CHANGELOG actually ship.

**Session 145's withdrawn finding was withdrawn correctly.** This pass re-raised the empty
column selection independently and was wrong for the same reason: `migrateColumns` runs only
in the `version < 1` persist branch, so `[]` does not decay into a permanent `DEFAULT_COLUMNS`
override. Verified in `stores/ui.ts` rather than argued — the change was written and then
reverted.

One flake seen and not fixed: `test_demo_detector_coverage_clickhouse.py::…[find_value_combos]`
failed once in a full run and passes in isolation on both the clean and the modified tree, and
the next full run was green at 2203. It shares a ClickHouse instance with the rest of the
suite; worth a look if it recurs.

## Session 145 — 2026-07-31: the chart link undid itself four seconds after opening

**Why.** A review of PR 230 — the 1.9.0 release PR, `release/1.9.0` → `main`. One blocker,
and it is the release's own headline fix defeating itself.

- **`?c_chart=<id>` was dropped moments after the page loaded.** Session 142's whole argument
  is that three filter members (`ids`, `anomalyRunId`, `collapseRoutine`) have no URL form, so
  a chart must be addressed by id and read back out of storage. But `VisualizePage`'s scale
  probe fires on *any* field change, and `autoProbedField` is initialized at mount — when the
  URL holds only `c_chart` and `field` is still null. Resolving the reference therefore looked
  exactly like the analyst picking a new field: the probe ran, its effect called
  `updateConfig`, and `takeOver` rewrote the URL as `c_*` params with the reference and the
  three narrowings gone. An agent chart scoped to one detector run's 47 events opened as the
  whole timeline, drawn as if it had always been that shape. The same write also reverted a
  scale the analyst had just chosen.

  The tests missed it for a specific reason worth remembering: `vizApi.fieldNumeric` was
  unmocked, so in jsdom it rejected and `numericQuery.data` stayed null — the probe's effect
  never reached its `updateConfig`. The one API call on that page that *writes back to the
  URL* was the one left to fail. It is mocked now, and two of the file's existing tests fail
  without the fix.

  The rule is now stated once and applied to all four defaulting effects (field default,
  time-field scale, numeric probe, metric clamp): **while the URL names a saved chart,
  nothing writes the URL automatically.** A stored chart already answered every question they
  exist to answer. A *broken* reference is deliberately not "live" — a link to a deleted
  chart falls through to the params, where the page is building a chart again and the
  defaults are wanted.

- **The AI opt-in reported the wrong half as failed.** `ColumnPicker` inferred "did the run
  fail, or the save?" from `recommendMutation.isError`, which is sticky across mutations: an
  unrelated local "Re-suggest columns" that had failed earlier made a failed *opt-in write*
  report as "your choice was saved, the suggestion did not start". That is the one wrong
  answer available — the analyst is never asked again for a consent that was never recorded.
  The confirm now tracks its own stage.

- **`_ACTIVE` was documented as a guard and used as a hint.** Only
  `start_column_recommendation` claimed the slot, so the CLI and the demo build — which call
  `run_column_recommendation_job` directly — could run two jobs over one timeline, trading
  writes and each rolling back a placeholder the other owned. The claim is now the guard for
  every caller: a job that finds the slot held by another stands down without touching the
  payload.

- **A settled recommendation misdated its own columns.** The `running` placeholder carries
  the previous answer forward (so the grid holds still), and its `generated_at` has to be the
  *recompute's* start, since that is the clock the explorer measures staleness against. When
  a crash left that placeholder to be settled, the previous run's columns kept the dead run's
  timestamp — in the one record a case export and the audit trail carry forward.
  `columns_generated_at` parks the real timestamp on placeholders only, and settling puts it
  back; carry-forward reads it first, so repeated failures cannot walk an answer's date
  forward one recompute at a time.

Also: the frontend build image goes to `node:24-alpine` rather than the bot's `node:25` (odd
numbered, EOL mid-2026 — a poor floor for an image an operator installs on an isolated host
and never updates), with `ci.yml`/`release.yml` moved from Node 22 to 24 so the runtime that
tests the frontend is the one that builds it; and the two exact-patch action pins the bot
introduced are back to majors, matching every other action in the repo.

One review finding was withdrawn rather than fixed: unchecking every column stores `[]`,
which `resolveVisibleColumns` treats as a deliberate choice. That is correct and tested —
`migrateColumns` runs only in the `version < 1` persist branch, not on every rehydrate, so
the empty selection does not decay into a permanent `DEFAULT_COLUMNS` override, and "Reset to
defaults" stays enabled as the way back.

## Session 144 — 2026-07-31: reviewing the release branch against itself

**Why.** A read of PR 212 before cutting 1.9.0. Four of the findings were in code sessions
139–143 had just written, which is the useful kind: each is a case where the reasoning was
right and one line of the implementation did not follow it.

- **`read_story` spent its budget on the cap, not the text.** `remaining` was decremented by
  `min(STORY_TEXT_TRUNCATE, remaining)` rather than by the characters actually taken, so a
  200-character paragraph cost 8000. Four short blocks exhausted a 24000 budget and the
  fourth came back empty and `truncated` — the marker session 143 added precisely so the
  model would treat a cut block as unread, now firing on a complete one. The three tests
  written with it all used full-size blocks, where the two numbers are equal; a test with ten
  short blocks pins it.

- **The merged-tags facet materialized the whole annotation table to ask a yes/no question.**
  `list_event_ids_with_any_annotation` was called for its truthiness on the endpoint the
  filter panel hits constantly. `has_any_annotation` is the same question as a `LIMIT 1`. (The
  *filter* path genuinely needs the ids and is unchanged — and is no worse than any other tag
  filter, since `bulk_annotate_by_filter` is uncapped and a bulk-tagged value reaches the same
  size.)

- **A chart's stored filters had two writers and one format.** Session 142 taught the backend
  to carry `collapseRoutine`/`eventIds`/`runId` — "dropping them silently *widens* a chart's
  scope" — but the frontend's `filtersToViewPayload` neither writes nor reads them. So the
  analyst's Save on an agent chart scoped to a detector run stored the whole timeline, and a
  chart the backend wrote drew wider on screen than the same chart frozen into its export:
  the screen-versus-report divergence session 142 existed to close, reappearing on the agent
  path. Fixed in a chart-only layer (`chartFiltersToStored` / `parseStoredChartFilters`) so
  saved Views, which deliberately never freeze `collapseRoutine`, are untouched. The
  "Open in Visualize" link stays lossy for those three — they have no URL form by design —
  and `STORIES.md` now says so.

  The same change replaced `hasActiveFilters` as the persistence gate. It is a *chip* helper
  and omits `excludeTag` and `annotationTagValue`, so a chart whose only narrowing was an
  excluded tag stored no filters at all. The gate is now "did anything survive
  normalization", which cannot drift from what is actually stored.

- **`ANNOTATED_TAG` is served, not mirrored.** Session 140 left the string hardcoded on both
  sides under a "keep in step" comment; a rename would have stopped the filter matching
  without raising anything. `/api/health` now names it (`annotated_tag`, beside
  `capabilities`, which is bool-only) and `useAnnotatedTag` reads it with **no fallback
  literal** — a default would be the second copy this removes. The chip waits for health
  rather than guessing.

Also: the access-log redactor's name list is wider and folds case (an IdP that capitalizes
`Code` would have slipped through a list built for one provider); the `annotated` tag value
versus the `annotated` filter *field* — same word, different axis — is called out in
`MODEL_REFINEMENT.md` and at the constant; and a router-level test pins that the resolved ids
actually reach `EventQuery`, which every previous test stopped short of.

## Session 143 — 2026-07-31: the agent read the report through a keyhole

**Why.** Noticed while looking at the story tools: `read_story` truncated each markdown block
at `ATTR_VALUE_TRUNCATE * 8` — 1600 characters, roughly 400 tokens — while
`propose_story_block` accepts up to `VESTIGO_STORY_MAX_MARKDOWN_BYTES` (256 KiB). The read was
capped at 0.6% of what the write allows, so ordinary prose (a few paragraphs) came back cut.

**What made it a correctness bug, not a tuning one.** The cut was unmarked. `_truncate`
appends "…" and nothing else, so the payload said nothing about a block being partial, and
there is no `read_story_block` and no offset — the tail is unreachable through any agent
surface. A model asked to summarize or continue the analyst's narrative therefore reasoned
over half a paragraph believing it had read the block, which is precisely what the system
prompt's evidence rule forbids elsewhere (the `_listing` tools have always reported `returned`
alongside `total` for exactly this reason). The agent also could not read back what it had
itself written.

**Shape of the fix.** Markdown gets its own budget, because it is the document under
discussion rather than incidental string data like an attribute value: capped per block
(`STORY_TEXT_TRUNCATE = 8000`) and per response (`STORY_TEXT_BUDGET = 24000`), spent in
document order. One enormous block can no longer eat the whole response, a long story degrades
block by block instead of all at once, and every block still returns its id/kind/origin —
structure the model can act on beats a list that stops early. Every cut now carries
`truncated: true` and the real `text_length`, with `truncated_blocks` on the response, and the
docstring tells the model to treat a cut block as unread rather than summarize it.

Not done, deliberately: paginated block reads. The honest marker plus a cap that fits real
prose covers the reported problem; a `read_story_block(block_id, offset)` is only worth adding
if the agent is asked to revise long reports, and that is a design question about write
parity, not a truncation fix.

## Session 142 — 2026-07-31: a saved chart is the slice it was built over

**Why.** Reported from use, and the deeper bug behind session 141: build a chart with
Explorer filters active (exclude a few known-good accounts), add it to a story, and the
story's bar chart lists the excluded accounts again. Session 141 saw the same fact from the
other side — "no filters are attached, because a saved chart stores none" — and treated it
as the correct state of the world. It wasn't.

**What was wrong.** On the Visualize page the primary filters come from the URL, inherited
from the Explorer and shown by `InheritedFiltersBar`. `ChartConfig` deliberately holds only
chart *shape*, so `chartConfigToStored` persisted a `SavedChart.config` with no primary
filters at all — only the comparison layer's custom filters survived. Every consumer then
redrew the chart over the whole timeline: the story block (`ChartBlockCard` passed no
`filters` to `ChartCanvas`), the **frozen export** (`_stored_chart_to_spec` built a
`ChartSpec` with no filters, so `execute_chart_spec` ran unscoped), the block's "Open in
Visualize" link, and the agent's `ChartProposalCard`, which rendered the filtered chart and
then saved the shape alone. The backend half-knew: `spec_to_stored_chart_config` refused a
spec with base filters and advised "save the chart from the filtered Explorer view instead"
— which did not in fact capture them.

**Shape of the fix.** The filters are stored as a sibling key of the chart keys —
`{v: 1, …chartConfig, filters: <view payload>}` — not as a new `ChartConfig` field. On the
live page the URL owns those filters and `filterParamsPreservingChartConfig` depends on the
`c_*`/filter-param split; folding them into `ChartConfig` would create a second owner.
Storage is the one place the chart and its scope legitimately travel together, which is
exactly the relationship `View.view_filter` already has, so the payload format is the same
one (`filtersToViewPayload`) and both sides reuse the existing translators
(`_filter_payload_to_spec` / `_spec_filters_to_payload`).

`v` stays `1`: the key is additive, absent on older charts, and absence means "whole
timeline" — what those charts have always done. Nothing migrates, and nothing recovers the
filters charts saved earlier never stored; re-save them from the filtered view.

Loading a saved chart in the rail now restores both halves in one URL write, so the Visualize
page, the story block and the export agree by construction rather than by coincidence. The
agent's rejection path is gone: a `ChartSpec.filters` now has a home, so `propose_story_block`
embeds the slice the agent proposed over. While making the two translators exact inverses
again, `collapseRoutine`, `eventIds` and `runId` — `FilterSpec` members the Explorer can't
produce but an agent chart can — were added to the stored payload, because dropping them
silently *widens* a chart's scope.

**Known limits, stated in `STORIES.md` rather than papered over.** `collapseRoutine` from the
Visualize page is not frozen (it isn't URL-serialized and derives from live dispositions), and
`qMode: "semantic"` survives the payload but has no server-side equivalent, so an export
re-runs it as a keyword query — now a `ROADMAP.md` Milestone 3 item, since this change makes
a pre-existing View-block gap reachable for charts too.

## Session 141 — 2026-07-31: a story's chart link opened Visualize, but not the chart

**Why.** Reported from use: "Open in Visualize" on a story's chart block navigated to the
right case and timeline and then drew nothing recognisable — the preset picker, on a default
chart.

The Visualize page holds its entire chart state in `c_*` URL params (`paramsToChartConfig`),
which is what makes a chart shareable as a link at all. `ChartBlockCard` linked to the bare
`…/visualize` path, so every one of those params was absent and the page did exactly what an
empty URL asks for. The agent's `ChartProposalCard` had always built its link through
`chartConfigToParams`; the story card was the one place that didn't.

The block resolves the saved chart already, in order to draw it — so the fix is to run that
same config through `chartConfigToParams` for the href. No filters are attached, because a
saved chart stores none and the story's own canvas draws it unfiltered: the link now lands on
the chart the reader was looking at, not a differently-filtered relative of it.

## Session 140 — 2026-07-31: every annotated event says so, without a row saying it

**Why.** Asked for: an event that anyone or anything annotates should also carry a tag
`annotated`. The interesting part was choosing *where* it lives.

There are two independent tagging systems sharing one UI panel — `events.tags`, an
`Array(String)` written once by the parser at ingest, and annotation rows in Postgres with
`annotation_type="tag"`. The first was never an option: adding to it means an
`ALTER TABLE … UPDATE` mutation over an already-ingested event, which rewrites whole parts
and breaks the invariant that the ingested record is immutable and hash-identified.

That left a stored tag row versus a derived one, and stored loses on every axis. The marker
would itself be an annotation (recursion), would need an idempotency check, would need
cleanup when the last real annotation is deleted or else it lies, and would add a row per
event to every bulk detector write. Derived has none of that: `ANNOTATED_TAG` is resolved
from "does this event have any annotation", so it is correct by construction and there is no
write path to get wrong.

- `list_event_ids_with_any_annotation` — deliberately unfiltered by type or origin. A
  comment, an agent proposal and a detector finding all count, which is what makes the tag
  mean what its name says rather than "has a tag".
- `_resolve_tags_filter` unions those ids in as a fourth tag population, and only when the
  filter actually names the tag — it costs a query.
- The merged-tags facet offers it once the timeline has an annotation, following the same
  rule as every other value there.
- The grid renders the chip from the annotations it already holds, so no extra fetch.

## Session 139 — 2026-07-31: two things a deployment's own log showed

**Why.** Reading the journal of a live instance, not a test run. Both findings
predate today's merges; neither would ever fail a test, because both are about what
the app does *around* a correct response.

- **`GET /api/cases/` ran `alembic upgrade head` on every request.** `list_cases` and
  `create_case` each opened with `await store.init_schema()`, so the busiest endpoint in
  the UI re-ran the migration machinery per call — a connection, a version-table check
  and a migration lock, on the hot path. The startup lifespan already does this once,
  which is the documented contract; the handler calls were redundant, and under
  concurrency the lock is a contention point rather than just noise. Both removed. The
  `Context impl PostgresqlImpl` / `Will assume transactional DDL` pair that bracketed
  every case-list hit in the journal is gone with them.

- **OIDC authorization codes were logged in the clear.** Uvicorn's access log writes the
  full request target, and the callback carries `code` and `state` as query parameters —
  that is the protocol working as designed, but it put a live credential into the system
  journal on every SSO login. `AccessLogRedactor`, attached to the `uvicorn.access`
  logger at app creation, replaces the value of every sensitive parameter name while
  keeping the path and the parameter names, so an operator can still see that a callback
  carried a code. Attached to the logger rather than a handler, since uvicorn owns the
  handler and an embedding process may replace it. The audit trail was already clean —
  it records `request.url.path`, never the query.

## Session 141 — 2026-07-31: the scorer's uniqueness test was per-timeline, not per-source (PR #214)

**Why.** A second review of #214 found the endpoint tests never ran the endpoint, and
one scoring bug that got *worse* the more sources a timeline held — the shape the
feature exists for.

- **Per-row-unique fields ranked first on multi-source timelines.** `distinct` is
  max-across-sources while `coverage` sums across them, so the uniqueness ratio was
  divided by the source count: four 1000-event sources carrying a unique-per-row value
  read as 1000/4000 = 0.25, inside the full-grouping-credit band, then boosted by
  breadth (the heaviest weight). Measured `1.06` — top of the list — for the emptiest
  possible column, while identical single-source data was correctly rejected.
  `score_columns` now keeps the per-source `(distinct, coverage)` pairs and
  `_uniqueness_ratio` takes the max ratio over sources that clear
  `_MIN_COVERAGE_FOR_UNIQUENESS`, falling back to the aggregate (taper only, no
  rejection) when no source has enough values to judge — which is what preserves the
  tiny-source behaviour.
- **Four endpoint tests posted to `/api/cases` instead of `/api/cases/`** and died on
  405 before reaching the assertion. The contribute gate on
  `POST .../recommend-columns` and the `use_ai` passthrough — the two guards on shared
  state and on egress — were untested in practice. Both now genuinely run.
- **`_MAX_PREFERENCE_ENTRIES` is enforced on the merged blob**, not only on the request.
  The endpoint merges one level down, so 500 fresh keys per call grew the row without
  bound — exactly what the limit exists to stop. Over the ceiling is a 400, never a
  silent truncation: dropping an opt-in the caller believes it recorded is how a
  disclosure gets skipped. Inner keys are also length-bounded (128).
- **A dead `running` payload now settles on the timeline *list* too**, not only the
  single read. The two endpoints reporting different states for the same timeline was
  the bug; a caller that only ever lists never saw it resolve.
- **The disclosure no longer claims a saved opt-in was lost.** The confirm is two steps
  (persist the opt-in, then start the job) and both fed one boolean, so a failed job
  request read as "that did not save". Errors are now `"save"` / `"run"`, with copy per
  case. Nothing leaves the machine either way.
- **`_ACTIVE` is claimed with `setdefault` before the `try`**, so the job never stomps a
  slot another job holds and the `finally` never releases one it did not take. The API
  path claims it in `start_column_recommendation` before spawning; the CLI path claims
  it here.

## Session 140 — 2026-07-31: the column-suggestion review, and one setting fewer (PR #214)

**Why.** A review of #213 turned up five findings. Fixing the disclosure one exposed a
design problem underneath it: `VESTIGO_COLUMN_RECOMMEND_MODE` was a second, admin-only
tri-state layered on top of a question the codebase already answered — is an agent
endpoint configured and reachable (`agent_available()`). It forced the disclosure into
an incoherent shape: on a default (`heuristic`) instance a non-admin got a blocking
modal disclosing egress that was not happening and offering an action they could not
take. The setting was the cause, not the gate.

- **The setting is gone** — from `core/config.py`, `settings_registry.py` (with its
  one-member "Explorer" group), `.env.example`, `/api/health` and the frontend types.
  Suggestions always run; the scorer is local and reads a cache that already exists.
- **The LLM half is now an explicit per-(user, timeline) opt-in.** The job takes
  `use_llm`, defaulting to **False**, and every automatic trigger leaves it there — so
  ingest, timeline creation, the CLI and the demo build score locally and **egress is
  never a side effect of uploading a file**, which is a stronger posture than the
  merged branch had. One caller sets it: `POST .../recommend-columns` with
  `{"use_ai": true}`, behind "Suggest with AI" in the Columns picker. The first press
  on a timeline opens the disclosure (endpoint, model, exactly what is sent);
  confirming records `preferences.column_advisor_optin[timeline_id]` and only then
  runs. Cancelling sends nothing; the next timeline asks again. The result stays shared
  — the opt-in governs who *causes* egress, not who may read it, and the audit row
  names the actor. This supersedes `column_advisor_notice_ack` from session 139.
- **`_ACTIVE` could wedge a timeline permanently** (finding #1): the job claimed the
  slot before its `try`, so an early return leaked it and no further job for that
  timeline could ever start. Claimed inside the `try` now, covered by the `finally` on
  every path, with a regression test.
- **A `running` payload now settles on read** (finding #3), not only on the next boot: a
  cancelled task never restarts the process, and the explorer polls on that word.
  `settle_running_payload` is shared by the boot sweep and the read path so they cannot
  drift.
- **"Reset to defaults" clears the local override** instead of writing one (finding #5).
  Writing `DEFAULT_COLUMNS` quietly opted that browser out of every future
  recomputation; "Use suggested" is redundant now and is gone.
- **Demo seeding is genuinely best-effort** (finding #2) — the two calls outside the job
  were unguarded and could fail a first login over a column layout.
- **The privacy claim is now tested.** `_format_candidates` renders every promise the
  disclosure makes and had no coverage; a test asserts the prompt carries the candidate
  table and no case, source or timeline id, with samples truncated at 40 characters.

## Session 139 — 2026-07-31: the grid opens on columns the corpus actually has (issue #213)

**Why.** Every timeline opened on `timestamp / artifact / message`. One of those three
earns its place: `artifact` usually restates a filter the analyst already set, and
`message` truncates to nothing in a 160px cell. The fields that would tell them
something — `user`, `src_ip`, `event_id`, whatever this corpus has — were a popover
away and only if you knew to look. So the most important screen in the product opened
on its least informative view, every time.

- **`Timeline.recommended_columns`** (migration `0024`, one nullable JSON column on the
  `field_mappings` precedent). Derived per timeline, shared by everyone with access,
  carrying the columns, a per-column reason, which method produced them, and the source
  set it was computed over. `status` is the contract with the frontend: `running` while
  a job is in flight, `ok` to apply, `insufficient` for "we looked and found nothing" —
  recorded rather than left null, so the explorer stops waiting and the job is not
  re-run hoping for a different answer.
- **`src/vestigo/columns/`**, three layers so the expensive part stays optional.
  `recommend.py` scores candidates off the existing per-source field-stats cache
  (`db/field_stats.py`) — **zero new ClickHouse scans on the common path** — weighting
  breadth across sources highest, then fill rate, then a cardinality band that rejects
  both the constant and the per-row-unique, with hashes/GUIDs/paragraph-length values
  gated out and a small name vocabulary as the tie-breaker. Pure, deterministic, 24 unit
  tests. `advisor.py` is one typed LLM call that **reorders and selects from** those
  candidates; `jobs.py` orchestrates and persists.
- **Not a different recommender by accident.** `db/field_recommend.py` answers "what is
  worth vectorizing" and therefore rejects precisely the fields an analyst wants on
  screen — ports, status codes, IPs. Only its value-shape regexes are shared.
- **The AI half is bounded by code, not by prompt.** Gated on the same cached
  `agent_available()` probe `/api/health` uses; the model sees a candidate table and
  nothing else — no ids, no events; every token it returns is intersected with that
  table; a malformed, short, timed-out or unreachable response is indistinguishable from
  "no LLM configured" and the deterministic ranking stands. The stored `method` says
  which one won. Documented in `AGENT.md` §"Outside the agent loop".
- **The AI half is also opt-in, and says why.** The candidate table carries up to three
  real sample values per field — evidence-derived strings — so `auto` is egress and the
  default is `heuristic`: scorer only, nothing leaves the machine. The first Explorer
  visit on an instance with an agent configured shows a one-time dialog
  (`ColumnAdvisorNotice`) naming what would be sent, the endpoint and the model; an
  admin can enable `auto` from it, everyone else reads it, and the acknowledgement is
  per user (`preferences.column_advisor_notice_ack`, `PUT /api/auth/me/preferences`).
  The demo build passes `allow_llm=False`, so seeded content never triggers a model call
  and first-login seeding never waits on one.
- **A `running` payload can no longer wedge a timeline.** `JobStore` is in-memory, so a
  restart mid-job used to leave `status: "running"` in Postgres forever with the
  explorer polling it every 3s. The placeholder now carries the previous answer forward
  (the grid holds still during a recompute instead of flapping to the defaults), a
  startup sweep relabels whatever a dead job left behind
  (`clear_stale_running_recommendations`, in the lifespan rather than `_startup_recovery`
  so a ClickHouse outage cannot skip it), and the client stops believing a `running`
  claim older than ten minutes.
- **Soft, never blocking.** The issue asked for the timeline to be disabled until the
  process finished; a hung LLM endpoint making a timeline unbrowsable is the wrong
  trade, so the grid renders the built-in defaults immediately and re-lays out when the
  job lands, behind a `role="status" aria-live="polite"` line.
- **Precedence is one function** (`lib/columns.ts`): the analyst's own choice, then the
  suggestion, then `DEFAULT_COLUMNS`. Read by both `ExplorerPage` and `ColumnPicker` so
  the ticks always match the grid. "Use suggested" *clears* the local override rather
  than copying it in, so a later recomputation still reaches that browser. (Session 140
  folded this into "Reset to defaults" and dropped the separate button.)
- **Scheduled from every path that creates a knowable source set**: post-ingest (beside
  `refresh_source_field_stats`, isolated so a failure never reaches the ingest
  rollback), timeline creation, the CLI, the demo build, and a contribute-gated
  `POST .../recommend-columns` behind "Re-suggest columns" in the picker.
- **`VESTIGO_COLUMN_RECOMMEND_MODE`** (`heuristic` by default, or `auto` / `off`) in a
  new "Explorer" settings group; the job itself enforces it, so the CLI and the demo
  build honour `off` without their own check. **Removed in session 140** — replaced by
  a per-(user, timeline) opt-in. Every run writes a `timeline.recommend_columns` audit
  row naming the method, the model, the chosen columns and the full candidate set.
- **Known gap, filed in `ROADMAP.md`:** timelines with `field_mappings` get no
  suggestion for the mapped fields. The grid reads `attributes[colId]` directly, so
  neither the canonical name nor one raw spelling renders correctly — recommending
  either would recommend a column that looks broken.

## Session 138 — 2026-07-31: OIDC discovery followed a redirect it should have followed

**Why.** A live deployment against a Nextcloud IdP got a 500 on every SSO click.
Nextcloud 301s `/.well-known/openid-configuration` to
`/index.php/.well-known/openid-configuration`; `_oidc_metadata` used a default
`httpx.AsyncClient` (`follow_redirects=False`), so `raise_for_status()` raised on the
301 and nothing caught it.

- **Follow redirects on discovery** (`api/routers/auth.py`). Every mainstream OIDC
  client does — pinning the issuer to `/index.php` would also have worked but leaves the
  issuer string disagreeing with the endpoints the document advertises.
- **Discovery failures are now 502 with the attempted URL**, plus a `WARNING` log, rather
  than an unhandled exception and a traceback per click. That half was not doc-fixable:
  it applied to any unreachable or misconfigured IdP, not just Nextcloud.
- **Documented the issuer**, which was the actual gap — `docs/DEPLOYMENT.md` gained an
  "OIDC single sign-on" section with per-IdP issuer forms (Authentik, Nextcloud,
  Keycloak, Okta, Google), a `curl` verification one-liner, and the redirect-URL rules.
  `.env.example` carries the short version.
- **Noted the env-pin footgun** in `.env.example`: `VESTIGO_OIDC_ENABLED=false` left in a
  `.env` pins the field, so the admin console's SSO toggle silently does nothing (the
  override is dropped with an INFO log). Surfacing env-pinned fields in the settings UI
  is still open — filed in `ROADMAP.md`.

## Session 137 — 2026-07-30: README reordered, and a count that did not add up

**Why.** The README read as a wall of text. The cause was ordering, not volume: the
AMiner/Timesketch positioning occupied lines 28–55, so the screenshot (line 57) and Quick
start (line 109) both sat below it — a visitor met the prior-art discussion before learning
what the tool does or seeing it run.

- **Reordered.** Pitch → screenshot → Quick start → Capabilities → Architecture → How it
  compares → Documentation. The comparison keeps its substance but moves below
  Architecture and condenses ~28 lines into ~14, with the full five-axis version left where
  it belongs in `CONCEPT.md` §8. The two relationships stay distinct per the tone rule:
  Timesketch as the invited comparison, AMiner explicitly labelled a method source.
- **Tightened the Capabilities bullets** from up to nine lines each to at most five, bold
  lead-in plus one or two sentences, long tails pushed to the doc links that already exist.
  1099 → 1004 words while adding a bullet.
- **Stories was missing entirely.** The living-report subsystem ships (`docs/STORIES.md`)
  and `CONCEPT.md` §8 names it as a differentiator, but the README never mentioned
  reporting. Added as a capability bullet and a documentation-list entry.
- **The detector count was wrong.** The intro said "fourteen analysis tools" while
  Capabilities said "twelve statistical detectors ... plus log-template clustering, a Sigma
  rule runner, and semantic similarity" — fifteen. Per `ANOMALY_DETECTION.md` the split is
  twelve statistical (log templates among them, and "value combinations" being a variant of
  detector 1 rather than its own tool) plus Sigma plus semantic similarity. Also dropped
  "every one of them explainable down to the SQL it ran", which is not true of the vector
  search.
- Fixed "throug" and "Aminer". Laid the screenshot out for a 2×2 grid with a placeholder
  comment; the capture list is filed under Milestone 3.
## Session 136 — 2026-07-31: the demo case, reviewed

**Why.** A review of PR #211 before merge. One bug could take a user's demo case away
permanently, one endpoint was an authenticated storage-amplification primitive, and the
rest were smaller correctness and honesty problems.

- **A claim that never dispatched is now released.** `maybe_seed_demo_case` stamped
  `demo_case_seeded_at` and *then* dispatched; when dispatch hit the concurrency cap the
  `RuntimeError` was swallowed by the never-fail-a-login handler and the stamp stayed. That
  is precisely the post-upgrade backfill case — fifty users stamped, one seeded, and no way
  back for anyone whose case list wasn't empty. `PostgresStore.release_demo_seed` gives an
  unspent claim back, so the next login retries.
- **One demo case per account.** `POST /api/demo/seed` had no per-user guard: a loop over it
  wrote a quarter of a million ClickHouse rows per call. Cases now carry `is_demo`
  (migration 0023) and the endpoint answers 409 while the caller still has theirs. The same
  flag keeps other users' copies out of an admin's case list, which would otherwise show one
  per account, and drives the restore offer's visibility in the UI.
- **Seeds are cancelled at shutdown.** Nothing cancelled the background tasks, and
  `build_demo_case`'s teardown caught `Exception` — which `CancelledError` is not — so a
  shutdown mid-ingest left a half-populated case in someone's list. The lifespan now calls
  `cancel_pending_seeds()`, and the build tears down on `BaseException`.
- **The cap is a setting, and it is 1.** The build is CPU-bound Python holding the GIL, so
  each concurrent seed contends with the API's own loop. `demo_max_concurrent` replaces the
  hardcoded 2, following `transfer_max_concurrent`. `DEPLOYMENT.md` said the overflow
  "queues"; it does not.
- **The full sweep, finally run.** It had never been: 2113 tests, one failure, and it was
  session 135's own. `test_enrichers.py` carries a second copy of the pre-Alembic column
  list that `test_postgres_store.py` has, and only the latter was updated for 0022 — so the
  adoption path tried to add `demo_case_seeded_at` to a column that `create_all` had already
  made. Both copies now drop both columns. Two copies of that list is the actual defect;
  the next migration will trip over it again.
- **The job store is per-test now.** `get_job_store()` is a process-wide singleton, so a
  test leaving a job queued held an admission slot for every test after it. Invisible while
  the demo cap was 2 and immediately fatal at 1: a seed that silently never dispatched.
  An autouse fixture gives each test its own store, which also retires the never-finishing
  tasks the 429 test used to leak.
- **A flake worth naming, not fixed.** `find_value_combos` in auto mode picks its two
  fields from `recommend_novelty_fields`, which ranks on ClickHouse's *approximate*
  `uniq()`. The demo coverage test caught it going quiet once and never again across
  repeated runs. The detector is fine; a canary test that can flip is not, and pinning the
  fields there would trade the auto path — the one the UI actually uses — for a green run.
  Left as-is deliberately, recorded here so the next flake is not mistaken for a new bug.
- **Smaller.** Row ordering in the generators sorted ISO *strings* — correct only by
  accident of a fixed UTC offset, now sorted on the parsed instant. Dead `noqa: BLE001`s
  (`BLE` is not in the ruff select list) removed. Restore requests are audited at dispatch,
  not only on success, so an abusive loop is visible. Frontend comments claiming the
  capability meant "the archive is packaged" outlived the archive by a whole architecture.

## Session 135 — 2026-07-30: a demo case for every new user

**Why.** A new user's first screen was an empty case list, which is the worst possible
introduction to a tool whose whole argument is detection-as-workflow. Every user now finds
a fabricated investigation waiting for them: 251k events, four sources, 30 days, a quiet
three-week baseline and a seven-day intrusion, with an analyst's notes, tags, saved views,
a baseline definition, four Sigma rules and a story already in place. Spec:
`docs/superpowers/specs/2026-07-30-demo-case-design.md`.

- **Realism over meta-labels.** Nothing in the case names the detector it triggers. The
  annotations read as one investigator's working notes, and the story explains the incident,
  not the product. Two of the findings are deliberately benign and called out as such (an
  NTP-drifting host, a backup job whose schedule legitimately moved) — a demo where every
  anomaly is malicious teaches the wrong reflex.
- **Generated, not shipped.** The first cut built a prebuilt `.vestigo` archive, which came
  out at **146 MiB** — events travel uncompressed inside the archive by design, so the
  "10–15 MB" the design assumed was off by an order of magnitude. Committing that on every
  regeneration was not acceptable in a repo that has to stay airgap-shippable, so the
  generator moved into `src/vestigo/demo/` and runs per user instead: ~2.5s to fabricate the
  four source files, a few seconds to ingest them through the real `IngestionPipeline`. It is
  deterministic, so every copy is identical down to the source files' SHA-256 hashes, and the
  provenance story is the one an ordinary ingest tells.
- **Seeded once per user, ever.** `users.demo_case_seeded_at` is claimed with a conditional
  UPDATE, so two simultaneous logins cannot both dispatch, and the stamp survives deletion —
  a deleted demo stays deleted. Existing users backfill through the same path at their next
  login, since their stamp is null. `POST /api/demo/seed` is the way back, offered from the
  empty case list. Hooked into `_issue_session` rather than the login handler, so OIDC
  behaves identically; the call swallows its own errors, because no demo case is worth a
  failed login. Two builds run concurrently instance-wide; the rest get a 429.
- **The coverage test is the actual deliverable.** `tests/test_demo_detector_coverage_clickhouse.py`
  runs all thirteen non-embedding tools plus the four Sigma rules over the seeded case and
  asserts each returns real findings. The first version of that test passed vacuously — it
  fell back to the result *object*, which is always truthy — and once fixed it immediately
  caught that event sequences found nothing over the default `series_field`. The demo's
  promise is only true for as long as something checks it.
## Session 134 — 2026-07-31: PR #210 review pass — the ratchet's own blind spot

**Why.** A review of the tier-1 design work before merge. Five findings, all fixed in the
branch; nothing filed.

- **The ratchet did not cover the file the same PR created.** `designSystem.test.ts` globbed
  `components/**/*.tsx` and `pages/**/*.tsx` only, so `lib/`, `hooks/`, `stores/` and every
  `.ts` file were unscanned — including `lib/guidance.tsx`, which the PR had just created to
  hold JSX copy with token classes, and `components/viz/lib/colors.ts`, which returns
  `var(--viz-*)` strings into the SVG export path where a dead token exports a blank fill
  rather than a visibly wrong colour. Demonstrated by planting a bogus token in each and
  watching all four assertions pass. The token check now scans every `.ts`/`.tsx` under
  `src/` except `src/test/` (whose files quote token names in prose and fixtures); the two
  budgeted checks stay on components and pages, since only JSX has `text-[Npx]` or
  `<button>`. Widening it surfaced three false positives, both classes now handled: block
  comments are stripped before the scan (`export.ts` documents `var(--x)` as a placeholder),
  and `var(--viz-series-${slot})` is skipped as a computed name — `vizColors.test.ts` already
  asserts that family literally.
- **Both regexes were lowercase-only.** `--colorError` would have been invisible to the
  definition scan and the reference scan alike, and so silently exempt. Both now take the
  full custom-property character set.
- **The legacy-key adoption could never run for the browsers that needed it most.** It sat in
  `migrate`, which zustand calls only when the store already has persisted state at an older
  version. A browser that dismissed guidance but never wrote a UI preference has
  `vestigo-guidance-*` keys and no `vestigo-ui` entry: it lost the dismissal *and* kept the
  keys forever, because its next preference write persists at v5 directly and `migrate` never
  fires again. Moved to `onRehydrateStorage`, which runs on every load.
  `guidanceLegacyAdoption.test.ts` covers all four paths and three of its cases fail against
  the previous implementation.
- **The Sigma tab had no empty state**, so its Run button would scan an empty timeline and
  report zero matches — which reads as "these rules cleared you", the exact failure the PR
  fixed in the detector views. It now gets the same guidance-plus-empty-state treatment as
  Patterns. The duplicated hint JSX behind that (three near-identical copies) collapsed into
  one local `NoEventsState`, and the two adjacent identical `activeTab === "patterns" &&
  nothingToAnalyse` guards became one. `investigateEmptyTimeline.test.tsx` now covers the
  gating with the tab bodies stubbed — the Sigma case fails against the pre-fix panel.
- **The "events appear as they land" promise depended on the panel's host.** `InvestigatePanel`
  set no `refetchInterval` on `["timeline-sources", …]`; it stayed fresh only because
  `ExplorerPage` polls the same key at 4s while ingesting. Stated on both queries now, so the
  copy does not silently rely on a caller.

Also restored the multi-line `detector-shared` imports in thirteen views, which the tier-1
commit had collapsed into ~200-character single lines directly above multi-line imports in
the same files. `E501` is ignored by convention, but the diff was gratuitous.

**Verified.** 689 frontend tests (680 + 9 new), typecheck, lint and a production build pass.
Each fix was demonstrated failing against the pre-fix code before being trusted.

## Session 133 — 2026-07-30: frontend design audit; dead tokens and the brand mark

**Why.** An out-of-band design pass before 1.9 work begins. The starting suspicion was the
Investigate panel's user guidance; the audit found that plus a broader pattern, and one
class of silent visual bug worth fixing immediately.

**Fixed this session.**

- **Three CSS custom properties were referenced but never defined.** `--color-error` in 12
  files (`EntropyView`, `OrderViolationsView`, `DistributionDriftView`, `ProportionShiftView`
  and others) — the real token is `--color-danger`, so every "this is the bad direction"
  arrow in the detector views rendered in inherited body text colour instead of red. The one
  colour-coded signal in the views whose entire job is signalling did nothing.
  `--color-bg-subtle` (`CorrMatrix.tsx:168`, `VisualizePage.tsx:1429`) → transparent
  correlation-matrix cell fill for the no-value case. `--color-border-focus`
  (`FrameBar.tsx:55`) → a hover border change that was a no-op. All three now point at
  defined tokens. Notable: all of it compiled, typechecked, linted and passed tests. Tailwind
  emits `text-[var(--color-error)]` happily and CSS resolves an undefined var to inherit,
  silently — which is the argument for the lint ratchet now filed in `ROADMAP.md`.
- **The brand mark used two hues from nowhere.** `VestigoMark.tsx` hardcoded `#8b5cf6` violet
  and `#06b6d4` cyan against an accent of `#3b6e91`/`#5aa8b0`, fixed across both themes. The
  mark's semantics were already right — the band that is out of cadence was the odd colour —
  so it now carries `var(--color-accent)` for the cadence bands and `var(--color-anomaly)`
  for the offset one: the same two colours the analysis views use for "expected" and "this
  one stands out", stated in the app's own vocabulary and following the theme.

**Then tier 1: enforcement, and the copy work that needed no design round.**

- **The ratchet — `frontend/src/test/designSystem.test.ts`.** Three checks over a raw glob of
  `components/` and `pages/`, following `vizExplainers.test.ts` (the repo's only other
  source-scanning test, whose header explains why it uses Vite's raw glob rather than
  `node:fs`: the frontend tsconfig carries no node types). Undefined `var(--…)` is hard and
  starts at zero; `text-[Npx]` and raw `<button>` outside `components/ui/` are budgeted per
  file in `designSystemBudget.ts`, seeded at 119 and 119 across 66 files. Three assertions,
  because a floor nobody lowers is not a ratchet: over budget fails, a *beatable* budget fails
  with "lower it to N", and a budget entry for a deleted file fails. Proven in all three
  directions before being trusted. Two things it immediately paid for: it found six further
  `--color-border-focus` references that the morning's `grep | head` had truncated away, and
  the budget seeding showed the real counts were 119/119, not the 118/130 the audit reported.
  Vitest stubs CSS imports to `""` even under `?raw`, so `vite.config.ts` opts `index.css`
  into `test.css.include` — scoped to that one file so no other test pays for the Tailwind
  pipeline.
- **Guidance is now unbypassable rather than merely centralized.** `lib/guidance.ts` became
  `guidance.tsx` keyed by panel id, and `GuidancePanel` lost its `title` and `children` props
  — it takes `id: GuidanceId` and reads both from the registry. The four Investigate panels
  that inlined their JSX could not be fixed by convention (that is what the file already
  asked for and did not get); with no copy props to pass, inlining is a type error. Bodies
  stay JSX because the copy carries `<strong>`, `<em>` and a `font-mono` span. `satisfies`
  rather than a type annotation, or the keys widen to `string` and `GuidanceId` constrains
  nothing. Converter copy moved to a sibling `converterCopy` export — it is not panel content.
  `guidanceCoverage.test.ts` covers only the reverse direction, copy nothing renders, which is
  the half the type system cannot see.
- **Dismissal is restorable.** Collapse state moved from `vestigo-guidance-*` localStorage
  keys into the `vestigo-ui` store (v4 → v5, migrating the old keys in). The old code read its
  flag once into `useState` at mount, so clearing storage left every open panel collapsed
  until remount — the reason a reset button alone would have looked broken. "Show guidance
  again" sits in the Settings *Onboarding* section beside "Restart onboarding tour": same
  intent, and `Compass` was already both buttons' icon. Still per-browser; per-user is filed.
- **Empty states.** `AnalysisEmptyState` joins the chrome in `detector-shared.tsx` — the one
  piece that escaped that file's stated purpose and was hand-rolled identically thirteen
  times. It borrows `viz/primitives/ChartEmptyState`'s contract (primary line = what happened,
  `hint` = cause and next action, copy stays at the call site because only the detector knows
  why it is empty). The "no events" claim lifted to `InvestigatePanel`, which is the only
  place that can tell an un-ingested timeline from a detector that ran clean — it already
  queries `listSources`, and it distinguishes "still ingesting" from "nothing uploaded", with
  a link to the case overview. `components/analysis/` had contained no `Link` at all. All
  thirty-odd strings rewritten so none states absence twice, and the `insufficient_data` arm
  that `ValueNoveltyView` and `OrderViolationsView` were missing — silently reading as a clean
  result when the detector never ran — was added.

**Verified** on an isolated instance (`tsig_verify` databases, port 8099): a case with no
sources yields a default timeline with `sources: []`, and a 600-row seed exercises both the
`ok`-with-no-findings and `insufficient_data` arms the rewrite targets. The built bundle
carries the new copy and none of the old. The browser extension was not connected, so the
rendered panel and the reset-without-reload interaction were not driven by hand — the latter
is asserted by a unit test that fails against the old `useState` implementation.

**Filed, not fixed.** The rest is in `ROADMAP.md` Milestone 3 under "Frontend design-system
consistency", now stated as burning the budget file down to `{}`. Two items are correctness
rather than taste: compact density does not scale the 119 arbitrary font sizes, and the
Investigate panel still teaches the disposition model at the moment nothing on screen
demonstrates it — the tier-1 work fixed that panel's plumbing, not its placement.

## Session 132 — 2026-07-30: PR #208 fourth review pass, and the 1.8.6 merge

**Why.** A fourth review before merging 1.8.6. Six findings raised, four real. Both of the
substantive ones were in `evtx2vestigo`, and both were the same shape as session 131's: a
module that states an invariant carefully, then breaks it somewhere the invariant's own
author wasn't looking.

- **Converter-derived keys overwrote native fields.** Session 131 made `_extract_event_data`
  scrupulous about never letting one Windows element overwrite another — and then `build_row`
  wrote `host`, `user`, `src_ip`, `src_port`, `MapDescription` and the `Map*` properties
  straight into `attributes`, discarding an EventData field of the same name. Plausible on
  third-party channels, and invisible on the row. The derived value still has to win the
  plain key (the platform reads these by name — the GeoIP enricher wants `src_ip`), so this
  resolves the opposite way from `_free_key`: the *native* value steps aside to a numbered
  spelling. The `EventData_`-prefixed form now goes through `_free_key` too, so a record
  carrying a literal `EventData_Channel` alongside an `EventData` `Channel` keeps both.
- **A record id repeated inside one chunk collapsed two records.** The cross-chunk case was
  handled deliberately and documented; one level down, `_scan_chunk`'s `setdefault` kept only
  the first occurrence, so two records sharing an id in a partially overwritten chunk got the
  same offset, size and `content_hash` — one forensic identity for two records, which is
  exactly what the per-chunk scan exists to prevent. Occurrences are now listed in document
  order and consumed positionally (the parser yields them in that order), and the footer's
  duplicate counter reports a repeat wherever it fell instead of only across chunks. The test
  forges a duplicate into the fixture: the parser does yield both records, and each hash
  reproduces from its own span.
- **The charset fallback probe omitted the sentinel guard.** The violation scan appends
  `VESTIGO_NOT_SENTINEL_SQL` in temporal mode; the probe did not, so a group living only in
  sentinel-timestamp rows was reported absent — buying the whole-scope fallback learn the
  probe exists to avoid, plus a warning naming a group no finding can ever come from.
- **`max_gap_seconds` coalesced on truthiness.** Correct only because the API floor is `ge=1`.
  Keyed on presence now, so a future floor change can't route a persisted 0 to the other
  detector's key.
- **One finding was wrong and is recorded as such.** The `group_field` type guard looked
  incomplete — `TOP_LEVEL_NON_STRING_COLUMNS` holds only `timestamp`, so `byte_offset` seemed
  to slip through. It isn't a resolvable column token at all (`TOP_LEVEL_EVENT_COLUMNS` has no
  numeric members besides `timestamp`), so it routes to a string `attributes` lookup and the
  frozenset is already complete. No change.

**Verification.** Full CI green on the PR head: backend lint + test, frontend
typecheck/lint/build, container smoke test, CodeQL. Locally 467 tests across the
converter/detector/router/agent suites, `ruff check` + `ruff format --check` clean, frontend
674 tests. `vendor_evtx_maps.py --check` **was** re-run this session against a real
`EricZimmerman/evtx` checkout — upstream HEAD is exactly the pinned `03a7a1f` — and reports
the embedded maps and manifest in sync, closing the one item sessions 130 and 131 both left
unverified.

## Session 131 — 2026-07-30: PR #208 third review pass, and the 1.8.6 version cut

**Why.** A third review of the 1.8.6 PR. Six findings, all fixed. The one that mattered
most was the least interesting: the release PR had no release in it.

- **Nothing was bumped.** `pyproject.toml`, `src/vestigo/__init__.py`,
  `frontend/package.json` (and its lockfile) all still read 1.8.5 and the changelog
  section was still `[Unreleased]`, on a branch titled `release: 1.8.6`. Prior releases
  land as a `chore(release): X` commit; this one had none. Now 1.8.6 everywhere.
- **The grouped charset row ceiling truncated in silence.** `LIMIT plim BY grp` preserves
  each group's budget, but the 5,000-row ceiling *under* it orders by novelty length
  across every group — so hitting it drops whole low-novelty groups, not a slice of each.
  The rest of that code path is scrupulous about naming every deviation in `warnings`;
  this was the one that vanished, and a truncated run came back looking like a clean one.
  It reports now, and says which fields.
- **Two of the grouped warnings had lost their field.** `wide_dropped` and
  `thin_unevaluated` were keyed by field; `fallback_absent`/`fallback_thin` were flat sets
  merged across every scanned field. The same group can be thin for one field and absent
  from the baseline window for another, so the merged form put a group in both sentences
  with no way to tell which field either was about. All four are per-field now.
- **The gap bound measured the wrong thing.** `dateDiff('second', …)` counts second
  *boundaries crossed*, so a 1.2 s step straddling two of them reported 2 and
  `max_gap_seconds=1` segmented a burst whose every step was barely over a second.
  `age` counts complete elapsed units, which is what "farther apart than N seconds" means.
  NULL-on-first-row behavior is identical (measured on 26.6.1, through the whole
  `Nullable(Int64)` → `UInt8` → `UInt64` chain), so the segment-start comment still holds.
  The live test that pins it was written twice: the first fixture used 200 ms steps and
  passed under *both* functions — 1.2 s steps are what actually discriminate, and the
  test now fails if the call is reverted.
- **An orphaned tool result rendered as a call.** Pairing answers by identity, and a
  result row whose call row is missing from the transcript matches nothing — it then fell
  through to the call branch and drew an argument-less row for a call the agent never
  made. `tool_result` settles it without reopening the zero-argument-call ambiguity that
  session 130 closed: the server writes args on a call row and a result on a result row,
  never both.
- **`evtx2vestigo` still had one silent overwrite.** EVTX permits a repeated
  `<Data Name="X">` in one `<EventData>`, and `UserData`'s recursive `iter()` can put two
  same-named tags on one key; both kept only the last value. Given how deliberately the
  `DataN` positional/named collision is resolved, this was the collision that wasn't.
  First occurrence keeps the plain spelling Sigma addresses, the rest are numbered in
  document order, probing for a free key so a literal `X_2` in the same record doesn't
  collapse into it.

**Verification.** Backend suite and `ruff check`/`ruff format --check` clean; frontend
`typecheck`, `lint` and 674 tests pass. The `age` change is covered live against
ClickHouse 26.6.1, not by SQL-text assertion. Still not re-run: `vendor_evtx_maps.py
--check` (needs an upstream checkout) — the blob region was untouched and the manifest
hash was refreshed with `--manifest-only`.

## Session 130 — 2026-07-30: PR #208 second review pass

**Why.** A second review of the 1.8.6 PR, after session 129's fixes. Eight findings, all
addressed. The substantive ones were in the grouped charset path again — not in what it
computes, but in whether its own `warnings` describe what it did.

- **The two per-group skip guards meant opposite things and were treated as one.** An
  alphabet over 5,000 characters says *the question does not apply* (a novel character
  carries no signal in free text); fewer than 20 distinct values says *not enough
  evidence*, which does not exonerate the group. They are split now: wide → dropped and
  named, thin → scored against the fallback. "Absent from the baseline window" is just the
  `n_vals = 0` case of the thin condition and takes the same route, which removes a
  discontinuity where less evidence would have bought better treatment.
- **The warnings lied in two directions.** Thin groups *were* being fallback-scored while
  reported as "not evaluated", and `no_fallback_fields` claimed absent groups went
  unevaluated whenever the fallback learn failed — including when no group was absent.
  Warnings now name the groups, keep "no baseline values" and "too few baseline values"
  apart, and state which guard the fallback itself tripped, since "too few distinct
  values" would send an analyst to widen a baseline that was never the problem.
- **Self-baseline grouped mode had no fallback at all**, so enabling `group_field` deleted
  thin groups from a run that previously scored them against the merged whole-scope
  alphabet — a coverage regression caused by a precision feature. It now falls back to
  exactly that reference (`group_basis = "scope-merged"`).
- **The fallback learn is a whole-scope heavy scan and ran unconditionally per field.** A
  bounded `SELECT DISTINCT` probe over the suspect windows now decides whether it is
  needed; a truncated probe counts as "needed", because an unchecked group must never be
  assumed safe. Findings carry `details.group_baseline_distinct_values`, so a report says
  *why* a fallback scored a row rather than leaving it to be inferred.
- **A review finding of my own was wrong, and the code now records why.** I claimed a
  wide-alphabet group could be scored against the fallback. It cannot: the fallback is
  learned from a superset of every group's own data, so its alphabet is at least as wide,
  and a group over the ceiling guarantees the fallback is refused too. Measured on 26.6.
  The explicit `skip` exclusion is kept as belt-and-braces so the routing does not rest on
  that argument holding, and the comment says as much.
- **Live-ClickHouse coverage for the D14 SQL.** The D14 tests asserted SQL *text*; the new
  constructs only fail at execution. `test_charset_group_field_clickhouse.py` and
  `test_sequence_max_gap_clickhouse.py` exercise array-of-array parameter indexing,
  `LIMIT … BY grp LIMIT …`, the `skip`/`has_fb` routing, and the two-level segment window —
  including the `Nullable(DateTime64(3))` assumption behind "gap_s is NULL on each
  partition's first row", which now fails a test if it ever stops holding.
- **`evtx2vestigo`.** The `DataN` collision fix was order-dependent — with the positional
  element first, the named one overwrote it and the positional value was lost. It is
  decided from the record as a whole now. A fallback `byte_offset` also stamps
  `byte_offset_basis=record_id`: a record id is indistinguishable from a real offset by
  inspection, and `content_hash_basis` is a statement about the hash, not the offset.
- **Agent panel tool rows.** Pairing discriminated call-vs-result on whether the row had
  arguments, so a zero-argument call persisted with `null` args read as a result and
  mispaired a real one; keyed rows now pair on `tool_call_id` and only unkeyed legacy rows
  use the heuristic. The row is keyed by call identity rather than list position (it owns
  `<details>` open state), and that state has one owner instead of a native toggle and a
  React handler racing on the same click — which also makes it behave the same in jsdom,
  where `<details>` is not natively implemented.

## Session 129 — 2026-07-30: PR #208 review findings

**Why.** Full review of the 1.8.6 release PR: two scale/semantics problems in the new
grouped charset path plus a tail of validation and perf items. No shape from the PR was
redesigned.

- **A reviewed "defect" in the gap segmentation was not one.** The review argued that
  `gap_s` being NULL on a partition's first row made `if(NULL, 1, 0)` NULL, the window `sum`
  NULL, and a NULL `seg` its own partition — losing the n-gram over rows 1..`ngram`.
  ClickHouse takes the *else* branch for a NULL condition (`if(NULL > n, 1, 0)` is `0`,
  `UInt8`), so segments already started at 0. Checked live on 26.6 against a 9-event series
  with a 3-day gap: 7 complete 3-grams unbounded, 5 with a 1 h bound, `a→b→c` present both
  ways, and byte-identical results with and without a defaulted lag. The comment now records
  the semantics it relies on, with the measurement behind it.
- **Grouped charset scanned `events` once per group.** Group cardinality is caller-chosen
  and unbounded, so `group_field=attr:src_ip` meant one heavy scan per host per field. It is
  now one scan per field: the per-group reference alphabets go in as parallel
  `{grps, sets}` arrays, each row picks its own by `indexOf` (`greatest(gidx, 1)` because
  ClickHouse evaluates both `if` branches and rejects index 0), and `LIMIT plim BY grp`
  preserves the per-group finding budget.
- **A group the baseline window never saw was silently unevaluated** — the newly-provisioned
  host, i.e. the interesting one. Those groups are now scored against a fallback reference
  learned *outside* the suspect windows (learning it over the whole scope would let the
  suspect values into their own reference and mask themselves), findings carry
  `details.group_basis` so a report never conflates the two references, and the row shows it.
  Guard-skipped groups and an unusable fallback are reported in `warnings` instead of
  vanishing; events missing the grouping field stay a real group, rendered `(no value)`.
- **Smaller items.** A non-string `group_field` is refused with 422 rather than reaching
  ClickHouse as a type error (group expressions are also `toString`-wrapped); `view_filters`
  is size-bounded at 16 KiB now that it is persisted per message; agent tool rows only format
  their payload once expanded (a `<details>` renders children regardless of state) and only
  pair a streamed result on a non-empty `tool_call_id`; `evtx2vestigo` gained a `DataN`
  collision rule (named field wins, positional moves to `DataN_pos`), a corrected header
  layout comment, and an explicit note that fallback rows' `byte_offset` is not a file
  offset. `scripts/vendor_evtx_maps.py --manifest-only` refreshes the converter's manifest
  hash without an upstream checkout.

## Session 128 — 2026-07-30: `evtx2vestigo` review findings

**Why.** PR #209 review of the session-127 converter. Nothing was shipped-broken, but two
latent defects sat in the part of the design the single-chunk test fixture cannot reach.

- **The synthetic one-chunk image carried an invalid header CRC32.** `_iter_chunk_blobs`
  rewrote the first/last chunk numbers and the chunk count but not the checksum at offset
  124 — and the count was written as `<I` into a 2-byte field. The fixture is a one-chunk
  file whose header already reads first=last=0, count=1, so the mutation was a *no-op there*
  and every test passed while the multi-chunk path — i.e. every real log — had no coverage at
  all. Confirmed by hand on a synthetic two-chunk file: all emitted images had a bad CRC, and
  the current `evtx` wheel simply tolerates it. A stricter parser release would have rejected
  every multi-chunk file. The header checksum is now recomputed, and
  `TestChunkImages` asserts image validity plus a 14-record round trip.
- **Offsets are now scanned per chunk, not per file.** The whole-file
  `{record_id: (offset, size)}` dict kept the *first* of duplicate ids, but the parser still
  yields both records — so in a re-chunked or partially overwritten log two distinct records
  received the same `byte_offset` *and* `content_hash`, and `derive_event_id` is a function
  of exactly those. Two events, one identity. Scanning each chunk immediately before parsing
  it removes the ambiguity structurally (a chunk's records can only resolve against their own
  chunk) and drops peak memory from one file's worth of offsets to one chunk's. The footer's
  `chunk_scan` note keeps its old key names, duplicate count included.

**Also.** Directory input now runs the magic/text-export check per file — a `wevtutil` dump
saved as `.evtx` was silently contributing zero rows with exit 0 — warning and skipping
rather than aborting a triage collection, plus a warning for any file that parses to zero
records. `Refine` regexes are capped at 8 KiB of input: vendor time proves a pattern
*compiles*, not that it terminates, and EventData is evidence. `_RESERVED_ATTR_KEYS` is a
hand-copy of the server's `TOP_LEVEL_EVENT_COLUMNS` and is now pinned to it by a test.

**`converter_version` is per-script, not suite-wide.** All six converters read `1.3.0` and
`INPUT_FORMATS.md` keyed "writes the forensic footer" on `>= 1.3.0`. `evtx2vestigo` is on its
first version, so it is now `1.0.0` and the docs say what is actually true: probe for the
footer keys, do not infer them from a version number that each converter advances on its own.

**Verification.** 52 tests in `tests/test_evtx_converter.py` (was 39): multi-chunk images and
their checksums, duplicate-id offset distinctness, `xml_sanitized` and the rendered-XML
offset fallback (via a stubbed parser — byte-patching the fixture is impossible, the parser
validates chunk checksums), `--split` size mode, `--until`, junk-directory handling. Not
re-run: `vendor_evtx_maps.py --check`, which needs an `EricZimmerman/evtx` checkout; the
generated blob region was untouched, and the manifest hash was refreshed directly.

## Session 127 — 2026-07-30: `evtx2vestigo`, binary Windows Event Logs

**Why.** Windows event logs were reachable only through `evtx2timesketch`, which takes a
*text* export. That re-anchors provenance to the analyst's XML dump rather than
`Security.evtx`, and pushes millions of records through the row-by-row server path. The
Sigma runner was also aimed largely at Windows rules with no Windows data to match.

**What the design turns on.**

- **The parser exposes no byte offset.** `pyevtx-rs` yields `event_record_id`, `timestamp`
  and rendered XML — nothing addressable. So the converter walks the EVTX container itself
  (4096-byte header, 64 KiB chunks, per-record magic + size + id) and joins the resulting
  `{record_id: (offset, size)}` onto the parsed records. `content_hash` then covers that
  same raw span, which is what makes `dd bs=1 skip=<byte_offset> count=<record_size> |
  sha256sum` reproduce it with no Vestigo tooling. Hashing the rendered XML instead would
  have made event identity a function of the parser's version.
- **Per-chunk parsing, not whole-file.** The obvious approach — hand the parser the file —
  aborts *permanently* at the first bad chunk header, and cannot be resumed: on
  `sample_with_a_bad_chunk_magic.evtx` it yields 14 records and stops. Feeding it one
  chunk at a time (templates and the string cache are chunk-local, so a chunk plus a header
  is a complete document) recovers 270. Same cost — 0.04 s either way on a 12 MB log.
- **Sigma-canonical attribute names.** `FieldResolver.resolve` matches `attributes` keys
  literally and case-sensitively, so `EventID` must be exactly that, unpadded. `EventData`
  keeps native Windows names because that is already what SigmaHQ rules address; System
  spellings win on collision (`EventData_<name>` otherwise) so a payload field cannot
  rewrite `Channel`. Map-derived prose is namespaced under `Map*`. Snake_case duplicates
  for evtx2timesketch parity were rejected — they double the attribute count and add a
  spelling Sigma will never resolve.
- **The EvtxECmd corpus is embedded, not referenced.** Converters are single-file
  downloads, so `scripts/vendor_evtx_maps.py` parses all 468 `.map` YAML files at vendor
  time, validates every XPath expression and `Refine` regex, and emits a zlib+base64 blob
  (181 KB, decoded lazily). The converter therefore needs no PyYAML and can contain no
  unsupported expression. `--check` fails the build on drift.

**Things that were not true until checked.**

- The record *header* id and the `<EventRecordID>` element can disagree (1 vs 319457771 in
  the test fixture — an extracted log renumbers one but not the other). The header id is
  the offset join key, so it is now on the row as `evtx_record_id`.
- Some maps key their `Lookups` on the raw `%%14592` while also carrying a `Refine` that
  strips the `%%`. Applying refine-then-lookup blanks them; the converter tries the lookup
  before *and* after refining rather than guessing the author's intent.
- A real 4739 record in `security.evtx` carries a raw `\x03` in EventData, which is illegal
  in XML 1.0 — the parser renders XML no parser will read back. Rather than dropping a
  genuine event, illegal characters are replaced with U+FFFD (the convention the CSV/JSONL
  path already uses for undecodable bytes) and the row is flagged `xml_sanitized`.

- **"Rules match with an empty `fallback_fields`" was wrong**, and the end-to-end run caught
  it. The names resolve correctly — a stock 4672 rule compiles to
  `attributes['EventID'] = '4672' AND attributes['Channel'] ILIKE 'Security'` and matched
  459 events — but `fallback_fields` tracks whether a *mapping vouches for the name*, not
  whether the match is right, so correct-by-construction names are still flagged. A
  timeline field mapping cannot fix it (`validate_field_mappings` rejects the identity
  mapping as shadowing the raw key); a global ruleset `vestigo-fieldmap.yml` can. Measured
  over the real SigmaHQ `rules/windows/builtin` corpus: an identity fieldmap took the run
  from 873 fallback flags to 0 with zero SQL and zero match-count differences. Docs now say
  this instead of the claim that was easier to write.

**Verification.** Scanner record-id sets match the parser exactly on all 8 upstream samples
including a 12 MB dirty log (14,621 records, 10 bad chunks skipped) and the two the
whole-file path cannot read at all. Zero records lost anywhere in that corpus.
`tests/test_evtx_converter.py` (39 tests) covers the offset round-trip, the blob
invariants, and the Sigma naming contract.

End-to-end, the real **SigmaHQ `rules/windows/builtin` corpus (326 rules, commit
`1aacbed`) compiled and ran against an `evtx2vestigo` timeline with zero errors**, and the
five rules that hit were each reproduced by hand from the raw Parquet — including
`User Added to Local Administrator Group` narrowing 6 candidate 4732 events to 2 via
`TargetUserName`/`TargetSid`/`SubjectUserName`, which is what proves the native *EventData*
names resolve and not just the System ones.

## Session 126 — 2026-07-29: AMiner detector gap audit, and two claims that were not true

**Why.** Session 125 fixed how we *talk* about logdata-anomaly-miner. This session read its
`aminer/analysis/` source module by module and checked what we actually adapted against
what we claim to have adapted. Coverage is roughly two thirds of the upstream catalogue,
and the batch/forensic reframing (analyst-declared baseline definitions replacing
`learn_mode` + persistence) holds up as the right port. Two claims did not.

- **The entropy detector was mislabeled.** `docs/ANOMALY_DETECTION.md` §6 said "Adapted
  from AMiner's `EntropyDetector`". It is a different statistic: AMiner learns a
  character-**bigram** transition table and flags low mean pair probability; we compute
  per-value Shannon character entropy against a Tukey fence. That difference has teeth —
  a lowercase-latin DGA domain among English hostnames, the example the section leads with,
  has unremarkable Shannon entropy and is **not** flagged, while AMiner's model catches it.
  Corrected in the doc and in the `find_entropy_outliers` docstring, with the capability gap
  filed as **D11** (ship the bigram model as a second `method` on the same detector).
- **Two scope narrowings were undocumented.** Charset learns one alphabet per field where
  AMiner learns one per `id_path_list` identifier, so hosts that legitimately differ get
  merged into a reference alphabet that flags neither. Sequence n-grams have no gap bound
  where AMiner resets a sequence after `timeout` seconds, so a quiet source manufactures
  "sequences" from events days apart. Both are now Caveats; closing them is **D14**.
- **Five real gaps added to Milestone 4**, ordered truth-first then payoff-per-effort:
  D12 time-of-day habit (`PathValueTimeIntervalDetector`), D13 cross-field value correlation
  (`VariableCorrelationDetector` — intra-record, distinct from D10's temporal rules),
  D15 impossible-speed transitions (`MinimalTransitionTimeDetector`), D16 multivariate
  window profiles (`EventCountClusterDetector`), D17 new field key (`NewMatchPathDetector`).
  D12/D13/D15 are the cheap ones — they reuse the `lagInFrame` partitions, the `_col_expr`
  field resolution and the G-test/BH pool that already ship.
- **`PCADetector` moved to explicitly skipped**, with a reason rather than an omission: its
  output is a reconstruction error in a rotated space that cannot be traced back to events,
  which is exactly what the field-agnostic/SQL-explainable constraint exists to prevent.
  D16 is the same signal, readable, cheaper.
- **Standing requirement recorded on the milestone**: a detector is not shipped until its
  Method-tab explanation, visible SQL/params and disposition wiring land with it. Reasoning
  an analyst cannot read fails the reproducibility bar regardless of the statistics.

Docs touched: `ANOMALY_DETECTION.md` (§6 rewrite, charset + sequence caveats), `ROADMAP.md`
(M4 restructured into the priority tiers, priority list at top), `CONCEPT.md` §8 (gap list
instead of a two-item omission), `CLAUDE.md` (same), `db/anomaly_stats.py` (docstring).
No behavior change.

## Session 125 — 2026-07-29: AMiner is a method source, not a competitor

**Why.** Sanity check on session 124's positioning: had the new copy claimed superiority
over logdata-anomaly-miner? Audit says no — every explicit "better"/"ahead" either named
Timesketch or was scoped to "the analyst who wants both at once". But the framing was
sloppy in a way worth fixing.

- **The two projects were introduced in one breath as prior art we improve on.** They are
  not the same relationship. AMiner solves a different problem — online detection over live
  log streams — and is not in our category at all. Timesketch is.
- **The real bug was the asymmetry.** "Where we are honestly behind" named only Timesketch,
  which implies we are behind AMiner on nothing. False, and false *by construction*: per
  ROADMAP M4 our detectors are deliberately narrowed to field-agnostic and SQL-explainable
  to meet the forensic-reproducibility requirement, `TSAArimaDetector` is skipped outright,
  and D10 (their `EventCorrelationDetector`) is unbuilt.
- **Fixed in all three places.** README now separates the two relationships into their own
  paragraphs and says plainly that we are not competing with or replacing AMiner; CONCEPT
  §8 opens with "the debt is of two different kinds", states that anyone needing live-stream
  detection should run AMiner, and its behind-paragraph now names both projects; CLAUDE.md's
  References section splits into "same category" vs "method source, not a competitor" and
  the tone rule gained an explicit prohibition on lumping the two together.

## Session 124 — 2026-07-29: say what we are better at, and what we are not

**Why.** The positioning undersold the project. "Sits between a heavyweight SIEM and one-off
notebook scripts" describes a gap being filled, not a tool worth choosing, and CONCEPT.md
dismissed Timesketch in five words ("powerful but operationally heavy and broad") — both
too timid and, in being that terse, faintly rude about the project we borrowed the entire
investigative model from.

- **README leads with three positions instead of a gap.** Detection as a first-class part
  of the investigation (fourteen tools, explainable to the SQL, baseline-scored, verdicts
  that survive re-scans); provenance at event granularity (per-event content SHA-256 +
  byte offset, hashed configs, immutable hashed sources); and one process against three
  services with no cluster, broker or worker fleet. Credit to Timesketch and
  logdata-anomaly-miner is now explicit and warm rather than a parenthetical, and the
  section closes by naming Timesketch's maturity as real.
- **CONCEPT.md §8 rewritten from three bland bullets to five grounded ones**, plus an
  explicit "where we are honestly behind" (production hardening, analyzer ecosystem,
  community). §2's one-line dismissal became a specific, fair critique — the deployment
  floor and detection-as-side-panel — rather than an adjective.
- **Every claim was checked against code before it was written**: 98 settings fields, no
  celery/redis anywhere in `pyproject.toml`, `byte_offset`/`content_hash`/`file_hash` on
  `models/event.py` and folded into `derive_event_id`, reproducible canonical-JSON snapshot
  hashing in `stories/schemas.py`. Deliberately avoided any assertion about Timesketch's
  *current* feature set, since nothing in this session verified one — the comparative claims
  are about architecture and about what Vestigo ships, which are checkable.
- **CLAUDE.md gained a tone rule** so this does not drift either way later: confident about
  what ships, never dismissive of prior art, never a claim without code behind it, and no
  unverified comparative statements about another project's features.
- Milestone 5's heading dropped "parity" — W8 (schema-on-read) was never parity work.

## Session 123 — 2026-07-29: Vestigo is not a "small team" tool

**Why.** The README's "for small security teams" was traced back to `CONCEPT.md` §3, written
at project inception and never revisited. It was doing two jobs at once — describing who the
tool is for, and standing in for a real deployment constraint — and only the second is true.

- **The size framing is gone.** README, `CONCEPT.md` §1/§3, `CLAUDE.md` and `TECH_STACK.md`
  no longer scope Vestigo to a headcount. Nothing in the product actually cares: case-level
  RBAC, teams and the audit trail behave identically at any size, and the data path was
  never sized to a team (300M-row reference case, 80 GiB+ timelines). `CONCEPT.md` §3 now
  says a lone examiner and a large IR organization are both in scope; `TECH_STACK.md` §3.3
  justifies Postgres-over-SQLite as "multi-user, whatever the headcount" rather than
  "2–10 analysts".
- **The real constraint got its own home.** New `DEPLOYMENT.md` §"Operational scale": run
  exactly one app process per instance, because five subsystems keep state in that process's
  memory — `core/jobs.py`, `core/events_bus.py`, `core/login_backoff.py`, `db/viz_cache.py`
  and `get_settings`'s `lru_cache` — with a table of what a second worker breaks in each
  (invisible jobs, one-worker SSE, a lockout threshold that multiplies by worker count, cold
  caches, settings changes that reach one worker). Says plainly what to do instead: scale the
  box and ClickHouse, and don't pass `--workers`. The pre-existing settings-cache paragraph
  now points there instead of half-permitting multi-process.
- **Two standing decisions were resting on the wrong noun.** A11's "fine for the small-team
  threat model" became the assumption it actually makes — every authenticated user may know
  who else has an account — with a trigger that describes a *sensitive directory*
  (compartmented investigations, several groups sharing an instance) rather than a large org.
  CSRF's "LAN threat model" gained an explicit trigger set: internet exposure, or moving off
  a single trusted process. The persistent-job-store entry now links to Operational scale and
  names multi-process scale-out as its trigger.

Nothing here changes behavior; it makes the docs describe the constraint that exists instead
of a market segment that does not.

## Session 122 — 2026-07-29: dependabot triage, and the patch the standing decision missed

**Why.** The push in session 121 surfaced two open Dependabot alerts. Both were assessed
for real exposure rather than taken at face value.

- **`react-router` GHSA-qwww-vcr4-c8h2 (high) is patched on 7.x, and we had missed it.**
  The 2026-07-27 standing decision rested on "the last `react-router-dom` release is 7.18.1,
  so no installable version sits outside the advisory range". That premise expired on
  2026-07-28, when `react-router@7.18.2` shipped PR #15353 — *"Harden RSC CSRF codepaths"*,
  the backport of #15311, which is the fix released in 8.3.0 on 07-22. Upgraded
  `react-router-dom` 7.18.1 → 7.18.2: a lockfile and manifest bump with **zero source
  changes**, `tsc -b --noEmit` and `oxlint` clean, 75 test files / 653 tests passing.
  The alert will probably persist anyway — GitHub and npm still range the advisory
  `>= 7.12.0, < 8.3.0` and never carved out 7.18.2 — so the ROADMAP entry now says to
  dismiss it as "fix already applied" instead of acting on it. It also records *why* the
  `dependabot.yml` ignore was refused, which is the reason this patch was catchable at all:
  ignoring `< 8.3.0` would have suppressed 7.18.2 along with the noise.
  The pre-existing unreachability argument still holds independently (SPA, zero
  `unstable_*` imports, upstream files the fix under "unstable features"), so this was
  defense in depth, not an incident.
- **`diskcache` GHSA-w8v5-vhqr-4h9v (medium) needs no action, and cannot get one.** No patch
  exists (`first_patched_version: null`). It arrives as `pysigma → diskcache` and is used
  only by `sigma/data/mitre_attack.py` and `sigma/data/mitre_d3fend.py`, which we never
  import — verified by constructing the app plus our sigma modules and confirming
  `diskcache` and both modules are absent from `sys.modules`, rather than by reading
  imports. The attack additionally needs write access to `~/.cache/pysigma/`, which already
  implies code execution as that user. Filed as a standing decision to dismiss. Noted
  alongside it: those two pysigma modules `urlopen` MITRE data from GitHub, so pulling them
  in would break the airgap guarantee — a second reason to keep them out of the import
  graph.

## Session 121 — 2026-07-29: backlog triage + documentation audit

**Why.** Nothing in flight and no open issues or PRs, so every outstanding item was
re-evaluated against the codebase and the docs were audited for drift.

- **Backlog triage.** Each ROADMAP item was checked for progress, obsolescence, whether it
  still makes sense, and whether it is worth doing. Three moves: the converter-benchmark
  residue, the Sigma end-to-end-test residue and the story-artifact upload deferral became
  trigger-bearing standing decisions rather than open items (each was already a decision
  with a trigger, filed as work); the Sigma `logsource` scoping half was promoted out of
  "residue" because unscoped rules are a precision defect, not polish; and the
  `api/routers/events.py` split moved from standing decision to open item because its own
  revisit trigger fired — 3100 lines on 07-20, 3319 today, still growing untouched. The
  OpenAPI-types item gained the evidence that made it urgent (`types.ts` 1240 → 1549 lines
  since filing). M7 is now explicitly marked as a go/no-go design gate rather than a
  committed build, since it redefines the product's scope.
- **`ANOMALY_DETECTION.md` said twelve tools; there are fourteen.** The Sigma runner (§13)
  and log templates (§14) shipped as full sections but were never added to the intro list
  or the code map. Three trailing changelog-shaped sections ("Reality check", "Explicit
  baseline + suspect windows", "Unified disposition taxonomy") were audit history, not
  reference: everything durable in them was already documented in place (the z-score
  normality caveat sits in the frequency section, the window semantics in the normality
  model), so they collapsed into one short implementation-notes section. −45 lines.
- **`CONCEPT.md` listed unbuilt features as shipped.** §6.4 promised embedding-space
  outlier detection and rare-cluster highlighting; `db/similarity.py` only does
  neighbor search. Rewritten to describe what actually runs, with the omission stated as
  the deliberate choice it is. Also added the shipped surfaces it never gained (charts,
  Stories, agent, live collaboration) and corrected "simple multi-user auth" to the
  RBAC/audit reality.
- **`CLAUDE.md` architecture map had drifted.** The router list named 3 of 16 modules; the
  `db/anomaly_stats.py` entry named 2 detectors and described the `baseline_end` split
  point that baseline definitions replaced; five top-level packages (`agent`, `sigma`,
  `stories`, `transfer`, `enrichers`) were missing; the frontend component list was four
  directories short; and a note claimed TECH_STACK still had TBDs.
- **Smaller fixes.** `TECH_STACK.md`'s "container-first, single-node Docker Compose
  deployment" principle contradicted the native-`uv`-app model every other doc states.
  Annotation types in `CONCEPT.md`/`MODEL_REFINEMENT.md` still said "comment, tag, or
  highlight" instead of `tag`/`comment`/`anomaly`, and did not distinguish annotations from
  dispositions. `MODEL_REFINEMENT.md` gained a pending-revision banner for the M7 Artifact
  rename. `STORIES.md` dropped the roadmap ID from its title.
- **`PROGRESS.md` split.** Sessions 71–100 moved to
  `archive/PROGRESS_SESSIONS_71-100.md`; the live file is 987 lines instead of 2402.
- Verified: every relative markdown link and heading anchor across `docs/` and the
  top-level `*.md` files resolves (scripted check, zero failures).

## Session 120 — 2026-07-29: the last three open defects, closed

**Why.** Nothing in flight; the backlog held exactly three issues (#158/#159/#160),
all `priority: low`, triaged as ROADMAP B5/B6. Clearing them makes the backlog purely
feature-shaped before the next feature batch.

- **`LoginBackoff`'s `max_entries` is now an actual bound** (#158). The filed scenario —
  unbounded growth via rotating usernames — does not hold: rotating keys sit at
  `locked_until = 0.0`, which `_prune_expired_locked` drops, since it deletes everything
  with `locked_until <= now`. The real residual was the case where pruning frees
  *nothing*: all `max_entries` keys locked into the future, so `setdefault` inserted past
  the cap for as long as the flood sustained those locks. Pruning now falls back to
  evicting the entry whose lock expires soonest, and the bound check is skipped entirely
  for a key already tracked (`setdefault` on a present key cannot grow the dict). Eviction
  necessarily discards the evicted key's failure count — the entry *is* the slot being
  freed — so that key gets `threshold` unthrottled attempts before a lock re-arms. That is
  a larger concession than the existing prune makes (prune only drops keys whose delay was
  already waited out), and it is priced: reaching the path costs ~50k requests at the
  defaults, and the key freed is whichever lock was closest to expiring anyway, never a
  chosen victim — a victim under active attack has an exponentially growing lock, which
  sorts away from the minimum. Documented at the eviction site rather than papered over.
  Three new tests pin the behaviour, two of them failing pre-fix (4 entries where the cap
  is 3); the third guards the already-tracked-key short-circuit.
- **The detector-run inspection API stays, and is written down** (#159).
  `GET /api/cases/{case_id}/detector-runs/{run_id}` has no frontend caller by design: it
  is the explainability affordance for a `run_id` surfaced in a filter, an audit
  `target_id` or an export — an analyst can ask months later what parameters produced it
  without re-running the detector. It looked orphaned because the whole `run_id`
  mechanism was documented nowhere but the code and an archived PR review. New
  "Persisted detector runs" section in `docs/ANOMALY_DETECTION.md` covers what a
  `DetectorRun` stores, `run_id` as a filter param across events/count/histogram/
  bulk-annotate/export/viz, and the 404-on-stale-id contract.
- **`.env.example` needed no change** (#160). The issue's "~23 of ~88" is stale: the file
  names 66 of 98 settings fields, and all 98 are covered by a `SettingSpec`
  (`tests/test_settings_api.py::test_registry_covers_every_settings_field`), so every
  field is editable in the admin console whether or not it appears here. The curated
  subset plus the precedence header shipped in session 109 is the design. The one name in
  the file with no matching field, `VESTIGO_FRONTEND_REBUILD`, is a genuine env-only var
  read in `web/app.py`.
- ROADMAP's "Open defects" section is deleted rather than left standing empty.

## Session 119 — 2026-07-29: story exports were dropping every chart

**Why.** Issue #197: "story exports dont render diagrams, only the sections."

- **The charts were never in the file.** `ChartFrame` starts at `width = 0` and learns
  its real width from a `ResizeObserver` in an effect, gating on `{width > 0 && <svg/>}`.
  The export renders through `renderToStaticMarkup`, which runs no effects and has no
  `ResizeObserver`, so the width stayed 0 and each chart block emitted an empty `<div>`.
  Nothing errored, which is why it read as "only the sections".
- **Fixed with a pinned static width**, not a raster fallback: `ChartStaticWidthContext`
  supplies `ChartFrame`'s *starting* width, and `SnapshotRenderer` provides 848px (the
  `max-w-4xl` article minus its `p-6` gutters). A live `ResizeObserver` still overrides
  it, so nothing about the on-screen charts changes. The export stays real `<svg>` —
  selectable text, no resolution ceiling, and still self-contained, which a PNG/SVG
  round-trip through the server would have complicated for no gain.
- **Verified by rendering it**, not just by asserting a tag: the exported document
  screenshots with both charts drawn — bar chart with category labels and value
  annotations, time histogram with axes and rotated tick labels. The regression test
  requires at least one `<svg>` per resolved chart block plus actual drawn geometry, and
  fails on the pre-fix build (1 svg — a lucide icon — for 2 chart blocks).

## Session 118 — 2026-07-29: "Add at top" was adding at the bottom

**Why.** The story editor's top inserter put its block last. Found while reading
`BlockPicker`/`StoryEditor` for an unrelated defect; confirmed against a live story
(`[(1024,'first'), (2048,'second'), (3072,'ADD AT TOP')]`).

- **`after_block_id: null` means opposite things on two endpoints.** On create it appends
  at the end; on move it goes to the top. That split is deliberate and documented — every
  append caller depends on the create meaning (the "Add to story" pushes, the agent's
  `propose_story_block` default) — so flipping it would silently prepend for all of them.
  The real gap was that create could not express "top" **at all**: a block going above
  everything has no anchor to name. The button therefore sent `null` and got an append.
- **Create takes an explicit `at_top`**, mutually exclusive with `after_block_id` (422 if
  both, enforced in the router so the contract shows up in the OpenAPI schema). Default
  behaviour is untouched, which is what the append callers keep relying on.
- **One definition of "top of document."** `PostgresStore._story_top_position` — halve
  below the first block, renumbering from index 2 when there is no room — is now shared by
  insert-at-top and move-to-top instead of the move path owning a private copy.
- Pinned at three levels: the store (stacking twelve at-top inserts forces the renumber),
  the API (order after insert, explicit `null` still appends, both-fields 422), and the
  editor (the top inserter sends `at_top`, the between-blocks one still sends its anchor).

## Session 117 — 2026-07-29: the "freeze" after session 115 was a leaked input lock

**Why.** Story view still "froze" after #193 was fixed — but only when inserting a
**view/chart/event** block, never a text block, and aborting the picker was enough.

- **It was not a render loop.** A DevTools performance capture over 27s of the hung page
  showed 357ms of scripting and an idle main thread. Nothing was spinning: the page kept
  rendering and polling (the ingest progress modal animated throughout) and only stopped
  accepting *input*. That reading is what redirected the search — session 115's fix was
  correct and unrelated.
- **Radix modal layers leaked the body pointer-events lock.** Each modal layer sets
  `pointer-events: none` on `<body>`, capturing the previous value and restoring it on
  unmount. `BlockPicker`'s embed items open a modal `Dialog` from inside a modal
  `DropdownMenu`'s `onSelect`, so the dialog mounted while the menu's lock was up and
  captured `"none"` as its own "original". The menu unmounted and restored `""`
  correctly; closing the dialog then restored `"none"` — with no layer open. Confirmed
  on the live page (`bodyPE: "none"`, `openDialogs: 0`, `openMenus: 0`) and reproduced in
  Chromium over CDP, which showed the capture order directly.
- **Fixed by not overlapping the layers**: the menu is `modal={false}`, so the dialog is
  the only layer managing the lock. The menu still closes on outside click and Escape and
  only gives up a scroll lock a four-item insert menu never needed. "Text" opens no
  dialog, which is why it was the one kind of block that still worked.
- **Regression test** pins the invariant that makes the overlap impossible — the menu must
  not lock `<body>` while open. jsdom is not used for the full open/abort cycle: Radix's
  dialog under React 19 + RTL's async `act` wrapper hangs there for unrelated reasons, and
  an async assertion would report that instead of this bug. `BlockPicker` is the only
  menu+dialog nesting in the app; the `Popover` call sites default to non-modal.
## Session 116 — 2026-07-28: a podman-built bundle could not install on a docker host

**Why.** Operator report from the field: `install.sh` on an intact, checksum-matching
1.8.4 bundle printed `Loaded image: localhost/vestigo-app:1.8.4-1a1690c` and then
`error: missing image(s) after load: vestigo-app:1.8.4-1a1690c`, and refused.

- **Image reference resolution differs between the two engines, and we wrote the name
  the ambiguous way.** `podman build -t vestigo-app:TAG` stores `localhost/vestigo-app`
  and `podman save` writes *that* into the archive. `docker load` keeps the name
  verbatim, but resolves a bare `vestigo-app:TAG` — what `compose.airgap.yml` and the
  installer's `image_usable` check both asked for — to `docker.io/library/vestigo-app`.
  Different image, absent, correct refusal. Podman on the far side resolves the short
  name to `localhost/`, which is exactly why every rehearsal passed: podman-built,
  podman-installed. The three backing services were already `docker.io/`-qualified and
  loaded fine either way, which made the app image look singled out.
- **Fixed by removing the ambiguity rather than by teaching the check to guess.**
  `APP_IMAGE="localhost/vestigo-app:$TAG"` in the builder, the same string in the compose
  file's `image:`, the same in the installer's check. Nothing pulls it, so the registry
  component costs nothing. A retag on the target unblocks bundles already carried out;
  `docs/DEPLOYMENT.md` §Troubleshooting has it, as the third entry that looks like a
  damaged bundle and isn't.
- **Guarded.** `test_the_app_image_is_fully_qualified_in_all_three_files` pins each of the
  three spellings and additionally fails on *any* surviving unqualified `vestigo-app:$`
  reference in those files — one missed spot restores the bug whole.
- **Released as 1.8.5**, since a fix that only ships inside a bundle needs a version an
  operator can name.

## Session 115 — 2026-07-28: the story view was rendering itself to death

**Why.** Issue #193: "freeze after adding a block to a story in the story view".

- **It was an infinite render loop, and adding a block was incidental.**
  `MarkdownBlock`'s effect reported edit mode with `[editing, onEditingChange]` as its
  dependencies, `StoryEditor` passed a fresh inline closure on every render, and
  `setEditingIds` always returned a new `Set` — so React could never bail out via
  `Object.is`. render → new closure → effect → setState → render. Measured at ~700
  updates/second, not settling, for **one** markdown block and no interaction. Adding a
  block only mattered because it mounts the first `MarkdownBlock`: opening any story that
  already contained a text block froze the same way. React reports this as a console
  error ("Maximum update depth exceeded") rather than throwing, which is why it presented
  as a hang and not an error screen.
- **Fixed on both sides**, since either alone stops it and neither should depend on the
  other staying correct: `MarkdownBlock` keeps the callback in a ref and depends on
  `editing` only, and `setEditingIds` bails out when membership is unchanged.
- **The aggravators are gone too.** A view block embedded up to 200 rows into a 320px
  scroller and built every one of them on every render, per block — now windowed with
  `useVirtualizer` (fixed 22px rows, so cells truncate; the Explorer is where a long
  message is meant to be read). `ChartBlockCard` re-parsed its stored config into a
  `ChartCanvas` query key every render; memoized. The row count under the table still
  describes the whole embedded set, which is what the export snapshot renders
  independently — `storyViewBlockRows.test.tsx` pins both halves of that.
- **One story query, one set of options.** `StoryEditorPage` and `StoryEditor` each
  declared `["story", …]` with different options; React Query merged them, so the
  behaviour was right by accident and read as a bug in both files. Now
  `components/stories/useStory.ts` owns the key and the poll.
- **How it slipped:** there was no `StoryEditor` test at all. `markdownBlockVersion.test.tsx`
  renders the block in isolation with a *stable* callback, so it is structurally unable to
  see a loop driven by callback identity. `storyEditorLoop.test.tsx` renders the real
  editor and asserts on React's own loop signal.
- **Review round (PR #194).** The first pass tested the two guards only together — but
  either one alone stops the loop, so reverting one left the suite green. Each half is now
  pinned on its own (`editingIds.ts::nextEditingIds` returning the identical Set; a parent
  that re-renders with a fresh closure not making the block report again), and both were
  verified to fail with their half reverted. The loop test also counts renders through a
  `Profiler` rather than waiting for React to complain at ~50 nested updates. Worth
  recording: **no timeout can catch a full revert of both guards** — that loop is
  synchronous, starves the event loop, and vitest's own timer never fires, so the runner
  hangs until killed. That is the argument for keeping both guards, not just one.
- **Also from the review:** `MarkdownBlock` never reported `false` on unmount, so a block
  deleted mid-edit stayed in `editingIds` forever and left the "your draft is kept" notice
  up with nothing to justify it. The callback ref is now kept current in an effect rather
  than during render (a render that never commits must not leave it pointing into a
  discarded tree). `useInvalidateStory` shipped exported but unused, with both call sites
  still hand-rolling it — they go through it now. The row preview's `<table>` had become
  divs for virtualization, dropping table semantics: the ARIA roles are spelled out, and
  truncated cells carry their full text in `title`.

## Session 114 — 2026-07-27: the proposal card the analyst never saw

**Why.** An agent turn proposed three story blocks and the chat showed three bare
`propose_story_block` tool rows instead of three cards.

- **W7 wired the tool into one of four render paths.** `AgentPanel` decides what a
  tool call looks like in four independent per-tool allowlists — the persisted
  transcript (`itemsFromMessages`), the live `tool_call` fold, the live `tool_result`
  fold, and the proposals-query invalidation. `propose_story_block` was added to the
  first only. Live, the call row fell through to the generic tool row and the result
  row produced nothing; after the turn, the transcript refetch *did* emit a
  `storyProposal` item, but the proposals list was the one fetched before the
  proposal existed, so the card hit its `!proposal` fallback — the same bare row,
  until the panel remounted.
- **The allowlists are now one map.** Patching each path individually would have left
  the shape that caused the bug, so `components/agent/proposalTools.ts` holds
  `PROPOSAL_TOOLS` (tool → the `ChatItem` kind it renders as) and all four paths derive
  from it. There was a fifth allowlist nobody had counted: `ToolSelector`'s
  `WORKFLOW_TOOLS` warned only for `propose_finding`/`propose_annotation`, so disabling
  `propose_story_block` or `propose_chart` silently removed their cards. It now derives
  from `CARD_TOOLS`, which also supplies the card's name in the warning copy. Adding a
  proposal tool is a one-line edit in that module.
- **How it slipped:** every frontend test for the agent panel covers the persisted
  path. `src/test/agentPanelStoryProposal.test.tsx` drives a real streamed turn —
  parameterized over `PROPOSAL_TOOLS`, so a tool added to the map inherits coverage of
  all four paths — and asserts the card renders, the raw tool row does not, and the
  proposals query is refetched. Two details the first pass got wrong and this one
  needs: the proposals mock must return an *empty* list first (the real fetch predates
  the proposal, and returning it immediately let the invalidation be reverted with the
  tests still green), and the mocked stream must stay open past its events (the panel
  drops live items once the turn ends, so an instant turn asserts the reload path — the
  one that was never broken).
- **Review round (PR #192).** Two substantive findings. One: two `ChatItem` kinds now
  resolve against the *same* `agent-proposals` query, and neither card checked the
  proposal's own `kind` — a card handed the other shape reads its payload off fields
  that are null there. `proposalOfKind` degrades that to the same tool row a missing
  proposal gets. Two: nothing pinned the *other* direction, so widening `PROPOSAL_TOOLS`
  to `CARD_TOOLS` would have broken `propose_finding`'s card (it renders from call args
  and must not touch the proposals query) with the suite still green — now covered, as is
  `ToolSelector`'s warning, parameterized over `CARD_TOOLS`. The rest were shape: the
  `void _unused` compile-check became a `satisfies` clause on `PROPOSAL_TOOLS`, and the
  cast into `CARD_TOOLS` became `cardToolName`, so the map's safety is local to the
  lookup rather than an invariant spread across two expressions.
- **Unrelated:** `tests/test_airgap_bundle.py` was landed unformatted on `main` and had
  been failing `ruff format --check` in CI since session 113; reformatted here. Behind
  it sat a test that had never passed: the fake engine's `case "$1 $2"` matched
  `"create "` exactly, but the installer probes `create <image> <command>`, so every
  probe fell through to the catch-all and reported the deliberately broken image usable.
  `install.sh` was right all along.
- **CI reported one failure at a time.** Both the backend and frontend jobs abort on the
  first failing step, so the format slip above hid every test result behind it for a
  session — including that never-passing test. Each verification step now carries
  `if: !cancelled() && steps.deps.outcome == 'success'`: all of them run, the job still
  fails if any did, and a failed dependency install still short-circuits rather than
  cascading. `container-smoke` keeps aborting — its steps genuinely depend on the
  previous one (build → run → health).

## Session 113 — 2026-07-27: what the first real install found

**Why.** 1.8.4 shipped, and the bundle was carried to a fresh unprivileged LXC guest.
Two host problems and one installer defect, in that order.

- **The installer believed an engine that had failed.** `docker load` registers an
  image's metadata *before* unpacking its layers and exits 0 either way. On a host
  that could not mount overlay, four `Error unpacking image … err: permission denied`
  lines scrolled past, the exit status was 0, and `image inspect` — which reads
  metadata — passed for all four. So the check that exists specifically to prevent
  "start a stack that cannot run" waved it through, copied the payload over a running
  install and started it. This is the `podman save -m` lesson from session 112's review
  round, one layer down and missed: **an exit status is not a result.** `install.sh`
  now captures the load output and treats an unpack error as fatal, and `image_usable`
  creates and removes a throwaway container, because preparing a snapshot is what
  actually needs the layers. The install directory is untouched in both refusals.
- **`docker compose` from the bundle directory drove the real project.** The bundle
  shipped its compose file as `docker-compose.yml`, one of the four names compose
  auto-discovers, and the project name is pinned to `vestigo` inside it — so a command
  run one directory too high found a stack, with no `.env` next to it. It travels as
  `compose.airgap.yml` now; only `install.sh` writes the canonical name, into the
  install directory.

**The two host problems, now in `docs/DEPLOYMENT.md` §Troubleshooting**, because both
present as bundle failures and neither is one:

- **Docker's containerd image store is the default from Docker 28**, so a *fresh*
  install gets it while long-lived hosts still run the classic `overlay2` graphdriver.
  It mounts overlay with `userxattr`, which an unprivileged LXC guest refuses — which
  is exactly why "Docker has always worked in my LXC containers" and this failing are
  both true. `{"features":{"containerd-snapshotter":false}}` in `daemon.json` restores
  the graphdriver. Verified against a real Docker 29.6.2: no `daemon.json`, and
  `docker info` reports `Storage Driver: overlayfs`.
- **runc cannot mount `/proc` in a guest that is not allowed to nest.** Images unpack,
  containers get created, every one fails to start. Fixed on the LXC host
  (`nesting=1` / `security.nesting`), not inside the guest — `sudo` there is not root
  on the host, which is why escalating changes nothing.

## Session 112 — 2026-07-27: the airgap promise, made true for containers

**Why.** Patching a production host exposed that "airgapped" only ever covered the
*native* install. The container path pulled `node:22-alpine` and `python:3.13-slim`
unconditionally, so `docker compose up -d --build app` on the isolated host failed at
DNS — and the follow-up `docker compose up -d` silently restarted the *old* image,
which looks exactly like a successful deploy. Runtime egress was never the problem;
build-time and upgrade-time were, and nothing in `docs/` admitted it.

- **`FRONTEND_STAGE` makes the node stage unreachable.** The Dockerfile gains a
  `frontend-prebuilt` stage that is `FROM scratch` and copies `frontend/dist` out of
  the build context. BuildKit skips a stage nothing reachable copies from, so selecting
  it means `node:22-alpine` is never resolved — verified by pointing the node stage at
  a nonexistent tag and watching the prebuilt build succeed anyway. `.dockerignore` had
  to stop ignoring `frontend/dist` for this to work at all.
- **`scripts/airgap-bundle.sh` produces one tarball.** Frontend, app image built from
  that frontend, every backing-service image, the compose file, `.env.example`,
  `nginx-tls.conf`, checksums, installer. Backing-service tags are grepped out of the
  compose file rather than repeated, so bumping one is a single edit.
- **`deploy/airgap/install.sh` is the whole far side.** Verifies its own checksums,
  loads images, creates `.env` only when there is none, repoints the image tag, starts
  the stack, waits for `/api/health`, and says so plainly when the wait times out.
  Re-running it *is* the upgrade path. Rehearsed end to end here: bundle built, stack
  loaded and started from it, `/api/health` answered, second run a clean no-op.
- **Two things the rehearsal caught that review would not have.** A `docker` binary
  with an unreachable daemon beat a working `podman` in both scripts' engine detection
  (now an `info` probe, not a `command -v`); and the backing services published host
  ports they never needed — a port conflict on any host already running Postgres, and
  an attack surface for services holding default credentials. They publish nothing now;
  the app reaches them over the compose network.
- **`docs/DEPLOYMENT.md` now names both routes** (bundle for containers, carried
  checkout for native) and documents in-place patching honestly, including that
  `docker cp` does not survive a recreate.

**Review round (PR #191).** Four defects, all in the class where the operator finds
out at the isolated host:

- **`podman save` needs `-m`, and does not say so.** With more than one image and no
  `-m`, podman reads the extra arguments as additional *tags for the first image* and
  writes a single-image archive carrying all four names — exit 0, no warning. The far
  side then loads a `postgres:17-alpine` that is really qdrant. Fixed, and both sides
  now count the archive's `manifest.json` entries against a declared
  `VESTIGO_IMAGE_COUNT` rather than trusting an exit status.
- **`--app-only` could not work.** The bundle's compose file declares all four
  services, so on a host without the backing-service images compose would go to a
  registry — the original failure, wearing a DNS timeout as a disguise. `install.sh`
  now verifies every referenced image exists after `load` and refuses, naming the
  cause, before anything is copied or started.
- **An upgrade unpacks a new directory, and compose names the project after it.**
  Extracting `vestigo-airgap-1.9.0-abc123/` beside the old install would have created
  a *second*, empty stack with new volumes, looking perfectly healthy. The compose
  file pins `name: vestigo`, and `install.sh` now runs the stack from a stable
  **install directory** (`/opt/vestigo`, `--dir`/`VESTIGO_INSTALL_DIR` to override)
  that the bundle only feeds — which also keeps the operator's `.env` across upgrades
  instead of regenerating it from the example. Volumes belonging to another project
  name are detected and reported rather than silently ignored.
- **Ordering.** Images load and are checked *before* the install directory is
  touched, so a bundle that cannot produce a working stack leaves the running one
  exactly as it was.

Also: unknown installer arguments are fatal (`--dry-run` used to mean "install"),
`VESTIGO_HEALTH_TIMEOUT_SECONDS` raises the health wait for slow hosts with long
migrations, the tarball gets a `.sha256` companion, and `docs/DEPLOYMENT.md` §"Route
A" is now a runbook — build, carry, install, upgrade, back up, roll back, diagnose.
`tests/test_airgap_bundle.py` grows six cases that drive `install.sh` against a fake
engine and a stub archive.

**Second review round (PR #191): CI caught what the local builds could not.**

- **`COPY --from=${FRONTEND_STAGE}` never worked on Docker.** Buildah/podman expands
  the variable; Docker refuses it outright — *"variable expansion is not supported for
  --from, define a new stage with FROM using ARG from global scope as a workaround"* —
  so every Docker build of this branch failed at parse time while the podman builds
  used to develop it passed. Exactly the asymmetry the rest of this session was about,
  one layer down. Now an alias stage, `FROM ${FRONTEND_STAGE} AS frontend` plus a
  literal `COPY --from=frontend`; the unaliased stage stays unreachable, so the
  offline property is unchanged. Verified against real Docker on both paths: the
  default build succeeds, and `--build-arg FRONTEND_STAGE=frontend-prebuilt` completes
  with `node:22-alpine` removed from the local store and never pulled. A test pins the
  alias form, since the terser one is an easy edit to make again.
- **CodeQL flagged `image.startswith("docker.io/")`** (`py/incomplete-url-substring-
  sanitization`, high). A test assertion, so not exploitable — but the rule is right
  about the shape: a reference is a structured name, so the check now splits it and
  compares the registry component exactly. Same for the `"vestigo-app" in image` guard
  beside it.

## Session 111 — 2026-07-27: a stringified tool argument took the whole app down

**Why.** A production conversation crashed the SPA at the router level:
`Cannot use 'in' operator to search for 'chart_type' in {"chart_type": "bar", ...}` —
the `in` check in `isLegacySpec` ran against a *string*. Some providers hand a nested
object argument back as JSON text, and `tool_args` is persisted verbatim as the model
emitted it, so the bad row is permanent and every re-render of that conversation hit it.

- **Readers of `tool_args` normalize.** `parseToolArgObject` (in `api/agent.ts`) parses a
  stringified argument, passes an object through, and returns `null` for anything else.
  The tolerance lives in `specToChartConfig`/`specToEventFilters` — the translation
  boundary every consumer already goes through — so no caller has to remember it;
  `AgentPanel` uses it additionally as the render-or-don't decision, and an unparseable
  spec now renders no card instead of throwing through the chart card's `useMemo`.
  It reaches inside the spec too: an unparsed `compare` made `compare?.mode` undefined
  and silently drew one layer where the model proposed two, and `Object.keys` on an
  unparsed `filters` map built a filter set that was wrong rather than absent.
- **The tool accepts it too.** `ChartSpec`'s before-validator `json.loads`es a string
  spec, which is cheaper than a validation error the model has to guess its way out of.

**Then the blast radius, because that crash was one symptom of four problems.**

- **The app had no error boundary at all.** `grep -rn "errorElement\|ErrorBoundary"` over
  `frontend/src` returned nothing, so *any* render-time throw in *any* panel unmounted
  every route — the reason one malformed row cost the whole product rather than one card.
  `components/ui/ErrorBoundary.tsx` now contains failures at three levels: `AppShell`
  wraps its `Outlet` (keyed by pathname, so navigating away recovers), the router carries
  a `RouteErrorPage` as the last net, and the agent's chart/finding cards — the ones
  rendering model-authored JSON — wrap themselves individually.
- **`FilterSpec` is a nested argument on 14 tools.** The tolerance therefore belongs to
  the *position*, not to `ChartSpec`: `ObjectArgModel` is the base for every nested tool
  argument, so a provider that stringifies one stringifies none of them into a failure.
  It covers the model *and* every field whose annotation admits a JSON object — the
  `dict` fields inside `FilterSpec` are as reachable that way as `ChartSpec.options` is,
  and driving it off annotations means a field added later is covered by default. Never
  a field that also admits `str`: `q` may legitimately hold JSON as free text.
- **`"chart_spec" in (content or {})` in `propose_story_block`** looks like the same
  shape but is not reachable: `content` is a *top-level* argument, and both pydantic-ai
  and the MCP SDK's `pre_parse_json` parse those. The membership test is guarded by an
  `isinstance` anyway — Python's `in` on a string is a silent substring match that then
  fails on `.get`, so the failure it prevents is a wrong answer, not an exception — and
  a test pins the upstream parsing the guard's unreachability depends on.
- **A chart card could render another proposal's spec.** Unkeyed (pre-`tool_call_id`)
  rows were paired by FIFO order, and the call row is persisted *before* its validation
  runs — so an `ok` result could pop a *rejected* spec and draw a chart contradicting its
  own title. Pairing now falls back to order only when exactly one proposal is buffered;
  ambiguous batches render nothing. A missing card is recoverable, a wrong one read as
  evidence is not.

**Review round.** Card `ErrorBoundary`s were keyed by array index, so a fallback outlived
the card that caused it once streaming appended items — card items now carry the proposing
call's `tool_call_id` as their identity. `ErrorBoundary` resets through
`getDerivedStateFromProps` rather than `setState` in `componentDidUpdate`, which rendered
the stale fallback once before replacing it. `AppShell`'s route lost its `errorElement`:
`AppShell` wraps its own `Outlet`, so nothing reaches the router there that the
`RequireAuth` route does not already catch. Three negative assertions that raced a
`setTimeout(0)` against the conversation query now await a positive anchor row.

**Second review round (PR #190).**

- **The standing decision on GHSA-qwww-vcr4-c8h2 was argued from a false premise.** It
  said `react-router` 8.3.0 "is not published"; `npm view react-router version` returns
  8.3.0, and 8.0.0–8.3.0 all exist. The conclusion survives for a different reason: we
  depend on **`react-router-dom`**, which the v8 line retired at 7.18.1 in favour of
  `react-router`, so taking the patch means migrating 41 imports. Rewritten in
  `ROADMAP.md` with the real blocker and a trigger that has not already fired.
- **"`ObjectArgModel` is the base for every nested-argument model" was documentation, not
  an invariant.** A later spec inheriting `BaseModel` would have lost the tolerance with
  no test failing — the symptom is one provider retry-looping in production.
  `test_every_nested_argument_model_derives_from_object_arg_model` walks the built
  server's signatures, transitively through model fields, and enforces it. The same walk
  found that `SHARED_SPEC_NAMES` (which decides whose `$defs` prose is slimmed and
  re-rendered into the system prompt) is hand-kept and can drift the same way, so it is
  pinned against the walk too.
- **`_admits_json_object` answered "no" to questions it could not answer.** An unresolved
  forward reference matched neither branch and the field was silently dropped from
  coercion. It raises `TypeError` now.
- **`FilterSpec` is on 14 tools, not "~20".** Counted off the built server; corrected in
  `AGENT.md`, here, and a test docstring. Also renamed
  `test_a_stringified_filter_spec_is_parsed_on_every_tool_that_takes_one`, which tested
  one tool.
- **A card boundary was a dead end.** `resetKey` is the only exit, and a card's `resetKey`
  is its immutable `tool_call_id` — so its fallback lasted the life of the conversation.
  The default notice now carries a "Try again", handed to custom fallbacks as well.
- **The FIFO-pairing change is retroactive** — cards render from the stored transcript on
  every open, so an affected old conversation loses cards it used to show. Stated in the
  `CHANGELOG.md` entry, which described the new behaviour without saying it reaches
  backwards.

## Session 110 — 2026-07-27: PR #189 review fixes

**Why.** A review of the session-109 branch found that generalizing "hide what isn't
configured" had introduced a way for a *configured* subsystem to disappear, and that the
new configuration layer had three gaps at its edges.

- **A cold availability cache is not the same answer as "nothing installed".**
  `capabilities.enrichers` read the enricher availability cache, which was filled by
  `refresh_availability()` from inside `_startup_recovery` — a background task, behind
  three ClickHouse-touching steps in one `try`. Any of them raising (the documented
  reason those steps are backgrounded at all) left the cache cold for the process
  lifetime, and the whole Enrichment UI vanished from an installation whose GeoLite2
  database was right there. The sweep now runs in the lifespan, where a local filesystem
  check belongs, and `_enrichers_available` fills a cold cache itself rather than
  reporting false — so the capability no longer depends on anyone's call ordering.
- **The CLI reads the settings layer now.** `load_runtime_settings` was called only from
  the API lifespan, so `vestigo ingest` ran on the environment and the defaults while the
  console showed the operator something else. It takes an optional store (the CLI owns
  its own; going through `api.deps` would open a second engine) and a `_bootstrap` helper
  pairs it with `init_schema` at all three command entry points.
- **Clearing beats pinning.** `save_runtime_settings` refused *any* mention of an
  env-pinned field, including `null`. Pinning a field that already had an override
  therefore stranded the row: the merge ignores it forever, and the console renders no
  reset control for a read-only field. Writes are still refused; clears are not.
- **Empty is ambiguous, and the annotation resolves it.** The console sent `""` for every
  emptied string, so clearing an optional field (`oidc_issuer`, `embedding_api_base_url`)
  stored an empty string and left it reading as customized. The payload now carries
  `nullable`, derived off the pydantic annotation like the bounds already are, and empty
  means "unset" only where `None` is a legal value — an empty `sigma_rules_path` is still
  the value that disables the global ruleset.
- **`capabilities` needs a session.** `/api/health` is exempt from the auth gate because
  the login page needs `oidc_enabled`, which meant the capability map — an inventory of
  which optional subsystems an instance runs — was readable anonymously. The body is
  split rather than the route closed; the frontend invalidates `["health"]` on login so
  the map arrives immediately, and drops it on logout.
- **Two smaller ones.** The Similarity tab could render with no tab to leave it by (the
  initial state and the content switch weren't gated, only the tab button), fixed with a
  derived `activeTab`. And `_require_transfer_enabled`'s docstring claimed it refused
  every transfer route while the export *download* is deliberately exempt — an archive
  already produced is single-use and swept shortly after, so refusing it would strand a
  legitimate export rather than prevent a new one.

## Session 109 — 2026-07-27: every setting in the database, every subsystem gated

**Why.** Configuration was split in two with no principle behind the split: the AI agent
had a DB-backed, admin-editable layer with env precedence and a proper UI, and the other
~95 `Settings` fields were environment-only — invisible to the operator running the app,
changeable only with a restart. The same inconsistency showed up in how unconfigured
subsystems behaved: the agent hid itself completely, while embeddings left a disabled
button and a Similarity tab that could only fail.

- **`core/settings_registry.py` is the catalog.** One `SettingSpec` per `Settings` field
  carrying only what the model can't tell us — group, label, help, and the policy flags
  `env_only` / `secret` / `restart_required` / `subsystem`. Kind and bounds are read back
  off the pydantic field, so a tightened `ge=` reaches the UI without a second edit. A
  coverage test fails the moment a field is added without a spec, which is the mechanism
  that keeps the promise ("everything is editable in the UI") true after this session.
- **Two layers, resolved per field: environment wins, then `app_settings`, then the
  default.** `get_settings()` is now a cached merge rather than an `lru_cache`d
  constructor; env-pin detection reads `get_base_settings().model_fields_set`, so
  applying overrides can't pollute the very set that decides precedence. An override
  stored before an operator pinned the field can never resurface — checked on save *and*
  on load. `get_settings.cache_clear` is preserved as an alias so the ~30 test call sites
  kept working.
- **Bad stored values degrade, they don't crash.** Every override is validated against
  the whole `Settings` model before it is written (the admin gets a 422, nothing is
  persisted) and again on load, field by field if the batch fails — a row written by an
  older version costs a warning, not a boot.
- **`core/capabilities.py` + `capabilities` on `/api/health`.** One predicate per optional
  subsystem. The frontend's `useCapabilities()` gates on it: no Similarity tab and no
  embed wizard without embeddings, no enricher dialog when no asset is installed, no
  export/import when transfer is off. The agent's tool server now *removes* the two
  embedding tools instead of registering error stubs — an unconfigured subsystem should
  not cost schema tokens or invite a call that can only fail. `schema_chars_for_scope`'s
  cache key grew the availability flag accordingly, since settings can now change under a
  running process.
- **`transfer_enabled` is a real switch.** `transfer_max_concurrent=0` already meant "no
  cap", so gating on it would have inverted the meaning; case transfer got its own master
  switch instead, enforced in the router (503) as well as hidden in the UI.
- **`.env.example` stopped pinning things by accident.** Copying it used to set ~20
  variables to their own defaults, which under the new precedence would make them
  permanently read-only in the console. Fields that only restated a default are now
  commented out; connection strings and the admin seed stay.

## Session 108 — 2026-07-27: one transfer path, progress on every upload (PR #188 review)

**Why.** A review of PR #188 found eight issues, and a scope question the PR did not
answer: session 107 gave the case import/export byte progress and left three other
transfers blind. The biggest of them is the *primary ingest path* —
`sourcesApi.upload`, capped at 10 GiB server-side, whose drop zone advertises "any
size" and whose ingest job does not exist until the whole body has landed
(`api/routers/cases.py:795`). Shipping a release about transfer feedback while the
transfer analysts perform most often stayed a disabled button was the wrong shape.

- **`api/client.ts` now has one file-transfer core.** #188 added `xhrRequest` *beside*
  the untouched `postForm`/`fetchBlob`/`fetchBlobGet`, leaving a progress-less path a new
  call site could pick by accident. `postForm`, `fetchBlob` and `fetchBlobGet` are now
  that core, each taking an optional `{ onProgress, signal }`; `postFormWithProgress` and
  `getBlobWithProgress` are gone. Plain JSON verbs stay on `fetch` — no file body, nothing
  to report. Both cores still share `apiErrorFromBody`, so there is one error surface.
- **Fixed a latent hang in that core.** Its error branch read `xhr.responseText` even for
  a `responseType: "blob"` request, where the getter *throws* `InvalidStateError` — inside
  an event listener, where nothing observes it. The promise would never settle and the
  dialog would sit on "Downloading…" forever. The test double now enforces the real getter
  semantics, which is what makes this stay fixed.
- **`hooks/useFileTransfer.ts` owns the guard, the abort and the rate.** Wraps
  `useMutation` rather than replacing it. The synchronous submit ref-guard — the actual
  #184 fix — was hand-written twice in #188 and would have been hand-written twice more
  here; now it cannot be forgotten. `AbortError` is classified as a cancellation, never an
  error, and `ApiError(0)` (the XHR "never reached the server" sentinel) gets its own
  wording in one place. Replaces `useTransferRate`.
- **Progress and cancel on every file transfer**, via a shared `ui/ProgressMeter` (pulled
  out of `JobStatusRow`, so job rows and transfer rows cannot drift) and a new
  `ui/TransferProgressRow`: source upload, case import, case export download, event
  CSV/JSONL export, enricher asset. Cancelling is safe at every upload site for the reason
  #188 gave for import — `receive_upload_to_tmp` streams to a temp file and rows/jobs
  follow only after it all lands.
- **Indeterminate progress is now a first-class state.** A chunked `StreamingResponse` has
  no `Content-Length`, so the event export can only ever report bytes-so-far; the
  exporter's `manifest` phase counts no items at all. Both previously rendered *no bar*,
  which reads as a stall on exactly the slowest steps. `_progress(phase, total=None)`
  replaces `total=0` for those phases, and `Progress` renders Radix's indeterminate state.
- **The event export dialog stopped misleading.** It claimed "Streams directly from the
  backend — no memory limit", true of the server and false of the browser, which buffers
  the whole Blob. It now says so and points at the case archive for very large sets.
- **`hasActiveFilters` replaces three separate answers to one question.**
  `InheritedFiltersBar` decided its empty state by string-comparing against caption prose
  (`describeFilters(...) !== "no filters"`); the filter rail and the Explorer toolbar each
  hand-rolled `Object.values(filters).some(...)`, which counted `sort`/`limit`/match-mode
  maps and so offered "Clear all filters" on unfiltered views. One predicate now, defined
  as "FilterChips would render at least one chip".
- **Smaller review items.** The exporter's blob loop was O(n²) (`next(s for s in sources
  …)` per hash) — now a hash-keyed map, with a note that `ix_sources_case_id_file_hash`
  makes the dedup defensive within a single case. `matchesAccept` treated every
  non-extension `accept` entry as a match, so a MIME-typed drop zone filtered nothing.
  The hidden file inputs left the tab order (`tabIndex={-1}`): a focusable input nested in
  a `role="button"` is two tab stops for one control. The export dialog now passes the
  abort signal it was already threading through but never using. `transferApi.getJob` was
  a duplicate of `jobsApi.get`; both transfer dialogs now poll under the tray's `["job",
  id]` key so TanStack collapses the import dialog and the tray into one request stream —
  deliberately still their own `useQuery`, not a read of the tray's store, so a dialog
  never depends on another component being mounted to see the job it started.
- **Deviation from the plan, recorded.** The export job is *not* handed to the job tray,
  though the import job is. The archive only becomes useful when the dialog turns it into
  a browser download and the server unlinks it once streamed, so a tray row would announce
  a finished export the analyst has no way to collect.
- **Tests.** 605 frontend tests (was 559) across 68 files: new suites for
  `useFileTransfer`, the source upload dialog, the event export dialog and the enricher
  asset upload, plus `hasActiveFilters`, MIME `accept` matching, tab order, and the
  reimplemented client helpers. Backend 1865 pass; the three `test_embeddings_capability`
  /`test_uploads` failures are the pre-existing missing-extra ones that reproduce on
  `main`.

## Session 107 — 2026-07-26: transfer progress, file-input primitive, Visualize scope (B2)

**Why.** Roadmap B2, the last of the reported defects with real user impact. #184: an
analyst could start the same multi-GB case import twice. `ImportCaseDialog.start()` set
`jobId` only inside the upload promise's `.then()`, and `running` was derived from `jobId`
— so for the entire upload nothing disabled the Import button. #183: neither transfer
dialog rendered anything from the job it was already polling, and the download had no
feedback at all, so a multi-GB export was indistinguishable from a hang. Alongside it, the
Visualize page inherits the Explorer's filters through the URL but barely said so — an
unfiltered chart and a chart of one narrow slice looked identical, which for a figure that
gets exported into a report is a forensic problem, not a cosmetic one.

- **The submit guard is a ref, not state.** A second click can land in the same task as
  the first, before React re-renders the button as disabled, so `submittingRef` is what
  actually closes the window; the `uploading` state only drives the label and the
  `disabled` attribute. `ExportCaseDialog` got the same guard — the endpoint is capped by
  `transfer_max_concurrent`, so a double-start is a 429 rather than a duplicate, but the
  bug class is identical.
- **`XMLHttpRequest` for the two archive transfers.** `fetch` has no upload-progress event
  and no sizeable request-body stream, so `client.ts` gained `postFormWithProgress` and
  `getBlobWithProgress`. They are not duplicates of the fetch helpers: the 401 →
  `onUnauthorized` dispatch and FastAPI's `detail` parsing (string *and* Pydantic array)
  moved into an exported `apiErrorFromBody`, which both paths share. It returns rather than
  throws, because an XHR event listener has no throw position a promise would observe. The
  fetch helpers keep their own code path deliberately — they are the common case and
  already covered by tests.
- **Cancel aborts the upload.** Safe by construction, and worth saying why: the router
  creates the job only *after* `receive_upload_to_tmp` returns, so an aborted upload leaves
  no job, no case, and nothing to clean up. Once a job id exists the abort is no longer
  offered, and the job is registered with the tray so closing the dialog stops hiding a
  running restore.
- **Backend progress got a denominator.** `_progress(phase, total=…)` now resets
  `processed`/`total` in the same write as the phase name. The reset is the point:
  `JobStore.update` *merges* progress dicts, so a phase publishing only its name inherits
  the previous phase's total and renders a percentage against the wrong denominator.
  Skipped members (blob missing on disk, unreferenced archive member) still advance the
  counter, or the bar stalls short of 100% on any archive with a gap. Tests replay the
  merge via a shared `ProgressRecorder` rather than inspecting individual writes.
- **Phase copy is keyed on `job.kind`.** `postgres`, `events` and `blobs` are shared tokens
  that mean opposite directions in the two jobs — packing vs. restoring — so one map would
  actively mislead. `lib/jobPhases.ts` returns null for an unknown kind or token rather
  than leaking a raw phase string.
- **One `ui/FileInput` for four sites.** They had drifted: only three cleared the input's
  value after a pick, and the one that didn't was the import dialog, where re-picking the
  same file after a failure fired no `change` event and looked like a dead button. Three
  exports (bare input, drop zone, picker button) cover all four call sites. The drop zone
  now filters dropped files against `accept` — the browser only enforces it in the picker,
  so drag-drop silently accepted what clicking could not — and stops the programmatic
  `.click()` from bubbling back into its own handler and reopening the picker.
- **Visualize states its scope above the chart.** `viz/InheritedFiltersBar` reuses the
  Explorer's `FilterChips` (per-chip removal included) behind an "Inherited from Explorer"
  label, with an explicit "No filters — charting the whole timeline" empty state, and folds
  the old standalone time-range row in as chips plus a "Reset range" escape hatch.
  `collapseRoutine` is deliberately *not* a chip: it is never URL-serialized, so rendering
  it alongside shareable filters would misrepresent it. Removals route through the existing
  `updateFilters`, so the `c_*` chart config survives them.
- **The chip-removal reducer is now shared.** It moved out of a ~65-line inline callback in
  `ExplorerPage` into `lib/fieldFilters.removeFilterEntry`, which also fixed
  `CompareFilterEditor`: its hand-rolled remover knew only `q` and `filters`, so the
  exclusion, tag and time chips it renders were inert.

Three pre-existing failures (`test_embeddings_capability.py` ×2,
`test_uploads.py::test_embed_refuses_ingesting_sources`) reproduce identically on `main`
and are unrelated to this work.

## Session 106 — 2026-07-26: PR #187 review remediation

**Why.** Review of the turn-checkpointing branch. The mechanism was sound, but
`agent/resume.py` had been written against a wrong model of pydantic-ai 2.17.0 — as if
the library did no history repair of its own, when it runs a three-pass
`_clean_message_history` pipeline (drop orphaned results → answer dangling calls → merge
adjacent same-role messages) before every single request. Three of the findings follow
from that one mistake. Verified as *correct* and left alone: `agent.iter(instructions=)`
is additive rather than a replacement (`SYSTEM_PROMPT` survives the resume note), a
historical `ModelRequest.instructions` is never resent, and the `new_messages()` boundary
survives the library cleaning the incoming history in place.

- **Stopped dropping truncated tool calls.** Pass 1 pruned trailing `ToolCallPart`s that
  never reached the executor. pydantic-ai deliberately does the opposite, and its reason
  applies here with force: removing a part rewrites the shape of a `ModelResponse` whose
  thinking signature was computed over the turn that included the call, and this blob is
  the only place those signatures live. Truncated arguments are already sendable, so the
  call is now kept and answered like any other. The `called_ids` bookkeeping it needed —
  a parameter, an accumulator and a branch in `stream_turn` — is gone with it.
- **Synthesized answers sit next to their own call.** Pass 2 batched every answer onto
  the end of the snapshot, which on a history with two unanswered responses separates the
  first one's answers from its calls by an intervening response — an adjacency the
  Anthropic protocol requires and no later normalization restores. Answers now go into
  the request immediately following the response that made the call, behind that
  request's existing tool results, mirroring the library's own placement.
- **A rejection is no longer replayed as a success.** `FunctionToolResultEvent.part` is a
  `RetryPromptPart` when a call was rejected, and the recorder was storing its `.content`
  and rebuilding it as a `ToolReturnPart`. It now stores the streamed *part*, which the
  repair reuses verbatim — so `outcome`, `metadata` and the content's type all match what
  the run saw. A genuinely unanswered call gets `INTERRUPTED_RESULT` stamped
  `outcome="interrupted"`, and a synthesized request is stamped `state="interrupted"`;
  both are public fields, and both make the interruption machine-readable in an export
  instead of legible only as prose.
- **`RESUME_MARKER` kept, its justification corrected.** The claim that ending on a
  `ModelRequest` puts two `role: "user"` messages back to back is false on 2.17.0:
  `_merge_consecutive_messages` folds that request into the next turn's prompt request.
  The marker stays as defence in depth — that merge is private API and the pin is `>=` —
  and a test now asserts the library still merges, so a bump that changes it fails in CI
  rather than as a 400 against a live endpoint. The "same as `agent/window.py`'s turn
  drop" analogy was dropped: that marker needs its response because it *is* a
  `UserPromptPart`, and it is only ever sent, never persisted.
- **No checkpoint before the model commits a response.** A text part's first delta can
  arrive before its `ModelResponse` lands in `new_messages()`, and repairing a snapshot
  of just the analyst's prompt closed the pair with `RESUME_MARKER` — fabricating a reply
  to the analyst. `stream_turn` now takes no checkpoint in that state.
- **Checkpoint writes are rate-limited.** Each is a full `dump_history` plus a
  whole-column JSON UPDATE of a monotonically growing blob, on the event loop of a
  single-process deployment: a 125-tool-call turn wrote a growing blob 125 times, so the
  bytes were quadratic in the turn's length. `_CHECKPOINT_MIN_INTERVAL` (3s) collapses a
  burst into one write, while `force=True` on every terminal exit — the stop path and all
  three `except` branches — keeps an actual interruption unthrottled. Worst-case loss is
  a few seconds of tool work, never an analyst's turn.
- **`history_partial_at` is visible.** It was on the row but absent from
  `AgentConversation.to_dict()`, so no API response and no export carried it. Added there
  and to the frontend's `AgentConversation` interface: a reader can now tell a replayable
  turn boundary from a mid-turn checkpoint without inspecting `raw_history`.
- **Migration renamed** `8030282d015f_…` → `0019_agent_history_partial_at.py`
  (`revision = "0019"`), matching the sequential convention every other revision follows,
  with the autogenerated comment scaffolding removed. A dev database that already applied
  the old id needs `UPDATE alembic_version SET version_num = '0019';` once.

Documented rather than coded around: if the overflow re-run dies before its own first
checkpoint, attempt 0's stamped snapshot stays on the record — it is a faithful account
of work that really ran. And a partial write bumps `updated_at`, floating an actively
streaming conversation to the top of the conversation list, which is the intent.

`uv run pytest` → 1863 passed, 3 failed (the same environmental embeddings failures as at
merge-base). Frontend typecheck, lint and 510 tests clean.

## Session 105 — 2026-07-26: an interrupted agent turn keeps its history

**Why.** `AgentConversation.history` — the only thing a follow-up turn replays — was
written exactly once per turn, in the `result` branch. Every other exit (analyst
presses Stop, provider 5xx, `UsageLimitExceeded`, the process dying) dropped the whole
turn's messages, and the *next* completed turn then overwrote the blob from the
pre-interruption base, so the work was lost permanently while the UI kept showing the
tool rows. The 2026-07-26 export has 125 persisted tool rows against an empty history
blob, and the follow-up turn re-ran the entire orientation sweep.

- **`agent/resume.py` (new).** `repair_partial` turns a mid-turn snapshot into
  something replayable: it answers tool calls left unpaired — with the result the turn
  actually streamed, or an explicit `interrupted` marker, never anything invented. Pure
  and idempotent, which is the same determinism constraint the sliding window holds to.
  `RESUME_NOTE` lives here too. (Session 106 reworked the details; see there.)
- **`stream_turn` now drives `agent.iter`** instead of `agent.run_stream`, so the run's
  live message list is reachable mid-turn. It fills a caller-owned `TurnRecorder` after
  every tool result and every completed node and yields a router-internal
  `{"type": "checkpoint"}` alongside. Per tool result, not just per node: a batch of
  four ClickHouse queries is seconds of work a `kill -9` must not erase.
- **`agent_conversations.history_partial_at`** (Alembic revision) records that the
  stored blob is a mid-turn checkpoint rather than a turn boundary. It needs the
  store's `UNSET` sentinel, because `None` *is* its clearing value.
- **The router persists on every non-result exit** — each checkpoint, the cancel branch,
  and all three `except` branches — and stamps `history_partial_at`; only a completed
  turn clears it. A stop is treated as an interruption like any other: the analyst's
  next message must be answered against this turn's work. While the stamp is set, the
  next turn carries `RESUME_NOTE` so the model builds on the findings instead of
  re-orienting. The recorder is reset per attempt, so the reactive overflow re-run
  replays from the same pre-turn base rather than concatenating attempt 0's messages.
- **`checkpoint` never reaches the client.** The router's stream loop ends in an
  unconditional `yield _sse(event)`, so the guard `continue`s; a test asserts no
  checkpoint event appears in the SSE stream of a stopped turn.

No new truncation logic: a resumed history is ordinary `message_history`, still sized by
`agent/window.py`, and the learned budget and calibrated `chars_per_token` persist per
conversation — so a resumed turn starts with the budget the interrupted turn paid to
learn. `docs/AGENT.md` §Turn checkpointing and resume documents the mechanism.

## Session 104 — 2026-07-26: PR #186 review remediation

**Why.** Review of the B1/B3/B4 batch. No correctness defects, but the external-data
path had one unbounded cost and several sharp edges worth filing down before the
mechanism becomes load-bearing across the query layer.

- **Export re-uploaded the whole membership payload once per batch.** `iter_events`
  rebuilds the WHERE clause per batch (the keyset cursor lives inside it), and each
  rebuild built a fresh `ExternalData`. A 50k-id JSONL export shipped ~2 MB fifty
  times. Introduced `_ExternalTables`, a content-addressed per-read registry, and
  threaded it through `_build_where(…, external_tables=…)` so the export serializes
  and uploads once. It also collapses identical lists reachable from two predicates
  (`ids=` + a tag filter over the same set) onto one table, and names tables from its
  own counter rather than the parameter counter — parameter numbering shifts between
  rebuilds, which would otherwise rename the table mid-export.
- **Payloads are deduped.** An `IN` test cannot care about a repeated value; the upload
  does, and annotation lookups routinely resolve to the same event id once per tag.
- **TSV escaping completed** to match ClickHouse's table exactly (`\0`, `\b`, `\f`
  alongside `\\`, `\t`, `\n`, `\r`), so every emitted sequence round-trips rather than
  relying on the reader to pass a raw control byte through.
- **Per-request upload cost documented, not hidden.** External data is per-request —
  ClickHouse's HTTP interface has nowhere to keep a temp table between requests, and
  the stateless pooled client is deliberate. One Explorer page under a large filter
  therefore uploads the table once per statement it issues (count + key scan +
  hydrate). Bounded and accepted; recorded at `EXTERNAL_LIST_THRESHOLD`.
- **Why the export 413 works is now pinned by a test.** Once `StreamingResponse`
  flushes headers no exception handler can run, so the route's pre-flight `count()` —
  which runs the identical WHERE — is what keeps an over-large export filter a clean
  413 instead of a truncated 200. `test_export_surfaces_too_large_filter_before_streaming`
  fails if that count is ever moved below the response construction.
- **Ingestion fast path.** `_raw_bytes_and_text` runs per line of every ingested file
  and had doubled its Unicode work (encode + validating decode, previously one encode).
  An `str.isascii()` guard — CPython's cached ASCII flag, O(1) — skips the round-trip
  for the overwhelming majority of log lines.
- **`Job._payload_lock` is `init=False`**, so a caller can't supply or share one, and
  the dead `# noqa: SLF001` (SLF isn't in the ruff select list) is gone.
- **B3's id change is in the operator docs**, not only here: `DEPLOYMENT.md`
  §Stability & upgrades now states that already-ingested data is unaffected and that
  only a *re-ingest* of an invalid-UTF-8 file produces different ids.

New coverage: external-table reuse across export batches, dedup, control-char
escaping, empty-string values surviving as rows, identical lists sharing one table,
multi-byte-but-valid UTF-8 offsets (the ASCII fast path's boundary), and the export
413 ordering.

## Session 103 — 2026-07-26: backend defect batch (B1, B3, B4)

**Why.** The three backend items from the triaged defect backlog, worked as one
change: they share no files but do share a release. B1 was the only hard 500 in
the tree.

- **B1 — large id filters no longer 500 ([#181]).** Any filter resolving to a big
  Postgres-side event_id list (`annotated=`, `ids=`, tag include/exclude) bound the
  whole list as one `Array(String)` parameter. clickhouse-connect form-encodes bind
  params past 4 KiB, and ClickHouse's Poco form parser caps a single field value at
  128 KiB, so the query died at ~3,300 ids with `code: 1000 … Field value too long` —
  a case became progressively un-filterable as tagging grew. Membership lists past
  `EXTERNAL_LIST_THRESHOLD` (512) now ship as **external data** (a multipart file part,
  1 GiB ceiling) and filter with `IN (SELECT * FROM …)`, which also builds a hash set
  instead of scanning a constant array per row. Applied to every large-list binder —
  `add_in_list`, `add_not_in_list`, the unified tag predicate's id half, exact
  field filters/exclusions, and the template-hash `NOT IN` — not just the reported one.
  External tables travel *with* their parameters (`QueryParameters` carries them,
  `_with_params` copies them, `_select` forwards them), because a WHERE clause that
  names a table is unexecutable without it. Whatever still overflows now raises
  `QueryRequestTooLargeError` and the app answers **413** with an actionable message
  instead of a raw ClickHouse 500.
- **B3 — byte offsets survive non-UTF-8 input ([#156], [#161]).** Offsets were measured
  as `len(line.encode("utf-8"))` over text decoded with `errors="replace"`; U+FFFD
  re-encodes to three bytes, so every `byte_offset` after the first bad byte was wrong
  and the event-to-source-byte invariant silently broke on real-world logs. Files are
  now decoded with `errors="surrogateescape"` and measured by re-encoding (exact
  original bytes), with the text handed on re-decoded via `replace` so payloads keep
  the same U+FFFD substitution and never carry lone surrogates into JSON/ClickHouse.
  Note: `content_hash` still covers the decoded text, so event ids for files with
  invalid UTF-8 change — they were derived from a wrong offset before. Also replaced
  the bare `assert` guarding the Parquet event-id identity invariant with a descriptive
  `ValueError` (`python -O` strips asserts, which would turn a broken identity into
  silent corruption) and added `S101` to the ruff select list, ignored under `tests/`.
- **B4 — async/concurrency one-liners ([#155], [#157]).** The three synchronous Qdrant
  deletes in the case/source cascade now run in `asyncio.to_thread` like the ClickHouse
  deletes beside them. `Job.to_dict()` snapshots `progress`/`result` under a new
  per-job payload lock (taken inside the store lock), so a worker thread updating
  progress can't change — or tear — a response mid-encode.

Verified: `uv run pytest` 1822 passed (3 pre-existing failures from the `embeddings`
extra not being installed in this environment, unchanged from `main`), `ruff check`
clean. B1 verified end-to-end against live ClickHouse per `/verify` on a 20 000-event
case with all events tagged: pre-fix `GET …/events?annotated=tag` → HTTP 500 with the
exact `code: 1000 / HTML Form Exception: Field value too long` from the issue; post-fix
→ 200 with `total: 20000`, and `histogram`/`events/count` likewise 200.

[#181]: https://github.com/overcuriousity/Vestigo/issues/181
[#156]: https://github.com/overcuriousity/Vestigo/issues/156
[#161]: https://github.com/overcuriousity/Vestigo/issues/161
[#155]: https://github.com/overcuriousity/Vestigo/issues/155
[#157]: https://github.com/overcuriousity/Vestigo/issues/157

## Session 102 — 2026-07-26: W7 second-pass review remediation

**Why.** A second review pass over the finished W7 branch before integration. No
criticals left, but five issues where a failure was reported in the wrong shape —
a 500 instead of a refusal, or a late symptom instead of an early error.

- **The agent's write path skipped the referent-scope gate.** The HTTP router checked
  that a block's `view_id`/`chart_id`/`timeline_id` belong to the case; the agent's
  propose and confirm paths ran shape validation only. A wrong id therefore survived
  all the way to export, as a frozen `resolution.error`, instead of being an error the
  model could correct. The check moved out of the router into
  `vestigo.stories.refs.validate_block_scope` and now runs on all three paths — and at
  confirm as well as propose, because a referent can be deleted in between. It also
  covers an `event_ref`'s `source_id`, which nothing had been checking.
- **A decided proposal could 500.** The legacy chart-config conversion in
  `_apply_story_block_proposal` sat outside the handler's `try`, so a stored spec that
  no longer converts (chart-local base filters have no `ChartConfig` representation)
  raised out of a *decided* proposal instead of reporting `applied: false` with a
  reason. Same for the `block is None` race when the story is deleted between the
  lookup and the insert. Everything that can fail on a stored payload is now inside
  one `try`, and the proposal always reports honestly.
- **The position retry was too broad.** `create_story_block`/`move_story_block` retried
  *any* `IntegrityError` 25 times, so a duplicate block id or a NOT NULL violation cost
  25 round-trips and then surfaced as a misleading "could not place a block". Narrowed
  to the `(story_id, position)` uniqueness violation the loop exists for; anything else
  propagates immediately.
- **A failed artifact upload was terminal.** Sealing is once-only and correctly so, but
  an export whose upload failed had no way back — the analyst's only route was a whole
  new export under a different hash. The Exports tab now offers **Render HTML** on an
  unsealed export, re-rendering from the *stored* snapshot (never a fresh resolution),
  so the artifact still attests to the same frozen record. The pre-upload size warning
  also measures UTF-8 bytes rather than `String.length`, which under-counted non-ASCII
  prose and let the warning arrive after the 413 it exists to pre-empt.

Verified: `uv run pytest` 1796 passed, `uv run ruff check .` clean, frontend
`typecheck`/`lint` clean, `npm run test` 510 passed.

## Session 101 — 2026-07-26: W7 Stories review remediation

**Why.** A three-way code review of the W7 branch (`009e50c..3bf2bb0`, ~8.5k lines)
found three silent-failure bugs in the features the design round was actually built
around, plus a set of smaller issues. Everything found was fixed; the two structural
root causes are worth recording because they explain most of the individual findings.

- **No compare-and-swap anywhere.** `update_story_block`, `move_story_block`,
  `seal_story_export_artifact` and the gap-position computation were all read-then-write.
  Under `READ COMMITTED` two collaborators could both read `version=1`, both pass a
  Python-side check, and both write `version=2` — the exact lost update the `version`
  column exists to prevent, and the invariant the router docstring and `STORIES.md`
  both promised. Same shape let two uploads double-seal an immutable export and two
  inserts tie for a position. All four are now conditional `UPDATE … WHERE <guard>` +
  `rowcount`, position mutations run under a `FOR UPDATE` lock on the parent story row,
  and `(story_id, position)` is unique (migration `0018`) with a bounded retry behind it
  — a lock orders transactions but doesn't make a pre-lock read current, and SQLite
  ignores `FOR UPDATE`, so the index is the real invariant. A sequential test cannot
  distinguish the old code from the new one, which is why none of this was caught;
  `tests/test_stories_store.py` now has genuinely concurrent `asyncio.gather` cases.
- **Untyped payload boundaries.** Two of the three criticals trace to the same habit.
  `SnapshotBlock.ref`/`data` were `Record<string, unknown>`, so eight renderers
  re-asserted the shape locally — and an `as unknown as ChartResult` cast hid that an
  uncompared time chart freezes a raw histogram (`{start, count}`) while the mark reads
  `{primary, comparison}`. Every time-histogram block therefore rendered blank bars in
  every export, silently. Symmetrically, the agent wrote its snake_case `ChartSpec` dump
  into `SavedChart.config`, which is contractually the frontend's camelCase `v: 1`
  `ChartConfig`, so an agent-authored chart block was undrawable in the export, the story
  card *and* the Visualize rail — with a test asserting the wrong shape, protecting the
  bug. Both are now discriminated unions with one typed mapper each
  (`snapshotToChartResult`, `spec_to_stored_chart_config`) and a round-trip assertion, so
  a future divergence is a build failure rather than a blank chart in a signed report.
- **The editor defeated its own concurrency check.** `MarkdownBlock` read `block.version`
  from a live prop refreshed by the 10s story poll, so a collaborator saving mid-edit
  became the base version: the server's check passed and their edit was destroyed with no
  409 and no conflict UI. A paragraph takes longer to write than the poll interval, so the
  conflict path was close to unreachable. The version is now captured at edit start.
- **Attestation gaps.** `delete_case` didn't delete stories, blocks or exports — leaving
  orphaned snapshots holding frozen event data from a case the operator believes is gone.
  A contributor could erase every sealed export by deleting the story, bypassing the
  admin-only export deletion; that path is now admin-only when exports exist, and the
  hashes go into the audit record. Exported charts ran under the *agent's* context-budget
  caps, so a report showed less than the analyst signed off on (top-50 frozen as top-30)
  and carried agent-facing clamp prose; `execute_chart_spec` now takes a `ChartLimits`
  and exports use `ANALYST_CHART_LIMITS`. `GET .../snapshot` serves the canonical hashed
  bytes so a third party can verify the hash directly, and a sealed artifact must embed
  the `snapshot_hash` it claims to render.
- **Bounds and honesty.** Block content, snapshot bytes, block count per export and the
  artifact stream are all capped via `VESTIGO_STORY_*` settings (the artifact cap now
  applies to the arriving stream, not to the already-buffered body). `_json_safe` coerces
  non-finite floats — `NaN`/`Infinity` are not JSON, and hashing them would leave an
  unverifiable attestation — and sorts sets. Embed cards distinguish "deleted" from
  "lookup failed", an event block resolves through a timeline that actually contains its
  source (the editor and the server-side resolver previously disagreed), pushes reuse a
  matching saved View instead of minting a duplicate per push, and the exported HTML marks
  agent-authored blocks. The RBAC test the plan specified now actually exercises the
  read-vs-contribute boundary rather than a non-member who is 403 on everything.

Verified: `uv run pytest` 1791 passed, `uv run ruff check .` clean, frontend
`typecheck`/`lint` clean, `npm run test` 503 passed.

