# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.8.5] — 2026-07-28

### Fixed

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
