# Honest Finding Totals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every findings response reports the exact number of findings the method produced across the full analysed scope, the page is the true top-`limit` by score, and every truncation the analyst sees is disclosed on screen.

**Architecture:** Five detectors (`value_combo`, `value_novelty` + its batched attribute pass, `numeric_range`, `charset`, `entropy`) currently put a small `LIMIT` into their aggregation SQL and report the page size as the total. Each gains a `count() OVER ()` (or `OVER (PARTITION BY key)` / `sum(hits) OVER ()`) column in the same statement, moves allowlist and normal-event suppression into `HAVING` so the count is post-suppression, and orders by an expression monotone in the score it reports with a per-field budget of `limit`. Hydration is chunked. The frontend renders "showing N of M", steps the per-method limit 50→80, and marks inexact totals with "+".

**Tech Stack:** Python 3.13 / ClickHouse SQL (window functions, array params), React 19 + TanStack Query + Zustand, pytest with the `FakeClient` harness in `tests/test_anomaly_stats.py` and live-ClickHouse tests (`pytestmark = pytest.mark.clickhouse`), vitest.

**Spec:** `docs/ANOMALY_DETECTION.md` §"Totals and truncation: what `total_findings` promises".

## Global Constraints

- `total_findings` = exact count across the full analysed scope, after allowlist and normal-event suppression, before the display cap. Otherwise `total_findings_exact = False` plus a warning naming why.
- `results` = true top-`limit` by score; per-field/per-group SQL page budgets are at least `limit`.
- Candidate caps (proportion shift, interval periodicity, drift, sequence novelty, sequence motif) stay as they are — they already warn.
- Suppression sets larger than `_SQL_SUPPRESSION_MAX = 1000` fall back to the Python filter and mark the total inexact.
- Hydration in chunks of `_HYDRATE_CHUNK = 500`.
- UI display cap: 50 by default, 80 after "show more" on the Investigate rail (`FINDINGS_LIMIT_STEPS = [50, 80]`); API stays `le=500`.
- Every whole-corpus SQL keeps `{heavy_scan_settings()}`.
- Ruff: `line-length = 100`, Google docstrings. `uv run ruff format --check .` must pass. No full `pytest` runs — targeted files only (`tests/test_anomaly_stats.py`, `tests/test_novelty_batched_clickhouse.py`, `tests/test_charset_group_field_clickhouse.py`, `tests/test_analysis_findings_api.py`, `tests/test_demo_detector_coverage_clickhouse.py`).
- Fake-client tests feed rows by position; every SQL that gains a trailing `n_total` column needs its fixtures extended with that column.
- Docs updated in the same commit as the detector change (`docs/ANOMALY_DETECTION.md`), `docs/PROGRESS.md` entry at the end.

---

### Task 1: Result contract, suppression helper, chunked hydration

**Files:**
- Modify: `src/vestigo/db/anomaly_stats.py:1004-1045` (`StatAnomalyResult`), `:1171-1186` (`_apply_allowlist`), `:1623-1643` (`_hydrate_finding_events`)
- Test: `tests/test_anomaly_stats.py`

**Interfaces:**
- Produces:
  - `StatAnomalyResult.total_findings_exact: bool = True`
  - `_SQL_SUPPRESSION_MAX = 1000`, `_HYDRATE_CHUNK = 500` (module constants)
  - `_sql_suppression(params, *, key_expr: str | None, evt_expr: str, allow_keys: list[str], exclude_event_ids: set[str] | None) -> tuple[str, list[str]]` — returns `(having_fragment, inexact_reasons)`. `having_fragment` is `""` or ` AND NOT has({allow:Array(String)}, <key_expr>) AND NOT has({excl:Array(String)}, <evt_expr>)` (each half only when its set is non-empty and ≤ `_SQL_SUPPRESSION_MAX`); it binds `params["allow"]` / `params["excl"]`. `inexact_reasons` names each set that was too large to bind (`"allowlist (1,203 entries) exceeds the 1,000-entry SQL bound; suppression applied to the page only"`), which callers append to `warnings` and use to set `total_findings_exact=False`.
  - `_sql_allow_keys(allowlist, field_token) -> list[str]` — the allowlist values for one `allowlist_field` (`COMBO_FIELD_SEP`-joined for combos).
  - `_hydrate_finding_events` now fetches in `_HYDRATE_CHUNK` batches.

