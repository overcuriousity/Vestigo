# Handoff — PR #349 review fixes (branch `fix/honest-finding-totals`)

Written 2026-09-03, mid-task, machine shutting down. Another agent takes over
from the pushed GitHub state.

## What this branch is

PR #349 made `total_findings` an exact count instead of the page length, and
made the Investigate rail disclose what it is not showing. A `/code-review 349`
pass found 8 issues in it. This commit fixes all 8. **The work is functionally
complete but not verified: 25 backend unit tests fail and must be updated
before merge.** Details in "What is left" below.

## The one design decision, and why

Finding 1 was the big one: the PR got its exact totals from a frameless
`count() OVER ()` / `sum(hits) OVER ()` in the paging statement, at ten sites
across value_novelty (per-field + batched), value_combo, numeric_range, charset
and entropy.

That window buffers every post-`HAVING` group in memory *after* the (spillable)
`GROUP BY`, and **window sorts cannot spill** — `db/_scan.py:857-859` says so
outright, and `db/queries.py:2533-2543` records `field_terms` dying at
`max_memory_usage` on exactly that pattern. Here it is worse: value novelty's
`HAVING cnt <= rarity_floor` keeps the *rare* values, which on a field like
`url` is nearly every distinct value in the corpus.

Options weighed, in the conversation, with the user:

1. **Companion count query per site (chosen).** Always exact. Costs a second
   corpus scan per site.
2. Bounded window: keep one scan, wrap the counted set in
   `ORDER BY <page key> LIMIT 50_000`, report `total_findings_exact=false`
   above the ceiling. One scan, bounded memory, exact in every realistic case.
3. Keep the window, retry on `MEMORY_LIMIT_EXCEEDED`.

I recommended (2) on cost grounds. **The user chose (1)**, explicitly: accurate
readings within available resources, extra SQL queries acceptable. That is the
decision on record — do not revisit it without asking.

The duplication hazard that made me hesitate about (1) — ten hand-written
statement *pairs* whose `WHERE`/`GROUP BY`/`HAVING` must agree forever — is
solved in code: `StatisticalAnomalyService._page_and_total`
(`src/vestigo/db/anomaly_stats.py`, ~line 1720) takes one `core` string and
composes both statements from it. The page is `core + page_tail`; the total is
`SELECT {total_select} FROM (core) {total_tail}`. They cannot drift.

The two run concurrently under `scan_fanout(2)` with `copy_context().run`, the
same shape as `EventQueryService._run_parallel` — so wall clock stays roughly
one scan's and the extra cost is ClickHouse-side. Concurrency on one client is
safe: `ClickHouseStore` sets `autogenerate_session_id=False` for exactly this.

## The 8 findings and what was done

1. **Frameless `OVER ()` in whole-corpus scans** (`anomaly_stats.py`) — all ten
   sites converted to `_page_and_total`. `n_total` is now read off the
   companion scan via the module helper `_scalar_total`. The four remaining
   `OVER ()` uses in the file (lines ~3606, ~3713, ~3751, ~7046) are
   pre-existing and structurally bounded (candidate caps of 1,000–2,000; the
   template set) — deliberately left alone.
2. **`analysis_cache` had no version** — added `CACHE_VERSION = 2` in
   `db/analysis_cache.py`, folded into `fingerprint()`. Without it every row
   cached before this PR stayed a hit forever and served the old page-length
   total with no `total_findings_exact`, which clients default to `true`.
3. **Dismissals made the truncation row fire spuriously** — the user chose the
   backend fix: `_apply_dismissals` (`api/routers/events.py`) now subtracts
   what it dropped from `total_findings`. Known residue, documented in the
   code: only dismissals *on the page* are knowable, because matching runs on
   serialized findings. Pushing dismissals into the detector SQL would make it
   exact and is deliberately not done — dismissal stays presentation-only and
   out of the reproducibility hash. The error can only leave the total slightly
   high, never low.
4. **charset counted rows the Python loop discards** — the loop's three
   `continue` paths now increment `discarded`, and a non-zero `discarded`
   appends an inexactness reason. All three are unreachable given the SQL's own
   filters; the counter exists so that if one is ever reached the total says so
   instead of stranding a permanent truncation row.
5. **Inexactness tooltip showed an unrelated warning** — new
   `StatAnomalyResult.total_findings_note`, built by `_inexact_note`, serialized
   as `total_findings_note`, carried through `MethodState.totalNote`, read by
   `DetectorStrip`. The reason is still in `warnings` too, but a client cannot
   find it there: runners append the reasons *after* every other caveat, so
   `warnings[0]` on a temporal run is a window-size caveat.
6. **`n_total` double-counted for two tokens on one attribute key** —
   `_batched_attr_novelty_rows` now returns `(rows_by_key, total_by_key,
   inexact)` and the caller sums `total_by_key.values()` once, before the token
   loop. `user` and `attr:user` resolve to one key and are now counted once.
