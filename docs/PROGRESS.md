# Vestigo Implementation Progress

Last updated: 2026-07-29 (session 119 — story exports draw their charts again).

Append-only session log, newest entry on top. Sessions 1–70 are archived in
[`docs/archive/PROGRESS_SESSIONS_01-70.md`](./archive/PROGRESS_SESSIONS_01-70.md).

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

## Session 100 — 2026-07-26: W7 Stories (Phase 3 Step 3)

**Why.** The last open item of Phase 3, and the feature Timesketch users reach for
most: a per-case document where the narrative and the evidence live together, so the
report assembles itself during the investigation instead of being written afterwards.
Design round: `docs/superpowers/specs/2026-07-26-w7-stories-design.md`; reference doc:
`docs/STORIES.md`.

- **Live editor, frozen exports.** The central tension the design round settled: embeds
  stay live while an analyst writes (the document tracks ingestion and detection as they
  progress), and `POST .../exports` freezes a server-resolved, SHA-256-hashed, immutable
  snapshot. Freeze-at-embed would have killed the live-report feel; freeze-only-at-export
  with no stored bundle would have left "what did this chart show?" unanswerable once the
  file was lost. The stored snapshot is the attested record, like a Source's `file_hash`.
- **Server resolves, browser renders.** Export is two-phase and deliberately asymmetric.
  The server executes every block itself — view queries through the same `_build_query`
  path the Explorer uses, charts through `execute_chart_spec` — and stores the hashed
  bundle. The client then renders that bundle to standalone HTML and uploads it once. The
  JSON is authoritative; the HTML is presentation, and an export is complete without it.
  This also kept a server-side chart-rendering stack (and a headless browser in an
  airgapped install) out of the deployment.
- **Nothing vanishes silently.** Per-block resolution is individually wrapped: a view
  deleted before the export freezes as `resolution.error`, visible in the artifact, and
  one bad block never fails an export. Truncation is always stated — a report showing 200
  of 14203 rows says which it is, in the editor and in the export.
- **Collaboration without new infrastructure.** Block-level optimistic concurrency
  (`version` per block, 409 with the winning row) plus 10s polling. Block granularity does
  the work: two analysts on different blocks never collide, and a conflict keeps the local
  draft with a load-theirs/overwrite choice. No CRDT, no WebSockets — the same call the
  streaming milestone already made for the live Explorer.
- **Integer gap ordering.** Positions stride by 1024; an insert between two blocks takes
  the midpoint, and an exhausted gap renumbers the story inside the same transaction.
  Boring on purpose: float positions accumulate precision failures exactly where a
  document is edited most.
- **Agent parity, phase-spec deferral rescinded.** The Phase 3 spec had deferred
  agent-authored stories; the user rescinded that during the design round, on the standing
  principle that the agent can do what an analyst can do. Stories shipped with
  `list_stories`/`read_story` (also on external `/mcp`) and `propose_story_block`
  (in-app only, like `propose_annotation`). `AgentProposal` gained `kind`/`payload` so
  both proposal shapes share one decide path and its 409 idempotency backbone. A
  `chart_ref` may be proposed with an inline spec — confirming saves the chart and embeds
  it in one step. Block edit/move/delete and export stay analyst-only: parity covers
  analytical contribution, not document arrangement or the attestation act.
- **Two extractions, no duplication.** `execute_chart_spec` came out of `propose_chart`
  so the export resolver runs the identical validated path, and `ChartCanvas`/`ChartMarks`
  came out of `ChartProposalCard` so a chart is drawn by one component in the Visualize
  page, an agent card, a story block *and* an exported snapshot. The snapshot renderer
  performs no network access at all — asserted by a test that fails if a render fetches.

## Session 99 — 2026-07-25: X1 third review round (untrusted-input bounds, event loop)

**Why.** A third review pass over PR #182. The archive layer's size discipline is
sound in aggregate but had a gap per member, and the two transfer paths were doing
all their heavy work on the event loop. Eight findings, all fixed here.

- **One member could still OOM the process.** `transfer_max_expanded_bytes` caps an
  archive's *total* expansion, which says nothing about any single member: a lone
  100 GiB `postgres/annotations.ndjson` sits far under a 200 GiB total, and
  `_read_bounded` pulled it into memory whole. NDJSON deflates ~20x, so it fits
  inside the 10 GiB upload limit — an out-of-memory kill any authenticated user
  could trigger. Fixed from both sides: `VESTIGO_TRANSFER_MAX_METADATA_BYTES`
  (default 2 GiB) rejects an oversized `postgres/*` member in the constructor,
  before it is ever opened, and `ArchiveReader.iter_ndjson` streams every stem row
  by row so the importer's peak memory scales with the largest *row*. The prescan
  and the revive loop both use it; `read_ndjson` survives as `list(iter_ndjson(…))`
  for the two genuinely small members.
- **Both transfer directions blocked the event loop.** `import_case` is `async` but
  called `verify_members` (SHA-256 over the whole archive), the id prescan and
  `extract_to` synchronously; export hashed and zipped every Arrow member and blob
  the same way. They run as `BackgroundTasks`, so the entire API — health, SSE,
  other users' queries — froze for the length of a multi-GiB transfer. All of it
  now goes through `asyncio.to_thread`, matching what the ClickHouse calls in the
  same loops already did.
- **The event Arrow member was never schema-checked.** `_insert_source_events` took
  the attacker-supplied IPC stream, patched four columns *by name*, and handed the
  batch to `insert_events_arrow`, which forwards it verbatim to ClickHouse. A
  renamed column made `get_field_index` return `-1`; a missing one silently took a
  server-side default instead of what `_normalize_event_row` writes on the way out.
  Now compared against `EVENT_ARROW_SCHEMA` per batch (a stream may change schema
  mid-way), raising into the existing all-or-nothing cleanup.
- **A failed import leaked blobs.** Blobs land in the instance-global retention dir;
  the cleanup path dropped the Postgres case and the ClickHouse partitions but not
  them, so a repeatedly-failing import accumulated case file content nothing
  referenced. The importer now tracks only the blobs *it* created — checked before
  `retain_file`, which short-circuits on an existing path — so a blob shared with
  another case is never removed. The test for the second half matters more than the
  first.
- **The startup sweep was `rmtree(temp_root)`.** Correct under its stated
  one-process assumption, but `VESTIGO_TRANSFER_TEMP_PATH=/data` wiped `/data` on
  every boot. Both sweeps now share `is_transfer_artifact` (a `*.vestigo` file or a
  job-id-named directory) and `sweep_stale(max_age_seconds=None)` is what startup
  calls; anything else under the path is left alone and warned about once.
- **Admission control was racy.** `count_active` then `create` let two simultaneous
  requests both pass at limit-1, so a cap of 1 admitted 2. `JobStore.create_if_under`
  does both under one lock. The import endpoint keeps a cheap pre-upload check so a
  full instance still says no before the body is on disk, and unlinks the temp file
  if a slot fills while the upload streams.
- **Docs claimed a check that could not fire.** `temp_root` chmods to `0700` and
  *then* asserts the mode has no group/world bits, so for a directory we own the
  mode is silently repaired, never rejected — the old test only passed because it
  monkeypatched `chmod` away. Kept the repair (it is the behavior an operator
  wants), corrected `.env.example` and `DEPLOYMENT.md`, and kept the assertion
  documented as the backstop for filesystems where `chmod` no-ops.
- **Smaller:** malformed `sources.ndjson` rows raised a bare `KeyError` from inside
  the events loop — now an `ArchiveFormatError` naming the field, raised while the
  revive loop is still on that stem; `sources.ndjson` is parsed once instead of
  three times; blob members are iterated in sorted order so warning order is
  reproducible; and the import dialog holds itself open to show the importer's
  warnings ("no blob for X", "user Y attributed to importer") instead of navigating
  past them — the job store is in-memory, so closing the dialog is the last chance
  to read them.

## Session 98 — 2026-07-25: X1 second review round (scaling, audit fidelity, limits)

**Why.** A second review pass over PR #182 after the session-97 hardening. The
archive layer held up; the two blockers were in the importer, and both would only
have shown up on a case bigger than the test fixtures.

- **Import was quadratic and unusable on a real case.** `_IdMap` rebuilt its
  substitution alternation every time a mapping was added, and the revive loop
  added one per row before touching that row's JSON columns — so the regex
  recompiled once per row. Measured on the branch: 200 rows 0.20s, 400 0.63s,
  800 2.60s, 1600 13.97s, ~4x per doubling; 25k audit rows extrapolated to about
  an hour of pure compilation. Compiling the alternation *once* costs ~2s even at
  200k ids and substitution is then free, so `_prescan_ids` now walks every stem's
  ref columns up front, `bulk_add` mints the new ids in one go, and `freeze`
  makes any later map growth raise instead of silently going quadratic again.
  Same workload after: 1600 rows 0.02s, 25k rows 0.41s, one compile. The prescan
  also closed an ordering gap — a chart config embedding an annotation id used to
  survive unrewritten, because charts revive before annotations.
- **`audit_log.target_id` was never remapped.** Every other reference was, so a
  restored audit trail pointed at ids that existed nowhere on the instance —
  it imported but could no longer be joined to the entities it described, which
  is most of why audit rows are in the archive at all. `target_id` spans every
  entity type, so no static ref column can cover it; archive ids are globally
  unique, so `_IdMap.lookup` resolves one without knowing its kind and passes
  through targets the archive never carried (teams, users, agent tokens).
- **Imported audit rows are now labelled.** They keep the actor, action, ip and
  timestamp the archive asserted — deliberately, that is the chain of custody —
  but any authenticated user can upload an archive, so an unmarked row is a
  forgery surface: an admin reviewing "what did user X do" would see fabricated
  entries. Every restored row now carries `detail.imported` (job id, importing
  user, source case id) and is badged **imported** in the admin audit view.
  Dropping the rows was the alternative and was rejected — a restored case with
  no provenance is worse than one with labelled provenance.
- **Admission control on transfers.** Both directions reserve real disk for the
  whole job and either can be started by any authenticated user, with nothing
  bounding concurrency. `VESTIGO_TRANSFER_MAX_CONCURRENT` (default 2, `0`
  disables) caps in-flight transfers instance-wide; the import check runs
  *before* `receive_upload_to_tmp`, since rejecting afterwards would mean the
  whole upload is already on disk.
- **Blobs no source references are ignored.** Content was already verified
  against its content-addressed member name, so an existing blob could not be
  poisoned — but an archive could still plant arbitrary files in the
  instance-global retention directory. Only hashes an archived source claims are
  retained now.
- **Startup sweep can no longer cost the rest of recovery.** It was the first
  statement in `_startup_recovery`'s single `try`, and `temp_root()` raises on a
  misowned or group-readable directory — so a misconfigured
  `transfer_temp_path` silently skipped orphaned-ingest reconciliation,
  enrichment re-runs and session purge. It now has its own handler.
- **Smaller.** Warning lists are aggregated per (stem, skipped source) instead of
  per row and capped at 50 plus a summary, since they ride into the `audit_log`
  detail JSON; `case.json`/`user_refs.json` are type-checked into
  `ArchiveFormatError` before reaching the ORM; the archived user map is looked
  up in batches of 1000 rather than one unbounded `IN (...)`; duplicate manifest
  member names are rejected; `POST /api/cases/import` uses
  `require_password_current`, matching `POST /api/cases` (`AuthAuditMiddleware`
  was already the enforcing boundary — this is consistency and an accurate
  OpenAPI schema, not a closed hole).