- [ ] **Step 1: Failing tests**

```python
def test_result_total_is_exact_by_default():
    r = StatAnomalyResult(
        status="ok", detector="value_combo", method="self-baseline", baseline_size=0
    )
    assert r.total_findings_exact is True


def test_sql_suppression_binds_both_sets_under_the_bound():
    params: dict = {}
    frag, reasons = _sql_suppression(
        params, key_expr="val", evt_expr="evt_id", allow_keys=["a", "b"], exclude_event_ids={"e1"}
    )
    assert "NOT has({allow:Array(String)}, val)" in frag
    assert "NOT has({excl:Array(String)}, evt_id)" in frag
    assert params["allow"] == ["a", "b"] and params["excl"] == ["e1"]
    assert reasons == []


def test_sql_suppression_falls_back_when_a_set_is_too_large():
    params: dict = {}
    big = {f"e{i}" for i in range(_SQL_SUPPRESSION_MAX + 1)}
    frag, reasons = _sql_suppression(
        params, key_expr="val", evt_expr="evt_id", allow_keys=[], exclude_event_ids=big
    )
    assert frag == ""
    assert "excl" not in params
    assert len(reasons) == 1 and "page only" in reasons[0]


def test_hydration_is_chunked():
    svc = _svc([])
    svc.ch.events_by_id = {f"e{i}": {"event_id": f"e{i}"} for i in range(1200)}
    findings = [
        ValueFinding(
            field="f",
            value=str(i),
            count=1,
            score=1.0,
            first_seen=None,
            event_id=f"e{i}",
            event=None,
            details={},
        )
        for i in range(1200)
    ]
    svc._hydrate_finding_events("c", ["s"], findings)
    assert [len(c) for c in svc.ch.hydration_calls] == [500, 500, 200]
    assert findings[1199].event == {"event_id": "e1199"}
```

