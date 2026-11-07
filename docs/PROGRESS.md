# Vestigo Implementation Progress

Append-only session log — what changed and why, newest first. This file keeps the recent
sessions only; older ones live in git history, and every release is summarized in
`CHANGELOG.md`. Plans belong in `ROADMAP.md`, not here.

Last updated: 2026-11-07 (1.20.2 released; session 244 — #437 vhost-prefixed access logs; roadmap state
refreshed; sessions before 232 trimmed to git history).

## Session 244 — 2026-11-07: vhost-prefixed access logs (#437), stale roadmap state

**The defect.** A `log_format` that prepends the virtual host and drops `%l`
(`$server_name $remote_addr - [...]`) has the same token count as `%h %l %u`. The combined
regex matched it with every slot shifted, so `src_ip` was empty on every row while the
import reported success. Apache's existing retry only stripped a `vhost:port` prefix, so a
bare `www.example.com` slipped past it too.

**The rule.** No regex can tell the two three-token layouts apart. What tells them apart is
that `%l` is an identd answer (a user name or `-`), never an address. One matcher tries
`%h %l %u`, `%v %h %l %u` and `%v %h %u`. The documented reading wins when its client slot
holds an address and its `%l` slot does not. A prefixed reading wins when its own client slot
holds an address. With an address in neither, the documented reading stands
(HostnameLookups on). The issue suggested the same shift heuristic. Two edge cases tightened
it:

- A vhost logged as an IP (`192.0.2.10 82.158.193.164 -`) passes "client slot holds an
  address" in the documented reading. The `%l` clause is what refuses it.
- The vhost token excludes commas. With `\S+`, the first element of an X-Forwarded-For chain
  with no `%l` slot (`192.0.2.1, 198.51.100.2 - [...]`) would have been read as the vhost,
  making the proxy the client. That line is ambiguous and is now counted as unparseable.

**Where it landed.** The vendored converters come from `overcuriousity/2timesketch`, so
the fix went upstream first (1.5.0) and was re-vendored. That release also fixes the
absolute-glob `NotImplementedError` in all seven converters that used `Path(".").glob`,
not only nginx. The native `nginx2vestigo` (1.5.0) got the same matcher. It also took
upstream's comma-joined `%h` slot and `client_host` column, which it had never received:
without them a proxied line was dropped and a hostname client was lost.

**Trailing fields are kept, not parsed.** The regex models one extra quoted field. Anything
after it is now preserved verbatim in `trailing_fields` and counted (row-count footer, audit
report, stderr warning). Parsing TLS metadata would mean guessing at a custom format, which
the counts make visible instead.