**Not done.** `ClickHouseStore` is still constructed bare and never closed by the
transfer modules — it has no `close()` and every call site in the repo does the
same, so this is the codebase convention rather than a transfer bug.

## Session 97 — 2026-07-25: X1 export/import review hardening

**Why.** Code review of the X1 branch (PR #182) before merge. Two findings were
security-relevant and reachable by the lowest-privileged account — import is open
to any authenticated user — and the rest were correctness gaps that would have
surfaced as silent data loss or resource leaks in a long-running instance.

- **Archive expansion is bounded.** The upload cap only limited *compressed*
  bytes, so a deflate bomb in a small `.vestigo` could OOM the process
  (`read_ndjson` reads a member whole) or fill the disk (`extract_to`). Sizes are
  now load-bearing: `ArchiveReader` requires an int `bytes` per member,
  cross-checks it against the zip directory entry, sums it against
  `VESTIGO_TRANSFER_MAX_EXPANDED_BYTES` (new setting, 200 GiB default, `0`
  disables) before reading anything, and every read aborts past the declared size
  in case a local header lies. Events and blobs are `ZIP_STORED`, so a real
  archive expands ~1x — a large ratio is an attack, not a big case.
- **Only manifest-listed members are read.** `postgres/*` members bypassed the
  verified set, and `read_ndjson` returned `[]` for a missing member, so an
  archive without `annotations.ndjson` restored a case with no annotations and
  reported success. Reads now raise for anything unlisted or absent.
- **Temp root moved and hardened.** In-flight archives lived at a fixed
  `/tmp/vestigo-transfer` created with a suppressed `chmod` — squattable by
  another local user on a shared host. New `VESTIGO_TRANSFER_TEMP_PATH` (default
  `data/transfer`); the directory is created `0700` and refused if it is a
  symlink, owned by someone else, or group/world accessible.
- **Archives stop leaking.** A failed export left its working dir and a
  never-downloaded export sat on disk until restart. The working dir is now
  cleaned in a `finally`, and each export first sweeps entries older than 24h —
  opportunistic rather than a timer, since this deployment has no scheduler.
- **Import correctness.** JSON id rewriting is a single regex pass (the old
  replace-loop could re-rewrite a fresh id — `generate_id` adds only 8 hex
  chars); `_IdMap` grew `pin`/`substitute` instead of callers poking `_map`;
  archived users resolve in one query with one warning per unknown *username*
  instead of one `SELECT` and one warning per row; and restored events have their
  embedding markers blanked, with a warning that vectors need re-embedding, since
  Qdrant data does not travel.
- **Export honesty.** The Postgres snapshot runs `REPEATABLE READ` on Postgres
  (skipped on SQLite, which rejects it) so a concurrent ingest can't tear the
  archive; skipped non-ready sources whose ids are still embedded in chart/run/
  proposal JSON now produce a warning; failed exports are audited, not just
  successful ones.
- **Misc.** Export options moved to a request body, the export dialog's download
  effect is ref-guarded (StrictMode fired it twice, and the second call 404s
  because the archive is deleted on download) with a retry affordance, and
  `ruff format` was applied to the two files that had drifted.

## Session 96 — 2026-07-25: X1 case export/import (`.vestigo` archive)

**Why.** Roadmap Milestone 9: any case — evidence, events, and all analyst work —
leaves the instance as one file and comes back intact, on the same or a different
instance. Archive/restore and cross-instance transfer are equal goals. Design:
`docs/superpowers/specs/2026-07-24-case-export-import-design.md`.

- **Export/import endpoints.** `POST /api/cases/{case_id}/export` (MANAGE gate —
  export is bulk case-data exfiltration — audit `case.export`) runs a background
  job building the archive; `GET /api/cases/{case_id}/export/{job_id}/download`
  streams it. `POST /api/cases/import` (any authenticated user, audit
  `case.import`) restores the archive as a new case owned by the importer —
  importer-owned restore, no auto-grants.
- **Transfer package** (`src/vestigo/transfer/`): `archive.py` (pydantic manifest,
  zip writer/reader, per-member SHA-256 verified before any write), `exporter.py`
  (direct-ORM Postgres snapshot with generic column serialization, Arrow event
  streaming, optional content-addressed blobs behind `include_blobs`),
  `importer.py` (in-memory old→new ID map rewriting all references, ordered
  inserts, Arrow `case_id`/`source_id` column rewrite, blob placement, field-stats
  recompute, all-or-nothing cleanup via the `delete_case` cascade). Event ids are
  preserved verbatim, so annotation→event cross-references survive. The
  content-addressed blob helpers moved from `api/routers/cases.py` to
  `src/vestigo/core/retention.py`.
- **Format.** Versioned single-file zip (`format_version: 1`): NDJSON per
  Postgres entity (case-scoped audit rows included), one Arrow IPC member per
  source, optional `blobs/<sha256>` members. Secrets (tokens, passwords, enricher
  API keys) never exported; pure caches and Qdrant embeddings recomputed on
  import. Users map by username, unknown names fall back to the importer with a
  warning.
- **Frontend.** Export button + dialog on the case card, import dialog on the
  case list — both follow the existing job-polling pattern.
- **ClickHouse pytest marker** (Milestone 2 residue, bundled): `clickhouse`
  registered in `pyproject.toml` and applied via `pytestmark` to all eleven
  `tests/*_clickhouse.py` files — `pytest -m clickhouse` selects them, and a run
  without the dev stack can no longer pass them silently.

## Session 95 — 2026-07-24: 1.6.1 code-review hardening

**Why.** A pre-merge review of the 1.6.1 range (9f194d8..0b1f7cb) reproduced two
bugs in the brand-new request guard and found the release's centerpiece —
calibration — had no positive test. All fixed on this branch before tagging.
(Merged after main's sessions 92–93; numbered 94–95 here to keep the log
append-only and collision-free.)

- **Guard counters survived no reduction race.** `make_window_processor` copies a
  fresh `WindowStats` wholesale whenever a later request reduces more — wiping
  `duplicate_calls`/`results_capped`, which the guard accumulates on the shared
  object *between* requests. Turns whose requests grow (the incident shape) lost
  exactly the forensic record the release promised. The counters now survive the
  copy the way `max_request_chars` already did.
- **Dedupe was defeated by parallel tool execution.** pydantic-ai runs one model
  response's tool calls concurrently, and the check-then-act spanned the wrapped
  call's `await`, so two identical parallel calls both executed. The first caller
  now plants an in-flight marker before its first await; concurrent duplicates
  await the original's outcome and get the back-reference, and a waiter whose
  original raised re-executes instead of referencing a phantom.
- **Tests for what the release is about.** Positive calibration round-trip
  (overflow + recorded send size → `measured_chars_per_token` persisted → retry
  budget uses it → next turn inherits it), the `_REQUEST_TOKENS_RES` phrasings,
  band-edge rejection in `calibrate_chars_per_token`, `schema_chars_for_scope`
  (magnitude, conversation and `disabled_tools` effects), the admin `warnings`
  array, and both guard races above.
- **The guard-rail now reaches the operator.** The admin agent page renders the
  settings `warnings` array (previously: server log and raw JSON only), and the
  chat's window marker names collapsed duplicates / capped returns alongside the
  elision counts.
- Housekeeping from the same review: `max_request_chars` persisted on fit and
  overflow rows (calibration stays auditable), "28 tools" corrected to 30 in the
  docs that count honestly, the guard docstring now describes the incident
  accurately (different empty-filter keys, identical *results*), the stray
  vitest cache under the root `node_modules/` untracked and ignored, unrelated
  `uv.lock` platform-marker churn reverted, and the missing 1.6.1 CHANGELOG
  entry written.

## Session 94 — 2026-07-24: agent context-window overflow fixed (1.6.1)

**Why.** An analyst investigation died with no error surfaced in the UI. The LiteLLM body:
`request (75967 tokens) exceeds the available context size (65536 tokens)`. The sliding
window (`agent/window.py`) is supposed to make this impossible — it runs before *every*
model request — yet it let a 76k-token request through a 49k budget without eliding
anything. Not the mid-conversation model switch (that raised the budget); the window failed
at a fixed budget. Four root causes, all shipped fixed.

**1 — the budget never counted the tool schemas.** `budget_for` reserved history + system
prompt but not the advertised tools, which ride *outside* the `messages` the window
processor sees. 14 of 30 tools each carry their own copy of the `FilterSpec` definition
(~13k tokens). `budget_for` now takes `tool_schema_chars` (measured per-scope by
`schema_chars_for_scope`, since `disabled_tools` varies) and reserves it. Verified the
copies cannot be hoisted into one shared `$defs` — the OpenAI function-calling wire gives
each tool an independent `parameters` schema; finding recorded in `agent/schema_slim.py`.

**2 — `chars/4` was off 1.7×.** Prose tokenizes near 4 chars/token; real tool payloads
(escaped JSON, base64 params, dotted-quad IPs, UUIDs) measured **2.35** on this overflow.
Default is now `CHARS_PER_TOKEN_DEFAULT = 3.0`, and `calibrate_chars_per_token` learns the
true ratio from an overflow body that names the request's token count (clamped 1.5–5.0),
persisted as `measured_chars_per_token` and reused next turn via `get_last_chars_per_token`.
Airgapped-safe — no tokenizer.

**3 — one turn spent the whole window on duplicates.** The failing turn issued three
`search_events` calls with empty-array (no-op) filters — byte-identical ~34k payloads,
~100k chars of pure duplicate, and they were the *newest* returns the window protects.
Fixed two ways: a `FilterSpec` validator now rejects empty-list filter values with an
actionable message, and a new `_RequestGuardToolset` (`agent/runtime.py`) wraps the toolset
and, per model request (`RunContext.run_step`), dedupes identical `(tool, canonical-args)`
calls to a `{"duplicate_of": …}` back-reference and caps one request's total return bytes at
`budget × 0.5 × chars_per_token`. Both counted on `WindowStats`
(`duplicate_calls`/`results_capped`) and recorded on the `role="window"` row.

**4 — the overflow retry destroyed the investigation.** It blindly multiplied the budget by
0.6, overshooting into turn-dropping, so the agent re-ran its whole orientation sweep three
times over (203 tool calls, ~half repeats). The retry now prefers the provider's reported
window (`_overflow_window_hint`) whether or not a budget exists, recomputing `budget_for`
with the reserved shares and calibrated ratio; ×0.6 is only the no-hint fallback.

**Also.** `add_agent_message` bumps the parent conversation's `updated_at` (a failed-only
conversation no longer freezes and sorts wrong); usage tokens already thread on the success
path. Config guard-rail `fidelity_config_warning` flags explicit `tool_fidelity=full`
against a window below `AUTO_FULL_MIN_WINDOW` (the exact `full` + 65536 shape), logged at
turn start and surfaced in the admin agent-settings `warnings` array. The failed turn's
partial messages already persist as individual rows (the forensic record); the replay blob
stays at the last consistent boundary by design (a half-turn would desync the next turn).