(Check `ValueFinding`'s actual constructor at `anomaly_stats.py:~880` before writing the test.)

- [ ] **Step 2: Run** `uv run pytest tests/test_anomaly_stats.py -k "total_is_exact or sql_suppression or hydration_is_chunked" -v` → FAIL (names undefined / hydration one call).
- [ ] **Step 3: Implement** the field, the constants, `_sql_suppression`, `_sql_allow_keys`, and chunk the loop in `_hydrate_finding_events` (`for i in range(0, len(ids), _HYDRATE_CHUNK): by_id.update(self.ch.get_events_by_ids(case_id, source_ids, ids[i:i+_HYDRATE_CHUNK]))`).
- [ ] **Step 4: Run** the same → PASS. Also `uv run pytest tests/test_anomaly_stats.py -q` to confirm nothing else moved.
- [ ] **Step 5: Commit** `feat(analysis): result contract for exact totals, chunked hydration`.

---

### Task 2: `value_combo` — exact count, SQL suppression, score-true ordering

**Files:**
- Modify: `src/vestigo/db/anomaly_stats.py:2414-2647` (`find_value_combos`)
- Test: `tests/test_anomaly_stats.py:1812-1930`, new `tests/test_value_combo_totals_clickhouse.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: SQL rows gain a trailing `n_total` column (UInt64). Self-baseline: `count() OVER () AS n_total`. Temporal: `sum(hits) OVER () AS n_total` where `hits = (w0_cnt > 0) + (w1_cnt > 0) + …`; ordering `ORDER BY best ASC` where `best = arrayMin(arrayFilter(x -> x > 0, [if(w0_cnt > 0, w0_cnt / {w0_total:Float64}, 0), …]))` (per-window totals bound as `params[f"w{i}_total"] = float(max(total, 1))`), tie-break `v0, v1`.

- [ ] **Step 1: Failing fake tests** — extend every existing combo fixture row with `n_total` (e.g. `("login_ok", "03:00", 1, fs, "evt-a", 2)`) and add:

```python
def test_value_combo_total_is_the_sql_count_not_the_page():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    rows = [(f"a{i}", "b", 1, fs, f"e{i}", 4000) for i in range(50)]
    svc = _svc(
        [
            FakeQueryResult([(1000,)], ["count()"]),
            FakeQueryResult(rows, ["v0", "v1", "cnt", "first_seen", "evt_id", "n_total"]),
        ]
    )
    r = svc.find_value_combos("c1", ["s1"], fields=["attr:a", "attr:b"], limit=50)
    assert len(r.results) == 50 and r.total_findings == 4000 and r.total_findings_exact


def test_value_combo_suppression_is_bound_into_the_sql():
    svc = _svc([FakeQueryResult([(10,)], ["count()"]), FakeQueryResult([], [])])
    svc.ch.client = RecordingClient(svc.ch.client._responses)
    svc.find_value_combos(
        "c1",
        ["s1"],
        fields=["attr:a", "attr:b"],
        allowlist={("attr:a\x1eattr:b", "x\x1fy")},
        exclude_event_ids={"e9"},
    )
    sql = svc.ch.client.full_queries[-1]
    p = svc.ch.client._all_parameters[-1]
    assert "NOT has({allow:Array(String)}" in sql and p["allow"] == ["x\x1fy"]
    assert "NOT has({excl:Array(String)}, evt_id)" in sql and p["excl"] == ["e9"]


def test_value_combo_temporal_orders_by_best_window_score():
    svc = _svc(
        [
            FakeQueryResult([(500,)], ["count()"]),
            FakeQueryResult([(300, 80, 20)], ["bl", "w0", "w1"]),
            FakeQueryResult([], []),
        ]
    )
    svc.ch.client = RecordingClient(svc.ch.client._responses)
    windows = _two_suspects(...)  # build like _one_suspect with two suspect windows
    svc.find_value_combos("c1", ["s1"], fields=["attr:a", "attr:b"], windows=windows)
    sql = svc.ch.client.full_queries[-1]
    assert "ORDER BY best ASC" in sql and "sum(hits) OVER () AS n_total" in sql
    assert svc.ch.client._all_parameters[-1]["w1_total"] == 20.0
```

Note the combo allowlist key: check `COMBO_FIELD_SEP`/`COMBO_VALUE_SEP` values at `:376` and use them literally in the test.

- [ ] **Step 2: Run** `uv run pytest tests/test_anomaly_stats.py -k value_combo -v` → FAIL.
- [ ] **Step 3: Implement.** In self-baseline SQL: add `count() OVER () AS n_total` to the SELECT, `_sql_suppression` fragment after `HAVING cnt <= {floor}` (key_expr = `concat(v0, '<COMBO_VALUE_SEP>', v1, …)` built from `val_cols` aliases; evt_expr = `evt_id` — HAVING may reference select aliases in ClickHouse). Temporal: add `hits`, `best`, `sum(hits) OVER () AS n_total`; ORDER BY `best ASC, v0, v1`. Read `n_total = int(rows[0][-1]) if rows else 0`, slice finding columns as before (`row[:n_fields]`, then the same offsets — `n_total` is last, so existing offsets stay valid). Drop the Python `_apply_allowlist`/exclude pass when the SQL bound them; keep it (on the page) only for the fallback case, and then `total_findings_exact = False`, warnings extended with the reasons. `total_findings = n_total`. Keep `all_findings.sort(...)[:limit]` (ordering within one page is unchanged; SQL already returned only `limit` groups).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Live-ClickHouse test** `tests/test_value_combo_totals_clickhouse.py` modelled on `tests/test_novelty_batched_clickhouse.py`: ingest 120 events with unique `(user, host)` pairs plus 10 common ones; assert `find_value_combos(..., fields=["attr:user","attr:host"], limit=50, rarity_floor=1)` returns 50 results, `total_findings == 120`, `total_findings_exact`; with `allowlist` of 3 combo keys → `total_findings == 117`; with `exclude_event_ids` of 2 representative ids → 118. Run: `uv run pytest tests/test_value_combo_totals_clickhouse.py -v` → PASS.
- [ ] **Step 6: Docs** — in `docs/ANOMALY_DETECTION.md` §"Value combinations", one sentence: temporal ordering is by the best per-window score, count is exact (link to the contract subsection).
- [ ] **Step 7: Commit** `fix(analysis): value_combo reports the exact total and the true top-N`.

---

### Task 3: `value_novelty` — per-field budget = `limit`, exact count per field, temporal ordering

**Files:**
- Modify: `src/vestigo/db/anomaly_stats.py:1975-2295` (`find_value_novelty`, `_batched_attr_novelty_rows`), `:2296-2370` (`_novelty_rows_to_findings`)
- Test: `tests/test_anomaly_stats.py` (novelty fixtures), `tests/test_novelty_batched_clickhouse.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `per_field_limit` parameter retained for API compatibility but the effective per-field budget is `max(per_field_limit, limit)`. Per-field SQL rows gain trailing `n_total` = `count() OVER ()` (self) / `sum(hits) OVER ()` (temporal). Batched SQL rows gain trailing `n_total` = `count() OVER (PARTITION BY key)` / `sum(hits) OVER (PARTITION BY key)`. `_batched_attr_novelty_rows` returns `dict[key, (rows, n_total)]`. `_novelty_rows_to_findings` signature unchanged (rows still positional; it ignores the trailing column via explicit slicing `row[:4]` / `row[:2 + 3 * n_windows]`). `find_value_novelty` sums per-field `n_total` into `total_findings` and applies `_sql_suppression` per field (key_expr = `val`, allow keys from `_sql_allow_keys(allowlist, field_token)`; for the batched pass the allowlist is bound as parallel arrays `allow_keys:Array(String)`, `allow_vals:Array(Array(String))` and the predicate is `NOT has(allow_vals[indexOf(allow_keys, key)], val)` guarded by `indexOf > 0` — same `greatest(idx, 1)` trick as the charset grouped SQL).
- Temporal ordering: `ORDER BY best ASC, val` with `best` as in Task 2.

- [ ] **Step 1: Failing tests** — extend each novelty fixture row with `n_total`; add `test_value_novelty_total_sums_exact_per_field_counts` (two fields, rows say 300 and 250 → `total_findings == 550`, page ≤ limit) and `test_value_novelty_batched_binds_per_key_allowlist` (RecordingClient; asserts the `allow_keys`/`allow_vals` params and the `PARTITION BY key` window). Update `test_value_novelty_self_baseline_limit_applied` to assert `params["lim"] == limit` when `limit > per_field_limit`.
- [ ] **Step 2: Run** `uv run pytest tests/test_anomaly_stats.py -k value_novelty -v` → FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run** → PASS. Then `uv run pytest tests/test_novelty_batched_clickhouse.py -v` — the oracle there is the old per-field SQL; update the oracle's expected totals (it compares findings, which are unchanged, and add an assertion that `total_findings` equals the oracle's unlimited count).
- [ ] **Step 5: Docs** — §"Which fields get scanned": replace the "25 per field" sentence with the budget rule.
- [ ] **Step 6: Commit** `fix(analysis): value_novelty counts every field exactly and pages the true top-N`.

