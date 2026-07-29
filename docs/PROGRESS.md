# Vestigo Implementation Progress

Last updated: 2026-07-29 (session 125 — AMiner positioning correction).

Append-only session log, newest entry on top. Older sessions are archived:
[1–70](./archive/PROGRESS_SESSIONS_01-70.md), [71–100](./archive/PROGRESS_SESSIONS_71-100.md).

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