**Tests.** New `tests/test_agent_runtime.py` (request guard: dedupe, arg-order canonicality,
per-request reset, byte ceiling, rejected-call-not-cached); guard-rail cases in
`test_agent_fidelity.py`; `updated_at` bump in `test_agent_api.py`; realistic escaped-JSON
payload fixture `tests/data/agent_payload_shape.py` (synthesized, not copied — the real
capture is a live case). How it slipped through: every window test built payloads from ASCII
filler where `chars/4` is roughly right, and the tool schemas rode outside the tested
`messages`.

## Session 93 — 2026-07-24: surface incomplete result loads + prove export completeness

**Why.** Follow-up to session 92. An analyst's Explorer showed "20,522 events loaded · all
loaded" while the AI agent's `search_events` reported 21,175 for the *same* filter; the agent
spent four turns guessing at the gap. Live investigation against a replicated localhost
instance (case `CloudTrail_2b558274`, 11-IP `src_ip` filter) **disproved every structural
theory**: not routine-collapse (none active), not duplicate `event_id` (`count()` =
`uniqExact(event_id)` = `uniqExact((timestamp,event_id))` = 21,175; table-wide dup gap is 56 in
an unrelated case), and **not a keyset-pagination skip** — a faithful ms-truncated keyset walk
yields all 21,175 rows over 212 pages. So keyset pagination is provably complete; the 20,522 was
session-92's null-`total` bug (no count shown) over a partial/point-in-time load, and the grid
compounded it by asserting "all loaded" from cursor state alone.

**What.**
- *Grid (`components/explorer/EventGrid.tsx`).* "all loaded" is now derived from
  `events.length >= total` (a completeness claim), not `!hasNextPage`. When both directions are
  exhausted yet short of the known total, the footer shows a red `⚠ N not loaded — reload`
  instead of falsely reading complete.
- *Export hard-fail (`api/routers/events.py`).* `export_events` pre-flights `count()` over the
  same `EventQuery`; `_stream_jsonl`/`_stream_csv` tally rows and emit a self-proving completeness
  trailer (JSONL trailing `_meta`, CSV trailing `# vestigo_export …` comment). On a shortfall
  they mark it incomplete and raise `ExportIncompleteError`, breaking the download so a
  silently-short custody artifact can't be mistaken for complete. `expected` is added to the
  `events.export` audit; a background task records the `events.export.result` (written/complete).
- *Tests.* New `tests/test_pagination_completeness_clickhouse.py` walks a real keyset over 200
  tied-millisecond rows + adjacent + null-timestamp sentinels via both `query` (grid) and
  `iter_events` (export), asc+desc, asserting loaded == `count()` — the invariant no mocked
  `test_queries.py` test covered. `tests/test_events_router.py` gains export hard-fail + success
  trailer cases. Builds on session 92's `EventQueryService.count()` / `GET .../events/count`.

**No** storage/sort-key change: the keyset was verified correct, so the fix is surfacing +
proof, not a pagination rewrite.

## Session 92 — 2026-07-24: true match count in cursor/jump-to-time sessions (bulk-tag regression)

**Why.** Bulk-tagging "all events matching the filter" showed and offered only the loaded-row
count (e.g. "Apply to 600") when the real match set was much larger (21k). Root cause: the
events list only carries a real `COUNT(*)` on its first *offset*-mode page (`db/queries.py::query`).
A cursor-only session — a jump-to-time seek, or paging before an offset page ever loaded —
leaves `page.total = null`, so the Explorer fell back to `events.length` for the grid footer,
the "select all matching" banner, and the bulk-annotate confirm dialog. The server-side write
itself already covered the full filter set correctly; only the count the analyst *saw* was wrong,
which both hid the true scope and undercut the "select all matching" bulk action.

**What.** Added a dedicated count path that always runs, independent of pagination mode:
`EventQueryService.count()` (`db/queries.py`, same `_build_where` as `query`/`query_event_refs`)
+ `GET .../events/count` (`api/routers/events.py`, mirrors `get_histogram`'s filter surface,
incl. `collapse_routine` so the count matches the collapsed grid and what a filter-scoped bulk
write touches). Frontend: `eventsApi.count` (`api/events.ts`), and ExplorerPage now fetches it
whenever `pageTotal === null`, using `pageTotal ?? countData.total` as the authoritative `total`
that already feeds the footer, banner, `selectionCount`, and confirm dialog. Tests: two new
`count_events` router tests (returns total; resolves the same routine-collapse scope as the bulk
write). No extra count scan on the common offset path (gated on `pageTotal === null`).

## Session 91 — 2026-07-23: `--since`/`--until` for native converters + forensic footer metadata

**Why.** Ingestion does no dedup (plain `MergeTree`, a fresh `source_id` per run), so
re-ingesting an rsynced log directory daily re-inserts every rotated + growing log —
duplicate rows, skewed anomaly baselines, polluted embeddings. The vendored `*2timesketch`
converters already ship `--since`/`--until` window filters that drop out-of-window rows
before output; the Vestigo-native Parquet-path converters lacked them.

**What.** Added `--since`/`--until` ISO-8601 time-window filtering to all six native
converters (`cloudtrail2vestigo`, `filterlog2vestigo`, `nginx2vestigo`, `pcap2vestigo`,
`suricata2vestigo`, `timesketch2parquet`). Parsing via a ported `_parse_since_until`
(handles trailing `Z`, naive→UTC, normalized to UTC); a per-row datetime compare guards the
single `_BatchBuffer.append` emit site in each script; the since/until datetimes are threaded
through into the worker entry points so the parallel paths filter correctly under `spawn`.
Rows with an unparseable/missing timestamp are **kept** (matches upstream `_filter_by_time`;
never silently drop undatable evidence). Each converter bumped `1.2.0 → 1.3.0` (feature; the
version rides into `parser_version` → `derive_event_id`, so `1.3.0` events get distinct ids,
which is correct — it records a real change in the producing tool).

**Forensic footer metadata (Tier 1).** Additive Parquet footer keys, no schema/column change,
tolerated by older readers: `vestigo.converted_at`, `vestigo.row_counts`
(`{parsed, skipped_malformed, skipped_by_time}` — the `--since` honesty story),
`vestigo.timezone_assumption`, `vestigo.parse_decisions`; `original_files` entries gained
`path` (absolute) + `mtime`. `row_counts` is written post-write-loop via
`ParquetWriter.add_key_value_metadata`, and `split_parquet` now merges footer KV so parts keep
it. Constants mirrored in `src/vestigo/ingestion/parquet_format.py`.

