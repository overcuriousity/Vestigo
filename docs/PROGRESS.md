# Vestigo Implementation Progress

Last updated: 2026-07-24 (session 92 — agent context-window budget fix, 1.6.1).

Append-only session log, newest entry on top. Sessions 1–70 are archived in
[`docs/archive/PROGRESS_SESSIONS_01-70.md`](./archive/PROGRESS_SESSIONS_01-70.md).

## Session 92 — 2026-07-24: agent context-window overflow fixed (1.6.1)

**Why.** An analyst investigation died with no error surfaced in the UI. The LiteLLM body:
`request (75967 tokens) exceeds the available context size (65536 tokens)`. The sliding
window (`agent/window.py`) is supposed to make this impossible — it runs before *every*
model request — yet it let a 76k-token request through a 49k budget without eliding
anything. Not the mid-conversation model switch (that raised the budget); the window failed
at a fixed budget. Four root causes, all shipped fixed.

**1 — the budget never counted the tool schemas.** `budget_for` reserved history + system
prompt but not the advertised tools, which ride *outside* the `messages` the window
processor sees. 14 of 28 tools each carry their own copy of the `FilterSpec` definition
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