---

### Task 4: `numeric_range` and `entropy` — budget = `limit`, exact count, SQL suppression

**Files:**
- Modify: `src/vestigo/db/anomaly_stats.py:2768-3012` (`find_range_violations`), `:3880-4130` (`find_entropy_outliers`), `:1645-1697` (`_finalize_findings`)
- Test: `tests/test_anomaly_stats.py` (search `find_range_violations` / `find_entropy_outliers` fixtures)

**Interfaces:**
- Consumes: Task 1.
- Produces: `_finalize_findings(..., total_findings: int | None = None, total_findings_exact: bool = True)` — when `total_findings` is given it is reported as-is; when `None` it falls back to `len(findings)` (still correct for the candidate-capped methods, whose findings list is the whole scored pool). Both detectors bind `plim = max(per_field_limit, limit)`, add `count() OVER () AS n_total` in the outer SELECT (after the band filter), apply `_sql_suppression` (key_expr = `val`, evt_expr = `evt_id`) as an extra `WHERE … AND` on the outer query, sum `n_total` per field, and stop calling `_apply_allowlist`/exclude in Python unless a fallback reason exists.

- [ ] **Step 1: Failing tests** — extend range/entropy fixture rows with trailing `n_total`; add one test per detector asserting `total_findings` is the summed `n_total` and `params["plim"] == limit` for `limit=80`.
- [ ] **Step 2: Run** `uv run pytest tests/test_anomaly_stats.py -k "range or entropy" -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Docs** — §4 and §6 "Caveats": one line each on exact totals.
- [ ] **Step 6: Commit** `fix(analysis): numeric_range and entropy report exact totals`.

---

### Task 5: `charset` — score in SQL, ordering by score, exact count

**Files:**
- Modify: `src/vestigo/db/anomaly_stats.py:3083-3878` (`find_charset_novelty`)
- Test: `tests/test_anomaly_stats.py` (charset fixtures), `tests/test_charset_group_field_clickhouse.py`

**Interfaces:**
- Consumes: Task 1, Task 4's `_finalize_findings` signature.
- Produces: the SQL computes `score` = `arraySum(c -> if(indexOf(ref_chars, c) > 0, -log(ref_counts[indexOf(ref_chars, c)] / n_vals), log(n_vals + 1)), novel)` with `ref_chars:Array(String)`, `ref_counts:Array(Float64)`, `n_vals:Float64` bound from the `_CharsetLearn` (`char_counts`, `n_vals`); grouped mode binds parallel `Array(Array(...))` per group plus the fallback's, selected by `gidx` exactly as `sets`/`fb` are today. `ORDER BY score DESC, cnt ASC`, `LIMIT {plim} [BY grp]` with `plim = max(per_field_limit, limit)`, `count() OVER () AS n_total` (ungrouped) / `count() OVER () AS n_total` under the grouped `LIMIT … BY grp` (a global count is what the analyst sees). Python keeps computing the score from the same inputs and uses its own value (the SQL `score` column is read only in tests, to assert the two agree to 1e-6). When a learn has no `char_counts` (temporal per-group reference), every novel char is unseen and the expression reduces to `length(novel) * log(n_vals + 1)`, which is the current Python behaviour — bind empty arrays.

- [ ] **Step 1: Failing tests** — extend charset fixtures with `score` and `n_total` columns (row shape: `val, [grp,] novel, cnt, first_seen, evt_id, [win_idx,] score, n_total`); add `test_charset_orders_by_score_in_sql` (RecordingClient: `ORDER BY score DESC` present, `ref_chars`/`ref_counts` bound) and `test_charset_total_is_the_sql_count`.
- [ ] **Step 2: Run** `uv run pytest tests/test_anomaly_stats.py -k charset -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; then `uv run pytest tests/test_charset_group_field_clickhouse.py -v` (add an assertion there that the SQL `score` equals the Python score for every returned row).
- [ ] **Step 5: Docs** — §5 "The score": note the ordering is now by that score in SQL, so the page is the true top-N.
- [ ] **Step 6: Commit** `fix(analysis): charset pages by its own score and reports the exact total`.