**Upstream.** `browser2timesketch` is the one vendored converter still missing the window
filter — it is `do-not-edit` (vendored from `overcuriousity/2timesketch`), so filed
[overcuriousity/2timesketch#4](https://github.com/overcuriousity/2timesketch/issues/4) for
upstream parity rather than hand-editing.

**Tests.** Per-converter `test_time_window_filter` (out-of-window dropped, wide-open window ==
unfiltered, footer `row_counts`/`converted_at` asserted; timesketch2parquet parametrized over
CSV + JSONL). Updated the `test_file_provenance` and embedded-spec-parity tests for the new
`original_files` keys and META constants; refreshed the native entries in
`converters/manifest.json`. Deferred to a later round: `line_number`/`raw_line` columns and
opt-in operator/host capture (touch schema + `parquet_reader` + `event_id`).

**Also — scatter degenerate-axis fix (`field_scatter`).** Upstream CI (ClickHouse 24.10) was
red on `test_scatter_degenerate_axis_nulls_coefficients`: a constant-value axis made
`rankCorr` raise `BAD_ARGUMENTS` ("All numbers in both samples are identical") at finalize —
and a wrapping `if` doesn't help because the aggregate is still finalized. Newer ClickHouse
(26.6) instead returns a *bogus* finite ρ for the same input. Fixed both: the stats query is
retried without `rankCorr` when that specific error is caught, and a degenerate axis
(`min == max` on either side) now nulls the entire Pearson/Spearman/regression block
explicitly rather than trusting per-aggregate server behavior. Common (non-degenerate) path
still one scan. Unrelated to the converter work but on the same branch at the user's request.

## Session 90 — 2026-07-22: review fixes on the statistical visualizations

Code review of the session-89 branch (PR #162) surfaced nine defects. Three were about
cost at the scale this product targets, three were the PR's own honesty contract being
broken by a caption or comment, three were parity/coverage gaps. All fixed on the same
branch; the 1.6.0 changelog entry was amended rather than a new version cut.

**Cost.** `kendall_tau` was the all-pairs O(n²) definition, running on every scatter render
inside a request holding a heavy-scan slot: measured 1.07 s at the UI's default 5 000-point
sample and 17.13 s at the API's 20 000-point ceiling. Replaced with Knight's O(n log n)
formulation (sort by (x, y), count discordant pairs as inversions of the y-sequence via
merge sort) — 0.056 s at 20 000 points, exact agreement with the brute-force definition
across a tie-density sweep and with the committed scipy constants. Shapiro–Wilk is now cut
to the 5 000 points Royston's approximation covers instead of silently returning nothing
past it. `field_numeric_grouped` runs its four scans as two parallel waves through the
existing `_run_parallel` rather than sequentially (the extent scan and the per-group
aggregate scan have no data dependency; the per-group aggregate therefore also runs on an
empty result set, which is the price of the concurrency).

**Reproducibility.** Every sampling path drew with `ORDER BY rand()`, so rerunning an
identical query produced a different chart and an exported scatter could not be regenerated
from the filters that made it — while the point strip's *jitter* was carefully
deterministic. All three paths now order by `cityHash64(event_id)`: uniform, independent of
the plotted values, stable across reruns and replicas, and no more expensive (same bounded
top-N heap). Pinned by a live-server test asserting two identical calls return identical
points.

**Honesty.** Three places claimed more than the code computed. (1) When Freedman–Diaconis
was undefined (zero IQR) the fixed 30-bin fallback was still reported as `bin_rule: "fd"`,
so the caption credited a rule that never ran — a test even pinned that. `bin_rule` is now
`fd | fd_fallback | manual` with a separate `bin_count_clamped`, and the caption names each
case exactly. (2) Past 5 000 sample points Shapiro–Wilk returned nothing, `recommendation`
silently became "spearman", and the UI blamed "sample too small" — the opposite of the
cause. Beyond the cap fix, the response now carries `recommendation_basis`, and the panel
labels the chip "default" rather than "recommended" when nothing measured it. (3) Two
comments claimed a wide grouped violin means "more events here"; `kdeFromBins` normalizes
by each group's own total, so a 10-event and a 10 000-event group with the same shape draw
identically wide. The shape scaling stays (it is what a grouped violin is for) and the
claims were corrected, with a caption line stating the reading and per-group n on the
tooltip.

**Parity and guards.** The `field_correlation` agent tool silently truncated a too-long
field list and de-duplicated in silence, so the service's own error could never fire — it
now raises, with the wording the HTTP 422s use. Grouped charts warn when groups were
omitted and when the grouping field's cardinality suggests an identifier
(`VIZ_GROUP_CARDINALITY_CAUTION`). The correlation matrix fades cells with p ≥ 0.05 or
fewer than 30 pairwise-complete events and puts both p-values in the tooltip — full-strength
colour on an unsupportable coefficient reads as a finding. `allocateWaffleCells` folds
categories past the grid's capacity into `Other`, so its "sums to exactly 100" invariant
holds by construction rather than by the top-N cap happening to be below 100.

**Coverage.** `tests/test_viz_router.py` gained the HTTP-level tests the two new endpoints
never had (the three 422 guards, plus argument pass-through and the `bins=None` automatic
path). Backend 1588 pass, frontend 470 pass.

## Session 89 — 2026-07-22: lecture-grade statistical visualizations

Audited the visualization stack against the HS Mittweida "Datenanalyse und -visualisierung"
lecture set (anatomy of a graphic, histograms, box/violin, bar, pie/waffle, line, scatter,
correlation/multipanel, descriptive statistics) and closed every identified gap except
geographic charts (deferred with its blockers named in `ROADMAP.md` Milestone 2). The
existing core held up: Stevens-scale legality per mark, zero-baseline bars, no dual-axis
charts anywhere, and captions that state top-N capping and sampling. What was missing was
analysis depth, so this round added it — for the analyst and the agent in the same commit,
since `agent/chart_meta.py` generates the frontend's table.

**New statistics, computed server-side.** `src/vestigo/stats.py` is a new pure-Python
inference module (no scipy — airgapped installs stay slim): regularized incomplete beta →
Student-t survival → Pearson/Spearman p-values, Kendall's tau-b with tie correction,
Shapiro–Wilk after Royston (1995) AS R94, and the Freedman–Diaconis bin rule. It is pinned
against scipy-computed reference constants committed as `tests/data/stats_reference_scipy.json`.
Everything ClickHouse *can* do is left to ClickHouse (`corr`, `rankCorr`,
`simpleLinearRegression`, `skewPop`, `quantile`) over the full filtered data; Python only
fills the gaps, and the response labels which numbers came from a sample.

Two ClickHouse behaviours were settled empirically against the live dev server (26.6) and
are now pinned by `tests/test_viz_stats_clickhouse.py`: multi-argument aggregates skip a
row when *any* argument is NULL, which is exactly pairwise-complete deletion and is why the
correlation matrix does not use `corrMatrix` (listwise) — and `assumeNotNull` must **not**
be used to "fix" the Nullable arguments, because it turns NULL into 0.0 and folds
non-numeric rows into the coefficient. `simpleLinearRegression` is the exception: its
tuple return corrupts clickhouse-connect's native parsing with Nullable inputs, so it (and
only it) gets `assumeNotNull` under an `IS NOT NULL` guard.

**New marks and aggregations.** Correlation matrix (`corr`, new `field_correlation`
aggregation + endpoint + agent tool; lower-triangle diverging grid, per-cell coefficient,
click-through to the pair's scatter); grouped box/violin (`field_numeric_grouped`: top-N
groups by count, per-group quantiles binned over the *global* range so silhouettes compare,
omitted groups reported and never merged into an "Other" box); waffle chart (reuses the
terms aggregation, largest-remainder allocation so cells sum to exactly 100 and no existing
category rounds to zero).

**Facetting was built and then cut in review.** Client-orchestrated small multiples (one
terms query names the panels, each panel re-runs the same endpoint with an added equality
filter) shipped in the first pass and was removed before merge. The reason is worth
keeping: each panel asked the server independently, so each got Freedman–Diaconis bin
edges from *its own* subset, while the grid pinned a shared count axis across panels.
Equal bar heights then meant different densities — the precise misreading small multiples
exist to prevent. Making it honest needs the bin range threaded through
`field_numeric_stats` (a shared ruler for every panel), which is a design round rather
than a review fix; deferred with that requirement recorded in `ROADMAP.md`. Removing it
also took with it the caption bug it caused (facet captions were filled from the
*unfacetted* query, so bin rule, skewness and overlay counts described data no panel
showed) and the streaming shared-scale bug (the count max was a max over *loaded* panels,
so an export taken mid-load captured non-comparable panels).

**Honesty fixes the lectures are blunt about.** Violin/box gained an optional jittered
overlay of sampled raw values (deterministic jitter, so an export reproduces the strip)
— a violin without points implies data it never measured. Pie gained a readability warning
past four slices or when two slices differ by under 10%, offering bar/waffle instead;
advisory, never a refusal, and the same rule runs in `propose_chart`. Line charts mark
their actual measured buckets (Tufte's graphical integrity). Histograms default to
Freedman–Diaconis bin widths with a manual override, and carry a density curve, mean/median
markers and skewness with its plain-language reading.

**Teaching mode.** `viz/lib/explainers.ts` is a single copy module and
`ExplainerPopover.tsx` its one renderer: every statistic gets *what it is / how to read it /
when to distrust it* plus the formula, and every chart type a one-line "how to read this".
The distrust section is mandatory (a test enforces it) — a statistic explained without its
failure mode teaches overconfidence, which is worse than not explaining it.

**Review fixes.** The scatter caption now carries the caveat its own explainer already
stated — past ~1000 sampled points Shapiro–Wilk rejects departures too small to change
which coefficient to quote, and the caption is the forensic export, so it has to say so
rather than read as a finding about the data. σ and the waffle grid were rendering without
explainers despite the "every statistic carries one" invariant; both got copy, and
`vizExplainers.test.ts` gained the converse check (every defined explainer is rendered by
some component) that would have caught it. `jitter.ts` swapped the smooth
`sin(i·12.9898)` GLSL hash for an integer bit-mix: the old one is continuous in `i`, so
the consecutive indices a point strip feeds it came out correlated and banded.

`docs/AGENT.md` documents the new tools, the field-slot rules (`field_y` required vs.
optional, `fields`), and the statistics contract.

## Session 88 — 2026-07-22: --split ported to native Parquet converters

Ported the upstream 2timesketch `--split N|SIZE` flag (vendored in session 87) to all
six native Parquet converters (`*2vestigo.py` + `timesketch2parquet.py`), each bumped
1.1.0 → 1.2.0. Parquet adaptation: the conversion writes a single `<output>.tmp` file
as before, then `split_parquet()` repartitions it into `<name>.partNNN.parquet` parts —
parts mode (`--split 4`) slices record batches for an exact `ceil(total/N)`-row
distribution, size mode (`--split 512M`) rotates on the part file's on-disk size after
each flushed row batch (batch scaled to the limit, so a part overshoots by at most one
batch). Every part carries the full interchange schema + provenance metadata and is
independently ingestible; row order is preserved. `manifest.json` hashes regenerated.

Also un-excluded the native converters from ruff (exclusion narrowed to the vendored
`*2timesketch.py` files, which `scripts/vendor_converters.py` regenerates verbatim) and
fixed the resulting findings (import sorting, `datetime.UTC`, `zip(strict=False)`,
`contextlib.suppress`, one justified `noqa: SIM115`).

## Session 87 — 2026-07-22: docs cleanup + 2timesketch re-vendor

Docs pass (PR #153): PROGRESS sessions 1–70 archived; stale point-in-time sections
stripped from `CONCEPT.md`/`TECH_STACK.md`/`MODEL_REFINEMENT.md` (incl. correcting the
false "allow_online not enforced" claim); new `docs/DEPLOYMENT.md` absorbs
`DEPLOYMENT_NGINX.md` plus the README's airgapped/compose/upgrade sections; `ROADMAP.md`
re-verified against the codebase and given an explicit priority order; `README.md`
rewritten lean (detector count 9 → 12, GeoIP enricher mentioned, Parquet-native vs.
stdlib converter variants distinguished); `AGENT.md` rewritten 968 → 528 lines with the
tool catalog as a 28-row table from `TOOL_REGISTRY` (prose said 27).

Re-vendored the 2timesketch converter suite at upstream `920767a` (was `53a1fb1`),
picking up the `--split N|SIZE` multi-file output flag across all converters and the new
generic Zeek NSM converter (`zeek2timesketch`, header-described TSV parsing — any log
type incl. rotated/gzip, 4-tuple promoted to the shared `src_ip`/`dst_ip`/`src_port`/
`dst_port` columns). Added the `zeek` entry to `scripts/vendor_converters.py`'s
`CONVERTERS` dict; the script already inlined the new shared `terminal.py` module.
Verified: all 13 vendored scripts `py_compile`, `tests/test_converters_api.py` green
(25 passed), and a functional zeek run over a sample `conn.log` produced the expected
Timesketch CSV.

## Session 86 — 2026-07-22: sliding-window review fixes (PR #152)

Review of PR #152 found no blockers but five real defects; all folded into the
unreleased 1.5.0.

**Truncation pass.** A single tool result larger than the whole budget was
reducible by neither pass — elision protects the newest request, turn dropping
cannot reach inside it — so the turn overflowed, retried identically and died:
the exact failure shape the window was built for, one degree worse. Pass 3
(`_truncate_newest`) now cuts the newest request's returns to a leading slice
(`{"truncated": true, note, head}`, floor `MIN_KEEP_CHARS = 500`) rather than
stubbing them, so the model keeps the shape of its own result. When even that
leaves the history over budget, `apply_window` warns — the analyst-facing
`context_overflow` error reads as "conversation too long", which that case is
not.

**A learned budget outlives its turn.** The reactive budget was a local, so a
deployment with no `context_window` burned a failed provider round trip *every*
turn. `PostgresStore.get_last_window_budget` reads the newest
`reason="overflow"` window row and seeds the next turn; a budget that overflows
again is tightened and re-persisted, so it converges. Configuration still wins.

**Honest stats.** `make_window_processor` kept per-field maxima, which could
pair one request's `estimated_before` with another's `estimated_after` — a
delta that never happened, in a record meant to stand up as evidence. It now
keeps the single largest-reduction request wholesale. Also: `_drop_turns`
measured a span as a sum of per-message estimates (each re-serialized with its
own JSON brackets) instead of one slice estimate; and `.env.example` still
documented the retired `VESTIGO_AGENT_COMPACT_THRESHOLD`.

## Session 85 — 2026-07-22: sliding context window replaces fidelity ladder + compaction (1.5.0)

Driven by a real failure: an exported conversation (`ornith:9b`, 64k window)
overflowed **twice inside its first turn** — the fidelity ladder dropped a tier
and re-ran the whole turn (the model re-issued the same broad plan, doubling
the work), compaction had nothing to fold (first turn), and the analyst got
`[interrupted]` instead of a report. The failing class is *mid-turn* overflow:
tool results accumulating inside one `agent.run`, which neither mechanism
addressed.

New `agent/window.py`: a deterministic sliding window applied via pydantic-ai's
`ProcessHistory` capability before **every model request** (mid-turn included).
Pass 1 elides the oldest `ToolReturnPart` contents to `{"elided": true, note}`
stubs (structure untouched — tool pairing/alternation survive all protocols);
pass 2 replaces the oldest whole user turns with one marker pair. Protected:
first user request (case context), the newest request's returns, the last turn,
all assistant prose. Pure function of (messages, budget) — replay under the
same config elides the same bytes; the stored history blob stays complete
(window applies at send time). Transparent to the model: stubs are visible and
the system prompt explains recovery (`get_event`, narrower re-runs).

Retired: the fidelity overflow ladder (`degrade`/`next_tier` — static
`tool_fidelity` shaping stays) and `agent/compaction.py` entirely (summarizer
ran on the same weak model, nondeterministic output, and its niche is covered
by pass 2 + "start a new conversation"); `compact_threshold` dropped everywhere
(migration 0015), `get_last_agent_usage` deleted. Router: proactive budget from
`context_window` (`×0.8 − est(system prompt)`); on overflow one reactive retry
(derive budget ×0.8 from the failed request, or tighten ×0.6 if already
windowed), then the friendly `context_overflow` error. Forensics: one
`role="window"` row + `agent.window` audit per reduced turn (reasons `fit` /
`overflow`); old `compaction`/`fidelity` rows still render read-only in the
panel. Version 1.5.0; net-negative LOC in `src/`. Spec:
`docs/superpowers/specs/2026-07-22-agent-sliding-window-design.md`.

## Session 84 — 2026-07-22: "locate this event in timeline" no longer scrolled (#150)

Regression from the #147 routine-collapse work (`e8626c4`, `16e4c89`), which
made collapse auto-on whenever any mute/routine disposition exists. The
Explorer's live events query is keyed on `effectiveFilters`
(`computeEffectiveFilters`, which folds the `collapseRoutine` overlay in), but
both cache-seeding paths in `ExplorerPage.tsx` built that key *by hand* and had
drifted: `handleJumpToTime` cleared filters and seeded/cancelled a hardcoded
`["events", …, {}, …]` key, and the `setFilters` soft-anchor seek re-applied
`anomalyRunId`/`semanticSearchIds` but omitted `collapseRoutine`. Once collapse
was on, the live key was `{collapseRoutine:true}` — the seeded anchor page (with
the target spliced in) landed in a cache entry the grid never read, so nothing
scrolled. Same defect silently reset the soft-anchor "keep scroll position on
filter change" to the top.

Per the owner's call, locate now **keeps** the active filters instead of
clearing them: it seeds the *current* `eventsQueryKey`, so the seed can't drift
from the live query by construction, and the neighbour pages are fetched with
the same `effectiveFilters` (surrounding rows stay filtered). The target is
force-included via `getById` (raw, ignores the view); a filtered membership
probe (`ids:[target]`) decides whether it's hidden, and if so `locatedHiddenId`
flows to `EventGrid`, which renders that row visually distinct (dashed edge +
faint tint + an "Hidden" `EyeOff` pill, tooltip explaining it's shown only
because it was located). Analysis-panel jump-to-time shares the behaviour. The
soft-anchor seek now composes its key through the same `computeEffectiveFilters`
helper so it can never drop an overlay again. Detail-panel Locate tooltip copy
updated. Tests: a locate-under-collapse regression in
`explorerRoutineCollapse.test.tsx` (target reachable in the grid after the seek,
`locatedHiddenId` set, every request carries collapse) — it would have caught
#150.

### Review pass: closing the drift class rather than the instance

Reviewing the above found that "the seed key can't drift from the live key by
construction" wasn't yet true, plus one race the fix itself introduced. All of
it is the same seam, so it landed in the same branch:

1. **Seed key built from the raw filter object.** `computeEffectiveFilters(f,
   …)` starts from whatever the caller passed. `handleApplyAgentFilters` passes
   a finding's filter set, which carries `ids`/`collapseRoutine` — fields
   `filtersToParams` deliberately drops. The live query composes from
   `paramsToFilters(searchParams)`, so the two differ (`{…, collapseRoutine:
   false}` vs `{…}`) and the seed lands unread. The seek now composes from
   `paramsToFilters(filtersToParams(f))` — the same round trip the live value
   goes through — so it matches for *any* caller, including future ones that
   pass fields the URL doesn't carry.
2. **Overlays set in the same batch were unreadable.** `handleApplyAgentFilters`
   calls `setCollapseRoutine`/`setAppliedIds` alongside `setFilters`, so neither
   state nor the mirror refs hold the new values when the seek composes its key.
   `setFilters` grew an optional `overrides: Partial<ExplorerOverlays>` second
   argument that the apply handler fills in. The mirror refs also moved from
   `useEffect` to render-time assignment, since a post-commit sync is one commit
   late for exactly this case.
3. **Jump vs. soft anchor now race on one key.** Locate keeping the analyst's
   filters means both seed paths write the *same* query key; the old
   `setFilters({})` inside the jump used to separate them and clear the pending
   soft anchor. A soft-anchor fetch still in flight would land after the located
   page and overwrite it. `handleJumpToTime` now invalidates any pending/in-
   flight soft anchor (and `setFilters` symmetrically invalidates a pending
   jump, whose key the filter change just orphaned).
4. **`locatedHiddenId` outlived its claim.** Cleared on filter changes only, so
   revealing routine events left a row badged "Hidden" while nothing hid it. An
   effect keyed on the overlays clears it. The row styling was also gated on
   `!isExpanded && !isSelected` — but a jump auto-expands its target, so the
   marker was invisible at the one moment it mattered; it is now a dashed edge +
   inset ring that layers over any row state.
5. **Leftovers.** The `isJumpClear` guard existed only for the removed
   `setFilters({})` call and its remaining effect was to skip the soft anchor
   when clearing the last chip — dropped. The "back to filtered view"
   breadcrumb still claimed a jump had cleared filters; only `handleContextQuery`
   produces it now, and the copy says so.

Tests: five more specs in `explorerRoutineCollapse.test.tsx`, each verified to
fail with its fix reverted — agent-apply seeds a page the grid actually reads,
a late soft anchor can't overwrite a jump's page, the hidden marker clears on
reveal, locate fetches neighbours through the active filters, and locate leaves
`locatedHiddenId` null when the target is already visible. The reveal toggle
carries a `data-testid` so the overlay-expiry path is drivable.

## Session 83 — 2026-07-21: agent chart cards lost when the model batches tool calls

An exported Kimi conversation showed 14 `propose_chart` calls all validating
`ok: true` while the analyst saw exactly one card — mispaired at that (last
call's title with the first call's result). `AgentPanel.tsx` paired call and
result rows through a single `pendingChart` buffer that assumed strict
call→result adjacency; Kimi batches parallel tool calls, so the transcript
persists N call rows followed by N result rows and every call overwrote the
buffer. Both render paths had it (`itemsFromMessages` and the live
`foldStreamEvent`).

FIFO pairing alone would still be wrong: parallel tool calls execute
concurrently, so result events arrive in *completion* order. The provider's
`tool_call_id` was already on the SSE events (`runtime.py`) but dropped at
persistence. Fix: new nullable `agent_messages.tool_call_id` column (migration
`0014`), threaded through `add_agent_message` and both persistence sites in
`api/routers/agent.py`; the panel now buffers pending charts in a Map keyed by
`tool_call_id` (FIFO fallback for pre-migration rows), a failed validation
consumes its own entry without shifting batch siblings, and unrelated tool
calls no longer clear the buffer. Tests: batch-of-N, completion-order results,
failed-sibling, and legacy-FIFO cases in `agentPanelChart.test.tsx`; backend
round-trip in `test_agent_api.py`.

## Session 82 — 2026-07-21: #147 blast radius — viz endpoints, the Visualize page, and the first-render flash

Review of PR148 asked the question its own doc invariant begged: does *every*
filter-driven endpoint resolve the routine scope? No — all seven viz endpoints
(`viz.py::_resolve_event_query`) dropped `collapse_routine` silently. The
frontend had always sent it (`serializeEventFilterParams`), FastAPI ignored the
unknown query param — the same silent-drop failure shape as bulk-annotate's
pydantic `extra="ignore"`, one layer over. Concrete symptom: the field-histogram
modal's top-value list (viz, uncollapsed) disagreed with its own histogram
(events endpoint, collapsed) inside one modal. Fixed by threading one
`collapse_routine` param through `_resolve_event_query`, all six GET routes and
`CompareFilters`; the compare baseline layer stays deliberately uncollapsed
("the whole the primary is a part of", preserving the M24c superset invariant),
and `_is_unfiltered` now treats the scope as a filter so the per-source stats
cache can't serve the muted superset. Anomaly detectors and similarity are
confirmed out of scope by design (deliberately unfiltered timeline / no field
filters).

**The Visualize page could not know about mutes at all.** It inherits filters
from the URL, and `collapseRoutine` is deliberately never URL-serialized — so
after #147 the page would have silently charted the uncollapsed superset with
no indicator. Decision (what does a forensic analyst need and expect): full
Explorer parity. The analyst pivots Explorer → Visualize expecting the same
event set; muted templates are high-volume by nature and dominate chart
y-axes; and nothing may be hidden silently. The page now derives collapse from
the disposition set via the same `lib/routineCollapse.ts` (single source of
truth in Postgres — a shared URL shows a teammate the same collapsed charts),
renders a "routine events collapsed" line with the same self-expiring reveal,
and gates every chart query on the disposition load. Agent chart proposals
stay spec-driven only — they must reproduce exactly what the agent ran.

**The first-render race.** Both pages fired their first data query before the
dispositions query resolved: collapse derives to `false` on an unknown set, so
every load with mutes present rendered the uncollapsed superset — the literal
#147 flash — plus a wasted ClickHouse scan, then refetched. Both now gate on
`dispositionsQuery.isSuccess` (TimelineHistogram grew an `enabled` prop for
the same reason). One small Postgres query before first paint.

Tests: viz router wiring tests (flag → resolver → EventQuery, both scope
halves, per-compare-layer), the motif half added to the bulk-annotate
regression test, serializer contract locks for `collapse_routine`, and two new
page-level render tests (`explorerRoutineCollapse.test.tsx` — the test that
would have caught #147 itself, asserting the request is gated and carries the
flag — and `visualizeRoutineCollapse.test.tsx`). The reveal toggle's accent now
marks the *override* (reveal active), not the collapsed default.

## Session 81 — 2026-07-21: #147 — the filter that was recorded but never applied

An analyst muted three templates in Templates → Mute, watched all three land in
"Muted templates (3)" with their counts, and saw the grid keep showing every one
of their events. The plumbing was never broken: the disposition was written, and
`_resolve_routine_collapse` → `template_hash NOT IN (...)` was correct and
tested. The gate was `ExplorerPage`'s `collapseRoutine`, a session `useState`
defaulting to `false` and flipped only by an unlabeled toggle in the top bar.
Muting never touched it. So a mute recorded a verdict and changed nothing, while
the UI copy promised its events "disappear from the grid immediately".

**Mute is a filter, and filters apply on creation.** Collapse is now derived
from the routine-disposition set rather than opted into; the toggle became a
*reveal* override. The override is stamped with the disposition-set signature it
was made against and expires when that set changes — without that stamp, an
analyst who revealed routine events once would silently defeat every subsequent
mute, which is the same symptom one step removed. Precedence lives in
`frontend/src/lib/routineCollapse.ts` (unit tested) rather than inline in the
page, because the agent's "apply to Explorer" seam depends on it: an agent
finding that ran *without* collapse must still reproduce uncollapsed when mutes
exist, so agent applies write an explicit override. The copy needed no
weakening — the fix made both claims true. Empty scope always resolves to
`false`, so unmuting the last template cannot leave a stat claiming zero
collapsed events.

**The sibling this exposed, which was the more serious bug.**
`bulk_annotate_by_filter` was the only filter-driven endpoint that never
resolved the routine scope — `list_events`, `get_histogram` and `export_events`
all did. So Explorer → select all → Tag wrote annotations onto muted events the
analyst could not see, while the confirm dialog's count came from the collapsed
query. Durable forensic records for events outside the displayed set. The
frontend had been correct all along: `BulkActionBar` receives `effectiveFilters`
and `serializeEventFilterFields` emits `collapse_routine` — pydantic's default
`extra="ignore"` silently dropped it, so the caller got no error and no effect.
Latent before (collapse was default-off, few users had a divergence), routine
after the #147 fix, which is why the two ship together. Exactly the failure
shape as the earlier `annotated` regression on the same endpoint, so the
regression test is written as its sibling. `ANOMALY_DETECTION.md` now states the
invariant: a filter-driven endpoint that skips this resolution is a bug, not a
missing feature.

## Session 80 — 2026-07-21: PR145/146 review — the degradation that left no trace

Review of the tool-result fidelity branch. The feature held up — the overflow
ladder's attempt bound is exactly tight (two tier drops plus two compactions,
so no interleaving can exit the loop without a terminal event), and the
determinism property is real and tested. Six fixes landed.

**A fidelity drop was invisible the moment the page reloaded.** Compaction
writes a message row *and* an `agent.compaction` audit row; the tier drop only
yielded an SSE event. So a reopened conversation showed a thinner investigation
with nothing in it explaining why — the reader would have had to know the
deployment's `tool_fidelity` at the time of the turn to reconstruct it, which is
exactly the inference the forensic requirement exists to remove. A drop now
writes a `role="fidelity"` row (`tool_result = {from, to, attempt, reason}`) and
an `agent.fidelity_drop` audit row. No migration — `AgentMessage.role` is free
text. The row also settles the second finding: in the *message* log, marker rows
(`compaction`, `fidelity`) are what separate a retry's re-executed tool rows
from the attempt before them, the job `attempt` already does on the audit side.

**A `note` claimed a reduction that had not happened.** The event-returning
tools passed `reduced=bool(page.events)`, so any non-`full` tier told the model
"attributes are omitted" even for events that had none — an untruth in an
exported record, and the same failure mode `_listing` avoids by reporting
`returned` beside `total`. `_event_reduced` now answers per event: attributes
dropped, message dropped, or message truncated.

Three consistency fixes: `FIDELITY_TIERED_TOOLS` moved from `tools.py` to
`fidelity.py` (it is a policy fact, and it was forcing a function-body import to
dodge the cycle); the tier is now a required argument on `_deflate_findings` and
`_slim_event`, so `get_event` states its exemption at the call site instead of
inheriting it from a default; and the two deflators no longer disagree about
what an omitted tier means. Doc corrections: the design spec's Files table
listed a `docs/ROADMAP.md` item that was never added, and `CLAUDE.md`'s `docs/`
map had no line for `docs/superpowers/`.

**Second review pass, same branch — five more.** The honesty rule the first pass
applied to the event-returning tools had not reached the anomaly path:
`_deflate_findings` treated the mere *presence* of an `event` key as a
reduction, so a finding whose example event was `None` (resolution failed) or
held nothing but a short `message` still carried the "call get_event for the
full record" note. `_finding_event_reduced` now answers it properly — and it is
a different question from `_event_reduced`, because a finding loses the whole
event object rather than just its attribute bag, so a bare timestamp going
already counts.

`auto` was a two-way switch on one threshold, which gave an 8k model and a 64k
model identical treatment and made `auto` with no configured window
indistinguishable from picking `message`. It is now a graded ladder (≥100k
`full`, ≥32k `message`, below that `minimal`, unset `message`), with the second
threshold taken from the same measurement as the first: the seven-detector
sweep's ~34k tokens of payload *is* a 32k window.

**A retried turn's writes were unexplained.** Re-running a turn re-executes its
tools, and two of them write — so a sweep that overflows twice can leave three
`DetectorRun` rows for one analyst question, indistinguishable in the Analysis
page from an analyst scanning three times. They are not suppressed (the scans
really ran; hiding a re-execution is what the marker rows exist to prevent) but
tagged: `AgentScope.attempt` rides into `_persist_detector_run`, which records
`params["agent_retry_attempt"]` when non-zero. Duplicate annotation proposals
stay plain — each is an action the analyst decides individually, and the marker
row above them already explains the pair.

`get_last_agent_usage` discarded usage measured before a `compaction` but not
before a `fidelity` row, though a tier drop invalidates a measurement for the
same reason: every tool result from there on is smaller, so the next turn's
estimate ran high and could spend a summarizer call the drop had already made
unnecessary. Both marker roles now count (`_AGENT_MARKER_ROLES`). Finally,
`FINDING_MESSAGE_TRUNCATE` became `SLIM_MESSAGE_TRUNCATE`: since the first pass
it also caps ordinary search hits, not just findings.

## Session 79 — 2026-07-20: PR144 review — what the relocation forgot to relocate

Review of the A13 branch before merge. The three levers held up; five fixes
landed, one of them a real regression the branch had introduced.

**The external `/mcp` surface lost guidance it used to have.** `mcp_http.py`
builds its server through the same `build_tool_server`, so external clients were
getting the slimmed, prose-free `$defs` — but the relocation target was
`runtime.SYSTEM_PROMPT`, which they never see. They paid the whole cost of the
transform and received none of the compensation. `FastMCP(instructions=...)` is
their only channel, and the session-78 note had correctly identified it as such
without drawing the conclusion. `SPEC_REFERENCE` and the new `RESULT_FORMAT_NOTE`
are now appended there, sharing the exact strings the system prompt composes
from, so the two surfaces cannot drift apart. The columnar result encoding
reaches `/mcp` the same way, for the same reason: one wire format, not two.

**`total` was describing a set the model had not been given.** The new
`MAX_LIST_ROWS = 200` cap sliced the rows but left `"total": len(rows)`
untouched, so a case with 5,000 annotations reported 5,000, returned 200, and
offered nothing to tell the two apart. That is precisely the silently-partial
set the system prompt's evidence rule exists to prevent. All seven capped list
tools now go through `_listing`, which reports `returned` next to `total`, and
the prompt tells the model to say so when they differ.

**The null-arm collapse is now scoped to optional fields.** Dropping the
`{"type":"null"}` arm is sound because the field is optional; on a *required*
field the arm is the whole statement that an explicit null is admissible, and
removing it would advertise a contract narrower than pydantic validates —
actionable by any provider that enforces the advertised schema client-side.
Nothing required is nullable today (checked), so this changes no current schema;
it makes that a property of the transform instead of a coincidence.

Two smaller ones: `compare` (all three kinds) and `run_anomaly_detector` were
the two dict-per-row results the branch had missed — the detector's copy is
reshaped *after* `_persist_detector_run` stores the dict-row payload the
Analysis page reads back. And the spec reference rendered enum values with
Python's `repr` (`'count'`), sitting in a block otherwise full of JSON the model
is meant to copy from; now `json.dumps`.

Re-measured after all of it: 28 tool schemas 32,863 chars, core profile 15,225,
system prompt 11,916 (it grew by the 649-char format note, which is now stated
once and shared rather than inlined). Fixed overhead ~11.2k tokens for the full
catalog, ~6.8k for core. Ten new tests; suite green.

Still not verified, unchanged from session 78: no real model has read the
relocated prose. Both surfaces now need that check — one in-app conversation and
one external MCP client — before tagging.

## Session 78 — 2026-07-20: A13 — halving the agent's per-request context (release 1.4.1)

Roadmap A13, all three levers, closing the item. The premise: tool schemas and
the system prompt are resent with *every* model request, so their size is a
per-request tax rather than a one-off. Measured first, before touching
anything — the 28 tool schemas serialized to **69,382 chars (~17.3k tokens)**,
over half a 32k local-model window before the analyst had typed a word.
`FilterSpec` alone (3.8k chars) was re-serialized into 12 tools; `propose_chart`
cost 11.4k on its own.

**(a) Schema slimming + prose relocation** — new `agent/schema_slim.py`.
Mechanical slimming (drop pydantic's `title`, collapse `anyOf[T, null]`, drop
`default: null`) took 22% off. The rest came from *relocating* the repeated
`$defs` prose: `FilterSpec`/`ChartSpec` field descriptions were being paid
twelve times per request, and are now rendered once into `SYSTEM_PROMPT` by
`spec_reference_block`. Relocated, never deleted — descriptions are what a
small model uses to pick a tool, so the block is **generated from the models'
own `Field(description=...)` values** and can't drift from them. Result:
69,382 → **32,994 chars (−52%)**.

The transform targets `Tool.parameters` (what `tools/list` advertises) and not
`Tool.fn_metadata` (what FastMCP validates against) — we advertise slim and
validate full. It lives in `build_tool_server` rather than a pydantic-ai schema
transformer so it applies identically across providers (the OpenAI profile
already strips `title`; the Anthropic profile strips nothing) and covers the
external `/mcp` surface.

The first version of `slim_schema` had a real bug worth recording: it stripped
every key named `title`, including the **parameter named `title`** that
`propose_finding` and `propose_chart` take — leaving those tools advertising a
`required: ["title", ...]` whose property no longer existed. The fix is to treat
`properties`/`$defs` as name→schema maps whose keys are user data. There was no
test asserting anything about generated schemas at all, which is exactly why
the overhead had been free to grow; `tests/test_agent_schema.py` now covers the
transform, the callability round-trip, and a **40,000-char budget guard**.

**(b) Tool profiles** — `ToolInfo.tier` (`core`/`extended`), surfaced on
`GET /api/agent/info`, driving Core / All presets in the tool-selector
popover. Deliberately *not* new state: a preset just computes a deny list and
flows through the existing `users.preferences["agent_disabled_tools"]` path, so
no migration. Because a disabled tool is removed from the request rather than
stubbed, "Core" reclaims context directly — 11 tools, **15,291 chars (~3.8k
tokens)**, and a total fixed overhead of ~6.6k tokens including the prompt.

**(c) Compact tool-result encoding** — new `agent/encoding.py`. Results live in
the persisted history and are resent on every later turn, so their cost
compounds; dict-per-row lists were repeating each key name once per row.
`columnar`/`columnar_auto` state the columns once and return rows positionally.
The biggest single win was `field_timeseries`, where all series share one time
axis: hoisting it into `bucket_starts` took a capped 8×60 result from 26,054 to
4,175 chars (−84%). `field_terms` −32%, `field_pivot` −44%, `time_punchcard`
−65%, `search_events` −31%.

Three constraints shaped this. Values pass through **byte-identical** — a
forensic result must stay reproducible, so this is a reshaping and the existing
`MAX_*` caps remain the only lossy step. Each result carries its own `columns`
legend rather than relying on a convention in the prompt, because persisted
history is replayed verbatim with no migration hook: one conversation can hold
both old dict-shaped and new columnar results, so every result has to be
readable on its own terms. And the re-encoding happens at the agent boundary
(`_columnize` in `agent/tools.py`), never in `db/queries.py` — those same
methods serve the Explorer and Visualize HTTP APIs, whose shapes the frontend
depends on. Checked before changing anything: the frontend reads exactly two
tool-result keys (`propose_annotation`'s `proposal_id`, `propose_chart`'s
`ok`), both untouched, so this is invisible to the UI.

Two incidental findings, both folded in. `FastMCP(instructions=...)` is never
sent on the internal path — `MCPToolset` needs `include_instructions=True` — so
it only ever reached external `/mcp` clients; kept (it is genuinely their only
steer) and commented so it isn't mistaken for the agent's live instructions.
And six metadata list tools returned **unbounded** row lists into the history;
they now cap at `MAX_LIST_ROWS = 200`.

Released as **1.4.1**. Worth noting the semver stretch: the Core preset is a
user-visible feature and the unreleased log already held five more, so this is
a minor version's worth of change carried on a patch number at the maintainer's
request.

## Session 77 — 2026-07-20: PR142 second review round — the two paths that missed the guard

A second review of PR142 before merge. The PR's whole thesis is that a chart
request must never succeed *quietly wrong*, and it enforces that in three
places (`_check_chart_field`, the `count == 0` raise, the scatter raise). Two
paths had not been given the same treatment:

- **Numeric mark over a `time:` field rendered a blank box.** `time:date` and
  `time:year_month` are `interval`, so `chartTypesFor` offered `histogram` and
  `scatter` — but `VisualizePage`'s numeric probe is disabled for time fields,
  and every render gate is `data && <Chart/>`. No spinner, no message, no
  chart. New `chartTypesForField(scale, field)` (`viz/lib/chartOptions.ts`)
  drops the numeric/scatter marks for a `time:` field and now backs the
  dropdown, the scale-change clamp and `defaultChartTypeForScale`. A saved
  chart or URL can still carry the pairing (the time-field effect is gated on
  `field !== autoProbedField.current`, which a restored config never trips), so
  the canvas also grew an explicit branch saying the field has no numeric
  values — the same thing `propose_chart` tells the agent.
- **`time:` tokens silently no-op'd in the detectors.** `anomaly_stats._col_expr`
  has no `time:` branch, so `run_anomaly_detector(fields="time:hour_of_day")`
  fell through to `attributes['time:hour_of_day']` — empty for every row. The
  detector finished clean with zero findings, reading as "nothing anomalous"
  rather than "never scanned". `list_fields` advertises these tokens (they are
  real for charts and filters), so the scoping had to be stated: it now says so
  in the docstring, and `_reject_time_fields` guards `fields`/`series_field`
  with an error pointing at frequency / interval_periodicity, which bucket time
  themselves.

Smaller, both naming-honesty rather than behaviour:

- `VIZ_COMPARE_MAX_{TERMS,BINS,BUCKETS}` → `VIZ_MAX_*`. The rebuilt
  `propose_chart` routes its non-compare paths through them too, so "COMPARE"
  in the name had stopped being true.
- `field_pivot`'s `x_distinct`/`y_distinct` carry two units — a *measured*
  distinct count the axis may have been truncated against, or the size of a
  bounded `time:` domain charted whole. Added `x_bounded`/`y_bounded` to say
  which, echoed in `propose_chart`'s summary and used by the caption builder to
  pass `undefined` for a bounded axis, so "top N of M" can never claim a
  truncation that did not occur. Additive response change; the hand-mirrored
  `FieldPivotResponse` was updated alongside (exactly the duplication
  Milestone 3's `openapi-typescript` item would remove).

Verification: backend 1363 passed (was 1359), frontend 386 passed (was 383),
ruff + oxlint + `tsc -b --noEmit` clean, `gen_chart_meta.py` still idempotent.
The pivot test fake derives `*_bounded` the same way the real service does, so
it cannot drift into claiming a measured count for a static domain.

## Session 76 — 2026-07-20: PR142 review fixes — virtual time fields reach the analyst

Review of PR142 (chart proposals + virtual `time:` fields) found the
analyst-facing half of the feature unwired: `viz/lib/timeFields.ts` was
generated and imported by nothing, so the Visualize picker showed raw tokens
and a weekday axis read "1".."7". `viz.py`'s own docstring justifies exposing
time fields to analysts because "anything the agent can chart the analyst has
to be able to rebuild by hand" — so this closed that gap rather than deleting
the generated module.

- **New `viz/lib/fieldDisplay.ts`** — token→label, value→display form, used by
  the picker, all six charts and the compare editor. The load-bearing rule:
  only text goes through it; keys, `scaleBand` domains, colour-map keys, sort
  comparators and click payloads stay on the canonical value, the only form
  that round-trips into a filter, URL or saved chart.
- **Three silent-wrong-answer bugs found while wiring it**, each verified to
  fail against the unfixed code before fixing: `BarChart`'s `sort="value"`
  ordered by display label (defeating the zero-padding `_time_fields.py` pays
  for — the axis reordered to `Mon, Sun, Tue, Wed`); `Legend` reports
  `key ?? label` to click-to-filter and `LineChart` passed no `key`, so
  clicking "Mon" filtered on a value that cannot exist; and
  `chartTypesFor(scale)[0]` is the *field-free* `time` histogram for every
  scale, so a scale switch silently dropped the picked field
  (`defaultChartTypeForScale` added).
- **Auto-probe bypass.** A `time:` field's SQL yields zero-padded strings, so
  `field_numeric_stats` could only ever report `count: 0` — the scan was pure
  waste and landed the analyst on nominal/bar, contradicting the statically
  known scale. `VisualizePage` now takes the scale from `TIME_FIELDS`.
- **Honest field stats.** `describe_field` reported a raw count under
  `coverage`, which means a 0-1 fraction everywhere else in the API →
  `non_empty_total`. `viz/fields` claimed `coverage: 1.0` for virtual fields,
  false whenever a timeline holds undated (sentinel) events → `null`, as is
  `distinct` for the unbounded date parts. A bounded `time:` pivot axis
  silently ignored `limit_x`/`limit_y` (53×31 = 1643 cells into the model's
  context with the limit accepted and never applied) → warns, stops echoing
  the limit, reports `matrix_size`.
- **A review finding that was wrong, and reverted.** The legacy `compare_*`
  shim maps a spec with no `comparison_filters` to `{mode: "off"}`; review
  called that infidelity, since the retired *backend* validated it as a
  baseline comparison. A test in PR142 already documented the counter-argument:
  `specToChartConfig` is what drew the card, and it drew one layer. The card is
  the artifact, so the translation follows the card. Both sides reverted, the
  comment extended so the next reader doesn't repeat the mistake.
- **Compare editor** offers a bounded time field's domain as labelled choices
  instead of free text, which invited typing "Mon" and building a filter that
  matches nothing.
- Smaller: `_capped` gained a floor so clamped `buckets` warns like every other
  option; `_check_chart_field` accepts the spellings `resolve_time_field`
  resolves; the field-vocabulary cache uses a `None` sentinel so an empty
  timeline is cached; `gen_chart_meta.py` emits camelCase `readsOptions` to
  match `ChartOptions` (snake_case matched no TS key).

Verification: backend 1359 passed, frontend 383 passed (was 346), ruff and
oxlint clean, `gen_chart_meta.py` regeneration idempotent by hash. Shared test
helpers extracted (`test/helpers/resizeObserver.ts`, `radix.ts`) — no existing
test drove a Radix Select, which is why the field-picker page test is new
ground.

## Session 75 — 2026-07-20: agent-tool feasibility items + roadmap triage

Docs-only session (no code changes).

- **Agent-tool feasibility → roadmap.** Assessed adding web search / Shodan /
  CyberChef-class tools to the agent: the toggle/audit/disclosure machinery is
  ready, the open work is policy. A8 expanded with the concrete requirements
  (OPSEC leak rationale, timestamped raw-response provenance, governance +
  disclosure reuse, AGENT.md sandbox-invariant update); new A12 (local
  CyberChef-class transforms — native, deterministic, offline, no OPSEC gate,
  can ship before A8).
- **Context-overhead measurement → A13.** The 27 tool schemas serialize to
  ~15k tokens (plus ~1.2k system prompt), resent every request — half a 32k
  local-model window. Dominated by `FilterSpec` inlined into ~14 tool schemas.
  Three levers recorded: `$defs`/`$ref` schema dedup, lean tool-profile
  presets, and header-once columnar tool-result encoding (results are resent
  in history every turn). Negative decision recorded: agent prose stays
  verbose — findings feed forensic reports and the transcript is custody
  record, so caveman-style terse-output schemes were rejected for output.
- **Roadmap triage.** `ROADMAP.md` reduced to open items only, per its own
  delete-when-done rule: shipped narrative removed (audit C1/H1–H4 block,
  Phase 3 Steps 1–2, Milestone 4's shipped-detector prose, Milestone 8's v1/v2
  ship notes + A9 — all live canonically in `PROGRESS.md` /
  `ANOMALY_DETECTION.md` / `AGENT.md`); six decision-records-as-checkboxes
  (M15, M23, M26, W4, A11, confirm-proposal crash-gap, events.py split) moved
  into an "out of scope & standing decisions" section with explicit revisit
  triggers; W7's double entry deduped (canonical: Phase 3 Step 3); stale
  events.py line count updated (1500+ → ~3100). User decision recorded:
  porting the remaining vendored `*2timesketch` converters to native Parquet
  is demand-driven, not planned — the vendored scripts are a permanent
  minimal-dependency alternative, not a porting queue.

## Session 74 — 2026-07-20: agent panel UX (four reported issues)

- **Stop button missing after navigating away.** `_active_turns` was a bare
  set the client never saw, so a reopened panel showed a usable input that
  409'd on every send. It is now a dict of per-conversation reservations
  (cancel `asyncio.Event` + start timestamp), surfaced as `active` on every
  conversation payload (polled while true), and `POST .../cancel` sets the
  event for the turn generator to notice. Aborting the client fetch alone was
  never enough — with no output flowing, Starlette may not notice the
  disconnect for a while and the turn keeps spending tokens. Cancel signals
  the generator rather than killing the task, so what the agent already wrote
  is persisted as a `[stopped]` assistant message: a stopped turn stays part
  of the record. The stop itself is audited (`agent.turn_cancelled`).
  - Review of the first cut caught the interesting one: the cancel check
    started out in the *caller*, which broke out of the turn generator and so
    closed it with a `GeneratorExit`. That derives from `BaseException`, so
    neither `except Exception` handler ran and the streamed text was silently
    dropped — the opposite of the guarantee being advertised. The check moved
    inside the generator, where a plain `return` persists and unwinds
    normally. The bug survived the first round of tests because they poked
    `_active_turns` directly and never drove the generator; there is now a
    test that actually cancels mid-stream and asserts on the message rows.
  - A stranded reservation (ASGI task dying between the endpoint reserving
    and the generator's first step) used to be an invisible 409; now that
    `active` is user-visible it would have been a permanent Stop button on a
    dead conversation, so reservations past `_TURN_STALE_AFTER` get pruned.
- **Tool selector vanished after the first message.** It was gated on
  `!activeId` because the tool set was frozen at creation. New audited
  `PATCH .../conversations/{id}` lets it be adjusted; the change applies from
  the next turn and never rewrites what earlier turns could do. Making the
  popover always-visible surfaced a latent bug worth noting: its mount-time
  seeding from the user's saved defaults would have overwritten an existing
  conversation's actual tool set *and* persisted that through the new PATCH.
  Hence `seedFromDefaults`. Review turned up the mirror-image leak — an
  unrestricted conversation reports `disabled_tools: null`, and the local
  state sync skipped those, so switching conversations kept the *previous*
  one's restriction and the next toggle PATCHed it onto the new conversation
  with a misleading audit row. Fixed with `?? []` plus keying the popover on
  the conversation id. `PATCH` is also a real partial update now: omitting
  the field no longer clears the tool set.
- **Panel not resizable.** `panelWidth`/`setPanelWidth` had been sitting
  unused in `stores/agent.ts` — wired up a drag handle copying
  InvestigatePanel's existing pattern verbatim.
- **Model was free text in the admin settings.** Now a dropdown fed by
  `POST /api/admin/agent-settings/models`, which reuses the availability
  probe's `GET /models` request (`availability.py::list_models` — same
  per-provider URL and Kimi auth quirks). It takes the *unsaved* form
  credentials so an endpoint's models show before committing it, falling back
  to the resolved config for the key, which the browser never holds. Env-pinned
  fields are deliberately not overridable per request: redirecting
  `api_base_url` while the key stays pinned would ship a key this API never
  discloses to a caller-chosen host. Free text remains the fallback whenever
  the listing is empty, and stays reachable for models a listing omits.
- **Finding filters were transient.** "Apply to Explorer" only writes the
  URL, so a useful filter set died with the conversation. Finding cards now
  also save one as a View via the Explorer's own `SaveViewDialog`.


## Session 73 — 2026-07-20: PR #140 review fixes + release 1.4.0

Merged `main` (persistent OPSEC notice / tool-selector popover) into the W6+A9
branch — clean auto-merge; `AgentPanel.tsx` took both sides, since main owned
the conversation-creation and footer regions while this branch owned the
propose_chart pairing and chart render branch.

Then a code review of the branch, fixed in order of severity:

- **Mute could collapse events it didn't announce.** The Templates tab offered
  every `attr:*` field, but a mute always resolved through
  `template_hash NOT IN (...)` — hashed over `message` alone. Muting an
  `attr:raw_line` shape therefore hid an unrelated set. `ANOMALY_DETECTION.md`
  had already specified message-only muting; the code just never enforced it.
  Now enforced in the UI (disabled, explained control) *and* in
  `_validate_scope` (`details.field != "message"` → 422).
- **Agent chart bin counts were dropped.** `specToChartConfig` routed
  `spec.limit` to `options.topN` for numeric kinds, but the histogram path
  reads `options.bins` — the agent's requested binning silently vanished and a
  meaningless `topN` rode into "Save"/"Open in Visualize".
- **`template_id` was not reserved** against canonical field mappings, which
  resolve *before* column tokens — a mapping of that name would shadow the
  facet and redirect drill-to-grid onto an unrelated attribute.
- **`int(d.value)` was unguarded** in `_resolve_routine_collapse`: one
  malformed `log_template` disposition would 500 the grid, histogram *and*
  export. Now `isdigit`-filtered — an unparseable row collapses nothing.
- **`list_log_templates` scanned the table twice**, re-running the regex chain
  and GROUP BY purely to count. Now one scan via `count() OVER ()`, the same
  window trick `QueryService._field_terms_body` uses.
- **The bloom skip index was dead weight**: `has({ths}, template_hash)` is not
  an indexable form (ClickHouse's `has` support is for array *columns*).
  Rewritten as `template_hash IN {ths}` on both count paths, with a comment so
  it doesn't get "fixed" back to the file's `has(...)` convention.
- Version literal replaced with `TEMPLATE_NORMALIZE_VERSION`; empty-value guard
  applied to `message` too; muted templates now listed from the dispositions
  rather than the current page (a mute outside the top-N was unreachable, so
  un-mutable); per-row mute spinner; saved-chart list invalidation; a
  `pendingChart` buffer that could pair a failed proposal's args with a later
  result; and a doc comment describing a fallback that did not exist.

Suite: 1170 backend passed, 315 frontend passed. The 10 failures in
`test_admin_api`/`test_agent_api`/`test_embeddings_capability`/`test_uploads`
are pre-existing dev-`.env` config collisions — verified identical on a clean
stash of these changes.

Cut **1.4.0** (not 1.3.1): the release is six new features and no breaking
changes, which is a MINOR bump under the semver policy the CHANGELOG declares.

## Session 72 — 2026-07-20: A9 agent viz parity (Phase 3 Step 2)

Gives the AI agent the same charting the analyst has on the Visualize page —
see `docs/AGENT.md` "Tools" for the full contract.

- **Five read tools** (`agent/tools.py`): `field_timeseries`, `time_punchcard`,
  `field_pivot`, `field_scatter`, `compare` (kind = time/terms/numeric, two
  independent `FilterSpec` layers) — thin wrappers over the same
  `db/queries.py` methods the Visualize page's endpoints call, threadpooled,
  each with its own cap tighter than the page's own UI bounds (`VIZ_*_MAX_*`
  constants) since viz series are dense and every point counts against the
  model's context window.
- **`propose_chart(title, description, spec)`**: the charting analog of
  `propose_finding` — `spec` is a `ChartSpec` (kind = terms/numeric/
  timeseries/punchcard/pivot/scatter/compare_time/compare_terms/
  compare_numeric). Validates by *executing* the underlying query (same caps
  as the read tools) and returns summary stats; writes nothing — no proposal
  row, unlike `propose_annotation`, since the only write in this flow is the
  analyst's own "Save" click against the existing `savedChartsApi.create`.
- **Frontend mapping** (`frontend/src/api/agent.ts`): `specToChartConfig`
  maps the backend `ChartSpec` onto the Visualize page's own `ChartConfig` —
  backend-opaque, same seam as `SavedChart.config` (the backend never learns
  the frontend's chart shape). `histogramToCompare` moved from `VisualizePage.tsx`
  into the shared `chartConfig.ts` so both the page and the new card use one
  copy.
- **`ChartProposalCard.tsx`**: renders in the agent chat panel — fetches live
  through `vizApi` (not the tool_result echo, so the chart stays consistent
  with current data/dispositions) and reuses the Visualize page's pure chart
  components. "Open in Visualize" is a route link carrying the mapped
  `ChartConfig` + filters as URL params; "Save" is the analyst's own
  `savedChartsApi.create` call. `AgentPanel.tsx` gained a `"chart"` `ChatItem`
  kind — `propose_chart`'s call row (title/description/spec) and its paired
  result row (`ok`) are matched up (both in `itemsFromMessages` for persisted
  history and `foldStreamEvent` for the live stream) before a card renders; a
  failed spec (unknown kind, missing required field) surfaces as a tool error
  with no card.

Tests: `tests/test_agent_tools.py` (20 new cases — read-tool cap clamping,
`propose_chart` dispatch/validation/cap clamping, registry parity),
`frontend/src/test/agent.test.ts` (`specToChartConfig` round-trip through the
same URL-param path "Open in Visualize" uses),
`frontend/src/test/chartProposalCard.test.tsx` (live-fetch smoke render per
kind, Save, Open-in-Visualize link), `frontend/src/test/agentPanelChart.test.tsx`
(call+result pairing — ok:true renders one card, ok:false or a missing result
renders none). Full backend suite and full frontend suite (35 files, 312
tests) green.

## Session 71 — 2026-07-20: W6 log template clustering (Phase 3 Step 1)

Structurally-distinct log-line shapes, browsable and mutable independent of any
detector run — see `docs/ANOMALY_DETECTION.md` §14 for the full design.

- **Schema**: `template_hash UInt64 MATERIALIZED cityHash64(<normalize-expr>)` on `events`
  (`db/clickhouse.py`), same shape as `search_blob`: bloom-filter skip index, async
  `MATERIALIZE COLUMN`/`INDEX` backfill, correct immediately on old parts (MATERIALIZED
  computes on read). No stored normalized-text column — reconstructed on demand via
  `any(message)` through the same expression.
- **Normalization** (`db/_template.py`): versioned (`TEMPLATE_NORMALIZE_VERSION = 1`),
  append-only regex chain masking timestamp/UUID/MAC/IPv6/IPv4/hex/digit-run substrings,
  RE2-safe. Field-configurable — the module builds the expression over any SQL expression
  a caller passes, not hardcoded to `message`, per user pushback during planning that a
  hardcoded field would violate the field-agnostic detector convention (Milestone 4).
  Digit masking is unconditional (confirmed decision): "HTTP 404"/"HTTP 500" collapse to
  one template; escape hatch is a future `template_hash_v2` column, never an in-place
  `ALTER MODIFY` of v1's expression (would silently split identity across old/new parts).
- **Browsing**: `StatisticalAnomalyService.list_log_templates` (`db/anomaly_stats.py`) —
  indexed fast path for `field="message"`, unindexed inline-hash path for any other
  `_col_expr`-resolvable token (the field-agnostic proof); `only_new` + a baseline's
  `baseline_end` is the entire "novelty" story (`HAVING first_seen >= baseline_end` on a
  grouped subquery, no anti-join, no BH-FDR/Finding machinery — a browser, not a scored
  detector). `GET /{case}/timelines/{tl}/log-templates` endpoint.
- **Facet**: `template_id` token in `db/_columns.py::SYNTHETIC_COLUMN_EXPRESSIONS`
  (`toString(template_hash)`) — resolves through the same allowlist every other field
  token uses, so the grid filters to one template exactly like any other field.
- **Mute + collapse**: `kind="routine"`, `detector="log_template"` disposition
  (`api/routers/dispositions.py`) — value = decimal template id, `details` snapshots the
  audit record. Deliberately **no occurrence-materialization job** (unlike
  `sequence_motif`): membership is a direct `template_hash IN (...)` predicate, no aux
  table needed. New `EventQuery.exclude_template_hashes` (`db/queries.py`),
  `ClickHouseStore.count_routine_collapsed` computes the *union* of motif- and
  template-collapsed events via one `UNION ALL ... uniqExact` query (a naive sum would
  double-count an event covered by both mechanisms) — `_resolve_routine_collapse` in
  `api/routers/events.py` now returns a `RoutineCollapseScope` split by detector; agent
  `_build_query` mirrors the same resolution for search/grid parity.
- **UI**: `TemplatesView.tsx` — new **Templates** sub-tab under the Investigate panel's
  Patterns tab (user decision: panel tab bar was already tight, not a 6th top-level tab).
  Shares the routine-dispositions cache key with `PatternsView` (both fetch unfiltered,
  split client-side by `detector`) so `useDisposition`'s hardcoded optimistic-update key
  keeps working for both mechanisms without one overwriting the other's cache entry.

Tests: `tests/test_template_expr.py` (regex-chain unit), `tests/test_template_clickhouse.py`
(19 live-ClickHouse cases — grouping, hashing, upgrade path, browsing, facet filter,
mute/unmute round trip), `tests/test_anomaly_stats.py`/`test_columns.py`/`test_queries.py`/
`test_dispositions_api.py`/`test_events_router.py`/`test_agent_tools.py` (unit extensions),
`frontend/src/test/templatesView.test.tsx`. Full backend suite (1149 passed, 10 pre-existing
unrelated env-config failures excluded) and full frontend suite (33 files, 292 passed) green.

