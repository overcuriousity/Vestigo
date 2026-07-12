# Vestigo Implementation Progress

Last updated: 2026-07-12 (session 54 — release 1.0: rename to Vestigo, brand, changelog,
release workflow).

## Session 54 — 2026-07-12: release 1.0 — TraceSignal renamed to Vestigo

Release-engineering session; no feature work. (Historical entries below deliberately keep
the old TraceSignal name — they are point-in-time records.)

- **Rename (full):** package `src/vestigo`, CLI `vestigo`/`vestigo-web` (was `tsig`/
  `tsig-web`), env prefix `VESTIGO_` (was `TS_`), DB defaults (`vestigo` for Postgres db/user,
  ClickHouse database, Qdrant collection prefix), converter scripts `*2vestigo.py`, Parquet
  footer keys `vestigo.*` with a `tracesignal.*` read fallback in
  `ingestion/parquet_format.py`. `docs/archive/` and this file's older entries keep the old
  name. Manifest hashes refreshed. Name chosen after availability check (PyPI/GitHub);
  *vestigo* = Latin "I follow the tracks".
- **Brand:** new geometric step-track mark (one band out of cadence = the anomaly) as
  `frontend/public/favicon.svg` (prefers-color-scheme aware), `VestigoMark` component in
  TopBar/LoginPage, `docs/assets/logo{,-dark}.svg` wordmarks, README `<picture>` header.
- **Version 1.0.0** in `pyproject.toml` + `frontend/package.json`; new `CHANGELOG.md`
  (Keep-a-Changelog) with a condensed 1.0.0 entry incl. rename migration notes.
- **Release workflow** `.github/workflows/release.yml` on `v*` tags: backend/frontend gates
  (mirrors ci.yml incl. tag-matches-pyproject check), buildx image push to
  `ghcr.io/overcuriousity/vestigo` ({version}, {major}.{minor}, latest), GitHub release with
  the CHANGELOG section as notes.
- **Docs:** README gets ghcr pull instructions and a "Stability & upgrades" section (Alembic
  auto-migration, Parquet v1 stability, append-only config-hash identity, honest
  no-in-place-migration statement for ClickHouse/Qdrant); SECURITY.md supported-versions
  table now 1.0.x.
- Remaining manual steps: merge PR, `gh repo rename Vestigo`, tag `v1.0.0`.

## Session 53 — 2026-07-12: vendor newest 2timesketch converter batch

Re-synced the vendored converter suite to upstream `overcuriousity/2timesketch` commit
`d6e5e61` (was `303869e`): four new stdlib-only converters — `apache2timesketch.py`,
`cowrie2timesketch.py`, `evtx2timesketch.py`, `syslog2timesketch.py` — added to
`scripts/vendor_converters.py`'s `CONVERTERS` table and vendored into
`src/tracesignal/assets/converters/` (manifest now 17 entries). Existing vendored files
changed only in their commit stamp. New in the vendor script: `MODULE_DEPS` — upstream's
`apache.py` imports from its sibling `nginx.py`, so dependency module bodies are now inlined
before the module body (the module's own top-level definitions shadow same-named dependency
ones; `X as Y` imports become alias assignments capturing the dependency's originals).
Verified: all 11 vendored scripts run standalone, apache parses combined-access and 2.4-error
sample lines correctly, `tests/test_converters_api.py` passes. Native `*2tracesignal.py`
Parquet ports of the four new formats added to M25's remaining list.

## Session 52 — 2026-07-12: M24 scan-avoidance + L1 removal + X3 grid indicator

User-prioritized hardening batch (user-friendliness/stability/speed over features); new
features (D10, Milestone 5) deliberately not touched.

**M24(b) — fused `field_value_timeseries` (`db/queries.py`).** Top-N value selection and
per-bucket counting merged into one nested-aggregate scan (no window functions — they can't
spill): inner `GROUP BY (sentinel-flag, bucket, value)`, outer ranking on total count with
sentinel rows included (matching `field_terms`' ranking exactly) while `groupArrayIf` keeps
sentinel buckets out of the plotted series. 3 scans → 2 without an explicit window, → 1 with
one (the common brush-zoom case). The range scan stays deliberately separate (grid must cover
all filtered rows, not just rows carrying the field). Live equivalence test with the retired
two-query flow as oracle (`tests/test_viz_timeseries_fused_clickhouse.py`): ties at the top-N
boundary, sentinel rows, clock offsets, mapped fields, high cardinality.

**M24(a) — cached first-load `field_terms` (`db/field_stats.py`, `api/routers/viz.py`).**
`STATS_VERSION = 2`: the per-source stats payload now carries per-field top-50 value counts
(every top-level candidate column + top 200 attribute keys by coverage; `LIMIT n BY key`
pattern; `HEAVY_SCAN_SETTINGS` added to all four per-source stats queries). Fields with any
top value over 256 chars get no list — cached values are served verbatim, never truncated.
`merged_field_terms` reproduces the exact live response shape; the cross-source top-N merge
is approximate (documented next to the distinct-max note; single-source timelines exact),
counts/total/other_count exact. The endpoint serves the cache only for fully unfiltered,
unmapped queries — skipping ClickHouse and the gate slot — and marks responses
`"cached": true`; any filter, mapped token, limit > 50, truncated key list, or oversized
field falls back live. Self-healing recompute absorbs the version bump (no migration).

**M24(c) — baseline-compare LRU (`db/viz_cache.py` new, `db/queries.py`, `viz.py`,
`core/config.py`).** The baseline comparison layer (filters dropped, timeline scope + primary
window kept) was a full-timeline re-scan on every filtered render. Now memoized in a
process-local thread-safe LRU (`TS_VIZ_BASELINE_CACHE_ENTRIES`, default 64, 0 disables).
Freshness is keyed, not TTL'd: the compare endpoint folds per-source
`source_field_stats.computed_at` + `events_total` into a token (moves on exactly ingest +
enrichment apply; missing stats row → no token → bypass). Time range/buckets and numeric
stats/bins cache exact; terms counts key on the primary's top-N list (hits only when it's
unchanged — accepted). Baseline mode also skips the primary range scan (comparison ⊇ primary,
so union range == baseline range — invariant documented at the construction site). Warm
render: one scan per gate slot instead of 2–4. Live oracle = the unchanged no-token path.

**L1 — legacy split removal (`api/routers/events.py`, `db/anomaly_stats.py`, frontend).**
`baseline_end`/`temporal` request params, the midpoint fallback, and `windows_from_split`
removed; `baseline_id` (saved baseline definitions) is the only temporal input. Verified
safe first: stored `DetectorRun.params` are displayed, never replayed; CLI has no detector
command; frontend only ever sent `temporal: false`. `BaselineDefinition.baseline_end` and
detector `temporal-*` method names untouched. `docs/ANOMALY_DETECTION.md` updated.

**Trailing-bucket loss fix (`db/_buckets.py`), found by the batch's /verify pass.**
`aligned_bucket_starts` excluded the bucket containing `max_ts` (`range` stop is exclusive,
`end_epoch` is that bucket's start), so the value×time chart and the compare time histogram
silently dropped every event in the trailing partial bucket — the newest data — and compare's
reported totals disagreed with the plotted buckets (1980/2000 in verification). Pre-existing
since the helper was introduced; grid now includes `end_epoch`. Explorer's histogram (own SQL
path) and the detectors' `_full_bucket_starts` (deliberately partial-excluding) were never
affected. `tests/test_buckets.py` pins the boundaries.

**X3 — event-grid disposition indicator (`EventGrid.tsx`, `ExplorerPage.tsx`).** Event-scoped
disposition rows (confirmed/dismissed/normal on one concrete event) now render a verdict icon
in the annotation column — Flag > EyeOff > ShieldCheck by priority, tooltip with kind /
detector / note. Data reuses the existing timeline dispositions query (already invalidated on
every verdict); value-scoped rows deliberately excluded from the grid.

## Session 51 — 2026-07-12: Enricher force re-run (poisoned-provenance recovery) + upgrade guard fix

**Enricher force re-run (`api/routers/cases.py`, `EnrichersDialog.tsx`).** Deployments that
hit the pre-session-48c partial-staging bug still carry poisoned `SourceEnrichment` rows:
provenance says "enriched at current config" while most events lack derived fields, so a
manual run reports "Every ready source is up to date" forever — the only documented recovery
was a manual SQL DELETE. The run route now takes `?force=true`, which skips the provenance
filter and re-enriches every ready source (apply is idempotent, so forcing is always safe —
just a full re-scan). The UI surfaces it: after a skipped run, the row's button becomes
"Force re-run" with an explanatory tooltip and the toast points at it. Manual runs (forced or
not) now also write an `enricher.manual_run` audit row with the source/skip lists.

**Enrichment apply memory guardrails (`db/clickhouse.py`).** A force re-run on the production
host killed ClickHouse mid-`finalize_enrichment_apply` (connection refused on 8123 — server
down, matching the session-52 OOM pattern), with M22's fresh `MATERIALIZE COLUMN/INDEX`
background mutations running at the same time. The partition-rewrite INSERT SELECT (whole
partition LEFT JOIN staged rows + GROUP BY into a Map) carried **no** memory limits — the only
heavy query shape without them. It now runs under `HEAVY_SCAN_SETTINGS` (hard per-query cap,
external group-by/sort spill, bounded threads) plus `join_algorithm = 'grace_hash'` so the
join hash table spills to disk. A capped apply that still exceeds the budget fails one job and
crash-recovers from Postgres staging; a dead server takes everything down. Remote-ClickHouse
deployments should pin `TS_STAT_SCAN_MAX_MEMORY_BYTES` to the DB host's RAM (same knob as the
detector scans).

**search_blob upgrade idempotency (`db/clickhouse.py`).** `_ensure_search_blob` early-returned
on column presence alone — a crash between `ADD COLUMN` and `ADD INDEX` would strand the table
without the skip index forever (fast path correct but permanently unpruned, silently). The
guard now requires column *and* index (`system.data_skipping_indices`); every statement is
`IF NOT EXISTS`, so resuming a half-done upgrade is safe. Regression tests for both fixes.

## Session 50 — 2026-07-11: Perf batch A — one-pass novelty scans + indexed text search