---

### Task 6: API passes `total_findings_exact` through; log templates set it

**Files:**
- Modify: `src/vestigo/api/routers/analysis.py:503-515` (`_run_log_templates` payload), the `/anomalies` payload builder in `api/routers/events.py` if it reshapes results (grep `total_findings`).
- Test: `tests/test_analysis_findings_api.py`

- [ ] **Step 1: Failing test**

```python
def test_findings_carry_total_findings_exact(client, seeded, stub_detector):
    body = client.get(
        f"/api/cases/{seeded.case_id}/timelines/{seeded.timeline_id}/analysis/findings",
        params={"method": "value_novelty"},
    ).json()
    assert body["total_findings_exact"] is True
```

and extend `test_log_templates_report_the_total_before_the_limit` with `assert body["total_findings_exact"] is True`.

- [ ] **Step 2: Run** `uv run pytest tests/test_analysis_findings_api.py -k "exact or before_the_limit" -v` → FAIL for templates (the stat detector path uses `asdict`, so it may already pass).
- [ ] **Step 3: Implement** — `"total_findings_exact": True` in the templates payload.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(api): findings responses say whether the total is exact`.

---

### Task 7: Frontend — per-method limit, "showing N of M", inexact marker, dead code

**Files:**
- Modify: `frontend/src/api/analysis.ts:60-70` (`MethodFindings.total_findings_exact: boolean`), `frontend/src/stores/ui.ts` (`findingsLimitByMethod: Partial<Record<MethodId, number>>`, `raiseFindingsLimit(method)`), `frontend/src/hooks/useMethodFindings.ts` (replace `METHOD_LIMIT` with the store value per method, default 50; `MethodState` gains `totalExact: boolean`, `limit: number`, `canRaise: boolean`, `raise: () => void`), new `frontend/src/stores/findingsLimit.ts` (`FINDINGS_LIMIT_STEPS = [50, 80]`, session-only store; `detector-hooks.ts` untouched — see correction above), `frontend/src/components/analysis/FindingGroup.tsx` (per-method disclosure row), `frontend/src/components/analysis/DetectorStrip.tsx:55` (badge), `frontend/src/components/analysis/InvestigateSheetHost.tsx:112` (sheet uses the same store limit).
- Test: `frontend/src/test/investigateRail.test.tsx`, `frontend/src/test/toolsSheet.test.tsx` (strip), new cases.

**Interfaces:**
- Consumes: Task 6's `total_findings_exact`.
- Produces: `useFindingsLimit(method)` reading/writing the store; `FindingGroup` renders, per method whose `findings.length < total`, a dashed row `data-testid="truncation-{method}"` with text `showing 50 of 1,204 <label> findings — show more` (button, present only while `canRaise`) or `showing 80 of 1,204 <label> findings` (no button at the ceiling); the group count in the header uses the exact totals (`Σ total`), not row counts; `DetectorStrip` renders `total.toLocaleString()` plus `+` and a `title` of the first warning when `totalExact` is false.

- [ ] **Step 1: Failing tests** — in `investigateRail.test.tsx` the `sweep.current` fixture: give `value_combo` 50 findings and `total: 4000`; assert the truncation row text `showing 50 of 4,000` and that clicking "show more" calls `raise`; a second case with `totalExact: false` asserts the strip badge reads `4,000+`. In `toolsSheet.test.tsx` (strip) assert the badge uses `total`, unchanged.
- [ ] **Step 2: Run** `cd frontend && npx vitest run src/test/investigateRail.test.tsx` → FAIL.
- [ ] **Step 3: Implement.** Store: `findingsLimitByMethod: {}`, `raiseFindingsLimit: (m) => set(s => ({ findingsLimitByMethod: { ...s.findingsLimitByMethod, [m]: FINDINGS_LIMIT_STEPS.find(x => x > (s.findingsLimitByMethod[m] ?? 50)) ?? 80 } }))` — session state, not persisted (same reasoning as `showWeak`). `findingsQueryOptions` takes `limit` as a parameter instead of the constant; both `useMethodFindings` and `useStreamingSweep` read it from the store. Delete the dead hooks and their imports.
- [ ] **Step 4: Run** `npm run typecheck && npm run lint && npx vitest run` → PASS.
- [ ] **Step 5: Commit** `feat(frontend): the rail says what it is not showing`.

---

### Task 8: Docs, roadmap, progress, demo coverage

**Files:**
- Modify: `docs/ANOMALY_DETECTION.md` (fix "until 1.20" → "before this contract landed"), `docs/ROADMAP.md` (delete any item this closes; add nothing), `docs/PROGRESS.md` (new top entry: what changed and why, the five methods named, the two that were already right), `CHANGELOG.md` only if the release ritual asks for it.

- [ ] **Step 1:** `uv run pytest tests/test_demo_detector_coverage_clickhouse.py -v` → PASS (every method still finds something in the demo).
- [ ] **Step 2:** `uv run ruff check . && uv run ruff format --check .` → clean.
- [ ] **Step 3:** Write the `PROGRESS.md` entry; grep `ROADMAP.md` for `total_findings|truncat|load more` and delete what this closes.
- [ ] **Step 4: Commit** `docs: honest totals — progress and roadmap`.
