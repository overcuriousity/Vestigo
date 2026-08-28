# Vestigo Implementation Progress

Append-only session log — what changed and why, newest first. This file keeps the recent
sessions only; older ones live in git history, and every release is summarized in
`CHANGELOG.md`. Plans belong in `ROADMAP.md`, not here.

Last updated: 2026-08-28 (session 195 — one field picker everywhere).

## Session 195 — 2026-08-28: one field picker everywhere

Six surfaces asked "which field?" six different ways, and none of them let an analyst type.
Two native `<select>`s in Investigate (log templates, sequence patterns), one in the export
dialog, two Radix `Select`s in Visualize and the compare-filter editor, and a bare inline
`<select>` for the method knobs. On a timeline carrying a few hundred `attr:*` tokens, a
dropdown is the wrong control — and none of the six could reach a field the cardinality
inventory had not caught up with yet.

- **`components/ui/FieldCombo.tsx`** — the one control. A text input that opens its full list
  on focus (browse, exactly as the dropdowns did), filters as you type against both the token
  and the label, walks with ↓/↑, and commits whatever you type when nothing matches. The box
  shows the raw token because that is what every caller stores and what an analyst would type;
  the pretty label and its hint (cardinality, `(time field)`, distinct counts) live on the
  rows. Escape reverts a draft without the caller ever seeing it.
- **Free entry, disclosed.** A committed token that is not in `options` renders a muted "not
  in this timeline's reported fields" note. It never blocks — the inventory legitimately lags
  a source ingested a minute ago — but a typo used to commit silently and come back as an
  empty chart or a scan that found nothing, with nothing naming the cause.
- **`lib/useAnchoredDropdown.ts`** — the portaled fixed-position anchoring (flip above when
  there is no room below, re-place on scroll/resize) extracted verbatim from `TagInput`, which
  had the only copy. `TagInput` now calls the hook, so there is one implementation rather than
  two to keep in step.
- **Six call sites converted.** `PatternsView`'s `<optgroup>` Standard/Dynamic split survives
  as the combo's section headers; `MethodFieldSelect` uses the borderless `inline` variant so
  it still reads as part of the knob's sentence; `ExportDialog`'s placeholder still names which
  of its three states the list is in. `CompareFilterEditor`'s *value* control stays a `Select`
  on a bounded `time:` field — its domain is complete and its canonical values are opaque
  ("1" is Monday), which is the one place free text builds a filter matching nothing.

Thirteen new tests pin the contract; the suite is 981 green.

## Session 193 — 2026-08-28: PR #305 review fixes, and a CI job that could not pass

### PR #305 review fixes (the chart lane's last unwired surface)

Three findings from the review of the session-192 branch.

- **The Visualize page never opted into `busyRetry`.** Its eleven gated aggregations — more
  than the rest of the app together — fell back to the global `retry: 1`, so a busy lane
  (a story export holding foreground slots, or three other panels) turned a 5-second wait
  into an error toast where the old unbounded queue had always produced an answer. Every
  *smaller* surface had been wired up; the biggest one was missed, which is the shape of
  gap a per-call-site opt-in with no type behind it produces. `busyRetryCoverage.test.ts`
  now fails any component that issues a `_foreground_scan` call through `useQuery` without
  it.
- **The busy badge only rendered under `isLoading`.** False the moment a key has data, so a
  filter change on a chart that was already drawn retried silently for four minutes behind
  stale marks and then failed. `TimelineHistogram` had added a `waiting && isFetching`
  overlay for exactly this; `ChartCanvas`, `FieldHistogramModal` and the Visualize canvas
  had not. Also names the Visualize page's active query once — the spinner, the badge and
  the grouped box/violin case (which had no spinner at all, since `numericQuery` is
  disabled while `groupedOn`) now read the same one.
- **The chart lane's thread reservation is a bound, not a reservation.**
  `detect_scan_memory_budget` divides by `N + 2` so the lane's memory comes out of the
  detectors' share; `detect_scan_max_threads` still divides by `N` alone, so the lane's
  threads are *added* to a box a full heavy gate already saturates — `2 x 10 + 4 x 5 = 40`
  on 20 cores. Kept as is (closing it means halving every sweep on a box where no chart is
  open, and it is a quarter of the 8x it replaced) and the prose corrected instead:
  `_scan.py`'s two docstrings, `ANOMALY_DETECTION.md`, `DEPLOYMENT.md` and the sizing
  calculator now state the additive bound and point at
  `VESTIGO_STAT_SCAN_MAX_THREADS = cores / (N + 2)` for operators who want the strict
  version.

### CI's ClickHouse answered `/ping` and refused every query