**M22 — search-blob text-search fast path (`db/clickhouse.py`, `db/queries.py`).** Broad
free-text search (`q`) was a full ILIKE scan OR'd across 6 columns + tags + attribute
values, issued ≥3× per interaction (page + count + histogram); the old `tokenbf_v1` index
on `message` was dead weight (ILIKE can't use it, the OR-chain defeats pruning) and is
dropped. New `search_blob` MATERIALIZED column: `lowerUTF8` concat of exactly the searched
fields ('\n'-separated, ZSTD(3)), with an `ngrambf_v1(3, 65536, 4, 0)` skip index. When
ready, `add_broad_text_search` prepends `search_blob LIKE lowerUTF8(pattern)` ANDed before
the unchanged OR-chain — a strict superset pre-filter (each field contiguous in the blob;
lowerUTF8 both sides mirrors ILIKE's folding), so **results are identical** with the fast
path on or off (live test incl. `%`/`_`/`\`, `ß`, Cyrillic, `İ`; `EXPLAIN indexes=1`
confirms pruning). Upgrade is automatic and idempotent (`_ensure_search_blob` in
`init_schema`): ADD COLUMN/INDEX, DROP old `message_idx`, then MATERIALIZE COLUMN/INDEX
**async** (`mutations_sync=0`) — startup never blocks on a 300M-row backfill; a
MATERIALIZED column reads correctly from unmutated parts, so only index pruning waits.
`ClickHouseStore.search_blob_ready()` gates the fast path (column present + no pending
`search_blob` mutation in `system.mutations`; True cached forever, False rechecked every
60 s). Operational note: on upgraded deployments the fast path activates once
`system.mutations` drains; failed mutations log a warning but keep the (correct, unpruned)
fast path on. Enrichment REPLACE PARTITION recomputes the blob from post-enrichment
attributes (live-tested).

**M23(b) — batched value-novelty scans (`db/anomaly_stats.py`).** `find_value_novelty` ran
one full `attributes`-Map scan per field (up to 15 per panel-open; ~12 GiB / ~23 s per field
at 300M rows). All plain-attribute fields now share a single ARRAY JOIN pass
(`_batched_attr_novelty_rows`, modeled on `field_inventory`'s memory-safe paired
mapKeys/mapValues pattern): `GROUP BY key, val` + `ORDER BY key, <old per-field order>` +
`LIMIT n BY key` reproduces the per-field ordering and per-field limit exactly, so findings
are **identical** to the old loop (live equivalence test against the retired per-field SQL
as oracle: `tests/test_novelty_batched_clickhouse.py`). Mapped-coalesce and top-level-column
fields keep the per-field query (coalesce can't be one ARRAY JOIN key; top-level columns
never read the map). `_MAX_AUTO_SCAN_FIELDS` stays — it now bounds ARRAY JOIN width, not
round-trips. Finding construction extracted to `_novelty_rows_to_findings`, shared by both
paths.

## Session 49d — 2026-07-11: Visualize v3 — click-to-filter, brush-zoom, punch card / pivot / sankey / scatter, viz scan guardrails

Four-part visualization upgrade (user-prioritized: interactivity + new chart types + speed/
robustness + UX polish; M24 scan-avoidance stays deferred, see ROADMAP).

**Backend hardening (`db/queries.py`).** Every viz aggregation now carries
`HEAVY_SCAN_SETTINGS` and acquires the `HEAVY_SCAN_GATE` admission semaphore — previously
detector-only, leaving chart scans unbounded (the exact stacking pattern behind the
session-52-incident OOM). Gating is on public entry points only; `field_terms` split into a
gated wrapper + ungated `_field_terms_impl` so `field_value_timeseries`/`compare_field_terms`
can't deadlock a `TS_STAT_SCAN_CONCURRENCY=1` deployment (regression-tested with a counting
gate). Also fixed a W2 clock-skew inconsistency: `field_value_timeseries`, `_bucketed_counts`,
and `_union_timestamp_range` bucketed/ranged on raw `timestamp` while `histogram` used the
offset-corrected expression — the time chart and value×time chart could bucket the same view
on different timelines. All three now use `effective_ts_sql` (sentinel guard stays raw).
`query_timestamp_range` grew an optional `settings` trailing-clause param.

**Three new aggregations + endpoints (`api/routers/viz.py`).**
`GET .../viz/time-punchcard` (day-of-week × hour-of-day counts; extraction pinned to UTC via
`toDayOfWeek(ts, 0, 'UTC')`/`toHour(ts, 'UTC')` so server timezone can never reshape the
card), `GET .../viz/field-pivot` (top-X × top-Y co-occurrence matrix: two parallel fused
terms scans fix the axes, one matrix scan folds outside-top-N to `''` per axis — the
compare-terms Other-rollup trick applied twice), and `GET .../viz/field-scatter` (uniform
random sample via `ORDER BY rand() LIMIT n` — no SAMPLE key on the events table — plus a
stats scan so axes/caption describe the FULL data, not the sample). All resolve filters
through the shared `_resolve_event_query`, so charts keep mirroring the Explorer view.

**Four new chart types (frontend, hand-rolled SVG, no new deps — d3-sankey skipped).**
Punch card (`PunchCard.tsx`, the "nights and weekends" view), field×field heatmap
(`PivotHeatmap.tsx`), Sankey flow (`SankeyFlow.tsx` — two-column bipartite stacking over the
SAME field-pivot response as the heatmap, so switching marks refetches nothing), and scatter
(`ScatterChart.tsx`, linear/log with per-axis fallback). `ChartConfig` gained optional
`fieldY` (`c_field_y`) + `limitX`/`limitY`/`sampleLimit` options — **still `v: 1`**
(additive; old frontends degrade unknown chart types to the default "time" chart, verified
by test). Rail: second field picker for two-field charts, per-chart option controls, two new
presets ("When does activity happen?", "How do two fields relate?"). Captions state UTC,
per-axis top-N rollups, and "showing N of M points (uniform random sample)".

**Interactivity — the explore→visualize→refine loop closes.** Click-to-filter on bar /
pie (slices + legend) / heatmap (rows + cells) / line (legend) / pivot cells / sankey
ribbons+nodes: charts report `[fieldToken, value]` entries via a shared
`ChartValueClickHandler` (`viz/lib/interaction.ts`); a `ChartActionPopover` offers Filter in
/ Filter out / Open in Explorer (two-step by design — misclicks on small marks must not
silently rewrite the filter set; filter-out hidden for two-value conjunctions where
per-key AND exclusion wouldn't mean "not this cell"). Other marks never clickable. Brush-zoom
landed on the workbench time chart (`CompareHistogram`, covering compare-off via the
`histogramToCompare` adapter): one overlay drives nearest-bucket hover AND drag-to-select,
ranges snap outward to the server's epoch-aligned bucket grid, and a "Reset range" chip
appears whenever start/end filters are active. Filter writes go through
`filterParamsPreservingChartConfig` — `filtersToParams` builds a fresh `URLSearchParams`, so
without re-copying the `c_*` keys a chart click would wipe its own config out of the URL
(unit-tested). `applyFieldFilter`/`dropMode`/`mapFieldTokenToFilterKey` extracted from
ExplorerPage into `lib/fieldFilters.ts` (behavior-identical) and now unit-tested directly;
Explorer and Visualize share one filter-mutation semantics.

**UX polish.** Compare is no longer silently hidden for unsupported chart types — the
control renders disabled with the reason ("no honest two-layer encoding" vs "not supported
yet"). Stale "treemap" docstring reference removed (`viz.py`). Scale auto-probe skips
field-free (punchcard) and two-field charts so it can't yank the analyst off a deliberately
picked chart type.

Tests: backend suite green incl. new settings/gate/skew/punchcard/pivot/scatter coverage;
frontend 231 tests across 24 files (chartConfig round-trips, fieldFilters unit tests, chart
smoke renders incl. pivot-click and brush-drag simulations, caption lines). ROADMAP: M24
rescoped to scan-avoidance only; new deferred M26 (histogram-implementation unification).

## Session 49c — 2026-07-11: self-review of the day's three commits, all findings fixed

The 8-angle review of `7ef57bb..0b2ad6b` surfaced 10 findings; every one fixed:

- **Tag crash (the real bug):** `tag_anomalies` had no `DistributionDriftFinding` branch —
  drift findings fell into the frequency `else` and 500'd on `r.series_field`. Branch added
  (KS and G-test content strings).
- **Drift correctness:** equal-median KS drifts (pure spread/shape change — exactly what KS
  catches that a median comparison can't) were mislabeled `down`; now `direction="spread"`,
  representative event = the tail that moved outward more. Categorical `top_contributors`
  now includes the `__other__` bucket (a tail-driven drift is headlined honestly), with the
  representative event still taken from the best *named* category.
- **Scan cost:** field classification for drift is now windowed — new shared
  `_numeric_ratio_probe` (also used by `recommend_numeric_fields`, killing the duplicated
  probe SQL) takes the baseline+suspect predicate, so auto-mode drift no longer pays an
  unwindowed whole-case scan `proportion_shift` never paid.
- **API shape:** `DistributionDriftFinding.value` (which held the *window label*, unlike
  every other finding type where `value` is a field value) renamed to `window_label`
  end to end; redundant `bl_tot`/`w_tot` test-dict keys dropped.
- **Frontend robustness:** the show-dismissed cache detection no longer relies on the
  positional `key[-1] === true` contract — `useShowDismissed` now contributes a named
  `"dismissed-shown"`/`"dismissed-hidden"` key segment that `useDisposition` finds by
  content; the KS effect is now worded honestly ("≥D of probability mass moved");
  the show/hide link extracted into one shared `DismissedToggle` (ResultsBar +
  OrderViolationsView); `pct` moved to `lib/format.ts` as `fmtPctAdaptive`.
- **Enricher crash recovery:** the job-run marker now durably records
  `completed_source_ids` (migration `0005`, `mark_enrichment_source_staged` after each
  source finishes staging), so reconciliation grants provenance to exactly the finished
  sources — a crashed 200-source job re-runs 1 source, not 200.

Not changed (reviewed, accepted): categorical GROUP BY still ships up to 10k rows/field on
a misclassified field (needed for the exact `__other__` mass, warned); ROADMAP's D9
"shipped" prose follows the existing D8 precedent; the `dismissed?` field on each finding
interface is enforced by the compiler at the generic access site.

## Session 49b — 2026-07-11: D9 value_distribution_drift detector

Milestone-4 D9 shipped (`detector="value_distribution_drift"`, `method="drift"`), adapted
from AMiner's VariableTypeDetector: per field × suspect window, one whole-distribution
test — numeric fields via ClickHouse's `kolmogorovSmirnovTestIf('two-sided')` over
`toFloat64OrNull` (first use in the codebase; one scan per field, all windows batched
with `-If` combinators, quantiles + drifted-direction argMin/argMax in the same scan),
categorical fields via a 2×k G-test over the top-50 baseline categories + exact
`__other__` bucket (folded in Python from the full GROUP BY, 10k-row scan guard). New
pure-math helpers: general `_chi2_sf(x, df)` (regularized upper incomplete gamma on
`math.lgamma`, df=1 delegates to the erfc form), `_g_statistic_k`, `_tvd`. One BH-FDR
pool across both branches; score `-log10(p)`; effect floors KS D ≥ 0.1 / TVD ≥ 0.05 and
a 20-sample-per-side floor (`TS_STAT_DRIFT_*`), fdr_q request-overridable. Findings are
per-field (allowlist key `(field, "*")`). Wired end to end: router dispatch/serialize/
persistence, config, frontend `DistributionDriftView` (temporal-only, cloned from
interval view) + DetectorAccordion row, 15 new backend tests (incl. hand-computed G and
chi² reference values), `docs/ANOMALY_DETECTION.md` §10 (similarity renumbered §11).
Verified against live ClickHouse: `kolmogorovSmirnovTestIf` parses and clickhouse_connect
returns the named result tuple as a *dict* (`{'d_statistic', 'p_value'}`), not an
indexable tuple — `_ks_pair` normalizes both shapes (caught pre-deploy by probing the
dev server; a tuple-index parse would have KeyError'd in production).

## Session 49 — 2026-07-11: X1 show-dismissed toggle + X2 TriageMeter dispositions

Frontend-only disposition polish (roadmap X1/X2, both closed):

- **X1.** Every detector view can now reveal dismissed findings in place: a
  `useShowDismissed` hook (detector-hooks) threads `include_dismissed=true` into the scan
  request and the query key's last element; `ResultsBar` (and OrderViolations' bespoke bar)
  grew a show/hide link next to the dismissed count; `FindingShell` renders dismissed rows
  dimmed with an EyeOff badge (FrequencyView's bespoke row just dims). `useDisposition`'s
  optimistic update branches on the key's trailing toggle flag: in a revealed cache, a
  dismissal flags the row (`dismissed: true`, `dismissed_count`+1, `total_findings`
  untouched) instead of removing it — matching what a refetch returns; `normal` still
  removes (backend suppresses it regardless). `dismissed?` moved from an intersection on
  the `AnomalyFinding` union into each finding interface so per-detector narrowing keeps it.
- **X2.** TriageMeter "reviewed" now counts event-scoped dispositions, not just user
  annotations: ExplorerPage consumes the `["dispositions", caseId, timelineId]` query
  (already invalidated by every disposition mutation) and `computeProgress` unions those
  event ids into the reviewed set. Value-scoped dispositions stay out — they don't map to
  single events.

Typecheck, oxlint, vitest (199, incl. 2 new useDisposition branch tests) all green.

## Session 48c — 2026-07-11: no provenance off partial enrichment staging

After the OOM crash chain, a manual enricher run reported "no job started" while the source
was unenriched. Cause: `_apply_staged_rows` wrote a full `SourceEnrichment` provenance row for
every source it touched — including sources whose staging was cut short by the crash (applied
via failure cleanup or startup reconciliation). The run route skips provenance-matched sources,
so partial provenance permanently blocked re-enrichment. Fix: `_apply_staged_rows` takes
`complete_source_ids`; the failure path passes the sources fully staged before the error,
startup reconciliation passes none (its scheduled re-run records provenance on success).
Partial rows are still applied (valid values, idempotent rewrite) — the source just stays
eligible. Audit `enricher.applied` now carries a `partial` flag. Recovery for already-poisoned
rows: `DELETE FROM source_enrichments WHERE case_id=... AND source_id=...`, then re-run.

## Session 48b — 2026-07-11: entropy detector no longer explodes chars into rows

Entropy detector hit the per-query memory cap in production (`MEMORY_LIMIT_EXCEEDED`, 4 GiB —
the HEAVY_SCAN_SETTINGS guard worked; server survived, endpoint 500'd). Cause: both entropy
queries computed `entropy(c)` by `arrayJoin`-ing each distinct value's characters out one row
each — every exploded row carried the full value string (+ cnt/first_seen/evt_id in the
violations scan), i.e. O(len²) bytes per value; long-value fields blew past the cap despite
external-GROUP-BY spill. Replaced with `arrayReduce('entropy', extractAll(val, '(?s).'))` —
same aggregate applied to the char array in place, one row per distinct value, linear memory,
and one whole GROUP BY layer dropped from the violations query. Verified identical entropies
against live ClickHouse. Suite 811 passed.

## Session 48 — 2026-07-11: keyset pagination in `list_events` (enricher OOM'd ClickHouse)

A production enrichment job took down ClickHouse (kernel OOM, `Connection refused` on :8123).
Root cause: `ClickHouseStore.list_events` paginated with `ORDER BY event_id LIMIT n OFFSET m`,
but the events table sort key is `(case_id, source_id, timestamp, event_id)` — no read-in-order,
so every batch re-sorted the whole source and materialized `offset + limit` full-width rows;
memory grew with the offset (O(N²) total work per source). Replaced OFFSET with a keyset cursor
(`after_event_id`, `WHERE event_id > cursor`): constant memory per batch, O(N) total.
`iter_source_events` now threads the last event_id of each batch as the next cursor; all its
consumers (embedding pipeline, enrichers) benefit unchanged. Test fakes updated to the new
signature. Full suite 811 passed.

## Session 47 — 2026-07-10: native converters memory-bounded, row order guaranteed (v1.1.0)

A 60 GB nginx log OOM-killed `nginx2tracesignal.py` in the field. Root causes fixed across
**all six native converters** (nginx/filterlog/suricata/timesketch2parquet chunked-parallel;
cloudtrail/pcap per-file-parallel), each bumped to `CONVERTER_VERSION = "1.1.0"`:

- **Parent-side result pile-up (the actual OOM, survived the first field patch).** The
  parallel loops submitted every chunk/file up front and consumed via `as_completed`; the
  `futures` list retained every finished chunk's Arrow IPC bytes for the whole run, so the
  parent accumulated ~the entire parsed file. Replaced with a bounded submission window
  (`workers * 2` in flight) consumed strictly in submit order — ≤ ~2×workers results ever
  resident, and rows land in **original file order** (forensic requirement; previously
  completion-ordered, i.e. nondeterministic).
- **Worker-side bounds:** chunk stride hard-capped at 128 MiB (`<PREFIX>_MAX_CHUNK_BYTES`),
  default workers capped at 4 (`<PREFIX>_DEFAULT_WORKERS`, still `-w`-overridable), and a
  MemAvailable-based warning before parallel runs.

Verified: parallel output byte-identical to sequential for both architectures (nginx 20k rows
across 29 forced chunks; cloudtrail 6 files × 4 workers), full suite 807 passed, manifest
sha256/size refreshed for the six native entries.

## Session 46 — 2026-07-10: PRs #86/#87/#85 merged; prod deploy hotfixes; scan-memory overhaul

Reviewed and merged the three open PRs to main in order (#86 panel fixes → #87 dispositions →
#85 sequence detector; signed merges), with review fixes applied first on each branch:
optimistic-removal now decrements `total_findings` (useMarkNormal/useDisposition), disposition
bulk-create runs in one all-or-nothing transaction, dismissed-rows read skipped on empty scans,
migration 0004 batches its inserts, `EventSequenceView` gained the Load-more wiring, and
`TS_STAT_SEQUENCE_NGRAM` is validated at settings load. Decided: bulk "Tag as anomaly" still
tags dismissed findings (dismissed stays presentation-only).

Prod deploy then surfaced two real-data bugs, fixed forward:

- **Migration 0004 tz-bind crash (Postgres only).** The lightweight `sa.table()` helpers
  declared `created_at` as plain `sa.DateTime` → asyncpg bound `TIMESTAMP WITHOUT TIME ZONE`
  and rejected the tz-aware legacy rows. SQLite (the test dialect) can't catch this. Fixed
  with `sa.DateTime(timezone=True)`; migration verified on prod (allowlist/normal/pinned rows
  all moved, timezones intact).
- **timestamp_order OOM on the 300M-row ViKo case (ClickHouse code 241).** Established
  empirically on prod: **ClickHouse (26.6) cannot spill window-function sorts** — the
  `MergeSortingTransform` feeding `lagInFrame` ignores `max_bytes_before_external_sort` (a
  plain ORDER BY over the same rows spills fine). Fix: `find_order_violations` scans **per
  source** (slim fixed-width columns; `message` hydrated afterwards for the reported rows
  only; 30M-row source ≈ 5 s on prod), and `total_findings` now reports the true case-wide
  violation count. Same restructure applied to `find_sequence_novelty` (ROADMAP X4, resolved):
  per-source totals/novel-gram scans merged in Python plus a cross-source baseline
  verification pass so "never in the baseline" stays case-wide.

Scan-memory guardrails overhauled (`db/_scan.py`): `max_bytes_before_external_sort` added to
`_HEAVY_SCAN_SETTINGS`, and `max_memory_usage` now auto-sizes to `TS_STAT_SCAN_MEMORY_RATIO`
(0.8) of detected RAM — min of cgroup limit, `/proc/meminfo MemTotal` (preferred; `sysinfo()`
overreports on ballooned VMs) and sysconf — with `TS_STAT_SCAN_MAX_MEMORY_BYTES` as the pin
(0 = auto, old 12 GB is the detection-failure fallback). Prod quirk: the box is an LXC guest
(128 GiB) on a ~503 GiB Proxmox host and docker containers bypass lxcfs, so in-container
detection sees 503 GiB — prod pins 64 GiB via compose env (part of the stash/pop-preserved
local diffs). Documented in the query-cost discipline section.

Verification: full backend suite 807 pass (services up), frontend typecheck/lint/197 green,
prod app healthy with the 0004 migration applied and detector scans verified directly against
the live 300M-row ClickHouse.

## Session 45 — 2026-07-10: D8 — event-sequence novelty detector (`sequence_novelty`)

Shipped roadmap D8 (AMiner `EventSequenceDetector` analog) end to end. Per source, events are
ordered by effective timestamp (record-order tie-breaks) and every run of n consecutive values
of one grouping field forms an n-gram; n-grams present in a suspect window but absent from the
baseline window are flagged, scored `-log(count / window_ngram_total)`. Temporal-only
(no self-baseline mode); counting is case-wide (n-grams built per source, counts summed).

**Backend (`db/anomaly_stats.py`)** — new `SequenceFinding` dataclass +
`find_sequence_novelty(series_field="artifact", ngram=3, …)`. Sequences are assembled entirely
in SQL: a `lagInFrame` chain over `PARTITION BY source_id, w_idx` (window index via a
`multiIf` over `_window_preds`), so an n-gram never mixes sources or spans a window boundary;
a `toNullable` lag guard drops incomplete leading n-grams (same trick as timestamp-order).
Two queries: per-window complete-n-gram totals (denominators + small-window warnings), then
the novel-gram GROUP BY with per-window `countIf`/`minIf`/`argMinIf` blocks (value_novelty
temporal shape), capped at `TS_STAT_SEQUENCE_MAX_CANDIDATES` (2000, warning on hit).
Representative event = first event of the earliest window occurrence, via lagged
event-id/timestamp columns. Config: `stat_sequence_ngram` (3), `stat_sequence_max_candidates`.

**Router (`api/routers/events.py`)** — `sequence_novelty` dispatch branch (n validated 2–5 →
422), new `ngram_size` param on `list_anomalies` + `TagAnomaliesRequest`, effective n
snapshotted into `resolution`/`DetectorRun.params`, `SequenceFinding` serialization
(`type: "sequence_novelty"`), tag-annotation content branch ("New sequence — …"). Allowlist
key = `(series_field, " → "-joined n-gram)`. No migration needed.

**Frontend** — `EventSequenceView.tsx` (temporal-only gate like proportion shift, grouping
field select + n selector, chip-arrow sequence rows), `DetectorAccordion` entry ("Event
sequences"), `MethodologyPanel` card, detector unions + `SequenceNoveltyFinding` type +
`ngram_size` param in `api/anomalies.ts`/`types.ts`.

**Tests** — 12 service tests (temporal-only/no-data/validation, baseline-without-n-grams,
scoring + details, multi-window attribution, allowlist/exclude, warnings, limit, SQL-shape and
W2-offset assertions via `RecordingClient`) + 3 router tests (dispatch defaults, `ngram_size`
override, serialization). Backend 211 pass in the two touched files; frontend
typecheck/lint/183 tests green.

**Docs** — `ANOMALY_DETECTION.md` new §9 (semantic search renumbered §10, intro list now ten
tools); `ROADMAP.md` D8 item replaced with a shipped summary.

## Session 44 — 2026-07-10: unified disposition taxonomy; stale-panel + 50-cap fixes

Started from the question "is *mark normal* the same as *mark as noise*?" — answer: the
app had no noise concept at all, and "normal" itself was fragmented across five mechanisms
(allowlist table, legacy per-event `normal` annotation, system `anomaly` annotations, the
`pinned` flag, baseline windows). Shipped in three commits:

**PR #86 (`fix/anomaly-panel-limit-and-stale`)** — two user-reported bugs. (a) Mark normal
looked like a no-op: four views keyed anomalies queries with UI slugs
(`novelty`/`range`/`combo`/`order`) while `useMarkNormal`'s optimistic filter matches
`queryKey[3]` against the backend detector id — keys now use detector ids, pinned by a
vitest suite. (b) Findings silently truncated at the hardcoded limit — detectors now report
`total_findings` (pre-cap survivors), views show "N of M" with stepped **Load more**
(50→150→500, `useFindingsLimit`).

**`feat/unified-dispositions` backend** — new `finding_dispositions` table + audited
`/dispositions` router replaces the fragmentation. Taxonomy: `normal` (baseline extension,
suppresses detection, hashed into `DetectorRun.params` via `dispositions_hash` — which now
also covers event-scoped exclusions, closing a reproducibility gap), `dismissed` (new:
presentation-only noise triage, filtered at response time with explicit `dismissed_count` +
`include_dismissed` escape hatch, run results stay unfiltered), `confirmed` (replaces
`pinned`; tag re-runs preserve confirmed `(event, detector)` pairs). Migration `0004` moves
allowlist rows / `normal` annotations / pinned intents, drops `detector_allowlist` and the
`pinned` column; `/allowlist` endpoints removed; annotation types tightened to
`tag`/`comment`. The old per-event-normal path was also un-audited — every disposition
write is audited now.

**`feat/unified-dispositions` frontend + docs** — `useDisposition` replaces `useMarkNormal`;
`FindingRowActions` grows Normal/Dismiss/Confirm and is now wired into **all** views
including the previously-missing ComboNovelty, Frequency, and OrderViolations;
EventDetailPanel's Persist became Confirm (annotation + disposition in one action); the
"Normal values" list became a kind-grouped Dispositions list; the dead legacy-normal grid
indicator was removed. Docs: `ANOMALY_DETECTION.md` normality model rewritten around the
taxonomy; ROADMAP L2 resolved, follow-ups X1–X3 filed.

Verification: backend suite green except the 3 pre-existing environmental failures
(embeddings extra); frontend typecheck/lint/197 tests green.

## Session 43 — 2026-07-09: docs audit — verify state vs. documentation, fix stale claims

Full docs/ sweep against code and test-suite state. `ROADMAP.md` proved accurate: every open
item (M15/M22/M23/M24/M25, D8–D10, W4–W8, L1/L2) verified still open in code (no
`hasToken` fast path, no Sigma/`extractGroups` code, `windows_from_split` still live,
journal/browser converters still vendored-only); every shipped claim (D1–D7,
proportion_shift, W2 offsets threading, migration 0003, ANOMALY_DETECTION §§1–8 incl. the
W2 clock-skew note) verified present. Fixed stale spots:

- `CONCEPT.md` — §6.2 "event store TBD" now names ClickHouse; §11 items 5 (auth) and 6
  (offline enforcement) checked off — both long shipped.
- `MODEL_REFINEMENT.md` — timeline `source_id IN (…)` scoping was described as "not yet
  exposed by any endpoint"; now points at `EventQuery.source_ids` +
  `_resolve_timeline_scope`.
- `ROADMAP.md` — missing blank line before "Explicitly out of scope"; dropped a stale
  "(this PR)" reference on the Alembic item.
- `CLAUDE.md` — archive filename `ROADMAP_PHASEN.md` corrected to `ROADMAP_PHASE{N}.md`.

Verification state: backend 752 passed / 15 skipped; frontend typecheck + 183 tests green.
The 4 backend failures are environmental, not code: 2 in `test_embeddings_capability.py`
(embeddings extra not installed in this venv) and 2 (`test_timeline_mappings_api.py`,
`test_uploads.py`) because the **local dev ClickHouse `tracesignal` database still had the
legacy `Nullable(DateTime64(3))` timestamp schema** — the app itself would refuse to start
against it. Resolved same session: the dev `events` table held 0 rows, so instead of the
full session-27 `EXCHANGE TABLES` migration it was dropped and recreated with the current
DDL via `ClickHouseStore.init_schema()`; both affected ClickHouse-backed tests pass now.

## Session 42 — 2026-07-09: W2 per-source clock-skew correction (COMPLETE)

Finished the query-layer-plus-threading work checkpointed in session 41. Every piece of the
per-source clock-skew correction is now wired end to end; the branch is green (full backend suite
passes except the pre-existing environmental failures that need a live ClickHouse / the optional
embeddings extra, and the frontend passes `typecheck`/`lint`/`test`).

**Router threading (`api/routers/events.py`, `viz.py`)** — `_resolve_timeline_scope`'s
`source_offsets` map is now threaded into all five `EventQuery(...)` constructions (explorer,
bulk-annotate refs, histogram, export, viz) and into every statistical-detector call via
`_run_stat_detector` (new `source_offsets` kwarg, passed from both `list_anomalies` and
`tag_anomalies`) and `_resolve_analysis_windows` (window midpoint now derived over effective time).

**Detector layer (`db/anomaly_stats.py`)** — `_window_preds`/`_window_totals` build predicates
over `effective_ts_sql` and bind the offset arrays; every detector (`find_value_novelty`,
`find_value_combos`, `find_range_violations`, `find_charset_novelty`, `find_entropy_outliers`,
`find_proportion_shifts`, `find_interval_periodicity`, `find_frequency_anomalies`) takes a
`source_offsets` kwarg and routes its window predicates, representative-event aggregates
(`minIf`/`argMinIf`/`maxIf`/`argMaxIf`), bucket SQL, and `get_timeline_range` through the
effective timestamp. `find_range_violations` additionally projects `source_id` into its numeric
subqueries (only when an offset is active — fast path stays byte-identical) because the
effective-ts expression references `source_id`. `find_order_violations` keeps its `lagInFrame`
skew math on the **raw** column (a uniform per-source shift cancels within a source) and shifts
only the reported `timestamp`/`prev_timestamp` in Python. `get_timeline_range`/`_buckets.py`'s
`query_timestamp_range` gained an effective-ts expression param.

**PATCH endpoint (`api/routers/cases.py`)** — `PATCH /{case_id}/sources/{source_id}` with a
`SourceUpdate` model (bounded ±10y), `require_case_contribute`, and a `source.update_offset`
audit row recording previous vs new.

**Export + run stamping (`api/routers/events.py`)** — `export_events` adds `applied_time_offsets`
to the export audit detail and prepends an offset-metadata line to JSONL (`{"_meta": …}`) / CSV
(`# applied_time_offsets=…`) only when an offset is active (untouched exports stay byte-stable).
`_persist_detector_run` stamps `source_offsets` into `DetectorRun.params`.

**Frontend** — `Source.time_offset_seconds` type, `sourcesApi.update` PATCH helper, a
`ClockOffsetControl` popover + offset badge on each source row (invalidates every timeline-scoped
query root on save), and the **L3 rider**: `analysisPanelWidth` → `investigatePanelWidth` in
`stores/ui.ts` (persist bumped to v4 with a carry-forward migrate step), the four
`InvestigatePanel.tsx` usages, and the stale `AnalysisPanel` comment mentions in
`ExplorerPage.tsx` / `scrollPosition.ts`.

**Tests** — 9 new detector SQL-scoping tests in `tests/test_anomaly_stats.py` (fast-path
byte-identity, effective-ts predicates, the `source_id`-projection fix, order-violation shifting),
store-setter tests in `tests/test_postgres_store.py`, `_persist_detector_run`/export-stream tests
in `tests/test_events_router.py`. The two pre-Alembic-adoption tests
(`test_postgres_store.py`, `test_enrichers.py`) now drop the 0003 column when simulating a
revision-0001 DB, and the router/viz/uploads test fakes were updated for the 3-tuple
`_resolve_timeline_scope`. Migration 0003 is exercised against SQLite by every store fixture; not
yet run against a live Postgres.

## Session 41 — 2026-07-09: W2 per-source clock-skew correction (IN PROGRESS — resume here)

Branch `feat/source-clock-skew` (based on `feat/interval-periodicity-detector`, which is
committed — commit `3e3ebd5`). **Working tree has uncommitted WIP, committed at the end of this
session as a checkpoint** — see the commit on this branch titled "wip(w2): query-layer clock-skew
correction, threading incomplete" for the exact diff. Tests for everything done so far pass;
nothing done so far is user-visible yet (no API/UI wiring).

### Design (decided, don't re-litigate)

Gated **effective-timestamp SQL expression**, not bound-shifting — bound-shifting breaks under
mixed per-source offsets and can't express cross-source `ORDER BY` or bucketing at all. New
module `src/tracesignal/db/_offsets.py`:

- `effective_ts_sql(offsets)` → bare `"timestamp"` when no in-scope source has a nonzero offset
  (the mandatory fast path — byte-identical SQL to pre-W2, verified by
  `test_zero_offset_map_keeps_sql_byte_identical`); otherwise
  `if(<not sentinel>, addSeconds(timestamp, transform(source_id, {clk_off_src:Array(String)},
  {clk_off_val:Array(Int64)}, 0)), timestamp)` — sentinel rows (year-2299 no-timestamp marker)
  are never shifted.
- `bind_offset_params(offsets, params)` binds the two parallel arrays only when active.
- `offset_raw_bounds(offsets)` returns `(max_offset, min_offset)` (both clamped to include 0) —
  used to widen a *raw*-column scalar bound alongside every corrected filter/cursor predicate, so
  ClickHouse's primary-index granule pruning survives the effective-ts expression (never changes
  the result set, purely a pruning aid — same trick as the existing redundant cursor bound).

### What's DONE (commit checkpoint, all tests green)

1. **Postgres model** — `src/tracesignal/db/postgres.py`: `Source.time_offset_seconds`
   (BigInteger, default 0) added after the `status` column (~line 109), doc comment explains
   query-time-only semantics; `to_dict()` includes it.
2. **Migration** — `src/tracesignal/db/migrations/versions/0003_source_time_offset.py`
   (`down_revision="0002"`, head). `add_column` with `server_default="0"`, SQLite-safe. **Not
   yet run/tested against a live Postgres** — only exercised implicitly if the test suite
   bootstraps SQLite through Alembic; verify with `uv run alembic upgrade head` before shipping.
3. **Store setter** — `PostgresStore.set_source_time_offset(case_id, source_id, seconds)` next
   to `set_source_status` (~line 1287 in the pre-migration numbering, search for the method
   name), returns the updated detached `Source` or `None`.
4. **Query path** — `src/tracesignal/db/queries.py`, fully wired and tested:
   - `EventQuery.source_offsets: dict[str, int] | None` field added (with doc comment).
   - `_build_where`: binds offset params once, computes `eff`/`max_off`/`min_off`, applies to
     the `start`/`end` time-range filter (corrected predicate + widened raw bound) and to
     `add_cursor` calls (both `after`/`before`).
   - `_ParameterizedQueryBuilder.add_cursor` gained `ts_expr`/`raw_widen_seconds` kwargs — when
     `ts_expr != "timestamp"` it emits the tuple compare on the corrected expression plus a
     separately-widened raw scalar bound (previously the raw bound reused the same `ts` param).
   - `query()`: `ORDER BY` uses `eff`; `_normalize_event_row` now takes `source_offsets` and
     shifts the presented `timestamp` (not `ingest_time`) by the row's source offset, skipping
     sentinel rows.
   - `iter_events()` (export streaming): same `ORDER BY eff` + `_normalize_event_row` offset
     threading — **exports already get corrected timestamps for free** once callers pass
     `source_offsets` on the `EventQuery`.
   - `histogram()`: both the explicit-range and derived-range (WITH-CTE) branches bucket over
     `eff` instead of bare `timestamp`; range min/max also computed over `eff`.
5. **Tests** — `tests/test_queries.py`, 9 new tests appended after
   `test_normalize_event_row_presents_sentinel_timestamp_as_null` (search for
   `# W2 — per-source clock-skew correction`): byte-identical-SQL fast path, corrected
   `ORDER BY`, widened raw bound on time filters, `_normalize_event_row` offset application +
   sentinel skip + wrong-source no-op, histogram bucketing over `eff`. All pass
   (`uv run pytest tests/test_queries.py -q --no-cov` → 97 passed).
6. **`_resolve_timeline_scope` signature change** — `src/tracesignal/api/routers/events.py`
   (~line 274): now returns a 3-tuple `(source_ids, field_mappings, source_offsets)` — the third
   element is `{source_id: offset}` for ready sources with a *nonzero* offset, or `None`. All 9
   call sites in `events.py` and the 1 in `viz.py` were mechanically updated via `sed` to unpack
   three values (`source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(...)`)
   — **the resulting `source_offsets` local is currently UNUSED at every call site**. Confirmed
   this compiles and lints clean (`uv run ruff check` passes — unused-local isn't in the
   configured rule set) so the tree is safe to leave mid-refactor, but it means **no endpoint
   actually applies offsets yet** — the query-layer plumbing is ready and tested, the router
   layer doesn't call it.

### What's NOT done — exact resume point

**Step A — thread `source_offsets` into every `EventQuery(...)` construction.** Five
constructions need `source_offsets=source_offsets` added:
`src/tracesignal/api/routers/events.py` lines ~631, ~764, ~991, ~1186 (search
`EventQuery(` in that file) and `src/tracesignal/api/routers/viz.py` line ~98. Each of these
functions already has `source_offsets` in scope from the now-updated `_resolve_timeline_scope`
unpack (or needs to receive it as a parameter if it's a helper called after the unpack — check
each call site's function signature).

**Step B — thread `source_offsets` into every statistical detector call.** This is the
largest remaining chunk. `src/tracesignal/db/anomaly_stats.py` detector methods take a
`source_ids` parameter but no `source_offsets` yet; the module-level `_window_preds(windows,
params)` (~line 352) and `_window_totals` (~line 1357) build baseline/suspect predicates
directly against the bare `timestamp` column and need to route through
`effective_ts_sql`/`bind_offset_params` from `db/_offsets.py` (same import as `queries.py`
uses). Concretely:
  - `_window_preds` needs a `source_offsets` param, must call `bind_offset_params` into the
    passed `params` dict and build predicates against `effective_ts_sql(source_offsets)`
    instead of the literal `"timestamp"` string baked into its f-strings (search for
    `f"(timestamp >=` in that function).
  - Every detector method (`find_value_novelty`, `find_value_combos`, `find_range_violations`,
    `find_charset_novelty`, `find_entropy_outliers`, `find_proportion_shifts`,
    `find_interval_periodicity`, `find_frequency_anomalies`) needs a new `source_offsets:
    dict[str, int] | None = None` kwarg threaded down to every `_window_preds`/`_window_totals`
    call it makes, plus any other bare `timestamp` reference in its own SQL (representative-event
    `minIf(timestamp, ...)`/`argMinIf(event_id, timestamp, ...)` aggregates, bucket SQL in
    `find_frequency_anomalies`, `get_timeline_range`/`get_timeline_midpoint` via
    `query_timestamp_range` in `db/_buckets.py`).
  - `find_order_violations` (`method="sequential"`, `lagInFrame` over raw `timestamp`
    partitioned by `source_id`) is the one exception per the original design: keep its
    `lagInFrame` on the RAW column (a uniform per-source offset doesn't change intra-source
    ordering or skew deltas), and only shift the *reported* `timestamp`/`prev_timestamp` values
    in Python before returning findings.
  - `src/tracesignal/api/routers/events.py::_run_stat_detector` (search for the function) must
    pass `source_offsets=source_offsets` into every `svc.find_*` call — it already receives
    `source_ids`; add the new parameter alongside it, threaded from the `_resolve_timeline_scope`
    tuple at each of the two callers (`list_anomalies`, `tag_anomalies`).

**Step C — PATCH endpoint + audit.** `src/tracesignal/api/routers/cases.py`: add
`SourceUpdate` Pydantic model (`time_offset_seconds: int`, bounds ±315_576_000 ≈ ±10y) and
`PATCH /{case_id}/sources/{source_id}` calling the already-written
`store.set_source_time_offset`; record an audit row (`action="source.update_offset"`,
`detail={"previous": ..., "new": ...}`) — pattern at `admin.py` PATCH handlers (search
`record_audit` calls there). 404 when the store setter returns `None`.

**Step D — export + DetectorRun stamping.** `export_events` (events.py, search
`async def export_events`): pass `source_offsets` on its `EventQuery`; extend the existing
export audit-detail dict with `"applied_time_offsets"`; when any offset is active, prepend an
export-metadata line (JSONL: `{"_meta": {...}}` first record; CSV: `# applied_time_offsets=...`
comment before the header) — only when nonzero, so untouched exports stay byte-stable.
`_persist_detector_run` (events.py): add `"source_offsets"` to the persisted `params` dict
(same pattern as the existing `allowlist_hash` stamping).

**Step E — frontend.** `frontend/src/api/types.ts` `Source` interface: add
`time_offset_seconds: number`. `frontend/src/api/sources.ts`: add an `update()` call using the
existing `patch` helper (`client.ts` already exports one — confirmed in original planning,
re-verify it's still there). `frontend/src/components/sources/SourceList.tsx`: small "Clock
offset…" edit affordance + a compact badge on rows with nonzero offset; invalidate
sources/events/histogram queries on save.

**Step F — L3 rider (independent, do anytime, zero risk).** Rename
`analysisPanelWidth`/`setAnalysisPanelWidth` → `investigatePanelWidth`/`setInvestigatePanelWidth`
in `frontend/src/stores/ui.ts` (bump persist `version` and add a migrate step carrying the old
value forward), update the 4 usages in `components/analysis/InvestigatePanel.tsx`, fix stale
`AnalysisPanel`/`BaselineManager` comment mentions in `pages/ExplorerPage.tsx` and
`stores/scrollPosition.ts`.

**Step G — full verification once A–F land.** `uv run alembic upgrade head` against a real
Postgres (migration 0003 untested live); full `uv run pytest`; `uv run ruff check . && uv run
ruff format .`; frontend `npm run typecheck && npm run lint && npm run test`; manual: two
sources, set an offset on one, confirm grid interleaving + histogram shift, run a detector and
inspect `DetectorRun.params`, export JSONL/CSV and check the metadata line, reset offset to 0
and confirm export goes back to byte-stable + both audit rows exist.

Original detailed design/plan (statistic formulas for the already-shipped interval_periodicity
detector, full W2 step list) is in the approved plan file from this work session if still
present under `~/.claude/plans/`; this PROGRESS entry is the authoritative resume pointer since
that plan file is not part of the repo.

## Session 40 — 2026-07-09: interval_periodicity detector (D6+D7 merged)

Shipped the `interval_periodicity` statistical detector (Milestone 4, AMiner
`PathValueTimeIntervalDetector`), **merging roadmap items D6 (per-value silence) and D7**. The
re-scope the roadmap asked for confirmed proportion_shift already owns whole-window vanished
values, so per-value silence collapses into this detector as the maximal `count = 0` "missed"
case rather than a separate build.

Per (field, value), inter-arrival gaps are computed strictly within each window (a `lagInFrame`
partitioned by `(value, window index)` via an `arrayJoin` of the window predicates, so a gap
can never straddle the baseline/suspect boundary). Which of two tests a value gets is decided
entirely by its **baseline** delta CV, so the suspect window never selects its own test:

- **Cadence break** (baseline CV ≤ 0.5, ≥ 5 gaps): two-sample Poisson-rate LRT of arrival rate
  with window durations as exposures (`_poisson_rate_g` → df=1 chi² via the existing `erfc`
  helper). `direction` = `missed` (the D6 silence case is `count = 0`, representative event =
  last baseline occurrence) or `accelerated`. Effect floor: rate must change ≥ `min_rate_ratio`×.
- **Beaconing** (baseline CV ≥ 0.8 or sparse; ≥ 10 window gaps): Greenwood spacing statistic
  `G = Σ(gap/span)²`, left-tail normal-approx p (`_greenwood_p`). `direction` = `new_regularity`.
  Effect floors: window CV ≤ 0.3 and active span ≥ 50% of the window.

The CV band 0.5–0.8 is a deliberate dead zone. All tests share one BH-FDR pool; score =
`-log10(p)` (comparable across the two statistics, unlike proportion_shift's raw-G score).
Temporal-only; first-seen excluded (`HAVING w0_n >= 1`). New `IntervalFinding` dataclass;
copies proportion_shift's candidate-cap, allowlist, `_finalize_findings`, and effective-threshold
snapshotting (`fdr_q`/`min_ratio` request params map onto the cadence FDR ceiling + rate floor).

- **Backend:** `db/anomaly_stats.py` (`find_interval_periodicity`, `_interval_window_block`,
  `_poisson_rate_g`, `_greenwood_p`, module docstring stanza); `core/config.py` (`stat_interval_*`
  knobs); `api/routers/events.py` (dispatch branch, `_serialize_finding`, endpoint/tag param
  strings, tag-reason content, `_persist_detector_run` params).
- **Frontend:** new `IntervalPeriodicityView.tsx`; `DetectorAccordion.tsx` registry + `Timer`
  icon; `api/types.ts` (`IntervalPeriodicityFinding` + unions); `api/anomalies.ts` detector
  unions; `MethodologyPanel.tsx` blurb.
- **Tests:** `test_anomaly_stats.py` — 20 detector tests + `_poisson_rate_g`/`_greenwood_p` unit
  tests (Greenwood E/Var validated vs a numpy simulation); `test_events_router.py` — dispatch,
  request-override, and serialization round-trip. Full suite green; ruff + frontend
  typecheck/lint/test clean.
- **Docs:** `ANOMALY_DETECTION.md` §8 (renumbered semantic search to §9); `ROADMAP.md` D6/D7
  deleted with a merge note.

## Session 39 — 2026-07-09: startup no longer blocks on ClickHouse recovery (502 fix)

Prod returned **502 Bad Gateway** because the ASGI lifespan awaited all recovery + housekeeping
work *before* `yield`: `_reconcile_orphaned_ingests`, `_reconcile_orphaned_enrichment_jobs`
(which applies staged rows to `events.attributes` per orphaned run), the enrichment re-run
scheduling, and the session purge — every one of them touching ClickHouse. With ClickHouse slow
or unreachable the lifespan never completes, uvicorn never starts accepting connections, and the
reverse proxy has nothing to talk to → 502.

Fix (`api/main.py`): only the two fast, required Postgres steps (`init_schema`, `_seed_admin`)
stay blocking before `yield`. Everything ClickHouse-dependent moved into a background
`_startup_recovery(store)` task spawned right before `yield`, wrapped in broad exception handling
(each step already self-heals on the next restart) and cancelled cleanly on shutdown. Booting the
HTTP server no longer depends on ClickHouse reachability. No behavioral change to the recovery
logic itself — same functions, just off the startup-critical path.

## Session 38 — 2026-07-09: PR #81 review hardening (Parquet converter pipeline)

Review of the M20/M25 Parquet-converter pipeline PR (#81) surfaced six issues; all fixed in
this session, with regression tests for the two behavioral ones.

- **Null-provenance guard (forensic, medium).** `parquet_reader.py::_stamp_batch` now rejects
  any batch with a null in `file_hash`/`byte_offset`/`content_hash`/`source_file`. Previously a
  null in these columns would be filled to `""` (or, for `byte_offset`, pass through and either
  crash the insert or collapse to `0` and collide with a real offset-0 row) **after** `event_id`
  was already derived from the pre-fill value — silently diverging the stored provenance from the
  id that certifies it. The interchange schema declares these fields nullable, so nothing rejected
  such a file upfront; now the reader does.
- **Sentinel comparison (low).** `parquet_reader.py::parse` used a hardcoded `.year == 2299` to
  map the null-timestamp sentinel back to `None`; replaced with `is_null_ts_sentinel()` from
  `db/_dt.py` so the check can't go stale if the sentinel value changes.
- **pcap unbounded-read DoS (medium).** `pcap2tracesignal.py` read attacker-controlled length
  fields (`incl_len` for classic pcap; section-header and generic block `block_total_length` for
  pcapng — each up to ~4 GiB) straight into a single `fh.read()`. Added a 256 MiB
  `_MAX_RECORD_BYTES` cap on all three paths so a crafted/corrupt capture raises `PcapParseError`
  instead of forcing a multi-GB allocation.
- **pcap docstring (nit).** Spelled out that `content_hash` covers a different on-disk byte span
  per format (classic = 16-byte record header + captured data; pcapng = whole block incl. trailer)
  so an examiner re-verifying by hand hashes the matching span.
- **`cases.py` comment (nit).** Clarified that the `ParserConfig` built for the interchange-Parquet
  footer read is a throwaway — the real parser identity comes from the footer (`source_parser`),
  never persisted or hashed.
- **Frontend nits.** `UploadDialog.tsx` file input now sets `accept=".csv,.jsonl,.parquet,.log"`;
  `ConverterPanel.tsx` download anchor gets `rel="noopener noreferrer"`.

Also regenerated `assets/converters/manifest.json` (pcap converter sha256/size changed with the
DoS fix — `test_manifest_hashes_match_committed_assets` enforces this). New tests:
`test_parquet_reader.py::TestNullHandling::test_null_provenance_rejected` (4-param) and
`test_pcap_converter.py::TestOversizedLengthGuard` (classic + pcapng). Full suite green.

## Session 37 — 2026-07-09: proportion_shift detector (G-test value-share shifts, BH-FDR)

New statistical detector answering "is this value significantly more/less frequent in the
suspect window than in the baseline?" — the gap between temporal value_novelty (only
`baseline_cnt = 0` first-seen) and frequency (bucket-level absolute-count z-scores that miss
evenly-spread rate shifts, fire on global volume changes, and in temporal mode mostly
re-report zero-baseline series). Per (field, value, suspect window): 2×2 **G-test**
(log-likelihood ratio, Dunning 1993) on raw counts, p-values via the exact df=1 chi² survival
function (`erfc(√(G/2))` — deliberately no scipy dependency, airgapped), **Benjamini–Hochberg
FDR** pooled across every test in the run, plus a **rate-ratio effect floor** so
statistically-significant-but-tiny shifts on huge timelines don't flood the list.

Decided semantics: temporal-only (`method="g-test"`; no windows → graceful
`insufficient_data`, not 422, so the DetectorAccordion sweep stays calm); two-sided with
`direction: up|down`; **vanished values included** as maximal "down" (representative event =
last baseline occurrence, Haldane–Anscombe +0.5 smoothing for the displayed ratio only);
**first-seen excluded** in SQL (`HAVING baseline_cnt >= 1` — value_novelty owns those, and the
definitional prune keeps the BH test count honest); per-field candidate cap (2000, highest
volume first) surfaces an FDR-coverage warning when hit; score = G. New `TS_STAT_SHIFT_FDR_Q`
/ `TS_STAT_SHIFT_MIN_RATIO` / `TS_STAT_SHIFT_MAX_CANDIDATES_PER_FIELD` settings; effective
thresholds snapshotted into the persisted `DetectorRun` params.

Files: `db/anomaly_stats.py` (`ShiftFinding`, `find_proportion_shifts`, `_g_statistic` /
`_chi2_sf_df1` / `_bh_qvalues`), `api/routers/events.py` (dispatch, `fdr_q`/`min_ratio`
request params, serialization, tag content), `core/config.py`; frontend
`ProportionShiftView.tsx` (temporal-only frame gating on top of `useBaselineRequest`),
`DetectorAccordion` row, `MethodologyPanel` card, types; docs: `ANOMALY_DETECTION.md` new §7
(similarity renumbered §8). 13 new service tests (hand-computed G/χ²/BH constants) + 5 router
tests.

## Session 36 — 2026-07-08: M25 — native Parquet converters for filterlog, suricata, cloudtrail, pcap

Ported four of the six remaining vendored `*2timesketch` scripts to native, standalone
`*2tracesignal.py` Parquet converters, following the `nginx2tracesignal.py` pilot's structure
(embedded schema/metadata constants verified by a parity test, stdlib + pyarrow only, minimal
CLI `-i/-o/-w/-v`). Each reuses its vendored counterpart's field-naming conventions
(attribute keys, artifact/message formats) so existing Timesketch muscle memory carries over;
new tests live in `tests/test_{filterlog,suricata,cloudtrail,pcap}_converter.py` with hand-built
fixtures under `tests/data/` (including `tests/data/gen_pcap_fixtures.py`, a one-off byte-level
pcap/pcapng generator — no scapy/dpkt dependency).

- **filterlog2tracesignal.py / suricata2tracesignal.py** — line-oriented, so both get full
  nginx-style intra-file chunked multiprocessing (newline-boundary chunking via
  `find_chunk_boundaries` + `ProcessPoolExecutor`).
- **cloudtrail2tracesignal.py** — a CloudTrail file holds one JSON `Records` array rather than
  one record per line, so `byte_offset`/`content_hash` are computed by re-scanning the array
  with `json.JSONDecoder.raw_decode` one object at a time
  (`iter_json_records_with_offsets`), giving each row an exact byte span in the original file
  without re-serializing. Parallelism is cross-file only (one worker process per input file).
- **pcap2tracesignal.py** — ports the from-scratch pcap/pcapng dissector unchanged (Ethernet/
  Linux-SLL/raw-IP, IPv4/IPv6, TCP/UDP/ICMP/ARP). Two deliberate simplifications vs. the
  vendored version: (1) parallelism is cross-file only, not the record-boundary chunking a
  line-oriented file would get — noted as a deferred follow-up in `ROADMAP.md`; (2) dropped the
  `heapq.merge` k-way global chronological sort across input files (a CSV/JSONL-timeline
  concern) — packets are written in file order since the server sorts on query.
- **Mid-session decision (user request):** the vendored `*2timesketch` scripts stay vendored
  **permanently** as a minimal-dependency (stdlib-only, no pyarrow) alternative. Re-added
  `nginx2timesketch` to `scripts/vendor_converters.py`'s `CONVERTERS` dict (it had been dropped
  when the nginx pilot shipped) and re-ran the vendor script against a local
  `overcuriousity/2timesketch` checkout, so nginx now also has both a vendored and a native
  entry, matching cloudtrail/filterlog/pcap/suricata. Native and vendored converters for the
  same source appear side by side in `manifest.json`/`/api/converters`
  (`test_converters_api.py` updated accordingly).

## Session 35 — 2026-07-08: M20 — Arrow bulk insert, Parquet interchange format, nginx converter pilot

Session 34's plan proposed *server-side* native raw-log parsing; discussion corrected the
requirement: keep the downloadable-converter workflow, but converters emit **Parquet** instead
of inflated Timesketch CSV, and the server bulk-ingests that via Arrow. Shipped:

- **Bulk Arrow ClickHouse insert (M20).** `db/_arrow_schema.py::EVENT_ARROW_SCHEMA` mirrors the
  events DDL; `insert_events()` now encodes through `_events_to_record_batch` (built strictly on
  `Event.to_clickhouse_row()` — sentinel/attribute rules preserved) and `client.insert_arrow`;
  new `insert_events_arrow()` pass-through for pre-built batches. Live round-trip verified
  against a real ClickHouse (UUID/FixedString-from-string, Map, Array, DateTime64 sentinel) in
  `tests/test_arrow_insert_clickhouse.py` (skip-if-unreachable). `pyarrow` is a core dependency.
- **Upload retention hardlink fix (M20).** `cases.py::_retain_file` replaces the second full
  `shutil.copy2` pass: exists short-circuit (content-addressed), `os.link`, copy fallback on
  `EXDEV`. Fast path requires `TMPDIR` and `TS_SOURCE_RETENTION_PATH` on one filesystem.
- **TraceSignal Parquet interchange format v1.** Spec + validation in
  `ingestion/parquet_format.py`: per-row `source_file`/`file_hash`/`byte_offset`/`content_hash`
  (all referring to the **original raw evidence file**) + event columns; footer metadata carries
  format version, converter name/version, and per-file sha256 provenance. Converter identity
  becomes `parser_name`/`parser_version`, so `event_id` is re-derivable from the raw log alone
  (`models/event.py::derive_event_id`, extracted from `Event._derive_id`).
- **Server Parquet ingest path.** `ingestion/parquet_reader.py::ParquetEventsParser` stamps
  server-side columns onto each record batch (no `Event` objects) and feeds the pipeline's new
  `parse_arrow_batches`/`_ingest_file_arrow` bulk branch; CSV/JSONL paths unchanged. Upload
  validates the footer up front (400 on non-interchange parquet) and records
  `converter@version` as the Source's parser. `.parquet` auto-detected; CLI works unchanged.
- **nginx converter pilot.** `assets/converters/nginx2tracesignal.py` (in-repo, requires
  pyarrow): access/error/redirect logs, plain/.gz, file or directory, multiprocessing chunk
  parsing for large plain files, zstd Parquet output, embedded provenance. Replaces the vendored
  `nginx2timesketch.py` (deleted; manifest entry marked `"native": true` and preserved across
  `scripts/vendor_converters.py` re-runs). Remaining six converters: ROADMAP M25.
- Frontend: copy-only tweaks (upload dialog formats, converter panel note, guidance). Docs:
  ROADMAP M20→M25 rewrite, MODEL_REFINEMENT/CONCEPT provenance conventions, plan doc archived
  as superseded.

## Session 34 — 2026-07-08: Fast end-to-end ingestion plan (nginx access logs)

Planning only, no code changed. A 50GiB nginx access log currently has to be pre-converted with
the vendored `nginx2timesketch.py` into a 150GiB+ CSV before it can even be uploaded, then
re-parsed single-threaded and inserted row-by-row into ClickHouse. Wrote a full design for
removing that bottleneck: bulk Arrow-based ClickHouse inserts (`insert_events`/
`insert_events_arrow`), a native parallel (multiprocess) nginx parser that ingests the raw log
directly — fixing `Source.file_hash`/`byte_offset` to point at the real evidence file instead of
a converted derivative — and a fix for the upload-receive double file-copy. Full design in
`docs/archive/PLAN_FAST_NGINX_INGESTION.md`; condensed pointers added to `docs/ROADMAP.md` M20/W8.

## Session 33 — 2026-07-08: UX polish sweep (issue #74 "fantastic UX")

Screenshot review surfaced friction that made the app powerful-but-steep. Fixed four
workstreams on `feat/baseline-windows`; no backend code changed (see B3 note).

- **Readable timestamps.** Event-grid Timestamp column widened (170→195px, `minSize`
  150) and the cell set `whitespace-nowrap tabular-nums` so the full
  `YYYY-MM-DD HH:MM:SS` never ellipsizes (`components/explorer/EventGrid.tsx`).
- **Custom UTC date picker.** New `ui/DateTimeField` (Popover + `date-fns`, month
  calendar + HH:MM, typed `YYYY-MM-DD HH:MM` accepted, clear affordance) replaces every
  native `datetime-local` — the raw German `tt.mm.jjjj` widget is gone. Used in the
  Explorer time range (`FilterRail`) and baseline/suspect windows (`WindowsNormality`,
  whose local `toInput/fromInput` were deleted for the shared `lib/time.ts` helpers;
  added `fmtDatetimeInputUtc` / `parseDatetimeInputUtc`). UTC contract preserved (issue #9).
- **Inline term help.** `lib/glossary.ts` (single source) + `ui/InfoHint` (Info icon +
  existing `Tooltip`) on baseline / suspect-window / self-baseline / temporal / normal-values
  / scan-all / compare-baseline (`FrameBar`, `WindowsNormality`, `detector-shared`,
  `InvestigatePanel`). First-run explainer via the existing `ui/GuidancePanel` atop the
  Anomalies tab (folds away permanently, localStorage-persisted).
- **Visualize first paint.** Default chart type is now the field-free events-over-time
  histogram (`chartConfig.ts`) — instant render on the already-optimized single-pass
  histogram, never an empty canvas. The numeric-stats *probe* is skipped while the time
  chart is shown (`VisualizePage.tsx`), avoiding the `field_numeric_stats` double-scan on
  first load. Empty states (`ChartEmptyState`) now carry cause-aware copy + a hint,
  including the sentinel-undated-events case for time-based charts.
- **Calmer Anomalies panel.** The "New definition" builder collapses behind a
  `+ New definition` button once saved definitions exist (`WindowsNormality`); definition
  name input gets a guiding placeholder.
- **B3 deferred (deliberate).** Did *not* rewrite `field_numeric_stats` to one scan — its
  docstring documents the two-scan design as a forensic-reproducibility choice (fixed-width
  bins). Deeper viz scan-avoidance moved to ROADMAP Milestone 2.
- Verified: `tsc`, oxlint (no new errors), 179 frontend tests, prod build all green.

## Session 32 — 2026-07-08: Investigate panel — unified analysis + baseline UX rework

## Session 32 — 2026-07-08: Investigate panel — unified analysis + baseline UX rework

The session-31 backend (explicit baseline definitions + value allowlist) was sound, but the
UI exposed it as two sibling panels (Analysis + Baselines) coordinating invisibly through a
store and the histogram, plus a per-detector self/temporal `ModeToggle` whose meaning shifted
with the active baseline. Fresh users had no mental model. Reworked the frontend into one
coherent surface with an aminer-shaped normality model (learned baseline window + manual
value allowlist = `learn_mode` + `allowlist_event`).

- **One `InvestigatePanel`** (`components/analysis/InvestigatePanel.tsx`) replaces
  `AnalysisPanel` + `BaselineManager` (both deleted). Reads top-to-bottom: frame → detectors
  → Windows & normality. Single `investigatePanelOpen` toolbar toggle (`stores/ui.ts`).
- **Global frame** (`stores/baseline.ts` gains `frame: "self" | "baseline"`). `FrameBar`
  sets the one scope every detector obeys; `useBaselineRequest()` reads it from the store,
  and the per-view `ModeToggle` is gone from all five value detectors + frequency. Baseline
  frame without a definition shows `NeedsBaselinePrompt` instead of silently running self.
- **Window editor** (`WindowsNormality.tsx`): baseline + N suspect rows with typed UTC
  datetime inputs *and* histogram-drag (arm a row → brush fills it via `pendingRange`).
  Client-side validation mirrors the router's `_validate_windows`. Create/edit/delete.
- **Allowlist made usable + clarified.** Value-based, aminer-aligned. Two entry points, one
  `useMarkNormal` hook: field-value rows in `EventDetailPanel` write a detector-agnostic
  `"*"` entry (all value detectors); analysis finding rows (`FindingRowActions`) write a
  detector-scoped entry. Backend: `_run_stat_detector` now applies entries whose detector is
  its own **or** `"*"` (`events.py`; no schema change — `"*"` is just a detector value).
  "Normal values" list shows scope per entry.
- Verified: `tsc`, oxlint, 179 frontend tests, 160 backend tests, prod build all green; two
  new backend tests cover the `"*"` wildcard suppressing across detectors vs. scoped entries.
  Live browser drive not run (dev stack not serving in this environment).

## Session 31 — 2026-07-08: explicit baseline + suspect windows for temporal anomaly detection

Replaced the single-`baseline_end` split point (which the UI never even exposed — it
silently used the timeline midpoint) with explicit, persistent **baseline definitions**:
a named baseline window plus 1..N labeled suspect windows per timeline. This is the USP
detailed-investigation workflow — "mark what was normal, mark what's suspicious, tell me
what diverges and why."

- **Alembic adopted** (`src/tracesignal/db/migrations`). Schema was previously
  `create_all` + hand-rolled inspector ALTERs; prod now has real data, so revision `0001`
  snapshots the full existing schema and `init_schema` stamps-then-upgrades (pre-Alembic
  databases get the legacy fixups one last time, then `stamp 0001`, then `upgrade head` —
  zero manual deploy steps). Revision `0002` adds `baseline_definitions` +
  `detector_allowlist`. Future schema changes are revisions, never inspector ALTERs.
- **New entities** (`db/postgres.py`, router `api/routers/baselines.py`): `BaselineDefinition`
  (baseline range + suspect windows JSON, derived `config_hash`, freely editable) and
  `DetectorAllowlistEntry` (`(detector, field, value)` never-anomalous). RBAC + audit;
  window-geometry validation (baseline/suspect overlap = 422, suspect/suspect = warning,
  ≤10 windows). Timeline/case deletes cascade both (and `SavedChart`, previously orphaned).
- **All six temporal detectors reworked** (`db/anomaly_stats.py`) onto a frozen
  `AnalysisWindows` contract; `windows_from_split` preserves the legacy split at the API
  edge so old runs/clients keep working. Statistics fixed: surprise denominators are the
  suspect window's own event count (per window); frequency derives its interval from the
  baseline window, zero-fills baseline buckets, scores only full suspect buckets, and warns
  on windows too short. Findings carry `window_label`. Verified end-to-end against the dev
  ClickHouse (caught two real SQL bugs the fakes couldn't: a `GROUP BY`/`any()` collision
  and a `-0.0` surprise).
- **D11 merged in** (roadmap item removed): "mark normal" on a finding now writes a
  value-level allowlist entry consumed as post-detection suppression, unifying with the
  time-based baseline model. The standalone per-event Normal toggle is gone from the grid
  and detail panel (legacy `normal` annotations still honored, read-only); timestamp-order
  findings keep the per-event path.
- **Forensic reproducibility**: `DetectorRun.params` snapshots `baseline_id`, the full
  window payload + `windows_hash`, and `allowlist_hash` + count — a run stays
  self-describing after the definition/allowlist is edited or deleted.
- **Frontend**: histogram baseline (blue) + suspect (amber) bands + a zoom/mark cursor
  toggle; `BaselineManager` (mark ranges → set baseline / add suspect, select/delete,
  allowlist list); `baseline_id` threaded through every detector view via a small store;
  run warnings surfaced; a "run all detectors" summary strip. `docs/ANOMALY_DETECTION.md`
  rewritten for the new model.

## Session 30 — 2026-07-07: context query, analyst-action audit, M3 polish batch

## Session 30 — 2026-07-07: context query, analyst-action audit, M3 polish batch

Shipped the easy+high-value batch from the new Milestone 5 (post-mortem workflow parity)
plus the remaining Milestone 3 polish items (all except the deliberately-opportunistic
events.py split):

- **W1 — Context query (frontend-only)**: `History` button in the event detail panel with
  ±1/5/15/60 min presets pivots the explorer to a time window around the event across all
  sources (`handleContextQuery` in `ExplorerPage`). Deliberately clears other filters
  (Timesketch context semantics); the existing `preJumpFilters` breadcrumb restores the
  prior view, and nested context queries keep the original breadcrumb. No backend change —
  `start`/`end` already flowed through the whole filter path.
- **W3 — Audit coverage for analyst actions**: `record_audit` added to `events.export`
  (audited before streaming starts — the attempt is the custody-relevant fact),
  `events.bulk_annotate` (matched/tagged counts + compact filter), `anomaly.run` (GETs are
  skipped by the generic middleware, so persisted detector-run launches were previously
  invisible), `anomaly.tag`, and `anomaly.persist_finding`. `export_events` and
  `list_anomalies` gained a `get_current_user` actor dependency.
- **M3 — ClickHouse URL parsing**: `_host`/`_port` string-splitting replaced by
  `_parse_url` (urllib), handling `https` (TLS + port 8443 default, `secure=` passed to
  clickhouse-connect), creds-in-URL (fallback when settings are at defaults), and bare
  `host:port` forms. Unit-tested.
- **M3 — Startup config sanity report**: `_lifespan` logs resolved offline mode, redacted
  datastore targets, audit/OIDC flags; warns on `environment=production` with
  `auth_cookie_secure=false`.
- **M3 — Large-file ingest regression test**: generated ~16 MiB CSV through
  `IngestionPipeline` with a discarding fake store; tracemalloc peak asserted < 8 MiB,
  protecting the H1 streaming fix against whole-file materialization.
- **M18 — `access_level` from the case API**: case list/detail responses now carry the
  caller's resolved level; the list path resolves in bulk from one membership query
  (`_bulk_access_level`), avoiding the feared N+1. Frontend `caseAccess.ts` reduced to a
  field read (`canManageCase(case_)`), client-side `resolveCaseAccess` deleted.

## Session 29 — 2026-07-07: PR #75 review fixes (D3/D5 + embed-wizard)

Addressed the review findings on the D3/D5 + embed-wizard branch. Correctness/altitude:

- **Charset huge-alphabet guard**: self-baseline mode measured `len(reference)` (non-rare
  chars only), so a CJK/base64 field where most chars are rare never tripped the
  `_MAX_CHARSET_SIZE` skip and flooded findings. Now measures the full alphabet
  (`len(char_counts)`); temporal mode already used the full baseline alphabet.
- **Charset double scan**: self-baseline ran a second whole-corpus `uniqExact` scan per field
  just for `n_vals`; folded into the char-counts query via `count() OVER ()`.
- **Entropy quadratic expression**: per-value `arrayMap(c -> countEqual(chars, c), …)` rescanned
  the char array per distinct char. Replaced with ClickHouse's linear `entropy()` aggregate over
  `arrayJoin`-ed characters.
- **Identifier crowd-out**: `_auto_string_fields` sliced a recommended(categorical)-first list to
  15, starving identifier fields (the detectors' primary target) on wide sources. Added a reserved
  identifier quota (`_select_auto_scan_tokens`); the frontend picker mirrors it
  (`selectAutoScanTokens`) so the "auto" preview matches what runs — previously the picker showed
  categorical-only and silently dropped identifiers on toggle.
- **nginx**: `location /api/cases/` captured the multi-GB source-upload endpoint, dropping the
  300s body/send timeouts to nginx's 60s defaults. Scoped the SSE block to a regex on the exact
  `/stream` path; reconciled the `client_max_body_size` doc/conf mismatch (2G vs 200G).

Cleanup: dedicated `stat_charset_rarity_floor` config knob; `HEAVY_SCAN_SETTINGS` derived from
`TS_*` settings (dropped the `_HEAVY_SCAN_SETTINGS` alias); shared `_finalize_findings` tail and
`_serialize_finding` via `dataclasses.asdict` for the new detectors; frontend `useHealth` hook,
`fieldsParamOf`/`FindingRowActions`/`useOpenEvent`/`truncate` de-duplication across detector views,
single reused embed-wizard trigger element, hoisted `RecordingClient` test double.

## Session 28 — 2026-07-07: charset + entropy detectors (D3/D5), enricher client fix (M1)

Milestone-4 detector expansion continued with the next two AMiner-inspired, field-agnostic
detectors, plus the Milestone-1 thread-safety fix:

- **M1**: the timeline-enrichers endpoint shared one `ClickHouseStore` across its
  `asyncio.gather(run_in_threadpool(...))` eligibility fan-out; `clickhouse_connect` clients
  are not thread-safe. Each check now builds its own store inside the worker thread.
- **D3 `charset`** (`find_charset_novelty`): per field, learn a reference character set over
  *distinct values* and flag values containing characters outside it (NUL bytes, homoglyphs,
  injection metacharacters — purely syntactic). Self-baseline inverts the degenerate
  whole-corpus charset into a rare-character rule (chars in ≤ rarity_floor distinct values);
  temporal learns the baseline window's alphabet. Score = value_novelty's surprise family
  summed per novel char; findings carry the chars + U+XXXX codepoints. Skips fields with
  < 20 distinct baseline values or alphabets > 5000 chars.
- **D5 `entropy`** (`find_entropy_outliers`): Shannon character entropy per distinct value vs.
  a Tukey fence over the field's baseline entropy distribution — above-band ≈ random-looking
  (DGA/encoded), below-band ≈ degenerate (padding). Both modes use the IQR fence (quantiles,
  unlike min/max, aren't degenerate over their own population). Values < 6 codepoints excluded
  throughout; score = excess ÷ band width like numeric_range.
- Shared `_auto_string_fields` helper: auto-selection for D3/D5 keeps identifier-kind fields
  (URLs, UAs, filenames) — exactly where injected metacharacters and random strings live —
  unlike value_novelty's categorical-only default.
- Frontend: `CharsetNoveltyView` + `EntropyView` on the shared detector scaffolding, registry
  entries, Method-tab sections; `docs/ANOMALY_DETECTION.md` §5/§6 (similarity renumbered §7).
- Validated live against ClickHouse 26.6: `extractAll(val, '(?s).')` round-trips NUL and
  unicode (incl. via `Array(String)` params); invalid UTF-8 bytes are *dropped* by re2 —
  documented as a caveat with a byte-level fallback option. End-to-end synthetic run flagged
  a DGA hostname (charset novel-chars + entropy above-band) and an `aaaa…` host (below-band)
  in both modes.

## Session 27 — 2026-07-07: 300M-row perf overhaul (timestamp sentinel, two-phase queries)

A production 80 GiB / 300M-row nginx ingest exposed that every Explorer "load more" click read
**187 GiB** (~80 s) and every anomaly-panel open ~1 TiB, taking the server down (swap
exhaustion, load 126). Root causes, measured live via `system.query_log` + `EXPLAIN`:

1. `timestamp Nullable(DateTime64(3))` in the MergeTree sort key (`allow_nullable_key`)
   disables ClickHouse's read-in-order optimization → every `ORDER BY timestamp LIMIT 100`
   became a full-partition top-N sort;
2. the keyset cursor's `coalesce(timestamp, sentinel)` wrapper was unsargable (no granule
   pruning);
3. the page query selected all 21 columns (incl. `message`/`attributes`) for the sort
   (no lazy materialization in CH 24.10);
4. `find_value_novelty`/`find_value_combos` aggregated `argMin(message, timestamp)` per group
   → 136 GiB decompressed per scanned field, ~7 fields per panel view.

Fixes (this session, PR #73): non-Nullable `timestamp` storing the year-2299 sentinel for
undated events (presented as `null` everywhere; `db/_dt.py` is the single home for the
sentinel + `TS_NOT_SENTINEL_SQL` guard); sargable plain-tuple cursor + redundant scalar
`timestamp <= :ts` bound; two-phase grid fetch (thin `(event_id, timestamp)` top-N, then
timestamp-bounded hydration by id — 187 GiB → ~4.5 MiB per page measured); single-element
list filters emit `=` instead of `IN` (fixed sort-key prefix requirement); detectors
aggregate only `argMin(event_id, …)` and batch-hydrate the post-limit findings via
`get_events_by_ids`; every whole-corpus detector scan carries `_HEAVY_SCAN_SETTINGS`
(external GROUP BY spill, 12 GB per-query cap, 8 threads).

**One-time migration (existing deployments)** — new code refuses to start against the legacy
Nullable schema (`init_schema` guard). With the app stopped: create `events_migration_new`
with the new DDL, `INSERT … SELECT` with `coalesce(timestamp, toDateTime64('2299-12-31
23:59:59.999', 3, 'UTC'))`, verify (row count, `countIf(IS NULL)` old == `countIf(= sentinel)`
new, `sum(cityHash64(event_id, content_hash))` checksum, min/max), then `EXCHANGE TABLES` and
keep the old table as `events_legacy_pre_migration` until burn-in. Preflight aborts if any
real event already carries the exact sentinel timestamp. Ran on dev (6.2M rows) and prod
(300M rows). Behavioral note: undated events now sort at the *top* of the default
newest-first grid (sentinel = max datetime; deliberate, keeps broken timestamps visible).

D4 (`find_range_violations`, AMiner `ValueRangeDetector`): for fields whose values parse as
numbers (syntactic `toFloat64OrNull`, never by meaning), learn a baseline band and flag values
outside it. Self-baseline (`method="iqr"`) uses the Tukey fence `[q1−1.5·IQR, q3+1.5·IQR]` over
the corpus — exact corpus min/max flags nothing by construction; temporal (`method=
"temporal-range"`) learns exact baseline-window min/max (AMiner-faithful). Findings group by
distinct violating value; score = distance outside band ÷ band width (normalizes severity
across fields of different scales); degenerate zero-width band floored to 1e-9. Fields with <20
numeric baseline samples skipped; all-skipped → insufficient_data. New `recommend_numeric_fields`
probes candidate coverage/cardinality then one batched `countIf(toFloat64OrNull(...) IS NOT NULL)`
query for the ≥90% numeric-ratio filter, exposed via new `GET /anomalies/numeric-fields`
(cache inventory + live probe, mirroring /anomalies/fields). Verified live: 100 events with
`resp_bytes` in [100,300] plus outliers 50000/60000 flagged both above the IQR band [-1.5,426.5]
ranked by excess, recommender detected resp_bytes as 100% numeric. New `NumericRangeView.tsx`
(band rendered inline as the explainability shot; AnomalyFieldPicker gained a `numeric` mode
fetching numeric-fields and showing parse ratios). Docs: ANOMALY_DETECTION.md §4 (5 tools now,
semantic → §5); MethodologyPanel block. Tests: 6 detector-unit + 1 router-dispatch.

D1 (`find_value_combos`, AMiner `NewMatchPathValueComboDetector`): the multi-field extension
of value_novelty. Groups by two or more field expressions together (`GROUP BY v0, v1, …`) and
scores each surviving combination by the same surprise `−log(count/total)`. Catches
combinations rare even when each field's values are common — verified live: a source of 50
`(login_ok, day)` events + 1 `(login_ok, night)` flagged only the night combo (surprise 3.93),
though `login_ok` alone is common. Both modes carry over (self-baseline rarity floor / temporal
baseline_cnt=0). Requires ≥2 fields; router returns 422 on a single explicit field, service
raises ValueError. Auto mode combines exactly the top-2 highest-coverage recommended fields —
no pair enumeration (105 pairs from 15 fields would be untriageable). Field expressions share
one params dict via the new `_col_expr(prefix=fk0/fk1/…)`. New `ComboNoveltyView.tsx` (2–4
field picker via `AnomalyFieldPicker`'s new min/max-selected props; query gated below 2 fields);
combo drill applies every (field,value) pair as a conjunction in one `setFilters` fold
(ExplorerPage `handleComboDrill`) rather than looping `handleDrillField`, which would clobber
against the same stale `filters` closure. Docs: ANOMALY_DETECTION.md §1 "Value combinations"
subsection (kept under value novelty rather than renumbering); MethodologyPanel block. Tests: 7
detector-unit + 2 router (dispatch + 422).

D2 timestamp-order detector. Prep (no behavior change): extracted the shared
analysis-view chrome into `frontend/src/components/analysis/detector-shared.tsx`
(ModeToggle, RefreshButton, DetectorStatusLine, FindingShell, TagFindingsBar, and the
useAnomalyMarkers/useDetectorRunId hooks), migrated ValueNoveltyView + FrequencyView onto
it (markup-identical), replaced the two-button anomaly sub-tab strip with a Radix `Select`
detector dropdown fed by a `DETECTORS` registry (flat buttons stopped fitting the 320px
panel at 3+ detectors), and standardized every anomaly query key to
`["anomalies", caseId, timelineId, "<detector>"|"fields", ...]`. Backend `_col_expr` gained
a `prefix` param (default "fk", behavior unchanged) so a multi-field query can bind several
field tokens into one params dict — groundwork for D1. (Note: the M19 `shouldInvalidate`
predicate from session 25 is preserved in the migrated views.) D2 (`find_order_violations`,
AMiner `TimestampsUnsortedDetector`): flags events whose parsed timestamp runs backwards
relative to *record order* within a source. Record order = `byte_offset` (monotonic per source
file), then line_number/event_id as tie-breaks — not the parsed timestamp, which would be
circular. Uses a ClickHouse `lagInFrame` window comparing each event to its immediate
predecessor (not a running maximum: a single future-dated outlier would otherwise cascade-flag
every later event until the clock caught up). Mode-less — `method="sequential"`, no
baseline/detect split, no mode toggle in the UI. `min_skew_seconds` (config
`stat_order_min_skew`, default 1.0s) suppresses sub-second logger jitter; score = skew in
seconds. Two queries: a per-source summary (violation count + worst skew, stashed in each
finding's `details` for the UI's per-source group header) and a global worst-first detail query.
New `OrderViolationsView.tsx` groups findings by source. Verified end-to-end against live
ClickHouse: a synthetic 5-row source with one 300s backwards jump flagged exactly that event
(the subsequent forward re-jump correctly *not* flagged, confirming lag-not-running-max),
correct prev-timestamp/skew/source-total, then cleaned up. Docs: new ANOMALY_DETECTION.md §3
(count 3→4 tools, semantic renumbered to §4), MethodologyPanel block. Tests: 5 detector-unit +
1 router-dispatch, full suite green.)

Previous (session 25 — PR #72 review fixes, five items: (1) SSE
invalidation now covers VisualizePage's `viz-field-terms` query key (was silently missing
from `INVALIDATE_PREFIXES`, so a teammate's tag edit never refreshed that chart);
(2) `_run_stat_detector`'s timeline-midpoint lookup and normal-annotation fetch now run
concurrently via `asyncio.gather` instead of sequentially awaited; (3) the duplicated
field-stats-cache inventory resolution in `_run_stat_detector` and `list_anomaly_fields`
is now one shared `_resolve_field_inventory` helper; (4) `find_value_novelty`'s two
near-identical `recommend_novelty_fields` call sites collapsed to one; (5)
`docs/ANOMALY_DETECTION.md` updated to describe the cache-backed inventory path added in
session 24; (6) the four frontend mutation sites (BulkActionBar, useAnnotationMutations,
FrequencyView, ValueNoveltyView) that hand-rolled their own `invalidateQueries` key lists
now reuse `useCaseStream`'s `shouldInvalidate` predicate, so SSE-driven and
mutation-driven invalidation can't drift apart again. `uv run pytest` (489 passed, same
4 pre-existing environment-dependent failures as session 24), `ruff check` clean, `npm run
typecheck`/`lint`/`test` clean (173 passed).)

Previous (session 24 — Phase-2 batch: M22 (a)(c)(d) + M19, four commits.
(a) `_ParameterizedQueryBuilder.add_in_list`/`add_not_in_list` gained a `cast_to_string`
flag: String columns (`source_id`, `artifact`) now emit `column IN {p:Array(String)}` so
ClickHouse can use primary-index/partition pruning, which the previous unconditional
`has(..., toString(col))` form defeated; only UUID `event_id` keeps the cast (error 386
NO_COMMON_TYPE). (c) derived-range histograms collapse the serial min/max range scan +
bucket scan into one round trip: a scalar CTE computes (min, max) and the interval
server-side, `intDiv(toUnixTimestamp(ts), iv) * iv` reproduces toStartOfInterval's epoch
alignment; payload interval/min/max come from the query result, never a Python
recomputation (toUnixTimestamp truncates DateTime64(3) to seconds). Verified live against
ClickHouse 24-alpine incl. empty-match and min==max edges; explicit start/end path
unchanged. (d) `find_value_novelty`'s fields=None path no longer runs the live
`field_inventory` map scan (the ARRAY-JOIN family that OOM'd ClickHouse pre-session-23):
the novelty router branch resolves the candidate inventory from the per-source
field-stats cache (`ensure_source_field_stats` + `merged_inventory`, same template as
`/anomalies/fields`) and passes it via new optional `inventory`/`inventory_total` params;
canonical-mapping aggregates stay live, surprise-score denominator unchanged. (M19)
`useCaseStream`'s `INVALIDATE_PREFIXES` now covers histogram, anomalies-novelty/-frequency,
and the viz-modal keys (field-histogram/-total, field-terms) so a teammate's bulk
anomaly-tagging refreshes those panels over SSE; predicate extracted as exported
`shouldInvalidate` with new vitest coverage. Also fixed dead local invalidations:
BulkActionBar/useAnnotationMutations invalidated `["anomalies", caseId]` which matches no
query (real prefixes are anomalies-novelty/-frequency), and the anomaly views' tag
mutations only invalidated `annotations` — even the analyst's own bulk actions left
panels stale. Note: 4 pre-existing test failures on this machine
(test_embeddings_capability ×2, test_rbac_api, test_uploads) fail on a clean tree too —
environment-dependent, unrelated to this batch.)

Previous (session 23 — large-source performance + value-novelty OOM fix.
Root cause of the unresponsive Explorer on a 5.5 GiB CloudTrail ingest: wide flattened
sources store every unioned column on every event, so each of 2.8M events carried 672
attribute map entries of which ~639 were empty strings — 73 GiB uncompressed in the
`attributes` column. Every map-scanning query paid for it: broad text search 3.3s/scan
(×4 scans per filter interaction), and the value-novelty field inventory's
`ARRAY JOIN mapKeys` + `attributes[key]` re-lookup exploded to ~2 billion rows and
OOM-killed ClickHouse at 56 GiB (the 500s on GET /anomalies). Fixes: (1)
`Event.to_clickhouse_row` drops empty attribute values at ingest — semantically
transparent since a ClickHouse Map returns '' for absent keys; (2) the inventory query
uses a paired keys/values ARRAY JOIN, pre-filters `val != ''`, approximate `uniq()`
instead of `uniqExact`, and external-GROUP-BY spill + a 12 GB query memory cap; (3)
`EventQueryService.query` runs the first-page COUNT and page fetch concurrently; (4)
`ClickHouseStore.init_schema` is cached per instance (was 3 DDL round-trips on every
query). Existing data cleaned via a one-off `ALTER TABLE events UPDATE attributes =
mapFilter((k,v) -> v != '', attributes)` mutation on the running deployment — attributes
went 73.3→5.05 GiB uncompressed, broad search 3.3s→0.37s, field inventory OOM→0.87s,
match counts verified identical. Also fixed the stranded infinite scroll: EventGrid's
load-more only fired from the onScroll handler and was gated on `!isFetching`, so
reaching the bottom while a page fetch was in flight skipped it — with the scrollbar
already pinned, no further scroll event ever came ("scrolled to bottom, nothing
happens"). A virtualizer-driven effect now re-checks when a fetch settles and keeps
loading while the tail rows are in view.)

Previous (session 22 — onboarding tour. First-login guided overlay walking
the core workflow in 11 action-driven steps: create case → open it → upload dialog →
converter-script hint → upload → default "All sources" timeline → Explorer column picker →
open event details → filter in/out buttons → Visualize link → done. Custom spotlight
implementation (no tour library): `frontend/src/lib/tourSteps.ts` (step schema:
route-gated, `[data-tour]` selector anchors, advance = manual Next | app event | route
change), `stores/tour.ts` (non-persisted state machine + `tourEvent()` fire-and-forget
helper), `components/tour/TourOverlay.tsx` (box-shadow spotlight, `pointer-events: none` so
the highlighted control stays clickable; card needs explicit `pointer-events: auto` +
pointerdown stopPropagation because an open Radix modal Dialog sets `pointer-events: none`
on body and dismisses on outside-pointerdown) and `TourProvider.tsx` (auto-start, completion
PATCH; uses `qc.setQueryData` instead of invalidate — an invalidate lets `useCurrentUser`
re-sync the stale cached user mid-refetch and instantly restart the tour). Persistence is a
new server-side `users.onboarding_completed` bool (guarded ALTER migration, `to_dict`,
`update_user`, PATCH /me) — refresh mid-tour restarts from step 1 by design; existing users
backfill to false and see the (always skippable) tour once. Settings page gained a "Restart
onboarding tour" section. Verified end-to-end with a headless-Playwright drive of all 11
steps including a real CSV ingest, finish/skip persistence across reloads, and the
settings restart path. Bug fixes on the way: UploadDialog kept a stale duplicate-warning /
error across close/reopen (missing `mutation.reset()`); Settings audit-trail download had
no error handling (silent unhandled rejection).)

Previous (session 21 — M21 storage redundancy cleanup, all three items from
the 2026-07-05 storage placement audit. (1) `Event.vector_id` removed everywhere (dataclass,
ClickHouse DDL/column lists/SELECTs, `_columns.py`, API/frontend event shape) — it was
unconditionally `str(event_id)`; Qdrant point IDs now use `event_id` directly. Existing
ClickHouse tables keep the orphaned column harmlessly (CREATE IF NOT EXISTS; inserts name
columns explicitly). (2) Dead `Source.embedding_model`/`Source.embedding_config` Postgres
columns deleted (never written; live config is Timeline-scoped) — also removed the orphaned
`_run_embedding_job` in `cases.py` (zero callers, vestigial source-level embed path from the
same era) and reworked `MethodologyPanel.tsx` to read the Timeline's embedding model/config
(it previously read the always-null Source fields, so it always showed the fallback text).
(3) Qdrant payload trimmed from a full row mirror to filter-relevant fields only
(`case_id`, `source_id`, `artifact`, `timestamp`) in both `Event.to_qdrant_payload` and the
embed pipeline's `_qdrant_payload`; full event detail resolves post-search via the existing
ClickHouse `get_events_by_ids` hydration. `tags` dropped from the payload (nothing filtered
on it natively; annotation tags mutate after embed and would silently go stale). Existing
collections keep fat payloads until re-embedded — payload shape is not part of
`EmbeddingConfig.config_hash`, so no identity change.)

Previous (session 20 — PR #65 review fixes. `Source.created_by` for CLI
ingests now stores `resolved_user.id` instead of `resolved_user.username`, matching every web
call site (`api/routers/cases.py`) — the mismatch would have silently broken any future
id-based creator lookup. `tsig embed` gained the `--user` attribution + `cli.embed.source`
audit-log row it was missing (PROGRESS.md previously claimed embed got "the same validation for
consistency" — it hadn't gotten audit parity). `tsig ingest`'s pre-scan `total_size` walk
(a second full directory `rglob`/`stat` pass, redundant with `IngestionPipeline`'s own byte
count and racy against directories that change between the two scans) is removed; the banner
no longer prints a size, the progress box reports it once ingestion starts. `tsig ingest`'s
three separate `asyncio.run()` calls collapsed into one. `SimilarityService.find_similar_by_text`
(`db/similarity.py`) now catches encoder failures and raises `EncoderUnavailableError`, mapped
to a 503 in `api/routers/events.py::semantic_search_events` — previously a flaky remote
encoder crashed semantic search with an unhandled 500, the exact failure mode the sibling
`_guard_encoder` fix (session 19) addressed only for the field wizard. Frontend `fmtDuration`
(`JobTray.tsx`) fixed to include seconds in its hour branch, matching `cli/progress.py`'s
`_fmt_duration` — they'd drifted, so web ETAs over 1h read differently from the CLI's.

Previous (session 19 — CLI ingestion promoted to a real feature. `tsig ingest
--case` previously accepted a case *name* and passed it straight through as the case ID with
no validation (`get_case` was never called), silently writing Sources against a
possibly-nonexistent case; it also never set `Source.created_by` and printed nothing during
multi-hour large-file runs. Now: new `tsig cases list` (unscoped, admin/CLI use — resolves
`owner_id`/`team_id` to usernames/team names via `list_users`/`list_teams`); `tsig ingest`
validates `--case` via `store.get_case()` before touching the file and rejects unknown IDs;
adds optional `--user` attribution (defaults to the sole active admin if unambiguous, else
errors) written to `Source.created_by` plus a `cli.ingest.source` audit-log row
(`record_audit`); and a new `src/tracesignal/cli/progress.py` ported near-verbatim from
ScalarForensic (`_ETATracker` Kalman throughput/ETA estimator, block-element progress bar,
duration formatter) driven by bytes off the existing `IngestionPipeline.progress_callback`
(same signal the web upload job already uses — no new plumbing in `pipeline.py`). New
`tests/test_cli.py` (11 tests: case listing, case/user validation, Kalman tracker math).
`tsig embed` also gained the same case-ID validation for consistency.)

Previous (session 18 — Milestone 2 batch, PR 7/7: M16b ColumnPicker
derived-key grouping (PR #54 finding #34). New `splitDerivedKey` in
`frontend/src/lib/enrichment.ts` (last-separator split, keeps the key contract mirrored
in one file). ColumnPicker's Dynamic fields group now collapses enrichment-derived keys
(`src_ip:geo_country`) under their parent attribute as a collapsed-by-default
"Derived (N)" disclosure, children labeled by output-field suffix; derived keys whose
parent isn't in the field list land in a trailing "Derived fields" group; an active
search auto-expands matching children (never hides a selectable field). Checkbox ids
stay the full raw key — selection persistence and the grid untouched. Frontend-only.
New vitest coverage: `columnPicker.test.tsx` (grouping, expansion, orphans,
search-expansion, raw-key selection) + `splitDerivedKey` unit tests.)

Previous (session 18 — Milestone 2 batch, PR 6/7: M16a staging-format
redesign. `EnrichmentResultStaging` regrained from row-per-(event, attr, output_field)
to row-per-(job, event) with a `fields` JSON map (`field_key -> value`, keys already
attr-prefixed) — ~3-6x fewer staging rows for multi-output enrichers, unique index now
`(job_id, event_id)`. `_process_batch` accumulates one map per event (empty maps skipped);
apply loop pages 4000 rows (was 10000 per-field rows) and expands maps back into triples
for `apply_enrichments` — no ClickHouse-side change. **Destructive migration**:
`init_schema` drops a legacy staging table (recognized by its `field_key` column) before
`create_all`; orphaned pre-upgrade staged rows are discarded, matching the pre-release
stance; the old `enricher_config_hash` ADD COLUMN block is gone. Dead helpers
`pop_staged_rows_for_job`/`delete_staged_rows` replaced by read-only
`list_staged_rows_for_job`. New tests: migration drop+recreate (idempotent), one-row-per-
event `_process_batch` grain.)

Previous (session 18 — Milestone 2 batch, PR 5/7: M15 per-source
field-stats cache. New `db/field_stats.py` + Postgres `source_field_stats` (versioned
JSON payload: top-level cols + attribute keys with distinct/coverage/3 samples; version
mismatch = cache miss, no migrations). Computed per source in 2 ClickHouse queries at:
ingest completion (isolated, never fails the ingest) and after every enrichment apply
(the only attributes mutation path; on refresh failure the stale row is dropped so reads
recompute). Read path is compute-on-read + store — pre-existing DBs self-heal. Converted
call sites: `list_fields` (ColumnPicker, timeline wizard, mapping validation),
`field_coverage` (timeline wizard — counts now exact instead of 20k-row samples;
`sampled_rows_per_source` removed from response + frontend type), `field_inventory`
(Visualize field picker, novelty recommender — `recommend_novelty_fields` accepts a
pre-merged inventory; canonical field-mapping coalesce aggregates stay live via new
`canonical_inventory`, since per-source counts can't dedupe multi-raw-key events). Merge
math: coverage sums exactly, distinct = max-across-sources (documented approximation).
Deliberately not converted: embedding wizard's `list_fields_by_artifact` (cost is the
cohesion value-sampling, not inventory). `delete_source` drops the cache row. New
`tests/test_field_stats.py`: live-ClickHouse parity vs the old scans, self-heal,
version-mismatch recompute, derived keys visible after `apply_enrichments`.)

Previous (session 18 — Milestone 2 batch, PR 4/7: CI container smoke test.
New `container-smoke` job: builds the reference image, boots it with `--network host`
against the same pg/clickhouse(glibc)/qdrant service containers the backend job uses,
asserts `/api/health` returns `status:"ok"` (would have caught C1's broken CMD import)
and that `/` serves the packaged frontend HTML; dumps container logs on failure.
Dockerfile gains `ARG INSTALL_EMBEDDINGS` (default 0) so the image skips the ~2 GB local
embedding stack once M5's `embeddings` extra lands — the smoke test then doubles as the
"boots without the extra" regression test.)

Previous (session 18 — Milestone 2 batch, PR 3/7: M17 job authz via case
RBAC. `Job` gains `case_id` (in `to_dict()` too), threaded through every
`job_store.create` site (ingest, embed, manual + automatic enrich, startup re-runs —
`run.case_id`). `GET /api/jobs/{id}`: creator/admin unchanged; otherwise READ access on
the job's case grants visibility (`resolve_case_access`), so case members can poll each
other's jobs and system jobs (`created_by=None`) become member-visible instead of
admin-only. Non-members still get 404 (no existence probing). Case-less jobs keep
owner-or-admin semantics. New `tests/test_jobs_api.py` covers the four quadrants.)

Previous (session 18 — Milestone 2 batch, PR 2/7: M5 dependency diet.
Removed never-imported `torchvision`/`onnxruntime`/`jinja2`/`alembic`; `torch` +
`sentence-transformers` moved to an optional `embeddings` extra
(`uv sync --extra embeddings`) — base install drops ~2 GB. Sole ML import
(`models/embeddings.py`) is now lazy inside `load()` with an actionable RuntimeError;
new `embeddings_available()` (importability OR `TS_EMBEDDING_API_BASE_URL` — remote mode
needs no torch) surfaces as `embeddings_available` on `/api/health` and gates embed-start
and semantic-search with a request-time 503 instead of a job that dies on ImportError.
Field-recommend already degraded gracefully. README quick-start/airgapped docs updated.)

Previous (session 18 — Milestone 2 batch, PR 1/7: ingest throughput.
`TS_INGEST_BATCH_SIZE` (default 20k) replaces the accidental reuse of
`embedding_batch_size` (64) as the ClickHouse insert batch in `IngestionPipeline` —
one HTTP insert per 20k rows instead of per 64, the dominant fix for the 100 GiB-over-LAN
ingest goal. CLI `--batch-size` falls through to the setting; enricher read paging
bumped to ≥1000; 413 upload rejection names `TS_MAX_UPLOAD_BYTES` and points at
`tsig ingest` for huge files; deferred native-protocol/async_insert options recorded
as ROADMAP M20. Remaining Milestone 2 PRs planned: M5 dependency diet, M17 job RBAC,
CI container smoke test, M15 field-stats precompute, M16 staging redesign +
ColumnPicker grouping.)

Previous (session 17 — final PR #54 cleanup batch, M16 bulk. Four commits on
`feat/enricher-subsystem`: **(1) micro-fixes** — GeoIP output-field names single-sourced
(order locked, config_hash-stable), `refresh_availability(key)` single-enricher form,
batched `count_events(source_ids=...)`, concurrent eligibility checks via `asyncio.gather`,
sidecar-first `check_availability` (no full `.mmdb` mmap when `.meta.json` carries
`database_type`), plus comments documenting: eligibility-regex role (#15), create_task-over-
BackgroundTasks rationale (#17/#21), deliberate reconcile divergence (#20 won't-fix), sorted
`list_fields` attributes (#33). **(2) shared abstractions** —
`ClickHouseStore.iter_source_events` batching generator (embedding pipeline + enricher jobs),
`api/uploads.py::receive_upload_to_tmp` (temp-file + hash + 413 handling, used by source and
asset uploads), `enrichers/base.py::effective_enricher_state` (single "explicit overrides
admin default" rule for `list_timeline_enrichers` and
`list_automatic_enrichers_for_source`). **(3) generic asset abstraction** — Enricher ABC
gains `asset_spec`/`asset_status()`/`install_asset()` + `AssetValidationError`; GeoIP
implements them (City-flavor validation moved out of admin.py; lazy db-path resolution);
GET/POST `/admin/enrichers/geoip/database` replaced by asset state folded into
`GET /admin/enrichers/config` + generic `POST /admin/enrichers/{key}/asset`; audit action now
`admin.enricher_asset_upload`; field-key contract extracted to
`base.FIELD_KEY_SEPARATOR`/`derived_field_key`. **(4) frontend** — new `lib/enrichment.ts`
(key contract mirror + `hasEnrichmentSiblings` + decorator registry), Explorer flag and
private/public badge now data-gated on enrichment siblings (user decision: badge means "was
enriched", so un-enriched private IPs show nothing), `AdminEnrichersPage` fully generic
(maps configs, asset section from `config.asset`), `privateIp.ts` IPv6 parsed to hextets
(zone suffixes, `::`, embedded IPv4; bitmask range checks; fixes uncompressed loopback and
`FEBF::` misclassification). Deferred to fresh branch: staging-format redesign + #34
(ColumnPicker cardinality) — roadmap M16 rewritten accordingly. 450 backend + 164 frontend
tests passing.)

Previous (session 16 — roadmap hardening batch M1–M4, M7, M8, shipped on
the enricher PR branch. **M1**: evidence-mutation failures now surface — `delete_source_events`
re-raises (only a missing `events` table stays a benign no-op), `delete_timeline_events`
aggregates per-source failures, DELETE source/case endpoints fail closed with 502 +
`source.delete_failed`/`case.delete_failed` audit rows and keep the Postgres row (the
authoritative evidence record) so the delete stays visible and retryable; ingest rollback is
still best-effort but logs each failed step and flags `cleanup incomplete` on the job error.
**M2**: one SQL escaping regime — `count_events` on `{name:String}` binds (numbered params
for the IN-list, empty list short-circuits), partition expressions built via a shared
validated `_partition_expr` (fail-closed charset guard mirroring `generate_id`'s contract,
Unicode `isalnum` + `-`/`_`). **M3**: in-memory exponential login backoff per
(username, client IP) — 429 + `Retry-After` after `TS_LOGIN_BACKOFF_THRESHOLD` (5) failures,
`base*2^(n-threshold)` capped at `TS_LOGIN_BACKOFF_MAX_SECONDS`; identical behavior for
unknown user vs. wrong password (no existence leak, tested); `auth.login_rate_limited`
audit action. **M4**: compose publishes Postgres/ClickHouse/Qdrant on `127.0.0.1` only
(loopback binds instead of the roadmap's internal-network+override idea — the native
`uv run tsig-web` dev workflow depends on localhost ports); README compose section un-staled
(app service is opt-in/commented). **M7**: JobStore caps retained terminal jobs at 200,
evicting oldest-finished first, never queued/running; mutations now behind a real lock.
**M8**: dead `secret_key` setting deleted everywhere. Roadmap also gained M17–M19 (PR #7
follow-ups rescued from the archive: job authz via case RBAC, `access_level` from the case
API, SSE invalidation misses histogram/anomaly panels). 438 tests passing.)

Previous (session 15, continued — enrichment persisted into
`events.attributes` (user decision: the ClickHouse events table is a normalized derivative
of the hashed, immutable source files, so dataset mutation is the better design): the
separate `event_enrichments` table, its read-time `_hydrate_enrichments` join, and the
`list_fields` "enrichments" response key are gone — **destructive**: `init_schema` now
`DROP TABLE IF EXISTS event_enrichments` (pre-release DBs deprecated; derived data,
re-running the enricher regenerates it). New write path: results stage in Postgres as
before, then one atomic per-source partition rewrite at job end
(`ClickHouseStore.apply_enrichments`: scratch triples table → `mapUpdate` LEFT JOIN copy of
the `(case_id, source_id)` partition → `REPLACE PARTITION`; idempotent, per-(case,source)
apply lock, scratch tables swept at startup; smoke-tested against live CH 24 — counts
stable, originals untouched, re-apply idempotent). Periodic flush +
`enrichment_flush_batch_count` removed (apply-once). Per-row `enricher_config_hash`
replaced by per-source Postgres provenance (`source_enrichments` upsert, audit
`enricher.applied`). Derived-field naming contract now `<attr_key>:<output_field>`
(`src_ip:geo_country`; GeoIP output fields renamed geo_country/geo_city/geo_country_code)
— sorts beside its source column and is filterable/exportable/visible in every read path
for free since it's a real attribute key. Frontend: `countryFlag.ts` reads the new sibling
keys, dead "Enrichments" ColumnPicker group removed, `FieldsResponse.enrichments` dropped,
EventDetailPanel long field labels now wrap (`break-all`) instead of overlapping values.
Immutability language reframed across `clickhouse.py`/`enrichers/*`/`field_mappings.py`/
`MODEL_REFINEMENT.md`: immutable = original evidence file + provenance hash columns, not
the derived attributes map.)

Previous (session 15 — enricher hardening, roadmap M9–M13 from the PR #54
review: per-run enricher instances via `Enricher.spawn()` (registry singleton now
metadata/availability-only; the shared-`_reader` close race is gone) with an in-memory
`(timeline_id, enricher_key)` run guard — manual "Run now" returns 409 with the conflicting
job id, auto-trigger skips with a log; GeoIP `enrich_value` validates input with stdlib
`ipaddress` and only swallows `AddressNotFoundError` — reader failures now fail the job
loudly (context note, no raw values) and a failed-but-alive job flushes+clears its own
marker; `enricher_config_hash` populated end-to-end (new `Enricher.config_hash()` mirroring
`ParserConfig`, GeoIP hashes db sha256+build_epoch from a `.meta.json` sidecar written at
upload — the upload's `copy_and_hash` digest is now captured — with a hash-and-persist
fallback for pre-sidecar installs; staging table gained the column via additive migration);
upload validation rejects non-City `.mmdb` flavors with an actionable 400 and
`check_availability` checks flavor too; startup reconciliation now *flushes* orphaned staged
rows to ClickHouse (audit `enricher.job_recovered`) and auto-schedules a re-run over the
timeline's current ready sources after availability refresh (argMax read-dedup makes the
overlap safe; ClickHouse-down leaves marker+rows for the next restart);
`EnrichersDialog.tsx` toggle/mode lost-update race fixed with the standard TanStack
optimistic-update pattern (`onMutate` cache patch, rollback on error, invalidate only when
last mutation settles))

Previous (session 14, continued — source ingest-status lifecycle:
`Source.status` (`ingesting`/`ready`, additive migration backfills `ready`); uploads create
the row as `ingesting` and the background job flips it to `ready`; `_resolve_timeline_scope`
(the single scope choke point) excludes non-ready sources so the explorer, histogram,
export, detectors, and wizards never see half-ingested data; timeline embedding refuses
409 while a member source is ingesting; field-mapping validation runs inventory checks only
against ready sources (structural rules always apply — `validate_field_mappings` now takes
`None` inventory to mean "unknown, skip inventory checks"); startup reconciliation removes
sources orphaned mid-ingest by a restart (partial events + row, audited as
`source.ingest_interrupted`) so re-upload isn't blocked by the file-hash duplicate check;
frontend shows an "Ingesting" badge in the source list and an Explorer banner with
poll-until-ready + auto-refetch when the source becomes visible)

Previous (session 14 — full repository audit; fixed all Critical/High
findings on `fix/audit-critical-high`: Dockerfile CMD now uses `--factory
tracesignal.api.main:create_app` (the shipped image previously pointed at a nonexistent
`app` attribute and could not start); CSV parser streams instead of `list(fh)`-ing the whole
file (incremental byte-offset/line tracking in `_RecordTrackingIterator`); `tsig-web` builds
the frontend only when `dist/` is missing (`TS_FRONTEND_REBUILD=1` forces) and enables the
uvicorn reloader only in development; embedding model load enforces `HF_HUB_OFFLINE` unless
`TS_ALLOW_ONLINE` and fails with an actionable message; all remaining blocking
`EventQueryService` calls in async handlers threadpool-wrapped; uploads single-pass
copy+hash off the event loop with a `TS_MAX_UPLOAD_BYTES` cap (413 mid-stream). Remaining
Medium/Low findings consolidated into a new phase-2 `docs/ROADMAP.md`; the fully-shipped
phase-1 roadmap archived to `docs/archive/ROADMAP_PHASE1.md`; CLAUDE.md frontend-build note
un-drifted)

Previous (session 13 — deployment: `docker-compose.yml` gained an `app` service
that builds/runs TraceSignal itself via a new `Dockerfile`, after the backing services;
`tsig-web` now always rebuilds the frontend on startup instead of skipping when `dist/` exists;
README documents the airgapped install path (build on an online machine, carry `.venv/` +
`frontend/dist/` over on a portable drive, backing services out of scope); archived
`docs/PLAN_ISSUES_5_10_11.md` to `docs/archive/` now that issues #5/#10/#11 are all shipped;
fixed a stale test asserting the old `text/x-python` converter content-type)

Previous (session 12 — issue #10: timeline creation wizard with query-time
field aggregation (`Timeline.field_mappings` metadata, coalesce resolution in
`db/field_mappings.py` threaded through filters/histogram/viz/export/detectors, field
discovery surfaces canonical names with provenance, `PATCH .../field-mappings` + audit,
`GET /cases/{id}/fields/coverage`, 4-step wizard with name+value-shape merge suggestions);
issue #5: full rename TraceVector → TraceSignal
(`tsig`/`tsig-web` CLI, `TS_` env prefix, hard cutover, `docs/MIGRATION_RENAME.md`); issue #11:
vendored self-contained 2timesketch converter scripts (`scripts/vendor_converters.py` →
`src/tracesignal/assets/converters/` + manifest), `GET /api/converters[/{name}]` download
endpoints, converter panel + static LLM-converter prompt in the upload dialog, and subtle
collapsible `GuidancePanel` guidance on the cases page and case overview)

Previous (session 11 — visualization v2: two-layer comparison with
server-enforced shared-grid invariants (`POST .../viz/compare`, kinds time/terms/numeric),
derived metrics as pure client-side transforms (Δ / rate / % of baseline / cumulative, nulls
for undefined bins), first-class time-histogram chart type, bar orientation + grouped compare
bars, numeric-histogram comparison overlay, per-chart options panel, unified on-screen/export
captions with truthfulness warnings, five task presets, saved charts (`SavedChart` Postgres
model + CRUD), URL-serialized `ChartConfig` (`c_*` params), and the Explorer histogram
tooltip anchor/clamping fix)

**Open follow-up:** none for PR #8 — every finding from its review (7 correctness bugs +
9 cleanup/design items) is resolved; see `docs/archive/PR8_REVIEW_FINDINGS.md`.

This document tracks implementation progress against the MVP defined in
[`CONCEPT.md`](./CONCEPT.md) and the tech-stack decisions in [`TECH_STACK.md`](./TECH_STACK.md).
See [`ROADMAP.md`](./ROADMAP.md) for the detailed scope breakdown and remaining work.

## Overall completion

**Estimated MVP completion: ~97 %**

Backend model, API, statistical anomaly detectors, the full frontend, and the full
auth/RBAC/teams/audit/live-collaboration layer are implemented and tested (341 backend tests,
118 frontend tests, both suites green; `ruff`/`tsc`/`oxlint` clean). What remains before MVP
closure is **offline-mode enforcement** — `allow_online` still isn't checked at most network
call sites (OIDC SSO is a deliberate, documented exception). GPU acceleration remains
aspirational (no code exists for it yet).

## MVP feature checklist

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | **Ingestion (CLI-first + web upload)** | ✅ Done | Streaming CSV/JSONL parsers; `tsig ingest --source` CLI; web drag-and-drop via `POST /api/cases/{id}/sources`. |
| 2 | **Source / Timeline / Artifact model** | ✅ Done | `Source` = one file; `Timeline` = grouping; `Artifact` = per-event Plaso class. Implemented across Postgres, ClickHouse, Qdrant, API, CLI, and tests. |
| 3 | **Storage & Vector Backend** | ✅ Done | ClickHouse `events` table with `tokenbf_v1` full-text index; Qdrant collections keyed by `(case_id, embedding_config_hash)` with vector-size config-match checks. |
| 4 | **Web UI (ELK-like investigation interface)** | ✅ Done | React 19 + Vite + TypeScript. Explorer (grid, filter rail, tag facets, histogram, export, saved views, bulk actions, column picker), light/dark theme + comfortable/compact density toggles, case/timeline/source management, job tray. |
| 5 | **Anomaly & Similarity Panel** | ✅ Done | Statistical engine (`value_novelty` + `frequency` z-score detectors, self-baseline and temporal modes) replaced the earlier embedding-distance-only approach; see `db/anomaly_stats.py`. Similarity search and semantic search remain Qdrant-backed. Detector runs persist to Postgres (`detector_runs`) instead of round-tripping live event IDs through the URL. |
| 6 | **Remote embedding support** | ✅ Done | OpenAI-compatible remote embedding endpoint as an alternative to local sentence-transformers. |
| 7 | **Authentication, RBAC, teams, audit trail, live collaboration** | ✅ Done | Session-cookie auth + optional OIDC, seeded one-time bootstrap admin with centrally-enforced forced rotation, case-RBAC dependency layer, teams with member/manager roles, append-only audit trail, SSE live-collaboration stream with per-tick access re-validation. Full security review completed, all findings resolved — see `docs/archive/PR7_REVIEW_FINDINGS.md`. |
| 8 | **Deployment & Operation** | 🟡 Partial | Reference `docker-compose.yml` (podman-compatible) builds and runs the app itself alongside the backing services; native `uv`/`tsig-web` workflow (always rebuilds the frontend, no stale-`dist` check); documented airgapped install path (README). Missing: offline-mode enforcement, GPU index selection. |

## Completed architectural decisions

- ✅ Language & packaging: Python 3.13 + `uv`
- ✅ Web backend: FastAPI + Uvicorn
- ✅ CLI ingestion: Typer
- ✅ Frontend: React 19 + Vite 8 + TypeScript, Zustand + TanStack Query/Table/Virtual
- ✅ Metadata store: PostgreSQL (async SQLAlchemy)
- ✅ Event store: ClickHouse
- ✅ Vector store: Qdrant (tested with v1.18.2)
- ✅ Embedding runtime: sentence-transformers (`all-MiniLM-L6-v2` baseline), plus an
  OpenAI-compatible remote endpoint option
- ✅ Data model: Case / Source / Timeline / Artifact (see `MODEL_REFINEMENT.md`)
- ✅ Auth backend: session-cookie auth for local users + optional OIDC SSO (see `TECH_STACK.md`
  §8)

## Known gaps / next logical steps

1. **Offline-mode enforcement** — `allow_online` is a config flag
   (`core/config.py`) that is read but never checked at most network call sites.
   Airgapped-by-default is a stated design goal (`CLAUDE.md`) that isn't fully enforced in
   code. OIDC SSO (`TS_OIDC_ENABLED`) is a deliberate, documented exception — it's
   operator-opted-in and independent of `allow_online` (see `TECH_STACK.md` §6).
2. **GPU acceleration** — no ROCm/CUDA-specific code paths exist anywhere in the codebase; this
   is still purely aspirational, unlike the other "TBD" items which have concrete partial work.
3. **Authentication, RBAC, teams, audit trail, live collaboration** — ✅ implemented
   (2026-07-02) and hardened through a full security review; all findings resolved — see
   `docs/archive/PR7_REVIEW_FINDINGS.md`. Remaining deliberately-descoped item from that
   review: `Job` has no `case_id`, so job-status polling is still authorized by creator
   identity rather than `resolve_case_access` (a teammate can't poll a shared case's embed
   job started by someone else) — flagged as a real follow-up, not done here.
4. **C13 tag push-down / C18 persisted detector runs** — ✅ both implemented (2026-07-02); see
   `db/queries.py` (`TagFilter`, `add_tag_filter`) and `db/postgres.py` (`DetectorRun`,
   `create_detector_run`/`get_detector_run`).