7. **Stale `row_ceiling_hit` warning text** — said "ordered by novelty", the
   grouped charset scan orders by `score DESC, cnt ASC`. Fixed.
8. **Page store keyed by method only** — `stores/findingsLimit.ts` is now keyed
   by `pageKeyOf(method, scope, params)`, the same identity the query is keyed
   by. New `useFindingsPageKey` hook; `InvestigateSheetHost` computes the key
   and passes `pageKey` into the sheet. "Show more" in the sheet's method mode
   no longer re-runs the rail's separately-scoped detector.

## Verification status — read this before claiming anything works

- **Frontend: green.** `npx tsc -b --noEmit` clean, `npm run test` 1275 passed
  / 144 files. `npm run lint` shows only pre-existing warnings.
- **Backend lint/format: green** (`ruff check`, `ruff format --check`).
- **Backend tests: 25 failures in `tests/test_anomaly_stats.py`** (204 pass).
  These passed before my changes. `tests/test_value_combo_totals_clickhouse.py`,
  `tests/test_novelty_batched_clickhouse.py`,
  `tests/test_charset_group_field_clickhouse.py` and `tests/test_analysis_cache.py`
  all pass (52 tests).
- Nothing was run against a real deployment. The parallel two-statement path
  has never executed against a live ClickHouse in this session.

## What is left — start here

### 1. Fix `tests/test_anomaly_stats.py` (the 25 failures)

Cause, not a code bug: `FakeClient` (line ~71) is **FIFO** — it pops one canned
response per `query()` call regardless of the SQL. Each converted detector now
issues two statements, so the totals statement consumes the response meant for
the next scan, and `_scalar_total` reads a page row (`ValueError: invalid
literal for int(): 'val_0'` is the signature).

Two things need doing to the fake, and they are the same edit:

- **Dispatch on SQL content instead of FIFO** for the totals statement. The
  companion is recognisable: it starts `SELECT {total_select} FROM (` and ends
  with `) AS scanned`. Give the fake a way to seed a totals answer per scan
  (e.g. `FakeQueryResult` entries tagged, or a `totals=` seed the fake returns
  whenever the SQL matches `) AS scanned`), and leave the page responses FIFO.
- **Make it thread-safe.** The two statements now run in two threads, so FIFO
  pop order is not even deterministic. A `threading.Lock` around `query()`, or
  content dispatch that removes the ordering dependence entirely.

Then each failing test needs its expectation updated: the page rows no longer
carry a trailing `n_total` column, and the total comes from the companion
answer. Several tests assert on SQL text (`test_*_sql_shape_*`,
`test_*_suppression_is_bound_into_the_sql`) — those should now assert the
suppression appears in **both** statements, which is the property
`_page_and_total` exists to guarantee and is worth a test of its own.

Run with `uv run pytest tests/test_anomaly_stats.py -q` (fast, ~6s — it is all
fakes). **Do not run the full backend suite; the user has asked for targeted
test files only.**

### 2. Consider: parallel vs serial in `_page_and_total`

If the concurrency makes the fake painful, running the two statements serially
is a one-line change in `_page_and_total` and costs only wall clock (each
statement then gets the full slot budget instead of half, and `scan_fanout(2)`
drops out). The user's requirement was accuracy within resources, not
concurrency — this is a free choice. Ask before flipping it if unsure.

### 3. Then

- Verify against a real deployment (`/verify` skill, or `podman compose up -d`
  plus `uv run vestigo-web`) — the two-statement path has never run live.
- `docs/ANOMALY_DETECTION.md` §"Totals and truncation" still describes the
  window-function mechanism as the way the exact count is produced ("`count()
  OVER ()` in the same statement … the pattern `list_log_templates` already
  uses", plus the paragraph arguing the window is bounded by what `ORDER BY`
  materialises anyway — that argument is wrong, a limit-aware sort keeps top-N
  and the window keeps everything). Rewrite it to describe the companion scan,
  and state the rule the file should carry going forward: **a frameless window
  is allowed only over a structurally bounded row set; otherwise the count is
  its own aggregate.** Also document `CACHE_VERSION` and the dismissal
  subtraction.
- `docs/PROGRESS.md` needs an entry for this work.
- Re-run `/code-review 349` before merge.

## Files touched

```
src/vestigo/db/anomaly_stats.py          (ten scan sites + _page_and_total, _scalar_total, _inexact_note)
src/vestigo/db/analysis_cache.py         (CACHE_VERSION)
src/vestigo/api/routers/events.py        (dismissal subtraction, total_findings_note)
src/vestigo/api/routers/analysis.py      (total_findings_note on the template path)
frontend/src/api/analysis.ts
frontend/src/hooks/useMethodFindings.ts
frontend/src/stores/findingsLimit.ts
frontend/src/components/analysis/DetectorStrip.tsx
frontend/src/components/analysis/InvestigateSheet.tsx
frontend/src/components/analysis/InvestigateSheetHost.tsx
frontend/src/test/{investigateRail,investigateSheetHost,fieldOverrides,fieldOverridesAutoCap}.test.tsx
```