The backend job had been red since 2026-08-26 and burned the runner's 6-hour default
timeout on every run, on `main` and on PR #305 alike. Not a regression in the branch under
review: `bcc3683` (PR #304) moved CI from `clickhouse-server:24` to `26.6.1.1193` to match
the tag `docker-compose.yml` pins, and images from ~25.x on ship a
`users.d/default-user.xml` that restricts the `default` user to `::1`/`127.0.0.1`. The job
reaches the service container through a published port, so its packets arrive from the
docker bridge and every query came back `401` / exception code 194 (REQUIRED_PASSWORD).
`docker-compose.yml` has mounted `deploy/clickhouse/allow-default-network.xml` against
exactly this since the image bumped; a service container starts before `actions/checkout`,
so there is no repo file to mount and CI never got the equivalent.

- **`CLICKHOUSE_SKIP_USER_SETUP: 1`** on both jobs' ClickHouse service. The entrypoint
  drops the drop-in entirely, which is the mount's effect without a file. Test-only —
  nothing in `.github/` is a deployment.
- **The reachability probe asks a question the failure could answer.** `/ping` is served
  before any user is resolved, so it said `Ok.` to a server that would refuse every
  statement — and `tests/conftest.py::pytest_configure` exists precisely so a run that
  cannot pass stops in a second instead of ending in a wall of red. It now runs `SELECT 1`
  as the configured user and reports ClickHouse's own diagnosis on failure.
- **`timeout-minutes: 60` on the backend job.** The suite runs in ~25 minutes; a hung test
  should not hold a runner for six hours to teach nobody anything.


## Session 192 — 2026-08-27: PR #306 review fixes (the chart lane's other half)

Seven findings from the review of the session-191 branch. The cancellation plumbing came
through clean; every real problem was in the *sizing* half of the change — the reservation
had been re-derived for memory and not for anything else.

- **The chart lane's threads were unaccounted.** `foreground_scan_settings()` reused the
  heavy `max_threads`, which is auto-sized as `cores ÷ N` precisely so a *full* heavy gate
  saturates the box (#301). Four chart slots at that width, each able to fan out two
  queries, put 8× a detector's threads on top of a box already fully committed — 100
  threads on 20 cores at the default concurrency. So four charts rendering during a sweep
  made every query slower, including the sweep, which is the opposite of what #300 is for.
  `detect_foreground_max_threads()` applies the same reservation the memory cap uses: the
  two slots the heavy divisor holds back, split across the gate. Not divided again by the
  fan-out width — two waves at half the heavy width is one heavy slot's threads, which is
  what the reservation already allows.
- **The busy retry window was seven times what it claimed.** `BUSY_RETRY_LIMIT` was sized
  against the 5 s `Retry-After` alone, but each attempt *also* parks for
  `FOREGROUND_WAIT_SECONDS` server-side before the 503 exists — so "about two minutes" was
  really about fourteen, and a wedged lane spent a quarter of an hour looking like a slow
  one. The wait is 5 s now rather than 30, which also fixes the second half of it: a parked
  caller holds one of anyio's threadpool tokens for the whole wait, so several panels
  retrying against a full lane could occupy the pool with *waiters* — including the poll
  `run_scan` uses to notice a disconnect, the very path `_kill_detached` avoids the pool for.
  Answering "busy" quickly frees the token and lets the (cheap) retry do the waiting.
- **The filter rail's autocomplete could raise a toast.** `vizApi.fieldTerms` became
  foreground-gated, so it can answer 503 — and that one query had neither `busyRetry` nor
  `silentError`, so typing in a filter field during a busy lane popped "scan lane busy" from
  a background suggestion nobody asked for. It usually never reaches a scan at all (an
  unfiltered call is served from the `field_stats` cache); this is the cache-gap path.
- **…and it paid for a scan it discarded.** The totals wave exists so the tail beyond the
  top-N can be reported *spillably*, but the autocomplete maps `values[].value` and throws
  `total`/`distinct`/`other_count` away. `field_terms(..., totals=False)` runs the top-N
  alone and reports the rows in hand; `other_count` is 0 rather than a subtraction against a
  total nobody measured, so nothing that renders an "Other" slice may use it.
- **`scan_exec._client()` built its store unlocked** from the bare daemon threads `_kill`
  runs on — and simultaneous disconnects are exactly the scenario that path exists for, so
  two of them each built a store and all but one leaked. One `threading.Lock`.
- **The halved heavy cap is documented, not reverted.** Moving the divisor to N + 2 cut
  every detector query's `max_memory_usage` in half at the default concurrency, permanently
  and whether or not anyone opens Visualize. That is the deliberate price of a lane that
  never queues behind a sweep, but it can turn a whole-corpus sweep that used to spill into
  one that hits `MEMORY_LIMIT_EXCEEDED` — so `docs/DEPLOYMENT.md` now carries an upgrade
  note saying so and pointing at the ceiling rather than at N.
- Sizing calculator: the chart lane's derived thread width is shown alongside its cap, and
  the cores line no longer implies a full box covers both lanes at once.

## Session 191 — 2026-08-27: PR #305 review fixes (scan admission classes)

Six findings from the review of the session-190 branch, plus the sizing calculator rework
that came out of the first one.

- **The chart lane could not spill.** The foreground cap was one heavy slot split four
  ways — with the default `stat_scan_concurrency=2`, `budget/12` against a detector's
  `budget/2` on `main`. That would be fine if a chart query could always spill, but two
  could not: `field_terms` carried `sum(count()) OVER ()` / `count() OVER ()` (a frameless
  window materialises every group after the GROUP BY) and `field_numeric_grouped` counted
  groups with `uniqExact` (a full in-memory hash set). Both died at `max_memory_usage` on
  exactly the high-cardinality field a chart is for. Both are now spillable `GROUP BY`
  aggregates — the trade `count_field_inventory` already documents — issued in parallel
  under the slot the caller holds, and the lane now reserves **two** heavy slots
  (divisor N + 2), so a chart is capped at half a detector's cap rather than a sixth.
- **`_run_parallel` dropped the scan context.** A bare `ThreadPoolExecutor.submit` starts
  with an empty context, so every fanned-out chart scan went out untagged and a disconnect
  could not `KILL` it. It runs under `copy_context()` now — which is also what keeps the
  two new parallel scans cancellable.
- **A dead ClickHouse turned a 499 into a 500.** `_kill` built its store lazily *outside*
  the best-effort `try`, so a connect failure on the first disconnect propagated out of
  `run_scan` and left the scan task with nobody to retrieve its result.
- **Nothing handled `run_scan`'s own cancellation.** On shutdown (or a failing `gather`
  sibling) the threadpool scan ran on holding its gate slot and its ClickHouse process.
  The `CancelledError` path now sets the flag and fires the `KILL` from a plain daemon
  thread — no event loop required, since the loop is what is going away.
- **The queue depth an analyst is shown.** `_waiting` was keyed by `id(gate)` and never
  pruned, so a collected gate's count could resurface under a recycled id; it is a
  `WeakKeyDictionary` now. And `ahead` was sampled at *entry* and reported up to 30 s
  later — it is counted where the deadline expires instead.
- **The frontend retried a busy lane forever.** No cap, no toast (`isScanBusy` was excluded
  from the error handler) and, in `TimelineHistogram`, no waiting text either, because
  `placeholderData` keeps `isLoading` false. A wedged lane therefore looked like a silently
  stale panel — the symptom #300 set out to remove. Retries stop after `BUSY_RETRY_LIMIT`
  (24 ≈ two minutes) and the 503 then surfaces as an error; the histogram shows "waiting
  behind N scans" over its stale bars while it retries.

The [sizing calculator](docs/sizing/index.html) now takes the hardware in hand — host RAM
and cores — and answers whether it is enough, then gives every memory value and setting
twice: the **minimum** that serves the described workload, and what the hardware supports
at **full spend** (as many scan slots as its cores and RAM allow without shrinking a query
below what a whole-corpus GROUP BY needs). A host that cannot host the stack at all, and
one that runs but spills every scan, are different verdicts and say so.

### Second review round

Six more findings on the same branch, all of them the *edges* of the two-class model rather
than its shape.

- **A fan-out spent its slot's budget twice.** The per-query cap is sized per gate *slot*,
  but `field_terms`' new totals wave, `field_numeric_grouped`, the violin's second wave and
  the compare layers each put two queries in flight under one slot at the full cap — a
  fully admitted foreground gate committing 2x what the heavy divisor reserved for it. The
  over-commit factor is exactly the fan-out width, so no gate sizing absorbs it:
  `_run_parallel` now declares its width to `scan_fanout` and the queries divide the slot's
  share (nested waves multiply). The `_run_serial` the first round added for the pivot
  stays, but as a latency/thread-count trade rather than the budget's only defence.
- **The disconnect KILL queued behind the scans it was freeing.** It went out through
  `run_in_threadpool`, and the situation a disconnect exists for is precisely the one where
  every anyio thread token is held by a scan parked on a gate. It uses the same plain
  daemon thread the `CancelledError` path already did.
- **Agent and MCP tools had no `ScanBusy` guard.** `chart_exec` got one for `propose_chart`;
  the thirteen other tool calls into gated aggregations did not, so a busy lane escaped as
  an unhandled `RuntimeError` — a 500 on the MCP surface. `chart_exec._scan` is now the
  shared `run_gated_scan` and every one of them goes through it.
- **A story export could fail a chart block.** An export is a job: no spinner, no request to
  answer 503 to, no retry. The bounded wait turned "the lane was busy for 30 s" into a
  failed block in an attested report. `unbounded_foreground_wait()` lets such a caller queue
  as it did before the gate existed.
- **The sizing calculator recommended hardware its own numbers said was too small.**
  `minimumPlan`'s working-set ceiling still divided by `concurrency` after `budgetFor` moved
  to `concurrency + foreground_slots` — at the default concurrency, half the working set the
  page had just computed the deployment needs.
- **`detect_scan_memory_budget` hardcoded `+ 2`** where everything else keys off
  `_FOREGROUND_SLOTS`, so changing that constant would have broken the "both gates fully
  admitted still fit" identity silently — and `scan_budget_report` would have gone on
  reporting the old total as fact.

## Session 194 — 2026-08-27: docs/ is reference documentation again

*(Landed on `main` as #306 while this branch was open, which is why it is numbered after sessions 190–193 despite its date — both branches claimed 190 in parallel.)*

`docs/` had accumulated three audiences in one directory: operator guides next to an
internal backlog, a 4,356-line session log, thirteen archived plans and PR-review dumps, and
twenty-four dated design records. An operator opening it could not tell which files were for
them.

- **Deleted the process records.** `docs/archive/` (superseded plans, per-PR review finding
  dumps, archived roadmap phases and progress splits) and `docs/superpowers/` (specs and
  execution plans) are gone — 36k lines. Git history keeps them. Every pointer into them was
  repointed at the reference doc that describes the shipped behavior, including seven
  docstrings under `src/` and three `CHANGELOG.md` entries; a link check over every markdown
  file passes.
- **`PROGRESS.md` 4,356 → ~740.** Sessions 1–169 trimmed; the header now says how to recover
  them and that this file is deliberately not exhaustive.
- **`ROADMAP.md` 646 → 394**, refreshed against the real issue list (its "verified" stamp was
  a month and four releases old, and #296–#298 were missing from it). Every open item and
  standing decision survives; the rationale essays around them do not.
- **Reference docs trimmed** where prose had drifted from contract into narrative:
  `DEPLOYMENT.md` 987 → 781 (post-mortem sections and a duplicated worked sizing example
  out, every operational fact kept), `AGENT.md` 907 → 811, `ANOMALY_DETECTION.md` 2,271 →
  2,103 — mostly the shared-machinery preamble, whose gate/mute/overrides/cache/scope
  sections carried the same fact three ways. The fourteen method sections are left alone:
  they are the peer-review contract this project claims, and shortening the explanation of a
  detector is not a docs improvement.
- **`MODEL_REFINEMENT.md`** loses its 2026-07-05 storage-placement audit — all three of its
  cleanups shipped in M21 — and states the current placement instead.
- **`README.md`** groups its documentation index by audience (running it / using it / why it
  is built this way) and drops the stale screenshot-grid TODO comment.
- **`CLAUDE.md`** records the new rule: `docs/` is reference documentation, not a working
  area — design rationale goes in the subsystem's reference doc, review findings in the PR
  thread or as a condensed roadmap item, history in git.

## Session 190 — 2026-08-26: scan admission classes (#300)

Production evidence on a 700M-event timeline: an analyst opening a per-value histogram
during an Investigate sweep got a spinner that never resolved, and a reload turned every
chart on the page into the same spinner. Three defects, none of them tuning:

- **Charts shared the detector lane.** `queries.py` gated `histogram`, `field_terms` and
  every other chart aggregation on the same `HEAVY_SCAN_GATE` as fourteen whole-corpus
  detectors; a sweep is 4 cheap plus 9 heavy requests fired in parallel, and a histogram
  joined the back of that queue. Now `FOREGROUND_SCAN_GATE` (4 slots) admits charts in
  their own lane. The heavy cap divides the budget by N + 1 and the reserved slot is split
  four ways, so both gates fully admitted still fit the total — the calculator and
  `scan_budget` report follow.
- **A queued request could never say so.** Every gate acquire was unbounded and blind. A
  chart now waits at most 30 s and answers 503 with `queued_ahead` and `Retry-After`; the
  histogram, per-value modal and chart canvas render "waiting behind N scans" and retry
  at the server's pace instead of toasting a failure or spinning.
- **A disconnect orphaned the work and the reload doubled it.** Nothing cancelled a
  threadpool scan or its ClickHouse query when the client left. `api/scan_exec.py::run_scan`
  binds a `ScanContext` (every scan's `SETTINGS` clause now carries
  `log_comment = 'vestigo-scan/<token>'`), polls `request.is_disconnected()`, and on
  disconnect sets the cancel flag — a parked `acquire_scan_slot` notices within a second —
  and issues `KILL QUERY` by tag. Verified against the reference ClickHouse.

The other half of the #300 report — the derived-range histogram scanning the corpus
twice — had already shipped in `6c69855`; the roadmap entry claimed otherwise and is gone.
The design round and execution plan for this session are in git history (commits `fdaf4d5`
and `5ab8d6c`); what shipped is described in `ANOMALY_DETECTION.md` §"Two admission classes"
and `DEPLOYMENT.md` §"Resource sizing".

## Session 189 — 2026-08-26: PR #304 review fixes (scan-budget accounting)

Five findings from the review of the session-188 branch, four of them in the new cache
accounting itself.

- **The fallback could raise the budget.** With the caches at or over the ceiling,
  `_resolve_scan_memory_budget` fell back to the 12 GB constant — larger than the ratio
  would have given on any ceiling under 15 GB. An 8 GiB app container went from 6.4 GB
  to 12 GB *because* its configuration was found to be over-committed. The fallback is
  now clamped to `ratio × detected`, so subtracting the caches can only ever lower the
  budget, and the result can never be 0 (which ClickHouse reads as unlimited).
- **The caches were subtracted from the wrong ceiling.** `scan_memory_ceiling()` returns
  local detection — the *app* container's RAM — whenever the probe never ran or an
  unbounded ClickHouse ceiling was capped by it. ClickHouse's cache maxima do not live
  under that number; `_cache_bytes_under` now applies them only to ClickHouse's own.
- **`uncompressed_cache_size` is never allocated.** It is gated on
  `use_uncompressed_cache`, which is off by default, so a stock server reports an 8 GiB
  maximum it never touches. Counting it took 8 GiB off the budget of every externally
  managed ClickHouse and could invent an `over_budget`. Both uncompressed caches are out
  of `_COUNTED_CACHES` and out of the probe query; `memory.xml` still pins them to 0 so
  the premise survives an upgrade.
- **A server-pinned `max_threads` is not a core count.** `auto(N)` is; a plain integer is
  an operator's thread limit, and dividing it by the gate ran scans at a quarter of the
  configured width while reporting it as `detected_cores`. `parse_max_threads_setting`
  now returns `(value, is_auto)`, a pin is honoured verbatim, and the report says
  `clickhouse_pinned` with `detected_cores: null`.
- **The airgap `memory.xml` guard was unreachable.** Under `set -e` the `cp` aborted
  first, on both failure shapes, with a `cp:` line naming no cause. The guard now runs
  ahead of the copy (bundle side and mount-target side). Its test passed anyway — twice
  over: it asserted only `returncode != 0` and the string `memory.xml`, and the bundle it
  built failed the manifest check before ever reaching the installer.

CI and the release workflow now run ClickHouse `26.6.1.1193` (the glibc build of the tag
`docker-compose.yml` pins) instead of `24`, which is what surfaced this: `24` has no
`primary_index_cache_size`, so the probe test asserted a name the server under test did
not have.

## Session 188 — 2026-08-25: Scan-budget truthfulness (#301, #302, #303)

`scan_budget.risk` reported `ok` for a configuration that does not fit, including the one
we ship. Three defects, one probe.

- **Caches were never counted** (#302). The ratio is now taken of
  `ceiling − caches`, which is what `stat_scan_memory_ratio`'s own help text always
  claimed, and `risk` compares `scans + caches` against the ceiling. The issue's own
  table understated it: at 26.6 defaults `index_mark_cache_size` and
  `primary_index_cache_size` are 5 GiB *each*, so the reference stack's caches (12 GiB)
  exceeded its whole 9.5 GiB ceiling. `memory.xml` now pins both; caches total 3.5 GiB
  and the stack fits, with 1.2 GiB of merge headroom, asserted by
  `tests/test_reference_stack_budget.py`.
- **Thread width was a constant** (#301). `stat_scan_max_threads` defaults to 0 = auto:
  cores ÷ concurrency, floor 2. The issue proposed reading CPU counts from
  `system.asynchronous_metrics`; 26.6 has none — no `CGroupMaxCPU`, no `OSNProcessors`.
  The real source is `system.settings.max_threads`, which reports `auto(N)` and *is*
  cgroup-quota aware (verified: `--cpus=2` → `auto(2)`).
- **The airgap install never had a ceiling** (#303). `install.sh` copied
  `allow-default-network.xml` and not `memory.xml`, so the compose bind-mount source did
  not exist and Docker made an empty directory of it. Fixed, plus a `-f` pre-flight
  assertion, `--check` coverage, and a parity test between the bundle script, the
  installer and the compose mounts. `risk` is now rendered on the admin Settings page,
  since a startup log line is what nobody reads.


Alongside: a **sizing calculator** at `docs/sizing/` (GitHub Pages, linked from the README).
It turns an expected dataset size, analyst count and deployment shape into recommended RAM,
cores and `VESTIGO_*` values, using the same arithmetic `db/_scan.py` uses — its constants are
*generated* from `core/config.py`, `db/_scan.py` and `memory.xml` by
`scripts/gen_sizing_constants.py`, with a parity test, so a public sizing page cannot recommend
values the app stopped using. Sizing numbers only, deliberately: an operator who pastes a
generated config has skipped the file that explains what the numbers mean.

Issue #300 (foreground histograms queuing behind detector sweeps in the shared gate)
stays open: it changes hot-path query behaviour and gets its own round.

## Session 187 — 2026-08-25: PR #299 review — the scan-budget half

The value-inventory export (#295) itself came through review clean. Everything below
is the scan-budget refactor that shipped alongside it, plus one concurrency hole in
the new export gate.

**The budget and the gate had drifted apart.** Session 186 moved the per-query budget
to query-build time so the ClickHouse probe could reach it. `HEAVY_SCAN_GATE` stayed
sized at import — it has to be, it is imported by value — so the divisor followed the
live setting while the semaphore did not. An admin lowering "Concurrent heavy scans"
from 4 to 2 in the console would double every query's `max_memory_usage` while the
gate kept admitting 4: twice the total budget, i.e. the exact OOM the pair exists to
prevent. `restart_required=True` on the spec is advisory text, not enforcement. Both
halves now read one frozen `_GATE_CONCURRENCY`, and `/api/health` discloses a pending
value waiting for a restart.

**An "unbounded" ceiling was still believed outright.** `resolve_clickhouse_ceiling`
classifies a ceiling ClickHouse merely derived (0.9 x RAM a limit-less container does
not own) as `bounded=False` — and the budget used it anyway. App in a 4 GiB container
against an unlimited ClickHouse on a 64 GiB host went from 1.6 GiB per query to ~23,
on the strength of a number the module had just called a guess. `scan_memory_ceiling()`
now caps an unbounded ceiling by local detection: two guesses, take the lower. An
explicit `max_server_memory_usage` is a decision and still stands as written — but it
is now clamped by `max_server_memory_usage_to_ram_ratio` exactly as the server clamps
it, which the reference stack needed: 10 GiB pinned against `mem_limit: 12g` and a 0.8
ratio is really 9.6, and the probe reported the optimistic number. `memory.xml` now
pins 9.5 GiB so the file says what it means.

**The export gate could stall the app.** `EXPORT_SCAN_GATE` is one slot held for the
whole client-paced drain, and it was acquired with an untimed blocking `acquire()`
inside the generator — which Starlette runs on an anyio worker thread. One analyst
backgrounding a large download blocked every other inventory export in the process,
each queued one parked on a thread from the pool every `run_in_threadpool` query also
needs. The slot is now taken by the endpoint *before* the response begins, bounded by
`VESTIGO_EXPORT_SCAN_QUEUE_WAIT_SECONDS` (default 30), and a wait that runs out is a
clean 503 rather than a truncated 200. Acquiring it after the headers are gone could
never have been anything else. The start-of-export audit row moved below the
acquisition while we were in there: a refused request produced no file, and a row
saying an export ran is the kind of claim this trail exists not to make.

**The merge wait was waiting on other people's merges.** `_await_merges` polled
`system.merges` for *any* merge on `events`, holding the admission slot, for up to 300
seconds. An instance with concurrent ingest always has one in flight, so it burned the
full five minutes every apply and stalled every detector sweep behind it. It now polls
the partition ids the apply actually staged, read off the scratch table before the
swap — no parts staged, no wait.

**One pre-existing test bug, found on the way.** `tests/test_scan_budget.py`'s
local-detection tests read module state that startup recovery writes once, so any
earlier test booting the app left the dev ClickHouse's real ceiling behind and three
tests failed by *ordering* — on `main` too, not from this work. An autouse fixture now
starts each of them from "the probe has not run".

**Two small ones.** `/api/health` re-detected local memory on the event loop on every
poll (three blocking reads); it runs in the threadpool now. And the export dialog's
field picker rendered neither the loading nor the error state of `viz/fields`, so a
failed request read as an empty list with a permanently disabled Download button.

## Session 186 — 2026-08-25: nothing was bounding ClickHouse

A production instance had been losing clickhouse-server to the kernel for months. The
enrichment apply was blamed, twice, and gated twice (sessions 52 and 56). It was never
the enrichers.

**Three ceilings, all off.** The airgapped compose — the file that actually reaches
production — carried no `mem_limit` on any service. The repository's own
`docker-compose.yml` at least carried them commented out; the bundle template never had
them. With no container limit, ClickHouse derived its own ceiling from
`max_server_memory_usage_to_ram_ratio` (0.9) times detected RAM, which in an unlimited
container is the whole 64 GiB host, so it never self-throttled. And
`deploy/clickhouse/memory.xml` shipped as a `.example` nobody is told to copy.

**The app made it worse, and reported success while doing it.**
`detect_scan_memory_budget()` measured the memory of whatever host *the app* process
sat on. Full-docker, no limits: 64 GiB detected, x 0.8 = 51.2 GiB total, / 2 slots =
**25.6 GiB authorized for a single query** — while Postgres, Qdrant and the app shared
the same RAM and ClickHouse's own ceiling sat at ~57.6 GiB. Every guardrail was
honoured exactly. The sum simply did not fit, the kernel picked the largest RSS, and
SIGKILL is not something a process gets to write a log line about. `restart:
unless-stopped` then erased the outage: the only visible trace was `docker compose ps`
showing ClickHouse up two hours against five days for everything else.

**The fix is to stop having two numbers.** The budget is now derived from *ClickHouse's
own reported ceiling* (`system.server_settings`, falling back to
`system.asynchronous_metrics`), probed once at startup. An operator sets
`max_server_memory_usage` and the app follows it. Local detection survives only as the
pre-probe fallback, and landing there is now logged as a warning naming the
misconfiguration.

That required the SETTINGS clause to stop being a module constant. It was frozen at
import — the one moment at which the app cannot ask ClickHouse anything, which is
precisely why the budget could only ever be sized from the app's own host. It is now
`heavy_scan_settings()`, called per query (69 call sites, mechanical). The side effect
is worth as much as the fix: every `stat_scan_*` value except `concurrency` is now
genuinely live, so four settings lost a `restart_required` badge that had been telling
the truth about an implementation detail nobody wanted.

`resolve_clickhouse_ceiling` distinguishes a ceiling an operator *set* from one
ClickHouse *derived* from RAM it does not own. Both are usable for sizing; only the
first is a limit. That distinction is the whole finding, so it is what `risk` reports:
`ok` / `over_budget` / `unbounded`, served on `/api/health` because a startup warning is
exactly what nobody reads.

**The merge window.** `finalize_enrichment_apply` held its admission slot "across the
INSERT *and* the REPLACE" on the stated grounds that the swap queues merges and merge
memory is not covered by the per-query cap. Both halves of that sentence were right and
the conclusion did not follow: `ALTER TABLE ... REPLACE PARTITION` returns as soon as
the swap is done, with the merges still ahead of it. The slot was being released into
the expensive part. It now waits on `system.merges`, bounded and non-fatal — the
partition is durable by then, so a slow merge must never fail a completed apply.

**Shipped, not documented.** Both compose files now set memory limits and mount
`memory.xml`, overridable per service via `VESTIGO_*_MEM_LIMIT`. A guardrail that ships
opt-in is a guardrail production runs without; that is the actual lesson, and it is why
none of these are opt-in any more.

Two test bugs surfaced on the way, both latent races rather than fallout.
`_SeqFakeClient` keyed canned results to a FIFO under one marker while `compare_*` runs
its two layers through `_run_parallel` — which layer got which counts was a thread race
that happened to land right. Markers can now be tuples that must all match, so a test
keys on what distinguishes the layers (`q=` compiles to `ILIKE`) instead of on arrival
order. And `test_other_database_errors_are_not_swallowed` used `code: 241` as its
example of an untranslated error; 241 is now deliberately translated, so it moved to
`code: 62`.

## Session 185 — 2026-08-25: what the review found in the value inventory (#299)

Four findings against session 184's export, all correctness, and two of them changed a
decision that session had recorded as deliberate.

**The scan gate is not one gate.** Session 184 wrote that `iter_field_inventory` holds
"its scan-gate slot for the whole drain" as though that followed from streaming. It does
not. Every other holder of `HEAVY_SCAN_GATE` is bounded by ClickHouse; this one is bounded
by the analyst's browser, and the gate has two slots, process-wide. Two people downloading
a large inventory over a slow link — or one who backgrounds the download — hold both, and
every detector on the box blocks behind them. The generator cannot even time itself out,
because while it is suspended waiting on the consumer its own code is not running.

The split that resolves it: the aggregation is what `HEAVY_SCAN_GATE` exists to admit, and
a sorted aggregate cannot emit its first row until every group exists — so **the first
block proves the heavy scan is over**. The detector slot is handed back there, and a new
one-slot `EXPORT_SCAN_GATE` covers the drain. Exports queue behind each other rather than
stacking two live result streams; detectors stop paying for a slow browser. No setting for
the export gate: the supported answer to "I want more concurrent exports" is that you don't.

**The pre-flight could not survive its own use case.** `uniqExact` builds the full distinct
set in a hash table and does not spill — `HEAVY_SCAN_SETTINGS`' external-aggregation
thresholds simply do not apply to it. So the count died at `max_memory_usage` on exactly
the high-cardinality field the export was built for, and surfaced as a bare 500. This is
the same trap session 184 avoided for the *stream* (window sorts cannot spill either) and
then walked into for the count. It is now `count()` over a `GROUP BY` subquery: the same
grouping the stream does, so exact for the same reason, through the spillable path. The
grouping runs twice now. That is the price of the export working on the fields it exists
for. A `QueryMemoryExceededError` maps any remaining `code: 241` to a 413 that says narrow
the scope, alongside the older `QueryRequestTooLargeError` — the cap is ours, set so one
scan dies instead of the server, which makes hitting it explainable rather than a 500.

**Two smaller ones.** `iter_field_inventory` yielded ClickHouse cells straight through, so
a `content_hash` or `file_hash` inventory — both legal field tokens, and reachable from the
agent and the CLI even though the viz picker does not list them — wrote `b'3f2a…\x00'` into
the CSV. `decode_fixed_string` is now factored out of `decode_fixed_string_columns` for the
paths that yield a bare cell rather than a row. And in the export dialog only the mode
toggle was frozen mid-download: flipping the separator to tab made the progress row read
"Downloading .tsv" over an in-flight comma-separated request that would still save as
`.csv`. Every control that shapes the request is frozen now, events-mode format included.

Each of the four has a test that fails without its fix — checked by reverting each one.

## Session 184 — 2026-08-25: the value inventory export (#295)

An analyst wanted three columns out of a timeline — each distinct `attr:src_ip`, when it
was first seen, when it was last seen — and the only way to get them was to export every
event and aggregate the file elsewhere. `QueryService.field_terms` already grouped by the
value; `min`/`max` over the timestamp were two more aggregates in the same scan.

The aggregation is a *new* method rather than a flag on `field_terms`, for a reason worth
recording: `field_terms` can afford its `sum() OVER ()` because it takes a top-N, and the
inventory cannot — it is unbounded in group count, and window sorts are the one sort
ClickHouse will not spill to disk (`db/_scan.py`). `iter_field_inventory` is therefore
plain `GROUP BY`/`ORDER BY` under `HEAVY_SCAN_SETTINGS`, streamed to the client one block
at a time through a new `_select_row_blocks` (the streaming sibling of `_select`), holding
its scan-gate slot for the whole drain.

Two details are the difference between a file an analyst can rely on and one they cannot.
The no-timestamp storage sentinel is nulled out *inside* the aggregate, not filtered out
of the scan — so a value seen only on undated events keeps its true count and reports no
times, instead of claiming it was first seen in the year 2299. And the pre-flight
`uniqExact` is the same construction as the events export's `count()`: the number the
completeness trailer is proven against, and the last point at which a query failure can
still pick a status code rather than truncate a 200.

Columns and separator are the analyst's choice (issue thread). The one rule the server
imposes: the column a file is *sorted by* is always written, even when unticked — a file
ordered by a column it does not contain reads as shuffled. The UI says so rather than
silently adding it.

`lucide-react` went 1.32.0 → 1.34.0 (upstream latest) in the same branch. It was not a
planned bump: the installed copy in this checkout was missing its ESM entry and its type
declarations, so thirty-odd untouched files failed `tsc` and `vite build` could not resolve
the package at all — a broken install that reads exactly like a code error. Reinstalling
fixed it; taking the current release rather than re-pinning the old one is the cheaper end
state. Pinned exact, as every other frontend dependency here is, and the lockfile diff
touches nothing else.

The dialog's field and order pickers are native `<select>`s, not the Radix one. A Radix
Select inside a Radix Dialog puts two focus scopes in a loop under jsdom (the test hung on
`Maximum call stack size exceeded` in `react-focus-scope`); `UploadDialog` already had the
native precedent, and typing a prefix to jump is worth more than styling on a field list
that runs to hundreds of entries.

## Session 183 — 2026-08-24: upstream branch triage, and 1.14.0

Forty remote branches, and the question was which of them still meant anything. Only
sixteen were ahead of `main` at all; twenty-three were fully contained in it and had
simply never been deleted. That left five branches carrying real work plus eleven
dependabot bumps.

Two checks in this triage were wrong the first time, both worth recording.

The first: `git merge-tree` output was grepped for `CONFLICT`, but git on this machine
reports in German — `KONFLIKT`. Every branch therefore read as merging cleanly. Redone
by exit code, two did not: `docs/pr182-review-followups` conflicted in `ROADMAP.md`,
and `feat/d11-entropy-bigram` conflicts in `anomaly_stats.py`. A locale-dependent
predicate that fails open is worse than no predicate — it produces confident wrong
answers. Match on exit status, not on translated prose.

The second: the ten green dependabot PRs were green *individually*, against a `main`
that predated the batch. `vitest` and `@vitest/ui` each failed alone with `ERESOLVE`
because `@vitest/ui@4.1.10` peer-pins `vitest@4.1.10` exactly; neither bump is
satisfiable without the other, and only merging them together resolves. `mcp` 2.0.0
stays out for the same class of reason — every `pydantic-ai-slim[mcp]` release pins
`mcp>=1.24.0,<2.0` — and that PR was closed rather than left to be re-triaged monthly.
Two branches also had to be re-verified after the batch landed, because their green CI
had run against ruff 0.16.2 while `main` moved to 0.16.3.

`#293` looked like a merge and was not. Its backend job was failing on
`test_manifest_hashes_match_committed_assets`: both nginx converters had been edited
and neither `manifest.json` entry updated. That is not a test detail. The manifest is
the integrity record an analyst checks a downloaded converter against, so a stale hash
means the published checksum does not describe the script actually served. It then
turned out to be unmergeable for a second reason — its original commit was unsigned and
authored as `mstoeck3@hs-mittweida.de`, the global identity leaking past the repo-local
config, exactly the PR #139 regression. Signed and re-authored; `BLOCKED` became
`CLEAN`, which is the confirmation that the signature was the cause.

Released as **1.14.0**, not 1.13.1. The version files already said 1.13.1 from the
timeout branch, but by the time the release was cut it carried a new converter and a
new `vhost` attribute — added functionality, and the changelog claims SemVer. A patch
number would have had to file two `Added` items under `Fixed` to stay honest about
itself. The unreleased 1.13.1 section was folded into 1.14.0 rather than left standing,
since no tag was ever cut for it.


## Session 182 — 2026-08-20: ClickHouse strangled itself on a debug log

A production instance went fully unresponsive. The output was one stack trace repeating
forever, nested inside itself:

```
Cannot log message in OwnAsyncSplitChannel channel: Cannot log message in ...
Poco::Exception. Code: 1000, e.code() = 0, File access error: .../clickhouse-server.log
0. Poco::RotateBySizeStrategy::mustRotate(Poco::LogFile*)
```

`df` inside the guest reported 390 GB free, the log directory was writable, and every file
was present — so the three obvious causes were all wrong.

- **`e.code() = 0` was the clue.** A real filesystem fault carries an errno and Poco throws
  a specific subclass. A bare `FileException` with no errno comes from one place:
  `LogFileImpl::sizeImpl()` when the `ofstream` flush fails. The handle was already dead,
  not the filesystem. C++ streams latch their failbit, so it never recovered — and
  ClickHouse stats the log before *every* message to decide about rotation, then tries to
  log the failure, which needs the same stat. The logging thread span until the server
  stopped answering. Nothing crashed; a restart cleared it in seconds.
- **The trigger was `EDQUOT`, not `ENOSPC`.** `err.log` had it in plain text —
  `Disk quota exceeded` on a rename, and `Cannot reserve 1.00 MiB` against the system log
  tables. A quota enforced above the container, which `df` inside it cannot see. On ZFS
  `quota` counts snapshots and `refquota` does not, and `df` shows the refquota view, so a
  snapshot backlog exhausts the real ceiling while the guest looks healthy.
- **Our compose file helped it along.** The stock image logs at `trace` with a 1000M x 10
  rotation into the container's *writable layer* — we mount a volume for
  `/var/lib/clickhouse` but none for `/var/log/clickhouse-server`. Measured on the dev box:
  ~1.1 GB of log files, plus 1.32 GiB `trace_log` and 992 MiB `text_log` on the data volume.
  ~11 GB of ceiling nobody reads.

`scripts/clickhouse-log-recovery.sh` caps the logger (`information`, 100M x 3), disables the
unbounded telemetry tables, puts a 14-day TTL on `query_log`/`part_log` — kept deliberately,
they are what you want when an ingest misbehaves — and recreates the container, which is what
reclaims the space: the writable layer goes, the named volume stays. It refuses outright if
`/var/lib/clickhouse` is *not* on a volume, since a recreate would then delete every case.

Airgap-safe by construction (`--pull never`, image verified present first — a stalled
registry pull on an isolated host is its own outage), engine-agnostic across Docker and
Podman, and it backs up and validates its `docker-compose.yml` edit before applying it.

Verified rather than assumed: the drop-in was booted on a throwaway container running the
same image, confirming `logger.level=information`, zero `<Trace>` lines, the telemetry tables
absent and the TTLs applied. `tests/test_clickhouse_log_recovery.py` (16 cases) guards what
can rot silently — the XML's meaning, and that the compose anchor the script splices against
still exists.

Not fixed here, and it is the actual root cause: the quota lives on the hypervisor. Capping
the logs lowers how fast you reach the ceiling; it does not raise it.
`docs/DEPLOYMENT.md` §"ClickHouse log growth" documents both halves.

## Session 181 — 2026-08-20: haproxy2vestigo, and measuring a timezone instead of assuming it

A 1.2 GB Docker `json-file` log of a HAProxy 2.6 frontend had no converter. Now it has one:
`src/vestigo/assets/converters/haproxy2vestigo.py`, built on the `nginx2vestigo.py` template
(same CLI, `.gz`, directory input, parallel chunking, `--split`, `--since/--until`).

- **Two detected layers, no flags.** Envelope — Docker `json-file`, BSD syslog, or bare —
  then payload: HTTP log, TCP log, connection error, startup/reload. On the real file:
  4,207,331 events in 35 s, 0 skipped, 323 MB Parquet.
- **A catch-all needs a gate.** An unmodelled shape still becomes an event
  (`haproxy:message`) rather than being dropped, so the converter sniffs the first 200 lines
  for a *structured* shape and refuses the file if it finds none. Without the gate it would
  have "successfully" converted any text file at all.
- **The timezone is measured.** `accept_date` carries no offset, so the Docker envelope's
  RFC 3339 `time` sets the timestamp and each row records which clock it used in
  `timestamp_desc`. The observed `envelope − accept_date` skew goes in the footer as evidence.
- **The median was the wrong estimator, and the real file proved it.** First run reported a
  9999 ms skew — not clock drift but HAProxy's 10 s **tarpit** on the `PT--` sessions that are
  92% of that log. The difference is session duration plus write latency (accept stamped at
  session start, logged at session end), so only its **minimum** is bounded by the clock
  offset. A 5th percentile is not enough either — a log where every session is tarpitted
  leaves it nothing fast to land on, which is what a fixture written for exactly that case
  caught. The footer reports `_min` (the conclusion) beside `_p05`/`_median` (the context).

`tests/test_haproxy_converter.py` (24 tests, synthetic fixtures only), a `manifest.json`
entry, and `docs/INPUT_FORMATS.md` §"`haproxy2vestigo.py`" follow. The parser downloads panel
is manifest-driven, so no frontend change. Verified end to end: 200k events through
`vestigo ingest` carry `parser_name=haproxy2vestigo`, and the column advisor picks
`src_ip`/`http_path`/`client_real_ip` on its own.

## Session 180 — 2026-08-18: every LLM wall clock is an operator setting

A converter job on a slow local endpoint failed all four attempts with
`model call failed: ` — an empty reason, because a bare `TimeoutError` (and httpx's)
stringifies to `""` and `job.py` interpolated only `{exc}`. Behind it: three hardcoded
model timeouts nobody could reach from the admin console.

- **`converter_generation_timeout_seconds`** (default 180, was `generate_script`'s parameter
  default that the single call site never overrode). `timeout_s` is now a required keyword —
  a default there is a ceiling no operator can turn.
- **`agent_request_timeout_seconds`** (default 300, was `runtime.LLM_TIMEOUT`). The
  stranded-turn bound in `api/routers/agent.py` became `_turn_stale_after()`, computed per
  call, so an edited timeout does not need a restart to be respected by the sweep.
- **`column_advisor_timeout_seconds`** (default 45, was `ADVISOR_TIMEOUT_SECONDS`). The
  constant stays as the no-settings fallback.
- **Attempt errors name the exception type** (`model call failed: TimeoutError`). An
  attempt trail that cannot distinguish "refused" from "stalled" is not a forensic record.

`.env.example`, `docs/INPUT_FORMATS.md` §"Generated converters" step 2 and the settings
registry follow; the console renders all three with no frontend change.

## Session 179 — 2026-08-18: fourth review pass, the dependabot batch, 1.13.0

- **Two findings from a fourth `/code-review 277`.** `source_hash_in_use` asked
  `scalar_one_or_none()` of the converter-script lookup, so it raised as soon as two scripts
  shared a raw file — which is what every *regeneration* produces. All four callers swallow the
  exception, so the damage was silent and wrong rather than loud: an ingest rollback reported
  "source-row removal failed" after removing the row, startup reconciliation skipped its
  `source.ingest_interrupted` audit row and logged a retry that never comes, and retained blobs
  leaked. Second: the AST guard flagged `input`/`help` in *any* context, so `input = args.input`
  — the spelling the prompt's own `-i/--input` mandate invites — was rejected before the script
  ran and cost one of four attempts, with the enforced-constraints paragraph never naming them.
  They are now a separate shadowable set, refused only where the script never binds the name.
- **Eleven dependabot PRs merged** (numpy, pyarrow, alembic, pydantic-settings,
  sentence-transformers, pydantic-ai-slim → 2.31.1, and the frontend's Radix/lucide/
  `@types/node`/jest-dom). `mcp` 2.0.0 (#269) is **not** takeable and was left open: every
  `pydantic-ai-slim[mcp]` release pins `mcp>=1.24.0,<2.0`, so the bump makes the requirements
  unsatisfiable. Full suite green at 2681 with the embeddings extra installed.
- **1.13.0 released** — generated converters is the headline; see `CHANGELOG.md`.

## Session 178 — 2026-08-18: PR #277 third review — every finding fixed, minor ones included

A third `/code-review 277` (ten ranked findings, three cut by the cap, four cleanups); all
fixed (each with its reasoning in the PR #277 review thread).
What changed shape:

- **The static guard closes dynamic lookup and rebinding.** `pydoc.locate`, `__builtins__[…]`,
  `f = eval`, `os.__dict__[…]`, `x = os`, `f(os)`, `sys.path.insert`, `().__class__.__base__
  .__subclasses__()` all passed before. Now an imported module may only be the receiver of an
  attribute access, the object-graph dunders are refused on every receiver, and the deny-list
  covers import-by-string, deserialisers and `gc`/`inspect`. Docs call it best-effort, which it is.
- **The trail is complete.** Attempts append under a row lock (`append_converter_attempt`;
  two reuse jobs no longer clobber each other), model errors before a row exists are buffered
  and land on the row (or on a `failed` row named from the file when no draft ever arrived),
  a failure after the full run passed is an `ingest` attempt + audit, and every entry that
  sent a prompt carries that prompt's hash — the row's `prompt_hash`/`model` follow the draft
  that became the code.
- **Retention is lazy and reclaimed.** The raw file is kept only once a row is about to
  reference it; a job that fails before that takes its copy back; `source_hash_in_use`
  counts `converter_input_hash`.
- **Duplicate conversion is refused everywhere**: the job pre-checks (CLI, concurrent
  submits), registration re-checks, and migration 0032 makes the index a partial unique
  index. 0032 also adds `converter_scripts.raw_mtime`: the evidence file's own mtime (browser
  `lastModified`, CLI `stat`) is what the model is told and what the script sees on `-i`;
  absent, the model is told "unknown".
- **Reuse needs only the switch** (`converter_reuse` capability): saved converters run with
  the model down or never configured; the dialog offers *Use a saved converter* alone then.
- Startup reconciliation of `generating` rows runs before the lifespan yields; the upload
  dialog freezes its mode switch mid-transfer and its progress row follows the running
  transfer; the converters panel disables Regenerate while its job runs and polls the list;
  the prompts panel explains a failed load with a Retry.

## Session 177 — 2026-08-18: PR #277 second review — ten more findings fixed

A second `/code-review 277` on the branch after session 176; all ten findings addressed
(each with its reasoning in the PR #277 review thread). The
ones that changed shape rather than just code:

- **The script only ever sees a private copy of its input.** The runner hardlinked the
  retention copy into the workdir, so its `chmod` and any script writing to `-i` reached
  the evidence itself. `shutil.copyfile` now — a large log is copied per run, deliberately.
- **`check_script` is an allow-list.** Stdlib (minus the deny-list) plus `pyarrow`/`numpy`,
  import aliases resolved, `from x import *` refused, `sys.modules`/`getattr(module, …)`
  refused, destructive method names refused on any receiver (`Path.unlink`/`.chmod`
  included). Prompt and docs restate it.
- **The retention store knows converter rows own blobs** (`source_hash_in_use` unions
  `converter_scripts.raw_file_hash`), and `delete_case` cascades the rows.
- **No row stays `generating`**: the job's catch-all fails a row it created; startup
  reconciliation (`_reconcile_stale_converter_generations`) fails rows a restart orphaned
  and records the interruption as an attempt.
- **Same saved script over the same raw file is refused** (409 naming the existing source)
  via the new `sources.converter_input_hash` (migration 0031); a duplicate Parquet outcome
  is now said in the tray and the case jobs panel.
- `build_sample` streams (no per-line offset list; `count_lines` for the reuse path), the
  stderr partial-line buffer is capped, a non-gzip `.gz` upload is a 400 not a 500, and the
  disclosure copy names head/middle/tail rather than "the first N bytes".

## Session 176 — 2026-08-17: PR #277 review — every finding fixed

`/code-review 277` on the generated-converters branch; all findings addressed in one pass
(the PR #277 review thread has the full set):

- **Path traversal via the multipart filename** — the upload's `filename` was joined onto
  temp dirs unsanitised (sample file, regenerate copy, later `unlink()`). `sample.safe_filename`
  reduces it to a basename everywhere it is used; `ConvertJobInputs` applies it on construction.
- **Sample and full run now see the same file** — the runner stages the retention copy under
  the evidence filename (`input_name`), and a `.gz` upload's head sample is re-gzipped, so a
  suffix-driven script behaves identically in both phases and `source_file` names the
  evidence, not the hash.
- **`build_sample` on a one-line file** no longer indexes past `line_count`; the tail block is
  capped at its budget so one huge last line cannot exceed the disclosed size.
- **`register_source_for_ingest` rolls back** the freshly created `ingesting` row (and an
  unshared retention copy) when the timeline add fails, closing the orphan-row gap for both
  the upload endpoint and the converter job.
- **`validate_output` streams** the Parquet in Arrow batches (`map_lookup` for the unparsed
  flag, per-batch null counts, an offset-order tracker) — bounded memory in the API process.
- **Generation loop** — the "name already exists at v>1" redraft moved inside the loop and is
  recorded as a `generate` attempt without costing one; a lost `(case, name, version)` race
  retries with the next version instead of an unhandled `IntegrityError`.
- **Ingest failure after a valid conversion** is recorded as an `ingest` attempt plus a
  `converter.run` audit row and fails the job with the reason.
- **Runner**: rlimits are applied by a `-c` bootstrap inside the child (no `preexec_fn`
  from a threaded process); stderr EOF while the child runs now waits out the deadline and
  kills the group; `posix`/`_socket`/`_ssl`/`_posixsubprocess`/`_multiprocessing` denied,
  and the prompt reads the runner's list rather than a hand copy (system prompt v2).
- **Converter name is enforced**: the task header declares it once known, and
  `validate_output` checks the footer's `converter_name` against the row.
- **UI**: the tray's "View converter attempts" link is followed while the panel is mounted;
  invalidation fires on `failed` too; timestamps render in UTC like the rest of the app.
- Cleanups: `typed_completion` (`agent/oneshot.py`) shared by the column advisor and the
  generator; list endpoint defers the two large Text columns; scratch dirs removed on every
  path; CLI commands registered before the `__main__` guard; duplicate TS interfaces gone.
  One suggested cleanup was declined: generalising the importer's missing-stem rule would
  let a truncated archive restore silently (`test_incomplete_archive_fails_the_job`), so
  `_OPTIONAL_STEMS` stays explicit, now with the reason on it.

## Session 175 — 2026-08-17: generated converters — the model writes the script, the harness runs it

The upload dialog gains *Let AI write the converter* (and the CLI `vestigo convert-ingest`):
an analyst uploads any plain-text, time-annotated log, the configured model writes a
converter to the Parquet contract, the server runs it in a guarded subprocess, validates the
output, repairs on failure, and ingests the result — the produced Parquet *is* the source, so
nothing downstream changes. Scripts are case-bound rows (`converter_scripts`) the analyst can
download (with a provenance header), reuse on later uploads (no model call) and regenerate
with a hint (a new version, never an edit).

Decisions worth remembering:

- **Off by default, gated twice.** `converter_generation_enabled` plus the agent probe; the
  capability hides the UI, `convert`/`regenerate` answer 503 (house style, not the 404 the
  spec first said), list/download stay available because rows are records.
- **Guard is stdlib only** (no bwrap/containers, so uv and image deployments are unchanged):
  AST deny-list, `python -I`, scrubbed env, private cwd, `RLIMIT_AS/CPU/FSIZE/NOFILE`, own
  session. Measured: pyarrow imports at 2048 MB `RLIMIT_AS` and fails at 1024, so the setting
  floors at 2048. `RLIMIT_NPROC` was dropped — on Linux it counts the *user's* processes and
  starved OpenBLAS thread creation on a busy host; `OPENBLAS_NUM_THREADS=1` in the child env
  instead. What the guard does not stop is written down in `docs/DEPLOYMENT.md`.
- **The prompt is data.** `converters/prompt.py` renders the generation, repair and human
  copy-paste prompts from `ingestion/parquet_format.py`; the frontend now fetches the
  copy-paste text from `GET /api/converters/prompt` (the drift bug #204 cannot recur), and
  the assertions moved from `guidancePrompts.test.ts` to `test_converter_prompt.py`.
- **Egress is exactly** the head/middle/tail excerpt, filename, size, line count, mtime,
  version to declare and hint; the dialog names the model, endpoint host and byte count.
- **No repair on the full-file run** — a script that passed the sample and fails the whole
  file met a format change the sample did not show; regenerate with a hint instead of the
  harness sending more evidence than disclosed.
- **No job cancellation** (`JobStore` has none); timeouts bound every subprocess.
- `upload_source` lost ~120 lines to `register_source_for_ingest`, which the job reuses so
  both register a file identically (dedup, format detection, footer validation, retention,
  the row, the default timeline).
- Transfer carries `converter_scripts.ndjson` and the raw inputs as blobs; an older archive
  without the stem imports fine (`_OPTIONAL_STEMS`).

Tests: `test_converter_{prompt,sample,validate,runner,generator,scripts_store,scripts_api}.py`,
`test_converter_job_clickhouse.py` (real subprocess + real ingest with a fake model: happy
path, repair round, exhausted attempts, denied import, reuse, regenerate), transfer round
trip, CLI, plus `uploadDialogGenerate` and `generatedConvertersPanel` on the frontend.

## Session 174 — 2026-08-14: a declaration that steers nothing must not claim to

A second review pass over the same branch. Nothing here breaks a scan; every finding is a
place where the declaration *described* itself wrongly, which for shared, audited state is
the same class of bug.

`DetectorRun.params["field_overrides"]` was recorded unconditionally — including for a run
that named its fields explicitly (which bypasses the declaration in every detector) and for
methods that never receive it. That is the "auto does not describe what was scanned" problem
the key was added to fix, pointed the other way: the diary claiming a decision steered a scan
it never touched. It is now recorded only where it applied.

The PATCH endpoint validated method ids against all twelve, but four select no fields to
steer: `frequency` and `sequence_novelty` take one named `series_field`, `timestamp_order`
reads none, `log_template` clusters message text. A declaration against those was accepted,
audited, and rendered under "Declared fields" while the detector scanned exactly as before.
`FIELD_OVERRIDE_METHOD_IDS` (`db/analysis_plan.py`) is now the one list, shared by the
endpoint's 422 and by `_resolve_field_overrides`, which also keeps it out of the cache key.

Two disclosure bugs. `_drift_split_fields` cut its categorical branch by whatever the numeric
branch consumed, so a timeline with fifteen recommended numeric fields sliced the categorical
list to `[:0]` and dropped a pinned field with no note — a held-back field indistinguishable
from one that found nothing, which is the one thing this must never look like. And
`_auto_string_fields` derived its "held back" count from the whole candidate universe rather
than from what its quota would have scanned, so excluding a field ranked 22nd of 40 reported
a narrowing of a scan that was byte-identical to the undeclared one.

Frontend: the picker's identifier branch (charset/entropy) never re-applied the 15-field cap
after prepending pins, so it previewed 17 checked chips for a run that scans 15. The write
chain's serialization was per-hook-instance while two instances are mounted at once — the
method sheet's picker and the Tools summary — so declaring in one and resetting in the other
before the PATCH landed rebuilt the payload from the stale cache and dropped the in-flight
declaration from the timeline and the audit pair alike; it is now keyed by timeline at module
scope. A failed write is surfaced rather than swallowed: the chip returns to the server's
answer either way, which on its own reads as "nothing happened" rather than "not saved".

Released as 1.12.2.

## Session 173 — 2026-08-14: review of PR #264, the two halves that did nothing

A review of session 172's branch found the pin half broken in two places, both of which made a
control look present and do nothing — and one of which produced a false forensic claim.

`apply_field_overrides` built its pin list as "declared `true` and *not already selected*", so
only pins on fields the recommender had left out were promoted. A pin on a field it ranked 18th
kept rank 18 and was cut by the caller's `[:15]`; for `value_combo`, whose cap is 2, any pin
below third was cut — the exact case the code's own comment said it existed for. Nothing was
disclosed, so the field was neither scanned nor mentioned. Pins are now every `true`
declaration, promoted out of the kept list rather than skipped, and the per-detector cap is
re-applied afterwards (`charset`/`entropy` never re-cut, so a stored declaration could double
a heavy scan under one `HEAVY_SCAN_GATE` slot).

`_drift_split_fields` passed each branch's own selection as its `known` universe. A pin is by
construction a field the branch did not select, so every pin fell into the "not present in this
timeline" branch: pins never applied, and the numeric branch announced a categorical field as
absent while the categorical branch scanned it — a run that scans a field and disclaims it in
the same breath. The declaration is now resolved once against both recommenders' candidates,
and a pin neither of them selected is classified by the same syntactic numeric probe the
explicit-`fields` path uses, so it lands in the branch that probe indicates.

Two smaller things: `DetectorRun.params` now records the method's slice as it stood at run
time — `fields: auto` does not describe what was scanned once a declaration is edited, and an
applied pin leaves no trace in `warnings` the way an exclusion does — and `/analysis/findings`
hands the declaration it already read for the cache key to `_run_stat_detector` instead of
letting it re-read the timeline (24 redundant round-trips per 12-method sweep).

Frontend: `useFieldOverrides.declare` closed over the query-cache snapshot, which only refreshes
on the mutation's `onSuccess`, so two chip clicks in quick succession both built on the
pre-mutation state and the second PATCH — a full replace — dropped the first, including from
the audit row's `previous`/`new` pair. Edits now build on what is in flight and the requests are
chained so they cannot land out of order.

## Session 172 — 2026-08-14: per-timeline, per-method field overrides

Session 171 gave the field knobs back their pickers, which made the correction possible but
not durable: the picker's selection is per-run React state, so an analyst who takes a field
away from a detector takes it away again on the next sweep, and the next analyst never learns
they did.

The miss it leaves is semantic, not statistical. `recommend_numeric_fields` types fields
syntactically and says so in its own docstring: an HTTP status code parses as a number, so
`numeric_range` offers it, learns a band over `{200, 404, 500}` and reports the 500s as
outliers forever. No probe discovers that it is a categorical field wearing digits — only the
analyst does.

`Timeline.field_overrides` (migration `0029`, nullable) is where they say it:
`{method_id: {field_token: bool}}` — `true` pins a field into a method's automatic selection,
`false` takes it out, absent leaves the recommender's answer standing. Per method rather than
per field, because the same status code is meaningless to `numeric_range` and an excellent
`value_novelty` field. Written through `PATCH .../timelines/{id}/field-overrides` (contribute
access, unknown method ids and empty tokens 422, every change audited), shared per timeline on
`muted_methods`' contract rather than held per browser.

One helper does the work: `apply_field_overrides` sits between a recommender's answer and the
scan list of all eight detectors that pick their own fields, and returns what it held back for
the run's `warnings`. That keeps the shape "advice, never a lock" on every axis — an explicit
`fields=[…]` never reaches the helper and still scans an excluded field, the analysis plan
does not consult it, a pin naming a field the timeline lacks is dropped rather than scanned as
an always-empty column, and a held-back field is disclosed rather than silently narrowing a
scan into something that reads as "clean". Pins are applied before each detector's
`_MAX_AUTO_SCAN_FIELDS` slice, since being ranked below the cut is why a field gets pinned.

The findings cache key gains the method's slice: an answer computed before a field was
declared off is an answer to a different question.

In the UI the control is a small pin/exclude button beside each chip in `AnomalyFieldPicker`,
deliberately separate from the checkbox — scoping this run and deciding what the method reads
are different questions — and the picker's auto preview applies the declaration exactly as the
backend does, so the checked set keeps previewing what actually runs. The Tools sheet's Methods
tab summarizes what a timeline declares and resets it per method.

Not done: the agent has no tool for this. Every agent write today is a proposal an analyst
confirms, and a direct-write tool would be a new precedent that needs its own argument rather
than a side effect of this change.

## Session 171 — 2026-08-13: field knobs are choices again, not typing

Reported as a regression in the Investigate panels: the `fields` knob asks the analyst to
type field names. It does — and the same refactor took the single-field knobs with it.

`cadaa5c` replaced eleven per-detector views with one generic knob renderer that types every
knob as `<input type="text">`. Eight of those views mounted `AnomalyFieldPicker` (cardinality-
ranked candidates, Standard vs Dynamic grouping, coverage and distinct counts per chip,
`value_combo`'s 2–4 floor and ceiling); four more offered `series_field`, `group_field` and the
log-template `field` as `<select>`s built from the same `/anomalies/fields` inventory. A field
name is not free text — it is a fixed set of columns plus whatever `attr:` keys the timeline
happens to carry, and nobody can spell those from memory for a source they ingested an hour
ago. The knobs were reachable but unusable, which is the worse failure: the sheet's method mode
is what keeps the analysis gate advice rather than a lock, and that argument only holds if
running a method with your own parameters is actually possible.

Both controls are back. `kind: "fields"` renders the picker, configured per method from the
registry with the values the deleted views used (auto counts, `value_combo`'s 2–4, charset and
entropy's identifier-inclusive auto set, numeric-range's numeric candidate list) — so the
checked set previews what the backend will really scan. New `kind: "field"` renders
`MethodFieldSelect`, the single-field counterpart, which merges each knob's standard options
with this timeline's attribute keys. A picked selection travels as a list, which
`_FieldsParams._join_fields` already accepted alongside the comma-joined string; an untouched
picker still sends nothing, so "auto" stays the method's own default rather than an empty
string dressed up as one.

`MethodFieldSelect` renders only the `<select>` and inherits the labelled chrome from the
caller, so the fix adds no new arbitrary font size to the design-system budget.