**Security.** Dependabot raised two `joserfc` advisories the day after 1.20.1. Its PR (#444)
also moved `uv.lock` to revision 5 and rewrote unrelated platform markers, so the bump was
redone with the repo's uv as a three-line lockfile change. It shipped with #437 as 1.20.2.

**Stale state.** The `ROADMAP.md` header still described v1.18.3. It now names v1.20.1, the
releases since the last full re-verification, and that the Milestone 3 counts are from
2026-09-02. This file is trimmed to sessions 232 onward.

## Session 243 — 2026-11-06: dependency roundup, 1.20.1

All 33 open Dependabot PRs were merged on one branch and both lockfiles regenerated once
over the result, as in session 242. Three things needed a hand:

- The vitest, @vitest/ui and jsdom PRs each rewrote adjacent devDependency lines and
  conflicted. Dependabot also opened two @vitest/ui PRs (5.0.3 and 5.0.5) and a separate
  vitest 5.0.3 one; vitest goes to 5.0.5 with @vitest/ui, because the two are pinned as an
  exact peer pair.
- npm 10's arborist crashes with "Cannot read properties of null (reading 'edgesOut')"
  when that peer pair moves. npm 11, which is what CI runs on Node 24, resolves it cleanly,
  so the lockfile was regenerated with npm 11 from main's.
- uv.lock was regenerated with `--upgrade-package` for every bumped package, including
  the transitive-only ones. Taking one side of a conflicting lockfile hunk would otherwise
  silently drop a bump.

pysigma 2.0.0 is the only major runtime bump. Its PR lifts our cap to `<3.0.0`. The
Sigma backend, loader and runner tests pass unchanged against it.

## Session 242 — 2026-09-21: review fixes, dependency sweep, 1.20.0

A review of #378 found four defects in the new detectors; all were fixed before merge.

**Tagging.** `tag_anomalies` formats annotation text with one branch per finding type and
sends anything it does not recognise to the frequency-spike branch. The three new types
had no branch of their own, so tagging any of them raised `AttributeError`.
`_serialize_finding` had been updated and this function had not. Each type now has its own
branch, and a parametrized test covers all three.

**Zero-length transitions.** A transition's duration is the difference between two
timestamps, so it is only as precise as they are. `0 × min_ratio` undercuts every
positive floor, so a 0 s observation ranked first at score 1.0 even where the timestamps
are whole seconds and 0 s only means "under a second". It is now judged by the source's
resolution. A per-source probe, run only when a zero-length candidate exists, stops at the
first sub-second timestamp. Where one exists, 0 ms is instant. Where none does, the
observation is judged and scored at a one-second bound, and any pair this holds back is
counted in a warning. We considered skipping zero-length observations altogether, as zero
floors are skipped, and rejected it: that would lose genuine millisecond hops in precise
logs, which are exactly what the detector exists to find.

**Knob and setting validation.** The Bucket `choice` was sent as a string to an int
`Literal`. Numeric choices now send numbers, and the model also accepts the string form,
since a stored detector keeps the client's shape. The settings accepted bucket widths,
zones and correlation ratios that the runner refuses. The accepted bucket widths and the
zone check now live in `core/time_of_day.py`, where both sides read them.

**Dependencies.** All 14 open Dependabot PRs were merged at once. Two of them only pass
together with a partner: `react` needs `react-dom` at the same version, and `vitest` needs
`@vitest/ui` pinned to it, or `npm ci` stops on ERESOLVE. The frontend lockfile was
regenerated once over the merged result.

## Session 241 — 2026-09-16: value correlation (D13)

The third and last cheap AMiner analog, `value_correlation`, in the same one-commit shape.

**Rules, not pairs.** Value combos already find a rare `(x, y)`. What this detector adds is
the *rule*: an antecedent value `x` with at least 20 reference events whose dominant
consequent `y` covers at least 95 % of them, mined in both directions per field pair, and
tested per window with the 2×2 G-test proportion shift already has — conforming against
violating events, reference against window, one BH pool, a 2× floor on the violation-rate
ratio. The roadmap's design problem was field-pair explosion, and the answer is three caps
that each disclose themselves: auto mode pairs the recommender's top six categorical
fields (15 pairs), more than 20 pairs are truncated with a warning naming the count, and
each pair's `GROUP BY a, b` is capped at 5000 highest-volume rows with a warning that a
tail antecedent was not tested. Identifiers never enter auto mode, which is what keeps the
pair table small in the common case.

**Only rises, only breaks.** A rule that appears in a window is a value whose share rose,
which proportion shift owns; a rule that tightens is not a finding. And the self frame
mines its rules over the whole scope, so a rule broken from the start is not a rule and is
never tested — stated in the reference section as the frame's limit rather than hidden.
The finding carries `top_violator`, the consequent value that most often took `y`'s
place, because "where did it go instead?" is the first question an analyst asks of a
broken rule and the data to answer it was already in the scan.

**The demo needed nothing.** Every human account has one or two home workstations
(`_home_hosts`); the contractor has one, so `m.okonkwo ⇒ WKS-004` holds over 6,500
baseline logons at confidence ~1.0 and breaks on the jump host, the file server and two
finance workstations. Two-home users never form a rule (confidence ~0.5), the
administrator's hop is two events a day against 280 and keeps their rule intact, and in
the self frame the contractor's violations sit in the last five of 24 slices. Asserted in
both frames with `fields=["attr:user", "attr:computer_name"]`.

**The agent schema budget, again.** Adding `rule_confidence` to `run_anomaly_detector` was
absorbed by the compact docstring from session 240; the measured total is recorded in
`AGENT.md`.

## Session 240 — 2026-09-16: time-of-day habit (D12)

The second 1.20 detector, `time_of_day`, in the same shape as D15: one commit with its
gate entry, params model, agent knobs, run snapshot, settings, method card, finding type,
evidence figure, reference section and demo signal.

**The timezone is the design decision, and it is a knob plus a setting, not a timeline
attribute.** The roadmap's one requirement was an explicit zone stamped into
`DetectorRun.params`. Inventing a per-timeline zone would be a data-model change
(`MODEL_REFINEMENT.md` territory) for a single detector, so the zone is `stat_habit_timezone`
(server default `UTC`, set once for a site) overridable per run, validated against zoneinfo
and a strict token pattern, then inlined into SQL — ClickHouse takes a zone as a constant,
so it cannot be bound — and recorded on the run and on every finding. `_time_fields.py`
already pins its `toHour` to `'UTC'` for the same reason this detector cannot leave the
zone implicit: the server's zone can change under a stored run.

**Habit = buckets with enough reference mass; distance is circular.** A bucket is habitual
when it holds at least `stat_habit_min_bucket_count` (3) reference occurrences, and only a
value with at least `stat_habit_min_baseline` (20) of them has a habit at all. Score is
the circular distance in hours to the nearest habitual bucket, in multiples of the bucket
width, so 23:xx sits one hour from a 00:xx habit. The self frame is the same rule over the
whole scope: every thin bucket is, by definition, not habitual, and is scored against the
busy ones — which catches a one-off manual run and cannot catch a *repeated* new hour, and
the reference section says so rather than pretending otherwise; that is what the baseline
frame is for.

**One scan per field, bounded by value.** Rows are per (value, bucket, window), so the
per-field cap is 500 values rather than the usual 2000: at a 15-minute resolution with
four suspect windows a value can return 480 rows. The candidate set is the highest-volume
values, selected in a subquery over the same predicate.

**The demo's habits.** `walk()` gives every hour of the day a non-zero weight, so every
high-volume value (any human account, any home workstation) is habitual round the clock —
correct behaviour, and it means the demo signals had to come from low-volume scheduled or
role-bound streams. Baseline frame: the nightly backup program at 03:xx/04:xx against
three weeks of 02:xx (benign, deliberately the same move interval cadence already sees),
and the contractor on `JUMP-01` at 03:00 — the lateral-movement leg now visits the jump
host first, at night, and the jump host's baseline logons are the administrator's hop from
session 239, all office hours. Self frame: one manual backup run on a baseline afternoon,
twelve hours from the program's nightly slot; without it the self frame's only hit was two
runs that happened to spill past 04:00, which is the kind of RNG accident this file's
demo-coverage rule exists to replace with a fabricated signal.

**Environment note.** This machine cannot run rootless podman, so PostgreSQL 16 and the
pinned ClickHouse 26.6.1.1193 run user-space from `~/.local/share/vestigo-devstack/`; the
`embeddings` extra was installed to match CI's `--all-extras`. Qdrant is still absent, so
five case-delete tests (`test_stories_api`, `test_rbac_api`, `test_demo_api`) 502 here and
only here; they are unrelated to this work and pass in CI.

## Session 239 — 2026-09-16: transition speed (D15), the first 1.20 detector

The 1.20 cluster is the three cheap AMiner analogs left on the roadmap — D15, D12, D13 —
landed one commit each on `feat/1.20`, in that order because each reuses machinery the
previous fortnight touched. This session is D15, `transition_time`.

**What it is.** A transition is one step `a → b` between consecutive events of one stream
whose series-field values differ; the detector learns each pair's fastest transition and
reports one that undercuts it by `min_ratio`×. The stream is the source, split by a new
`partition_field` (the identifier whose moves are timed), which is the knob that makes
the question meaningful: without it a Windows log carrying every account times the gap
between *any* two consecutive logons, and two users on two hosts in the same second reads
as an impossible move. Rows without a partition value are left out rather than pooled
into one anonymous stream, for the same reason.

**Built on `_ngram_inner_sql`, not beside it.** A transition is an n-gram of length two
with `gram[1] != gram[2]`, so the assembly, the per-source scan discipline, the
record-order tie-breaks and the window-boundary guarantee are the sequence detectors'.
The helper gained an optional `partition_col` (added to every `PARTITION BY`, `None`
keeps the sequence detectors' SQL shape) and now emits `pkey` and the arriving event's
`last_eid` — the representative event is the `b` side, the one that arrived too soon.
Adding columns to a shared subquery is what moved `CACHE_VERSION` to 5, not the new
method id, which could not collide with an old key on its own.

**Both frames from day one (D18 is a rule now, not a migration).** Baseline:
`min-transition`, the floor is the pair's fastest baseline-window transition over at
least `min_transitions` of them, learned only for the candidate pairs the suspect scan
surfaced (Query B is bound to Query A's grams). Self: `self-min-transition`, the floor is
the pair's *next-fastest* transition anywhere in the scope — `groupArraySorted(2)` per
source, the two smallest merged across sources — which is leave-one-out by construction,
and two equally fast transitions vouch for each other. A zero floor is skipped in both
frames and counted in a warning: with second-resolution timestamps zero-length
transitions are routine, and "faster than instant" is not a claim. The self mode was
first named `loo-min-transition`; the demo coverage test's `self-`/`rare-` prefix
convention is the right one, so it is `self-min-transition` like its siblings.

**The demo had no such signal.** The baseline frame found nothing on the demo case: the
contractor's lateral moves are minutes apart while every account's random alternation
between its own home hosts produces baseline floors of seconds. Per this file's own rule
the fabricated signal was strengthened rather than the assertion: one administrator now
has a routine jump-host hop (JUMP-01 by RDP, FILE-01 by network logon 20–90 s later,
twice a working day, kept tight so their own workstation logons rarely fall between), and
each wmic remote process creation during lateral movement now logs the contractor on to
FILE-01 two seconds later — which from JUMP-01 is the administrator's pair at a tenth of
the time. Both frames assert on it.

**Surface.** Gate entry (same `series_distinct ≥ 2` floor as sequences: one value has no
transition; `needs_setup` in the baseline frame without a baseline), `_TransitionTimeParams`
(`series_field`, `partition_field`, `min_ratio`), the `/anomalies` and tag endpoints and
the agent tool gain `partition_field`, the persisted run snapshots `partition_field` and
`min_transitions`, three settings with registry specs. Frontend: the thirteenth method
card (`Gauge` icon, a "Stream" field knob), `TransitionTimeFinding`, a two-bar evidence
figure labelled by `reference_kind`, verdict/normalize/subject cases, and the mode sets in
`finding-frame.ts`. Docs: `ANOMALY_DETECTION.md` §15, the tool list, demo table and gate
table; README and CLAUDE.md counts.

## Session 238 — 2026-09-15: review of the D18/D19/D11 branch (PR #377)

Five findings from a review of the branch, all fixed before merge. Two changed what the
self frame reports; three closed gaps between what a run did and what it said it did.

**The self frame's complement was the whole scope (the one that mattered).** Proportion
shift's self mode already tested only the slices overlapping a value's `[first, last]`
span, but it measured them against `scope_total − slice_total`. That divides a
short-lived value's rate by the fraction of the timeline it lived through: a value present
in 2 of 24 slices at a 10 % share in each has a ~0.8 % whole-scope complement share, a
ratio of ~12×, and fires "up" in *both* slices of its own life — and so does every value
whose life covers less than half the timeline. Churning identifiers (session ids, ephemeral
hostnames, short-lived processes) are exactly this detector's target, so it was systematic,
not an edge case, and it scaled with the candidate cap (2000 values × 15 fields). The
complement is now drawn from the value's own active span.

The first attempt at that also dropped values confined to a *single* slice, on the argument
that they have no within-life complement and value novelty covers them. It does not:
novelty's rarity floor is three occurrences, and the demo/test case for this frame is a
500-event burst inside one hour — confined, not rare. `_find_proportion_shifts_self`'s own
docstring had said so ("a one-slice burst would otherwise be uncovered"). So the two cases
are split: 2+ slices measure against the active span, exactly 1 slice keeps the whole-scope
complement and reads "up" against `rest_count = 0`, which is the finding. Findings now carry
`value_active_slices`, `rest_frame` and `rest_slices`, because a rate ratio cannot be read
without knowing which denominator produced it. `CACHE_VERSION` moves to 4 — this is the
runner answering differently for identical inputs, which is what that constant is for.

**Self-drift's numeric branch was unbounded.** It issues one whole-scope KS scan per
(field, slice) — deliberately, since each KS state holds the whole field and K-in-one is K×
memory. But that is `_MAX_AUTO_SCAN_FIELDS × stat_self_slices` full passes in one request:
360 at the defaults, 1800 at `stat_self_slices`'s ceiling, and the analysis cache cannot
help the *first* run on a timeline, which is the one that would time out. The eligible pairs
now spend a `_MAX_SELF_KS_QUERIES` budget (60) through `_spend_ks_budget`, round-robin across
fields — the same quota-with-backfill rule `_select_auto_scan_tokens` uses for the field cap,
because a prefix truncation would scan the first fields exhaustively and report on the last
ones without scanning them. A budgeted run loses slice resolution, never a whole field, and
discloses the shortfall: an unrun slice is not evidence of nothing.

**Three disclosure gaps.** `_small_slice_warning` counted only slices with
`0 < t < 50`, so *empty* slices — tested by no runner, skipped by none either — were
invisible; a timeline with one dense day and a sparse tail advertised 24 slices while four
carried every test. `SelfSlices.bounds` returned a last-slice end up to `K−1` ms past the
span (`width_ms` is rounded up), and that end goes into the hashed run snapshot and the
Explorer's range highlight, so the record asserted a boundary past any event that exists; it
is clamped now, with `covers()` keeping the unclamped edge for overlap arithmetic so a value
first seen at the very last event still belongs to slice `K−1`. And `seedFromParams` seeded a
stored `variant: "shannon"` verbatim although the form spells a choice's default `""`,
handing the `<select>` a value no `<option>` carried — the browser then showed the first
option while the form state said otherwise.

Tests: `_spend_ks_budget`, `SelfSlices.bounds`/`covers` and `_small_slice_warning` are pure
and unit-tested in `test_anomaly_stats.py`; the complement change is covered in
`test_self_frame_clickhouse.py` against `late` (uniform across day 3, now silent) and `eve`
(one-slice burst, still found). Frontend: `methodKnobSeed.test.ts`, including a
registry-wide invariant that every seeded choice value is one the select can render.

## Session 237 — 2026-09-14: a baseline never restricts a detector (D18), deterministic quantiles (D19), entropy bigrams (D11)

Three Milestone 4 items on one branch, in the order the roadmap ranked them by truth of
shipped claims. Design for D18 was approved in issue #366 and lands as `ANOMALY_DETECTION.md`
§7–§10; the other two were spelled out in `ROADMAP.md`.

**D19 first, because it changes existing findings.** Every detector quantile — the range and
entropy fences that *gate* findings, the interval medians, the drift quantiles, the motif
median — was plain `quantile`, a random-generator reservoir above 8192 values. It is now
`quantileDeterministic` keyed on `cityHash64(event_id)` (or the distinct value where the
population is one row per value). `quantileExact` was rejected on purpose: it holds every
value in memory, which is the failure the scan-memory work just fixed on high-cardinality
fields. A real-ClickHouse test runs each detector twice over 40k rows and compares details.

**D18: the self frame.** Each of the four detectors dispatches to a private `_find_*_self`
beside its temporal sibling; baseline-frame code is byte-for-byte unchanged.
- *Slices* (`SelfSlices`): integer-millisecond boundaries over the effective span,
  `least(K−1, intDiv(dateDiff(ms), width))`, payload and `config_hash` persisted as
  `slices` / `slices_hash`. Proportion shift tests each in-span slice against its complement
  (no first-seen exclusion — a one-slice burst has `rest_count = 0` and is the finding);
  drift folds the top-50 categories by complement count, and runs one KS query per slice
  that has both sides above `min_samples` — a count scan first, because
  `kolmogorovSmirnovTest` throws on an empty side, and `assumeNotNull` on the argument,
  because the plain aggregate over a Nullable column returns a Nullable named tuple the
  driver cannot decode.
- *Cadence*: two passes per field. Pass 1 collects per-value deterministic gap quartiles,
  the longest internal gap with both ends, and first/last arrivals. Pass 2 binds each
  candidate's pause threshold through `transform(val, …)` and aggregates the retained gaps;
  a third pass joins per-(value, source) last arrivals to per-source last events for the
  trailing gap. The silence null is a Gamma fitted from the robust CV and the median
  (`_gamma_sf` extracted from `_chi2_sf` at a non-integer shape), Šidák over the value's
  gaps. The fabricated test data first said "trailing" where I meant "internal" — the
  heartbeat ended a day before its source did, and the detector was right.
- *Sequences*: the existing n-gram assembly with one pseudo-window, a rarity floor, and an
  exact case-wide recount on multi-source scopes.
- The gate's `needs_windows` is gone: only the baseline frame without a baseline is
  `needs_setup`; the self frame is gated on shape, and a one-instant span is the slice
  methods' one structural impossibility. `CACHE_VERSION` 2 → 3, since the old
  `insufficient_data` was cached under exactly the new key.
- Frontend: `lib/finding-frame.ts` reads `details.method`; rows, verdicts, evidence figures
  and the scope line branch on it. Whole-scope beaconing draws no figure — its claim is the
  regularity, stated in the verdict. The wizard's `NEEDS_BASELINE` is deleted.

**D11.** A `variant` knob (`shannon` | `bigram`), not a params key named `method`, since
`method` names the analysis method everywhere else. The bigram table is learned from the
reference population's distinct values in two spillable `GROUP BY` passes and bound into
the scoring scans as parallel arrays via `transform` — a `Map` parameter does not bind
through the driver — capped at 4096 pairs with a warning. The real-ClickHouse test is the
headline case: a lowercase-latin DGA among English hostnames, missed by Shannon, top under
bigram.


## Session 236 — 2026-09-12: the second review round, and the dependency bumps

A second review of PR #376 reported five findings (one medium, four low); each was checked
against the branch before it was fixed, and all five were real.

**One cache fill is an optimization, not the answer.** Session 235 routed every interactive
field-stats fill through `ensure_field_stats_for_request` so a full gate answers 503. That is
right wherever the cache *is* the answer, and wrong for `viz.get_field_terms`, whose live
fallback runs on the foreground lane: a miss behind a sweep turned an answerable chart into a
retry loop. It now catches `ScanBusyResponse` and goes live; a disconnect still propagates.
The busy path skips `merged_field_terms` rather than passing it `{}` — empty stats read as
"zero coverage" and would have answered a chart with no values.

**Phases.** `_run_ingestion_job` is shared with the convert-and-ingest job, whose phase map had
no `field_stats` key, so the tray went blank exactly where the phase was added; the key is
there now. `_apply_staged_rows` set the phase per source and never cleared it (`JobStore.update`
merges), labelling each later partition rewrite as field statistics; a `finally` clears it.

**Settings an older server refuses.** `query_plan_join_swap_table` *and* the 1.19.5
`max_bytes_ratio_before_external_sort` first appear in 24.12 (`system.settings_changes` on
26.6), and ClickHouse rejects an unknown setting, so every scan failed on an older server. The
review named only the first. The clause still carries both — the forward-looking reason for
keeping the JOIN bound in every clause stands — but `ClickHouseStore.init_schema` probes
`system.settings` once per process and `_scan.configure_server_settings` drops the ones the
server lacks, which on such a server is already their effect. An unreadable probe records
nothing and sends both. The probe lives in `init_schema` rather than the startup probe in
`api/main.py` because the CLI never runs the latter. Under test, the first `init_schema` can be
a recording fake whose canned rows read as "knows none" and stripped both settings for the
rest of the run (it failed `test_every_scan_clause_bounds_a_join_from_its_own_cap`), so an
autouse fixture pins the probe off; its own tests switch it on.

**Motif cleanup.** `delete_source_events` returned inside its `UNKNOWN_TABLE` branch, skipping
the side-table delete in the one case it most applies to, and the helper did not
`init_schema()` like its sibling, so a never-created table logged a traceback per delete.

**Dependencies.** The twenty open dependabot PRs are folded into this branch rather than merged
one by one (they all touch the same two lockfiles). Two did not apply as written: #363 pinned
`httpcore2` 2.10.0, which `httpx2` 2.12.0 (#362) does not accept, so it resolved to 2.12.0; and
vitest 5 (#375) needs `@vitest/ui` at the same version and no longer bridges jest-dom's global
`jest.Matchers` augmentation into `Assertion` — `tsc` failed on 300 matcher calls while every
test passed at runtime. The setup file now imports `@testing-library/jest-dom/vitest`.

## Session 235 — 2026-09-12: reviewing the 1.19.6 branch before it merges

A multi-agent review of PR #376 (session 234's work) verified 24 findings — ten bugs, fourteen
cleanups — and every one was fixed on the branch. What the review found, and what changed:

**The field-stats gate moved the problem rather than removing it.** Holding a heavy slot in
`compute_source_field_stats` was right; where it was waited on was not.
- Every interactive caller of `ensure_source_field_stats` (Explorer field list, export
  columns, the anomaly field inventory, Visualize terms and fields, the analysis plan, mapping
  validation, field coverage) parked *indefinitely* on a gate full of sweeps — outside any scan
  context, so neither bounded (no 503) nor released on disconnect. Two of them carried comments
  promising no heavy slot at all.
- One `asyncio.to_thread` per miss, each now parked, pinned the process's default executor:
  thirty misses on an eight-core host took every thread from ingest, enrichment and import.
- The post-ingest, enrichment, import and demo-seed refreshes all park their job in `running`
  after the source is already browsable, with nothing saying why.
- Now: `_scan.gated_heavy_scan` is the one decorator for the heavy class (hoisted from
  `anomaly_stats`; the gate is looked up at call time so tests patch one binding), with
  `bounded_heavy_wait(seconds)` as the mirror of `unbounded_foreground_wait`. Request handlers
  fill through `api/scan_exec.ensure_field_stats_for_request`: `run_scan` (cancellable) under a
  five-second bound, answering the same 503-with-queue-depth a full chart lane does. Jobs keep
  the unbounded wait and report a `field_stats` phase the tray names ("queued behind running
  scans"). Fills run at most `gate_size()` at a time, per call. `_resolve_mapping_validation_keys`
  takes an explicit `interactive` flag because the post-ingest re-validation inherits the
  upload request's context and must not be bounded.

**The blast-radius survey missed one.** `find_entropy_outliers` learned its band over
`SELECT DISTINCT` on the same auto-selected free-text fields the charset learner scans — the
shape the survey had just removed. Now a `GROUP BY`; `test_entropy_memory_clickhouse.py`
(3M distinct values, 256 MiB, four threads) is red on the old shape.

**The rewritten routine count.** Its docstring diagnosed the missing per-query cap and the
new query still had none. `count_routine_collapsed` and its two helpers now carry
`foreground_scan_settings()` (a bound, not a reservation — no slot), so the CHANGELOG's 32 MiB
describes production. The three code paths collapsed to one query (empty `IN []` is pruned by
the bloom index; verified on 26.6). `delete_source_events` now drops the source's
`motif_occurrences` rows: the source id is derived from the file hash, so a re-ingest brought
stale rows back into scope where they counted as collapsed and hid nothing. The one-row-per-
`event_id` assumption the count makes is documented where it is made; the CLI's re-ingest path
that violates it is slated for deprecation, not fixed.

**The calculator.** The minimum sized scans at a fixed four-thread width while the runtime
runs `cores // N` wide, so a host with more than 5×N cores that met the minimum ran hungrier
scans than the minimum's cap — the row now names the width and defers a specific host to the
right-hand column. The verdict said "Sufficient" while admitting fewer slots than the minimum
asked for (60 GiB / 12 cores at 1B events: 1 of 6.7 GiB against 2 of 5.1 GiB); it now has its
own wording and a test. The split-host copy said to leave auto alone unconditionally, but an
*unbounded* remote ceiling is clamped by the app host's RAM with no cache subtraction — the
copy, `config.py` and `DEPLOYMENT.md` now state the bounded condition and the pin fallback.
The measured points are data in `gen_sizing_constants.py` and the fit is computed (the
intercept had been hand-rounded by ~2 MiB).

**Cleanups.** The JOIN bounds (`max_bytes_in_join`, `join_algorithm`, swap off) live in
`_scan_settings_clause` beside the GROUP BY and sort thresholds, so every clause carries them
and a fan-out divides them with the cap. The two baseline-window charset learns reuse
`_alphabet_sql`, which is two SELECTs (window over the aggregate, `QUALIFY`) instead of four;
one `_DISTINCT_CHARS_SQL` constant. `maximumPlan` uses `budgetFor`. The three memory tests share
`conftest.insert_generated_events` (built from `_EVENT_COLUMNS`) and `cap_scan`; a `slow`
marker leaves them out of a quick local run (CI runs everything; a `setup-node` step and a
CI-only failure replace the silent Node skip). `tests/test_version_parity.py` checks all five
version files and `scripts/bump_version.py` edits them. The two open decisions live in
`ROADMAP.md`, where the backlog is.

## Session 234 — 2026-09-11: what the 1.19.5 production stack was still failing on

**The report.** A diagnostics bundle from the same airgapped 26.6 stack, now on 1.19.5, still
full of code 241. Two misconfigurations sat underneath everything:
- ClickHouse's container had 64 GiB, but `memory.xml` still pinned the reference 9.5 GiB — the
  trap `DEPLOYMENT.md` already describes.
- `VESTIGO_STAT_SCAN_CONCURRENCY=4` then cut that into 819 MiB per query.

The bundle's two post-upgrade failures were not config alone, though:
- One `interval_periodicity` scan spilled 48 files and died merging them back at that cap.
- Every enrichment apply died in `FillingRightJoinSide`.

**The enrichment rewrite never spilled.** Reproduced on the pinned binary:
- The `grace_hash` join splits into buckets only past `max_bytes_in_join`, which was unset (0 =
  never), and `query_plan_join_swap_table = auto` put the full-width `events` rows on the
  in-memory side (EXPLAIN: `Type: RIGHT`, events as the right table).
- Memory grew with the source: 2M events failed at 1, 2 and 4 GiB; 4M failed at 4 GiB.
- `max_bytes_ratio_before_external_join` was a red herring — setting it changed nothing; it only
  converts hash joins to grace.
- Fix, in the scan settings clause itself (session 235 moved it there from a helper the one
  call site used): bucket at a quarter of the cap, swap off, so the
  narrow staged maps are the build side. ~800 MiB at a 1 GiB cap on 2M and 4M events alike,
  output verified row for row.
- Swap-off is not what makes it pass (the bucket limit alone passed). It is what keeps a
  billion-event source inside `grace_hash_join_max_buckets` (1024).

**Blast radius.** A survey of every ClickHouse query for memory that escapes the per-query spill,
each finding checked against the code and the server before acting on it:
- **Charset novelty** learned every alphabet through `SELECT DISTINCT` (a set that cannot spill)
  under a frameless `count() OVER ()` (buffers every distinct value). Three million distinct
  values failed in every mode.
  - Now a `GROUP BY`, with the distinct total counted off an empty-string marker (`extractAll`
    never yields an empty match); column shape unchanged, so the Python loop and mock tests are
    untouched.
  - Verified identical per-character counts and totals against the old SQL.
- **`count_routine_collapsed`** — no settings clause, runs on every collapsed Explorer page —
  held `uniqExact` over every muted event's id: 1 GiB for 20M ids, charged to the server's
  ceiling. Now inclusion–exclusion (`|T| + |M| − |T∩M|`), exact because `event_id` is one row's
  provenance hash.
- **`ensure_source_field_stats`** gathered every cache miss with no gate slot, each a set of
  full-cap scans. `compute_source_field_stats` now holds a heavy slot; no caller holds one, so it
  cannot self-deadlock.
- **Not bugs, on measurement:**
  - The top-level `uniqExact` inventory columns are LowCardinality.
  - Window scans with explicit frames spill (already tested).
  - The grouped charset violation scan needs ~450 MiB at four threads over 3M rows with 20
    groups *and* with 2000 — not the group-count scaling its per-row constant arrays suggested.

**The sizing calculator was sized on a guess.** Its scan requirement was 12 MiB per million
events. Measured instead — smallest cap each corpus completed under, heavy clause, five threads:

| Events | window-sort peak (passing cap) | high-cardinality GROUP BY peak (passing cap) |
|---|---|---|
| 2M | 0.97 GiB (1 GiB) | 401 MiB (512 MiB) |
| 4M | 1.21 GiB (2 GiB) | 718 MiB (1 GiB) |
| 8M | 1.39 GiB (2 GiB) | 714 MiB (1 GiB) |
| 16M | 1.97 GiB (2 GiB) | 991 MiB (1 GiB) |
| 32M | 2.36 GiB (4 GiB) | 1.24 GiB (2 GiB) |

10 and 20 threads at 16M needed a cap one step higher than 5 threads did.

Memory follows log2(events), because the sort spills; spill disk and runtime follow events
(11.8 GiB and 45 s per field at 32M). The calculator now:
- Uses a least-squares log model of that table: `scanNeed()` in the page, `scan_memory_model`
  in the generator, with provenance.
- Walks N down from the core bound until a slot fits the corpus's need at the width N leaves.
  It used to divide by a fixed 2 GiB floor, which recommended ten 3.4 GiB slots for a
  5 GiB-per-scan timeline.
- Has an events slider reaching 10B.
- No longer tells a split-host deployment to pin the budget (the app's probe asks ClickHouse
  over its own connection).

`tests/test_sizing_calculator_behavior.py` runs the page's JavaScript under Node and asserts its
recommendations against the measurements.

**Every fix went red first** on the committed code:
- `test_enrichment_apply_memory_clickhouse` (1 GiB, 2M events)
- `test_charset_memory_clickhouse` (256/512 MiB, 3M distinct values; red against a stash of the
  old SQL)
- `test_routine_collapse_memory_clickhouse` (32 MiB, 3M muted events)
- `test_field_stats_admission_clickhouse` (peak in-flight 2 → 1)
- the calculator tests

Two things measured and left alone — the per-key `uniqExact` in the field-stats cache, and
the calculator's missing spill-disk line — are Milestone 3 items in `ROADMAP.md`.

## Session 233 — 2026-09-11: the spill that was asked for and never fired

**The report.** An airgapped v1.19.x full-docker stack, configured from the sizing guide:
`interval_periodicity` against a baseline definition failed with code 241 at an 819.20 MiB
cap, `while reading column attributes.key_src_ip`. 819.20 MiB is exactly
`VESTIGO_STAT_SCAN_CONCURRENCY=4` on the reference 9.5 GiB ceiling. The query is unchanged on
`main`, so an upgrade would not have helped.

**Reproduced exactly** on the pinned `26.6.1.1193` binary with the events DDL and 30M synthetic
events (~26M inside the windows): same message, same column, same cap. The documented
explanation — window-function sorts cannot spill — fell over at the first control: a plain
`ORDER BY` of the same rows died identically, even with `max_bytes_before_external_sort` at
100 MiB. The server's trace named the cause: `SortingStep: Adjusting memory limit before
external sort with 1.24 GiB (ratio: 0.5, available system memory: 2.47 GiB)`. Since 25.1,
`max_bytes_ratio_before_external_sort` defaults to 0.5, and a sort then spills only once the
query also holds half of the server's free memory — a number unrelated to the per-query cap
and far above it. With the ratio at 0, the plain sort *and* the window sort spill, and the
detector finishes at peak ~490 MiB.

**The fix** is one line in `_scan_settings_clause`: `max_bytes_ratio_before_external_sort = 0`
on every heavy and foreground scan. Verified end to end: the real `find_interval_periodicity`
at a pinned 819.20 MiB cap over the 30M-event corpus fails in 4.6 s on the old clause and
completes in 29.6 s on the new one. `tests/test_scan_spill_clickhouse.py` runs a sort (heavy
and foreground clause) and an `interval_periodicity`-shaped `lagInFrame` several times larger
than a 256 MiB cap; all three fail with 241 without the line.

**The GROUP BY ratio is deliberately not zeroed.** A first cut zeroed both, on the assumption
that aggregation was overridden the same way. A controlled probe (string keys, roomy cap,
128 MiB threshold) spilled eight times with the ratio on and eight times with it off: for
GROUP BY the ratio only *lowers* the absolute threshold, which is protection under memory
pressure, not an override. What the GROUP BY tests did turn up is a separate, pre-existing
headroom problem — a high-cardinality aggregation can die inside its own spill write at the
cap ÷ 2 threshold — now a `ROADMAP.md` item rather than a guess at a new divisor.

**Docs corrected.** Every "window sorts cannot spill" in `ANOMALY_DETECTION.md`, `config.py`,
`settings_registry.py`, `queries.py` and `anomaly_stats.py` now says what bounding a window
scan per source is actually for (fewer spill files; reading them back costs memory per file).
The separate claim that a frameless `OVER ()` buffers every group was left alone: a quick probe
of `count() OVER ()` over 20M rows passed under a 256 MiB cap, which neither confirms nor
reproduces the production failure those comments describe.

## Session 232 — 2026-09-10: three bug reports, three different answers

**1. `apache2timesketch` emits an empty `src_ip`.** Not what the report said — the column is
in every header and populated for combined, CLF, vhost-prefixed and 2.4/2.2 error logs, so
this was reproduced by variant rather than accepted. What empties it is the token: `%h` is
`\S+` to the parser and `normalize_ip` returns `""` for anything that is not an address,
which covers `HostnameLookups On` and — the quiet one — a `%v %h %u` LogFormat, where the
combined-format regex matches with the *vhost* in the address position, the real client
lands in `remote_ident`, and the vhost-stripping fallback never runs because the first parse
"succeeded". Neither shows up in the unparseable count. A comma-joined `X-Forwarded-For` in
`%h` fails every arm and drops the row outright — counted in the converter's own summary,
invisible once the CSV is ingested.

Fixed upstream and re-vendored at `8dd67db` (1.4.1) as 1.19.4: both converters emit
`client_host` (the `%h` token as logged, always populated) and populate `src_ip` only
when it validates, the address slot accepts a comma-joined chain and resolves to its
leftmost entry, and Apache's vhost fallback re-runs whenever the first parse found no
address. The dropped-row case now parses; every previously-correct row is byte-identical.
The first cut of that chain pattern, `\S+(?:,\s*\S+)*`, reached upstream master before CodeQL
flagged it on the re-vendor PR: `\S` matches the comma, so element and separator overlap and a
line of repeated `!,` backtracks exponentially — 95 ms at 20 repetitions, ×4 per two more, on
input written by whoever sends a request. Each element is `[^\s,]+` in 1.4.1 (0.6 ms on a
10 kB witness). No local test would have caught it; the scanner did.

Blast radius, surveyed across all 28 vendored converters: the split is between
`normalize_ip(x) or x` (haproxy, w3c, exchange, conntrackd — keeps the raw value) and bare
`normalize_ip(x)` (apache, nginx, and the JSON/regex readers whose token is already an
address). Only apache and nginx take a token that can legitimately be a hostname, so only
they lose data; cloudtrail has the best shape of all, keeping `sourceIPAddress` natively and
setting `src_ip` only when it validates. Nothing in Vestigo keys on the *name*: the GeoIP and
ASN enrichers `ARRAY JOIN mapValues(attributes)` and match values against a regex, so the
consequence is an empty field analysts filter and pivot on, plus derived keys landing as
`remote_ident:geo_country`. The fix had to go upstream — the vendored files carry a
do-not-edit header and a manifest sha256 — which is why it landed as its own release rather
than inside 1.19.3.
`INPUT_FORMATS.md` claimed "the GeoIP enricher wants `src_ip`" — corrected, since that
belief is what the evtx converter's own comment repeats.

**2. A source deleted out from under a named timeline.** `DELETE /sources/{id}` had no guard
and the trash button had no confirmation at all: one click cascaded `timeline_sources` and
silently rewrote every analyst-created grouping that listed the source, along with the saved
views, baseline windows and findings declared over that source set. It now refuses with 409
unless `force=true`, and the refusal names the timelines. The default "All sources" timeline
never counts — it tracks the case by definition and cannot be edited to exclude a source, so
counting it would make every delete a two-step for no information. The success audit row
gained `removed_from_timelines`, because the join rows are gone by the time anyone reads the
trail. The dialog lists the same timelines from the already-cached timelines query and waits
for it before enabling the confirm, so `force` is never computed from a list that has not
loaded.

**3. The enricher "Manual" mode does not let you select anything.** Correct, and not a bug:
"Manual" is a *trigger* setting (do not auto-run after ingest), the dropdown is disabled
until the row's switch is on, and there has never been a field picker — an enricher matches
attribute values against its own pattern across every field. The gap is that nothing said
so, which makes a greyed dropdown read like a failure. The row now states the scan scope and
the `<field>:<output>` naming contract up front (`output_fields` added to the timeline
listing so it names the real output rather than hardcoding GeoIP's), and both the enabled and
disabled dropdown carry a title explaining what the setting is and why it is unavailable. A
genuine per-timeline field scope is filed in `ROADMAP.md` behind Milestone 11 P4 —
`SourceEnrichment` records only the enricher's `config_hash`, so a source enriched under a
narrow scope would read as "already enriched" when the scope widens, and P4 is already
adding the `detail` column that fixes it. Building it first means building it twice.
