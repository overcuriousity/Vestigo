# D11 — Bigram Entropy Variant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a character-bigram statistic as a second *method* on Vestigo's existing `entropy` detector, so a value made of ordinary characters in an unusual order (a lowercase DGA domain among English hostnames) is flagged — the case the shipped Shannon-entropy statistic structurally cannot see.

**Architecture:** `find_entropy_outliers` gains an `entropy_method` argument. In `bigram` mode it learns a character-pair transition table from the baseline population in ClickHouse, materializes it into Python, and passes it back into the detect query as two parallel arrays cast to a `Map(String, Float64)`; values whose mean pair probability falls below a threshold are flagged. Everything else — field selection, window handling, allowlists, dispositions, the findings envelope — is reused unchanged.

**Tech Stack:** Python 3.13, ClickHouse (via `clickhouse-connect`), FastAPI, pydantic-settings, React 19 + TypeScript, pytest, vitest.

**Design spec:** `docs/superpowers/specs/2026-08-06-d11-entropy-bigram-design.md`. Read it before Task 1.

## Global Constraints

- Ruff config: `select = ["E", "F", "I", "UP", "B", "C4", "SIM"]`, `line-length = 100`, `E501` ignored — **do not wrap lines for length alone**. Google-style docstrings.
- **Default behavior must not change.** Any existing call with no `entropy_method` produces byte-identical Shannon results. `tests/test_demo_detector_coverage_clickhouse.py` proves this and must stay green.
- **Explainability bar:** a finding an analyst cannot read the reasoning of does not count as shipped. Every bigram finding carries its mean pair probability, the threshold it was compared against, and the specific rarest character pairs that sank it.
- Casefolding is **ASCII-only lowercase**, matching ClickHouse's `lower()` (not `lowerUTF8`). Python-side mirrors must use the same rule — there is a helper for it in Task 1, always use it.
- Bigrams are over **bytes**, not codepoints (ClickHouse `ngrams` is byte-based). This is a deliberate, documented decision.
- The bigram probability threshold default is **0.05** (AMiner's `prob_thresh`).
- The transition-table cap is **20 000** pairs, a module constant, not a user-facing setting.
- Every new `Settings` field needs a matching `SettingSpec` in `core/settings_registry.py` or a coverage test fails.
- Every detector change updates `docs/ANOMALY_DETECTION.md` **in the same commit** (Task 7 is the doc task; the reference doc is the contract).
- Live-ClickHouse tests carry `pytestmark = pytest.mark.clickhouse` and skip when the server is unreachable. Start it with `podman compose up -d` from the repo root.
- Run `uv run ruff check .` and `uv run ruff format .` before every backend commit; `npm run lint && npm run typecheck` before every frontend commit (from `frontend/`).

---

### Task 1: Pure helpers, constants and the threshold setting

The three pieces the detector needs that carry no SQL: the ASCII-lower byte-bigram helper (Python's mirror of what ClickHouse computes), the probability-table builder, the score formula — plus the configurable threshold.

**Files:**
- Modify: `src/vestigo/core/config.py` (near `stat_charset_rarity_floor`, ~line 91)
- Modify: `src/vestigo/core/settings_registry.py` (detectors group, after the `stat_charset_rarity_floor` spec, ~line 374)
- Modify: `src/vestigo/db/anomaly_stats.py` (module constants near the other `_MIN_ENTROPY_*` constants; helpers as module-level functions above `StatisticalAnomalyService`)
- Test: `tests/test_anomaly_stats.py` (append near the existing entropy tests, ~line 2160)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `_ENTROPY_BIGRAM_MAX_PAIRS: int = 20_000`
  - `_ascii_lower_bigrams(value: str) -> list[str]` — distinct-preserving, ordered, byte-level pairs of the ASCII-lowercased UTF-8 encoding, each decoded with `errors="replace"`.
  - `_bigram_probability_table(rows: Sequence[tuple[str, int, int]]) -> dict[str, float]` — rows are `(pair, cnt, head_cnt)`.
  - `_bigram_score(mean_prob: float, prob_thresh: float) -> float`
  - `Settings.stat_entropy_bigram_prob_thresh: float = 0.05`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_anomaly_stats.py`:

```python
# ---------------------------------------------------------------------------
# D11 bigram helpers
# ---------------------------------------------------------------------------


def test_ascii_lower_bigrams_are_byte_pairs_of_the_ascii_lowercased_value():
    from vestigo.db.anomaly_stats import _ascii_lower_bigrams

    assert _ascii_lower_bigrams("Host") == ["ho", "os", "st"]
    # ASCII-only folding, exactly like ClickHouse `lower()`: the Cyrillic
    # capital is left alone, so its bytes pair as-is.
    assert _ascii_lower_bigrams("AБ") == ["a\xd0", "\xd0\x91"]
    assert _ascii_lower_bigrams("a") == []
    assert _ascii_lower_bigrams("") == []


def test_bigram_probability_table_divides_by_the_head_count():
    from vestigo.db.anomaly_stats import _bigram_probability_table

    table = _bigram_probability_table([("ab", 3, 4), ("ac", 1, 4), ("ba", 5, 5)])
    assert table["ab"] == pytest.approx(0.75)
    assert table["ac"] == pytest.approx(0.25)
    assert table["ba"] == pytest.approx(1.0)


def test_bigram_probability_table_skips_a_zero_head_count():
    """A zero denominator cannot happen in a well-formed scan, but a division
    error in a detector is a worse outcome than a missing pair (which scores as
    unseen, the honest reading)."""
    from vestigo.db.anomaly_stats import _bigram_probability_table

    assert _bigram_probability_table([("ab", 0, 0)]) == {}


def test_bigram_score_is_normalized_distance_below_the_threshold():
    from vestigo.db.anomaly_stats import _bigram_score

    assert _bigram_score(0.0, 0.05) == 1.0
    assert _bigram_score(0.025, 0.05) == 0.5
    # At or above the threshold nothing is flagged, but the formula must not go
    # negative if it is ever called there.
    assert _bigram_score(0.05, 0.05) == 0.0
    assert _bigram_score(0.9, 0.05) == 0.0
    assert _bigram_score(0.01, 0.0) == 0.0


def test_entropy_bigram_prob_thresh_default():
    from vestigo.core.config import Settings

    assert Settings().stat_entropy_bigram_prob_thresh == 0.05
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_anomaly_stats.py -k "bigram" -v --no-cov`
Expected: FAIL — `ImportError: cannot import name '_ascii_lower_bigrams'` (and the `Settings` assertion fails on a missing attribute).

- [ ] **Step 3: Add the setting**

In `src/vestigo/core/config.py`, directly after the `stat_charset_rarity_floor` field:

```python
    # D11: mean character-pair probability below which the bigram entropy
    # method flags a value. AMiner's `EntropyDetector` default; lower = fewer,
    # more extreme findings.
    stat_entropy_bigram_prob_thresh: float = 0.05
```

In `src/vestigo/core/settings_registry.py`, after the `stat_charset_rarity_floor` spec:

```python
    SettingSpec(
        "stat_entropy_bigram_prob_thresh",
        "detectors",
        "Bigram entropy probability threshold",
        "Mean character-pair probability below which the entropy detector's bigram method flags a value.",
    ),
```

- [ ] **Step 4: Add the constants and helpers**

In `src/vestigo/db/anomaly_stats.py`, beside the existing `_MIN_ENTROPY_VALUE_LEN` / `_MIN_ENTROPY_BASELINE` constants:

```python
# D11: ceiling on the learned character-pair transition table. A memory bound
# on the query parameter, not an analyst-facing knob — the table is ordered by
# frequency before truncation, so a hit drops the *rarest* pairs, whose
# probability is nearest the zero a missing key already yields.
_ENTROPY_BIGRAM_MAX_PAIRS = 20_000
```

Module-level, above the service class (near the other module-level helpers):

```python
def _ascii_lower_bigrams(value: str) -> list[str]:
    """Return *value*'s consecutive byte pairs, ASCII-lowercased.

    The Python mirror of ClickHouse's ``ngrams(lower(val), 2)``: ``lower()``
    folds ASCII only (not ``lowerUTF8``), and ``ngrams`` operates on bytes, so
    a multi-byte character contributes byte pairs. Pairs are decoded with
    ``errors="replace"`` purely so they can be displayed on a finding — they
    are never compared against the table by their decoded form.
    """
    raw = value.encode("utf-8", errors="replace")
    folded = bytes(b + 32 if 65 <= b <= 90 else b for b in raw)
    return [folded[i : i + 2].decode("utf-8", errors="replace") for i in range(len(folded) - 1)]


def _bigram_probability_table(rows: Sequence[tuple[str, int, int]]) -> dict[str, float]:
    """Turn ``(pair, count, head_count)`` rows into ``P(c₂|c₁)``.

    *head_count* is the total count of every pair sharing this pair's first
    byte, computed by a window function in the learn query so it stays exact
    even when the returned rows are capped.
    """
    table: dict[str, float] = {}
    for pair, cnt, head_cnt in rows:
        head = int(head_cnt)
        if head <= 0:
            continue
        table[str(pair)] = int(cnt) / head
    return table


def _bigram_score(mean_prob: float, prob_thresh: float) -> float:
    """Normalized distance below the threshold: 0 at the threshold, 1 at zero probability."""
    if prob_thresh <= 0:
        return 0.0
    return round(max(0.0, (prob_thresh - mean_prob) / prob_thresh), 4)
```

If `Sequence` is not already imported in the module, add it to the existing `collections.abc` import.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_anomaly_stats.py -k "bigram" -v --no-cov`
Expected: PASS (5 tests)

- [ ] **Step 6: Run the settings coverage test**

Run: `uv run pytest tests/ -k "settings_registry or setting_spec" -v --no-cov`
Expected: PASS — this is the test that fails when a `Settings` field has no `SettingSpec`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff format src/vestigo/db/anomaly_stats.py src/vestigo/core/config.py src/vestigo/core/settings_registry.py tests/test_anomaly_stats.py
uv run ruff check .
git add src/vestigo/db/anomaly_stats.py src/vestigo/core/config.py src/vestigo/core/settings_registry.py tests/test_anomaly_stats.py
git commit -m "feat(detectors): D11 bigram helpers, constant and threshold setting"
```

---

### Task 2: The bigram branch in `find_entropy_outliers`

**Files:**
- Modify: `src/vestigo/db/anomaly_stats.py` — `EntropyFinding` (~line 787), `find_entropy_outliers` (~line 3734), the module docstring's `**entropy**` entry (~line 81)
- Test: `tests/test_anomaly_stats.py` (mock-based, asserts SQL text and finding shape)

**Interfaces:**
- Consumes: `_ENTROPY_BIGRAM_MAX_PAIRS`, `_ascii_lower_bigrams`, `_bigram_probability_table`, `_bigram_score` from Task 1.
- Produces:
  - `find_entropy_outliers(..., entropy_method: str = "shannon", prob_thresh: float | None = None)` — raises `ValueError` on an unknown `entropy_method`.
  - `EntropyFinding` with `mode: str` (`"shannon"` | `"bigram"`), the Shannon fields (`entropy`, `direction`, `lower`, `upper`) optional and `None` in bigram mode, and new optional `mean_prob: float | None`, `prob_thresh: float | None`, `rare_pairs: list[dict[str, Any]] | None`.
  - Result `method` values: `"bigram"` (self-baseline) and `"temporal-bigram"` (windows given).

- [ ] **Step 1: Restructure `EntropyFinding`**

A dataclass cannot have a non-default field after a defaulted one, so the required fields move ahead of the optional ones. Both construction sites use keyword arguments, so nothing else changes. Replace the dataclass with:

```python
@dataclass
class EntropyFinding:
    """One flagged value from the entropy detector.

    Two methods share this shape. ``mode="shannon"`` carries the value's own
    Shannon character entropy against a Tukey band (``entropy``/``lower``/
    ``upper``/``direction``); ``mode="bigram"`` (D11) carries the mean
    probability of its character pairs against a threshold (``mean_prob``/
    ``prob_thresh``/``rare_pairs``). The other method's fields are ``None`` —
    bits and probabilities are different quantities and are never conflated
    into one field.
    """

    field: str
    value: str
    count: int
    # 0–1 severity; per-mode formula, see the fields below.
    score: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]
    mode: str = "shannon"
    # --- shannon only ---
    # Shannon character entropy of the value, in bits.
    entropy: float | None = None
    direction: str | None = None  # "below" | "above"
    lower: float | None = None
    upper: float | None = None
    # --- bigram only (D11) ---
    # Mean learned probability of the value's character pairs, 0–1.
    mean_prob: float | None = None
    prob_thresh: float | None = None
    # The five lowest-probability pairs in the value: [{"pair": "qx", "prob": 0.0}, ...]
    rare_pairs: list[dict[str, Any]] | None = None
```

Then update the existing Shannon construction site (in `find_entropy_outliers`) to pass `mode="shannon"`, and add `"mode": "shannon"` to the Shannon `details` dict.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_anomaly_stats.py`. Follow the file's existing mock pattern for entropy tests (a fake client whose `query` returns queued `result_rows`) — read `test_entropy_no_data` and its neighbours first and match their fixture style exactly rather than inventing one.

```python
def test_entropy_rejects_an_unknown_method():
    svc = _svc_with_rows([])  # existing helper in this file's entropy tests
    with pytest.raises(ValueError):
        svc.find_entropy_outliers("c1", ["s1"], fields=["artifact"], entropy_method="trigram")


def test_entropy_bigram_learns_a_pair_table_then_scores_values():
    """One learn query per field, then one detect query carrying the table."""
    svc = _svc_with_rows(
        [
            [(120,)],  # total_events
            [(40,)],  # distinct qualifying baseline values
            [("ho", 30, 40), ("os", 25, 40), ("qx", 1, 40)],  # learned pairs
            [("qxvbnrtplkj", 0.0008, 3, "2026-03-10T06:00:00Z", "evt-1")],  # flagged values
        ]
    )
    result = svc.find_entropy_outliers(
        "c1", ["s1"], fields=["attr:host"], entropy_method="bigram"
    )

    assert result.method == "bigram"
    assert len(result.results) == 1
    f = result.results[0]
    assert f.mode == "bigram"
    assert f.mean_prob == pytest.approx(0.0008)
    assert f.prob_thresh == 0.05
    assert f.entropy is None and f.lower is None and f.direction is None
    # Score = (0.05 - 0.0008) / 0.05
    assert f.score == pytest.approx(0.984, abs=1e-3)
    # The explanation: the value's own rarest pairs, with their learned probabilities.
    assert f.rare_pairs[0]["prob"] == pytest.approx(0.0)  # "vb" — never learned
    assert all(set(p) == {"pair", "prob"} for p in f.rare_pairs)
    assert f.details["allowlist_field"] == "attr:host"
    assert f.details["allowlist_value"] == "qxvbnrtplkj"

    sql = " ".join(q for q, _ in svc.ch.client.queries)
    assert "ngrams(lower(" in sql
    assert "Map(String, Float64)" in sql
    assert str(_ENTROPY_BIGRAM_MAX_PAIRS) in sql or "maxpairs" in sql


def test_entropy_bigram_temporal_method_name():
    svc = _svc_with_rows([[(120,)], [(40,)], [("ho", 30, 40)], []])
    result = svc.find_entropy_outliers(
        "c1",
        ["s1"],
        fields=["attr:host"],
        entropy_method="bigram",
        windows=_windows(),  # existing helper in this file
    )
    assert result.method == "temporal-bigram"


def test_entropy_bigram_warns_when_the_pair_table_is_capped():
    rows = [(f"p{i}", 1, 10) for i in range(_ENTROPY_BIGRAM_MAX_PAIRS)]
    svc = _svc_with_rows([[(120,)], [(40,)], rows, []])
    result = svc.find_entropy_outliers(
        "c1", ["s1"], fields=["attr:host"], entropy_method="bigram"
    )
    assert any("capped" in w for w in result.warnings)


def test_entropy_bigram_skips_a_field_under_the_baseline_floor():
    svc = _svc_with_rows([[(120,)], [(_MIN_ENTROPY_BASELINE - 1,)]])
    result = svc.find_entropy_outliers(
        "c1", ["s1"], fields=["attr:host"], entropy_method="bigram"
    )
    assert result.status == "insufficient_data"


def test_entropy_shannon_findings_still_carry_their_own_fields():
    """The default path is untouched: mode is stamped, nothing else moves."""
    svc = _svc_with_rows(
        [
            [(120,)],
            [(2.0, 4.0, 40)],  # q1, q3, n
            [("aaaaaaaa", 0.0, 5, "2026-03-10T06:00:00Z", "evt-2")],
        ]
    )
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    f = result.results[0]
    assert result.method == "iqr"
    assert f.mode == "shannon"
    assert f.entropy == 0.0 and f.direction == "below"
    assert f.mean_prob is None and f.rare_pairs is None
```

If `_svc_with_rows` / `_windows` do not exist under those names in the file, use whatever the existing entropy tests use and keep the assertions identical.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_anomaly_stats.py -k "entropy" -v --no-cov`
Expected: FAIL — `find_entropy_outliers() got an unexpected keyword argument 'entropy_method'`.

- [ ] **Step 4: Implement the branch**

In `find_entropy_outliers`, extend the signature (keyword-only additions at the end, defaults preserving today's behavior):

```python
        entropy_method: str = "shannon",
        prob_thresh: float | None = None,
```

Immediately after the docstring, before `self.ch.init_schema()`:

```python
        if entropy_method not in ("shannon", "bigram"):
            raise ValueError(f"Unknown entropy method: {entropy_method!r} (expected 'shannon' or 'bigram')")
        bigram = entropy_method == "bigram"
        if bigram:
            method = "bigram" if windows is None else "temporal-bigram"
        else:
            method = "iqr" if windows is None else "temporal-iqr"
        thresh = (
            float(prob_thresh) if prob_thresh is not None else get_settings().stat_entropy_bigram_prob_thresh
        )
        run_warnings: list[str] = []
```

(Replace the existing `method = "iqr" if windows is None else "temporal-iqr"` line. If `get_settings` is not already imported in this module, follow whatever the neighbouring detectors do to read settings — several take their thresholds as arguments from the router instead; if that is the local convention, keep `prob_thresh` required-with-default from the router and drop the `get_settings()` fallback here, adjusting Task 1's test accordingly.)

Then, inside the per-field loop, branch. The Shannon path stays exactly as it is; the bigram path is:

```python
        for field_token in scan_fields:
            if bigram:
                # --- Count the qualifying baseline population (the same floor
                # Shannon mode applies, over the same population). ---
                cnt_params: dict[str, Any] = {**base_params, "minlen": _MIN_ENTROPY_VALUE_LEN}
                col = _col_expr(field_token, cnt_params, field_mappings)
                baseline_clause = ""
                if windows is not None:
                    base_pred, _ = _window_preds(windows, cnt_params, source_offsets)
                    baseline_clause = f" AND {base_pred}"
                cnt_sql = f"""
                    SELECT count() AS n
                    FROM (
                        SELECT DISTINCT {col} AS val
                        FROM {db}.events
                        WHERE case_id = {{cid:String}}
                          AND has({{src:Array(String)}}, source_id)
                          AND {col} != ''
                          AND lengthUTF8({col}) >= {{minlen:UInt32}}{baseline_clause}
                    )
                    {HEAVY_SCAN_SETTINGS}
                """
                crows = self.ch.client.query(cnt_sql, parameters=cnt_params).result_rows
                n = int(crows[0][0]) if crows and crows[0][0] is not None else 0
                if n < _MIN_ENTROPY_BASELINE:
                    continue

                # --- Learn P(c₂|c₁) over the baseline's distinct values. The
                # window function computes each pair's head total over the FULL
                # pair set, so the denominators stay exact even when the
                # returned rows are capped. ---
                learn_params: dict[str, Any] = {**base_params, "minlen": _MIN_ENTROPY_VALUE_LEN, "maxpairs": _ENTROPY_BIGRAM_MAX_PAIRS}
                lcol = _col_expr(field_token, learn_params, field_mappings)
                learn_baseline_clause = ""
                if windows is not None:
                    lbase_pred, _ = _window_preds(windows, learn_params, source_offsets)
                    learn_baseline_clause = f" AND {lbase_pred}"
                learn_sql = f"""
                    SELECT pair, cnt, sum(cnt) OVER (PARTITION BY substring(pair, 1, 1)) AS head_cnt
                    FROM (
                        SELECT arrayJoin(ngrams(lower(val), 2)) AS pair, count() AS cnt
                        FROM (
                            SELECT DISTINCT {lcol} AS val
                            FROM {db}.events
                            WHERE case_id = {{cid:String}}
                              AND has({{src:Array(String)}}, source_id)
                              AND {lcol} != ''
                              AND lengthUTF8({lcol}) >= {{minlen:UInt32}}{learn_baseline_clause}
                        )
                        GROUP BY pair
                    )
                    ORDER BY cnt DESC
                    LIMIT {{maxpairs:UInt32}}
                    {HEAVY_SCAN_SETTINGS}
                """
                lrows = self.ch.client.query(learn_sql, parameters=learn_params).result_rows
                table = _bigram_probability_table(lrows)
                if not table:
                    continue
                if len(lrows) >= _ENTROPY_BIGRAM_MAX_PAIRS:
                    run_warnings.append(
                        f"{field_token}: the learned character-pair table hit its {_ENTROPY_BIGRAM_MAX_PAIRS}-pair ceiling — pairs beyond it score as never-seen."
                    )
                evaluated_fields += 1

                # --- Score the detect population against the table. The table
                # travels as two parallel arrays cast to a Map: a missing key
                # yields 0.0, which is the honest score for a pair the baseline
                # never contained. ---
                viol_params: dict[str, Any] = {**base_params, "minlen": _MIN_ENTROPY_VALUE_LEN}
                bind_offset_params(source_offsets, viol_params)
                vcol = _col_expr(field_token, viol_params, field_mappings)
                viol_params["pairs"] = list(table.keys())
                viol_params["probs"] = list(table.values())
                viol_params["thresh"] = thresh
                viol_params["plim"] = per_field_limit
                win_idx_sel = ""
                win_idx_group = ""
                detect_clause = ""
                if windows is not None:
                    _, viol_sps = _window_preds(windows, viol_params, source_offsets)
                    win_idx_sel = f", {_suspect_multiif(viol_sps)} AS win_idx"
                    win_idx_group = ", win_idx"
                    detect_clause = f" AND ({' OR '.join(viol_sps)}) AND {VESTIGO_NOT_SENTINEL_SQL}"
                viol_sql = f"""
                    WITH CAST(({{pairs:Array(String)}}, {{probs:Array(Float64)}}), 'Map(String, Float64)') AS tbl
                    SELECT val, mean_prob, cnt, first_seen, evt_id{win_idx_group}
                    FROM (
                        SELECT
                            val,
                            arrayAvg(arrayMap(p -> tbl[p], ngrams(lower(val), 2))) AS mean_prob,
                            cnt, first_seen, evt_id{win_idx_group}
                        FROM (
                            SELECT
                                {vcol} AS val,
                                count() AS cnt,
                                min({eff}) AS first_seen,
                                toString(argMin(event_id, {eff})) AS evt_id{win_idx_sel}
                            FROM {db}.events
                            WHERE case_id = {{cid:String}}
                              AND has({{src:Array(String)}}, source_id)
                              AND {vcol} != ''
                              AND lengthUTF8({vcol}) >= {{minlen:UInt32}}{detect_clause}
                            GROUP BY val{win_idx_group}
                        )
                    )
                    WHERE mean_prob < {{thresh:Float64}}
                    ORDER BY mean_prob ASC, first_seen ASC
                    LIMIT {{plim:UInt32}}
                    {HEAVY_SCAN_SETTINGS}
                """
                vrows = self.ch.client.query(viol_sql, parameters=viol_params).result_rows

                for vrow in vrows:
                    if windows is None:
                        val, mean_prob, cnt, first_seen, evt_id = vrow
                        window: TimeWindow | None = None
                    else:
                        val, mean_prob, cnt, first_seen, evt_id, win_idx = vrow
                        wi = int(win_idx)
                        window = windows.suspects[wi] if 0 <= wi < len(windows.suspects) else None
                    if not val or mean_prob is None:
                        continue
                    mp = float(mean_prob)
                    # The explanation, built from the table already in hand: the
                    # value's own lowest-probability pairs.
                    seen: list[str] = []
                    for pair in _ascii_lower_bigrams(str(val)):
                        if pair not in seen:
                            seen.append(pair)
                    rare = sorted(seen, key=lambda p: table.get(p, 0.0))[:5]
                    rare_pairs = [{"pair": p, "prob": round(table.get(p, 0.0), 6)} for p in rare]
                    first_seen_str = _present_ts(first_seen)
                    evt_id_str = str(evt_id) if evt_id else None
                    details = {
                        "detector": "entropy",
                        "method": method,
                        "mode": "bigram",
                        "field": field_token,
                        "value": str(val),
                        "mean_prob": round(mp, 6),
                        "prob_thresh": thresh,
                        "rare_pairs": rare_pairs,
                        "count": int(cnt),
                        "baseline_n": n,
                        "table_pairs": len(table),
                        "casefold": "ascii-lower",
                        "allowlist_field": field_token,
                        "allowlist_value": str(val),
                    }
                    if window is not None:
                        details.update(
                            {
                                "window_label": window.label,
                                "window_start": ensure_utc(window.start).isoformat(),
                                "window_end": ensure_utc(window.end).isoformat(),
                            }
                        )
                    all_findings.append(
                        EntropyFinding(
                            field=field_token,
                            value=str(val),
                            count=int(cnt),
                            score=_bigram_score(mp, thresh),
                            first_seen=first_seen_str,
                            event_id=evt_id_str,
                            event=_stub_event(evt_id_str, case_id, first_seen_str),
                            details=details,
                            mode="bigram",
                            mean_prob=round(mp, 6),
                            prob_thresh=thresh,
                            rare_pairs=rare_pairs,
                        )
                    )
                continue

            # --- Shannon path (unchanged) ---
```

Finally pass the warnings through the existing tail call: `warnings=run_warnings` on `self._finalize_findings(...)`.

- [ ] **Step 5: Update the module docstring**

The `**entropy**` entry at the top of `anomaly_stats.py` (~line 81) currently describes only the Shannon statistic. Rewrite it to name both methods in one sentence each, keeping the register of its neighbours.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_anomaly_stats.py -k "entropy" -v --no-cov`
Expected: PASS — including the pre-existing Shannon tests, unchanged.

- [ ] **Step 7: Run the full backend suite**

Run: `uv run pytest -x -q`
Expected: PASS. `tests/test_events_router.py::test_serialize_finding_entropy_shape` constructs an `EntropyFinding`; if the reordered dataclass breaks it, add `mode="shannon"` to the construction and assert `out["mode"] == "shannon"` — that is a legitimate part of this change, not a workaround.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff format src/vestigo/db/anomaly_stats.py tests/test_anomaly_stats.py
uv run ruff check .
git add src/vestigo/db/anomaly_stats.py tests/test_anomaly_stats.py tests/test_events_router.py
git commit -m "feat(detectors): D11 bigram entropy method in find_entropy_outliers"
```

---

### Task 3: Live-ClickHouse proof that bigram catches what Shannon misses

This is the task that proves D11 closed the documented gap. It must fail against Task 2's code only if that code is wrong — the SQL constructs it exercises (`ngrams`, the `CAST` to `Map`, the window function, `arrayAvg(arrayMap(...))` over a WITH alias) cannot be validated by mock tests, and a wrong one fails as a query error or, worse, as silently zero findings.

**Files:**
- Create: `tests/test_entropy_bigram_clickhouse.py`

**Interfaces:**
- Consumes: `find_entropy_outliers(..., entropy_method=, prob_thresh=)`, `EntropyFinding.mode/mean_prob/rare_pairs`, `_ENTROPY_BIGRAM_MAX_PAIRS` from Task 2.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Start ClickHouse**

Run: `podman compose up -d clickhouse`
Then verify: `curl -s 'http://localhost:8123/?query=SELECT+ngrams(%27abcd%27,2)'`
Expected: `['ab','bc','cd']`. If `ngrams` is missing on this server version, stop and report it — the whole design rests on it.

- [ ] **Step 2: Write the failing test file**

Create `tests/test_entropy_bigram_clickhouse.py`, modelled on `tests/test_charset_group_field_clickhouse.py` (read it first — copy its `_event` helper, module-scoped `svc` fixture, skip-on-unreachable pattern and cleanup):

```python
"""Live-ClickHouse test for the D11 bigram entropy method.

The mock tests in ``test_anomaly_stats.py`` assert SQL *text*. What only
execution can prove is the claim D11 exists for: a lowercase-latin generated
name among ordinary hostnames is invisible to Shannon character entropy and
visible to a learned bigram table. It also covers the constructs that fail
only at runtime — ``ngrams``, ``CAST((keys, values), 'Map(...)')`` inside a
``WITH``, and the ``sum(...) OVER (PARTITION BY ...)`` head totals.

Requires the dev compose stack (skipped when ClickHouse is unreachable).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vestigo.db.anomaly_stats import (
    AnalysisWindows,
    StatisticalAnomalyService,
    TimeWindow,
)
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-entbigram-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-entbigram"

BASELINE_START = datetime(2026, 3, 1, tzinfo=UTC)
SUSPECT_START = datetime(2026, 3, 10, tzinfo=UTC)
SUSPECT_END = datetime(2026, 3, 11, tzinfo=UTC)
SUSPECT_TS = "2026-03-10T06:00:00+00:00"

# Ordinary hostnames: English-ish syllables, so their character pairs repeat.
WORDS = [
    "mailserver", "webproxy", "fileshare", "printhost", "database",
    "backupnode", "loginportal", "reportserver", "storagearray", "meetingroom",
    "accounting", "marketing", "engineering", "operations", "helpdesk",
    "warehouse", "frontdesk", "training", "logistics", "reception",
    "salesfloor", "designlab", "研究室host", "security", "planning",
    "recruiting", "shipping", "purchasing", "inventory", "scheduling",
]

# Lowercase-latin DGA names. Every character is ordinary; the ORDER is not.
# Shannon entropy of these sits inside the band the words produce, which is
# exactly the documented gap.
DGA = ["qxvbnrtplkjhgf", "zxcvbnmqwrtplk", "vbnmqxzlkjhgfd"]


def _event(i: int, ts: str, host: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("dns.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="e" * 64,
        parser_name="test-entbigram",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"lookup {host}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:entbigram",
        attributes={"host": host},
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0
    # Baseline window: ordinary hostnames only, well over the 20-value floor.
    for n, word in enumerate(WORDS):
        events.append(_event(i, f"2026-03-02T10:{n // 60:02d}:{n % 60:02d}+00:00", f"{word}.corp"))
        i += 1
    # Suspect window: the same ordinary traffic plus the generated names.
    for n, word in enumerate(WORDS[:10]):
        events.append(_event(i, SUSPECT_TS, f"{word}.corp"))
        i += 1
    for name in DGA:
        events.append(_event(i, SUSPECT_TS, f"{name}.com"))
        i += 1
    return events


@pytest.fixture(scope="module")
def svc():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    store.insert_events(_fixture_events())
    service = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    service.ch = store
    yield service
    store.delete_source_events(CASE_ID, SOURCE_ID)


def _windows() -> AnalysisWindows:
    return AnalysisWindows(
        baseline=TimeWindow("baseline", BASELINE_START, SUSPECT_START),
        suspects=(TimeWindow("suspect", SUSPECT_START, SUSPECT_END),),
    )


def _flagged(result) -> set[str]:
    return {f.value for f in result.results}


def test_bigram_flags_a_dga_name_that_shannon_entropy_misses(svc):
    """The whole reason D11 exists, asserted both ways in one test."""
    shannon = svc.find_entropy_outliers(
        CASE_ID, [SOURCE_ID], fields=["attr:host"], windows=_windows()
    )
    bigram = svc.find_entropy_outliers(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:host"],
        windows=_windows(),
        entropy_method="bigram",
    )

    assert bigram.method == "temporal-bigram"
    flagged = _flagged(bigram)
    for name in DGA:
        assert f"{name}.com" in flagged, f"bigram missed {name}"
        assert f"{name}.com" not in _flagged(shannon), f"shannon unexpectedly caught {name} — the fixture no longer demonstrates the gap"


def test_bigram_does_not_flag_ordinary_baseline_hostnames(svc):
    result = svc.find_entropy_outliers(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:host"],
        windows=_windows(),
        entropy_method="bigram",
    )
    for f in result.results:
        assert not any(f.value == f"{w}.corp" for w in WORDS), f"false positive on {f.value}"


def test_bigram_finding_carries_its_explanation(svc):
    result = svc.find_entropy_outliers(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:host"],
        windows=_windows(),
        entropy_method="bigram",
    )
    f = next(r for r in result.results if r.value.startswith("qxvbn"))
    assert f.mode == "bigram"
    assert 0.0 <= f.mean_prob < f.prob_thresh
    assert 0.0 < f.score <= 1.0
    assert f.entropy is None and f.direction is None
    assert len(f.rare_pairs) == 5
    assert all(p["prob"] <= f.prob_thresh for p in f.rare_pairs)
    assert f.details["window_label"] == "suspect"
    assert f.details["table_pairs"] > 0


def test_bigram_self_baseline_mode_runs(svc):
    """No windows: learn and score over the same population, like Shannon's
    self-baseline mode. The DGA names are a negligible share of the pair mass,
    so they still fall below the threshold."""
    result = svc.find_entropy_outliers(
        CASE_ID, [SOURCE_ID], fields=["attr:host"], entropy_method="bigram"
    )
    assert result.method == "bigram"
    assert any(v.startswith("qxvbn") for v in _flagged(result))


def test_a_stricter_threshold_narrows_the_findings(svc):
    """`prob_thresh` is the knob it claims to be."""
    loose = svc.find_entropy_outliers(
        CASE_ID, [SOURCE_ID], fields=["attr:host"], entropy_method="bigram", prob_thresh=0.5
    )
    strict = svc.find_entropy_outliers(
        CASE_ID, [SOURCE_ID], fields=["attr:host"], entropy_method="bigram", prob_thresh=0.0001
    )
    assert len(strict.results) < len(loose.results)


def test_non_latin_values_do_not_break_the_scan(svc):
    """Byte-pair bigrams over a multi-byte value must run and stay decodable —
    the fixture contains one CJK hostname."""
    result = svc.find_entropy_outliers(
        CASE_ID, [SOURCE_ID], fields=["attr:host"], entropy_method="bigram"
    )
    assert result.status in ("ok", "insufficient_data")
    for f in result.results:
        for p in f.rare_pairs or []:
            assert isinstance(p["pair"], str)
```

- [ ] **Step 3: Run it**

Run: `uv run pytest tests/test_entropy_bigram_clickhouse.py -v --no-cov`
Expected: PASS. Two failure modes need judgement rather than a fixture tweak:
- *A ClickHouse query error* — the SQL is wrong; fix Task 2's SQL, not the test.
- *`shannon unexpectedly caught …`* — the fixture stopped demonstrating the gap. Make the DGA names use only letter pairs the word list also contains (so their Shannon entropy matches) rather than deleting the assertion; that assertion is the point of the file.

- [ ] **Step 4: Confirm nothing regressed against a live server**

Run: `uv run pytest -m clickhouse -q`
Expected: PASS, including `test_demo_detector_coverage_clickhouse.py` — the proof that the default Shannon path is untouched.

- [ ] **Step 5: Commit**

```bash
git add tests/test_entropy_bigram_clickhouse.py
git commit -m "test(detectors): live-ClickHouse proof that D11 bigram catches DGA names Shannon misses"
```

---

### Task 4: API surface

**Files:**
- Modify: `src/vestigo/api/routers/events.py` — `_run_stat_detector` signature (~line 1725) and its entropy branch (~line 1976), the `GET` query params (~line 2553), the persisted-run params dict (~line 2488), the `POST` body model (~line 2886), both call sites (~line 2735, ~line 2995), the entropy marker text (~line 3071), the detector description strings (~line 2699)
- Test: `tests/test_events_router.py`

**Interfaces:**
- Consumes: `find_entropy_outliers(..., entropy_method=, prob_thresh=)` from Task 2.
- Produces: `entropy_method` and `entropy_prob_thresh` request parameters on both anomaly endpoints; `resolution["entropy_method"]` / `resolution["entropy_prob_thresh"]`; persisted `DetectorRun.params["entropy_method"]` / `["entropy_prob_thresh"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_events_router.py`, following the existing `test_run_stat_detector_dispatches_to_entropy` fixture style:

```python
@pytest.mark.asyncio
async def test_run_stat_detector_passes_entropy_method_through(patched_store, monkeypatch):
    fake_svc = _install_fake_service(monkeypatch)  # same helper the neighbouring tests use
    result, resolution = await _run_stat_detector(
        "case-1",
        "tl-1",
        ["src-1"],
        detector="entropy",
        fields="attr:host",
        series_field="artifact",
        z_threshold=None,
        limit=10,
        entropy_method="bigram",
    )
    assert fake_svc.entropy_calls[0]["entropy_method"] == "bigram"
    assert resolution["entropy_method"] == "bigram"
    # The effective threshold is stamped so the persisted run is self-describing.
    assert resolution["entropy_prob_thresh"] == 0.05


@pytest.mark.asyncio
async def test_run_stat_detector_defaults_entropy_to_shannon(patched_store, monkeypatch):
    fake_svc = _install_fake_service(monkeypatch)
    _, resolution = await _run_stat_detector(
        "case-1",
        "tl-1",
        ["src-1"],
        detector="entropy",
        fields="attr:host",
        series_field="artifact",
        z_threshold=None,
        limit=10,
    )
    assert fake_svc.entropy_calls[0]["entropy_method"] == "shannon"
    assert resolution["entropy_method"] == "shannon"
    assert resolution["entropy_prob_thresh"] is None


@pytest.mark.asyncio
async def test_unknown_entropy_method_is_a_422(patched_store, monkeypatch):
    _install_fake_service(monkeypatch, raise_value_error=True)
    with pytest.raises(HTTPException) as exc:
        await _run_stat_detector(
            "case-1",
            "tl-1",
            ["src-1"],
            detector="entropy",
            fields="attr:host",
            series_field="artifact",
            z_threshold=None,
            limit=10,
            entropy_method="trigram",
        )
    assert exc.value.status_code == 422
```

Extend `test_serialize_finding_entropy_shape` with a bigram sibling:

```python
def test_serialize_finding_entropy_bigram_shape():
    out = _serialize_finding(
        EntropyFinding(
            field="attr:host",
            value="qxvbnrtplkj",
            count=3,
            score=0.98,
            first_seen="2026-03-10T06:00:00Z",
            event_id="evt-1",
            event=None,
            details={"detector": "entropy", "mode": "bigram"},
            mode="bigram",
            mean_prob=0.0008,
            prob_thresh=0.05,
            rare_pairs=[{"pair": "qx", "prob": 0.0}],
        )
    )
    assert out["type"] == "entropy"
    assert out["mode"] == "bigram"
    assert out["mean_prob"] == 0.0008
    assert out["entropy"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_events_router.py -k "entropy" -v --no-cov`
Expected: FAIL — `_run_stat_detector() got an unexpected keyword argument 'entropy_method'`.

- [ ] **Step 3: Thread the parameters**

In `_run_stat_detector`, add to the keyword-only signature block (beside `group_field`):

```python
    entropy_method: str | None = None,
    entropy_prob_thresh: float | None = None,
```

Replace the entropy dispatch branch with:

```python
    if detector == "entropy":
        # D11: which statistic ran, and the effective threshold it used — a
        # persisted run has to say which of the two produced its findings.
        eff_method = entropy_method or "shannon"
        resolution["entropy_method"] = eff_method
        resolution["entropy_prob_thresh"] = (
            (entropy_prob_thresh if entropy_prob_thresh is not None else cfg.stat_entropy_bigram_prob_thresh)
            if eff_method == "bigram"
            else None
        )
        try:
            result = await run_in_threadpool(
                svc.find_entropy_outliers,
                case_id=case_id,
                source_ids=source_ids,
                source_offsets=source_offsets,
                fields=parsed_fields,
                limit=limit,
                per_field_limit=cfg.stat_per_field_limit,
                windows=windows,
                exclude_event_ids=exclude_ids,
                allowlist=allowlist,
                field_mappings=field_mappings,
                inventory=inventory,
                inventory_total=inventory_total,
                entropy_method=eff_method,
                prob_thresh=resolution["entropy_prob_thresh"],
            )
        except ValueError as exc:
            # An unknown method, which would otherwise surface as a 500.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result, resolution
```

In the persisted-run params dict, beside the `group_field` entry:

```python
            # entropy: which statistic ran (D11) and, for bigram, its effective threshold.
            "entropy_method": resolution.get("entropy_method"),
            "entropy_prob_thresh": resolution.get("entropy_prob_thresh"),
```

- [ ] **Step 4: Add the request parameters**

`GET /anomalies` (beside `group_field`):

```python
    entropy_method: str | None = Query(
        default=None,
        description="entropy only: 'shannon' (Shannon character entropy against a Tukey band, the default) or 'bigram' (mean learned character-pair probability against a threshold).",
    ),
    entropy_prob_thresh: float | None = Query(
        default=None,
        gt=0,
        le=1,
        description="entropy bigram method only: mean pair probability below which a value is flagged. Omit to use the server default.",
    ),
```

The `POST` body model gets the same two as `Field(...)` entries with identical descriptions and the same `gt`/`le` bounds. Both call sites pass them through (`entropy_method=entropy_method` / `entropy_method=body.entropy_method`, likewise for the threshold).

- [ ] **Step 5: Update the marker text**

The entropy branch of the annotation-content builder (~line 3071) reads `r.direction`, `r.entropy`, `r.lower`, `r.upper`, all `None` in bigram mode. Branch on the mode:

```python
        elif isinstance(r, EntropyFinding):
            event_id = r.event_id or ""
            src_id = r.event.get("source_id", "") if r.event else ""
            where = _window_phrase(r.details)
            in_window = f" in {where}" if where else ""
            if r.mode == "bigram":
                learned = (
                    "baseline-window character-pair table"
                    if result.method == "temporal-bigram"
                    else "corpus character-pair table"
                )
                rare = ", ".join(f"{p['pair']!r} ({p['prob']:.3f})" for p in (r.rare_pairs or [])[:3])
                content = (
                    f"Unlikely character sequence — {r.field}={r.value!r}: mean pair probability "
                    f"{r.mean_prob:.4f} is below the {r.prob_thresh:.3f} threshold ({learned}; "
                    f"rarest pairs {rare}){in_window}"
                )
            else:
                band_desc = (
                    "baseline-window entropy IQR fence"
                    if result.method == "temporal-iqr"
                    else "corpus entropy IQR fence"
                )
                look = "random-looking" if r.direction == "above" else "degenerate/repetitive"
                content = (
                    f"Entropy outlier — {r.field}={r.value!r}: character entropy "
                    f"{r.entropy:.2f} bits is {r.direction} the learned band "
                    f"[{r.lower:.2f}, {r.upper:.2f}] ({band_desc}; {look}){in_window}"
                )
```

- [ ] **Step 6: Update the endpoint prose**

The `**entropy**` paragraph in the endpoint docstring (~line 2699) describes only the Shannon statistic. Add one sentence naming the bigram method and what it catches that Shannon does not.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_events_router.py -v --no-cov`
Expected: PASS

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff format src/vestigo/api/routers/events.py tests/test_events_router.py
uv run ruff check .
git add src/vestigo/api/routers/events.py tests/test_events_router.py
git commit -m "feat(api): entropy_method + entropy_prob_thresh on the anomaly endpoints"
```

---

### Task 5: Agent tool argument

**Files:**
- Modify: `src/vestigo/agent/tools.py` — `run_detector` (~line 1640-1715)

**Interfaces:**
- Consumes: `_run_stat_detector(..., entropy_method=, entropy_prob_thresh=)` from Task 4.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Add the arguments**

Beside `group_field` in the `run_detector` signature:

```python
        entropy_method: str | None = None,
        entropy_prob_thresh: float | None = Field(default=None, gt=0, le=1),
```

and in the pass-through call:

```python
            entropy_method=entropy_method,
            entropy_prob_thresh=entropy_prob_thresh,
```

- [ ] **Step 2: Document them in the docstring**

The docstring is the model's only description of the knobs. In the tuning-knobs sentence, after `group_field`'s clause:

```
entropy_method (entropy only: 'shannon', the default, measures each value's
own character entropy against a learned band — base64 blobs among words;
'bigram' learns which character *pairs* the baseline contains and flags
values built from ordinary characters in an unusual order, e.g. a
lowercase DGA domain among English hostnames), entropy_prob_thresh
(bigram only: mean pair probability below which a value is flagged),
```

- [ ] **Step 3: Verify the tool schema still builds**

Run: `uv run pytest tests/ -k "agent_tools or tool_registry" -q --no-cov`
Expected: PASS

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff format src/vestigo/agent/tools.py
uv run ruff check .
git add src/vestigo/agent/tools.py
git commit -m "feat(agent): entropy_method knob on run_detector"
```

---

### Task 6: Frontend — mode control, bigram row, method copy

**Files:**
- Modify: `frontend/src/api/types.ts` — `EntropyFinding` (~line 442)
- Modify: `frontend/src/api/anomalies.ts` — `AnomalyParams` (~line 46)
- Modify: `frontend/src/lib/finding-normalize.ts` — the `"entropy"` case (~line 75)
- Modify: `frontend/src/components/analysis/EntropyView.tsx`
- Modify: `frontend/src/components/analysis/MethodologyPanel.tsx` (~line 273-298)
- Test: `frontend/src/test/entropyView.test.tsx` (create)

**Interfaces:**
- Consumes: the API shape from Task 4 — `entropy_method` request param; findings with `mode`, `mean_prob`, `prob_thresh`, `rare_pairs`, and nullable `entropy`/`direction`/`lower`/`upper`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Mirror the types**

`frontend/src/api/types.ts` — replace the `EntropyFinding` interface:

```ts
/** One flagged pair from the bigram method's explanation. */
export interface EntropyRarePair {
  pair: string;
  prob: number;
}

export interface EntropyFinding {
  type: "entropy";
  /** Which statistic produced this finding — the other mode's fields are null. */
  mode: "shannon" | "bigram";
  field: string;
  value: string;
  count: number;
  /** 0–1 severity; per-mode formula. */
  score: number;
  /** shannon: Shannon character entropy of the value, in bits. */
  entropy: number | null;
  direction: "below" | "above" | null;
  lower: number | null;
  upper: number | null;
  /** bigram: mean learned probability of the value's character pairs, 0–1. */
  mean_prob: number | null;
  prob_thresh: number | null;
  /** bigram: the five lowest-probability pairs in the value. */
  rare_pairs: EntropyRarePair[] | null;
  first_seen: string | null;
  event_id: string | null;
  event: Event | null;
  details: Record<string, unknown>;
  /** Present (true) only when the request passed `include_dismissed`. */
  dismissed?: boolean;
  /** Present (true) when a confirmed disposition covers this finding's event. */
  confirmed?: boolean;
}
```

`frontend/src/api/anomalies.ts` — in `AnomalyParams`, beside `group_field`:

```ts
  /** entropy only: "shannon" (character entropy vs a learned band, default) or "bigram" (mean character-pair probability vs a threshold). */
  entropy_method?: "shannon" | "bigram";
  /** entropy bigram method only: mean pair probability below which a value is flagged. Omit for the server default. */
  entropy_prob_thresh?: number;
```

- [ ] **Step 2: Update `finding-normalize.ts`**

Replace the `"entropy"` case:

```ts
    case "entropy":
      title = pair(f.field, f.value);
      subtitle =
        f.mode === "bigram"
          ? `mean pair probability ${(f.mean_prob ?? 0).toFixed(4)} < ${(f.prob_thresh ?? 0).toFixed(3)}`
          : `${(f.entropy ?? 0).toFixed(2)} bits, ${f.direction} [${(f.lower ?? 0).toFixed(2)}, ${(f.upper ?? 0).toFixed(2)}]`;
      ts = ts ?? f.first_seen;
      break;
```

- [ ] **Step 3: Write the failing component test**

Create `frontend/src/test/entropyView.test.tsx`. Follow the render/query-client setup of an existing analysis-component test in `frontend/src/test/` (read one first and mirror its harness — do not invent a new one). It asserts the two rows render their own explanation:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { normalizeFinding } from "@/lib/finding-normalize";
import type { EntropyFinding } from "@/api/types";

const bigram: EntropyFinding = {
  type: "entropy",
  mode: "bigram",
  field: "attr:host",
  value: "qxvbnrtplkj.com",
  count: 3,
  score: 0.98,
  entropy: null,
  direction: null,
  lower: null,
  upper: null,
  mean_prob: 0.0008,
  prob_thresh: 0.05,
  rare_pairs: [
    { pair: "qx", prob: 0 },
    { pair: "vb", prob: 0 },
  ],
  first_seen: "2026-03-10T06:00:00Z",
  event_id: "evt-1",
  event: null,
  details: {},
};

const shannon: EntropyFinding = {
  ...bigram,
  mode: "shannon",
  value: "aaaaaaaa",
  entropy: 0,
  direction: "below",
  lower: 2,
  upper: 4,
  mean_prob: null,
  prob_thresh: null,
  rare_pairs: null,
};

describe("entropy finding normalization", () => {
  it("describes a bigram finding by its pair probability", () => {
    expect(normalizeFinding(bigram).subtitle).toContain("mean pair probability 0.0008");
  });

  it("describes a shannon finding by its bits and band", () => {
    expect(normalizeFinding(shannon).subtitle).toContain("0.00 bits");
  });
});
```

Then add a rendering test for `EntropyRow` in the same file, using the harness from the existing analysis test you mirrored, asserting that the bigram row shows `0.0008`, the threshold, and both rare pairs, and that the shannon row still shows `bits`.

- [ ] **Step 4: Run to verify it fails**

Run (from `frontend/`): `npm run test -- entropyView`
Expected: FAIL — type errors on `mode` and the nullable fields, or a missing rendering branch.

- [ ] **Step 5: Add the mode control to `EntropyView`**

Add state and wire it into the query key and both requests (list and tag):

```tsx
const [mode, setMode] = useState<"shannon" | "bigram">("shannon");
```

Query key gains `mode`; `anomaliesApi.list` and `anomaliesApi.tag` both gain `entropy_method: mode`. In the toolbar, before the field picker:

```tsx
<SegmentedControl
  value={mode}
  onChange={setMode}
  options={[
    { id: "shannon", label: "Character mix", hint: "Shannon entropy of each value against a learned band — catches base64 blobs and padding." },
    { id: "bigram", label: "Character order", hint: "Learned character-pair probabilities — catches ordinary letters in an unusual order, like a lowercase DGA domain." },
  ]}
/>
```

Import `SegmentedControl` from `@/components/ui/SegmentedControl`.

- [ ] **Step 6: Branch `EntropyRow`**

Replace the bits-and-band block with a mode branch. Bigram:

```tsx
      <div className="text-xs text-[var(--color-fg-muted)]">
        mean pair probability{" "}
        <span className="font-mono text-[var(--color-fg-secondary)]">
          {(finding.mean_prob ?? 0).toFixed(4)}
        </span>{" "}
        &lt; {(finding.prob_thresh ?? 0).toFixed(3)} — rarest:{" "}
        <span className="font-mono text-[var(--color-fg-secondary)]">
          {(finding.rare_pairs ?? [])
            .map((p) => `"${p.pair}" ${p.prob.toFixed(3)}`)
            .join(", ")}
        </span>
      </div>
```

and swap the `MoveUp`/`MoveDown` arrow for a single indicator in bigram mode — a bigram finding has no direction, since there is no such thing as too *ordinary* a value. Use `Shuffle` (already the detector's registry icon) at `size={12}` with `text-[var(--color-danger)]`.

- [ ] **Step 7: Update the empty state and the methodology note**

The shannon empty-state hint currently advertises the gap. Rewrite both branches:

```tsx
: mode === "bigram"
  ? "No value's character pairs were unlikely enough against the learned table. Lower the threshold in settings if you expect generated names here."
  : "Character entropy alone misses a lowercase-only generated name — try Character order, which learns which character pairs this field normally contains."
```

The methodology note at the bottom gets a bigram branch in the same register:

```
Comparing windows: learns which character pairs the baseline window's values contain and flags suspect-window values whose pairs are improbable against that table. Character pairs are ASCII-lowercased and compared as bytes — a value of ordinary characters in an unusual order (a generated domain among real hostnames) scores low even when its character mix is unremarkable.
```

- [ ] **Step 8: Update `MethodologyPanel`**

Rewrite the entropy card's `Method`, `Signal`, `Score` and `Backend` rows to cover both methods in the same plain register as the other fourteen. Cover: what each measures; that Character order is the one for generated names among real ones; that both are syntactic and never interpret meaning; the score formulas (distance outside the band ÷ band width, versus distance below the threshold ÷ threshold); and that the bigram table is ASCII-lowercased byte pairs learned from the baseline population, capped at 20 000 pairs.

- [ ] **Step 9: Update the marker detail text**

In `useAnomalyMarkers`, branch the `detail` string on `f.mode` so a tagged bigram finding carries its own explanation into the timeline and any story — mirror the backend wording from Task 4 Step 5 so the two surfaces agree.

- [ ] **Step 10: Run the frontend checks**

Run (from `frontend/`): `npm run test && npm run typecheck && npm run lint`
Expected: PASS on all three.

- [ ] **Step 11: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): entropy method toggle and bigram finding rendering"
```

---

### Task 7: Documentation

**Files:**
- Modify: `docs/ANOMALY_DETECTION.md` §6 (~line 804-883) and the §"Detectors" index entries (~line 17, ~line 55)
- Modify: `docs/ROADMAP.md` — remove the D11 item (~line 208-219) and its priority-list entry (~line 16-18)
- Modify: `docs/PROGRESS.md` — new session entry on top
- Modify: `CHANGELOG.md` — under the unreleased/next section

**Interfaces:**
- Consumes: the shipped behavior from Tasks 1-6.
- Produces: nothing.

- [ ] **Step 1: Rewrite `ANOMALY_DETECTION.md` §6**

The section currently ends its AMiner paragraph with "The bigram variant is roadmap D11" and describes one statistic. Restructure it as a two-method section:

- Keep "What it answers" and "Why it's useful", adding the character-*order* question alongside the character-*mix* one.
- Replace the "Inspired by AMiner, but a different statistic" disclaimer with an accurate statement: we now ship both a Shannon-band method and a bigram-transition method; the bigram method is the batch, baseline-declared re-derivation of AMiner's `EntropyDetector`. Do **not** claim parity — their model is online and self-updating, ours learns one table from a declared baseline window, and that difference stays stated (the tone rule in `CLAUDE.md`).
- Add a "The measurement: character-pair probability" block beside the existing Shannon one: learned `P(c₂|c₁)` over the baseline's distinct values, mean over a value's pairs, flagged below the threshold.
- Extend the two-modes table to four rows (`iqr`, `temporal-iqr`, `bigram`, `temporal-bigram`).
- Add the bigram score formula: `(threshold − mean pair probability) ÷ threshold`.
- Extend the caveats with, one bullet each: ASCII-only casefolding; byte pairs rather than codepoint pairs (so displayed pairs may be partial codepoints for non-Latin values, though the statistic holds for a consistent script); the 20 000-pair cap and that a hit is reported as a warning; and that a field which is *mostly* generated learns generated statistics and flags nothing.
- Update the index entry at line 17 and the example table at line 55 to name both methods.

- [ ] **Step 2: Update `ROADMAP.md`**

Delete the D11 item from "Truth of shipped claims" and its entry from the numbered priority list at the top; renumber the remaining priorities. Per `CLAUDE.md`, delete rather than mark done. If "Truth of shipped claims" is left with only the already-`[x]` D14 entry, drop the subsection heading too and note in the milestone preamble that the truth-of-claims items are closed.

- [ ] **Step 3: Add the `PROGRESS.md` entry**

Newest-on-top, matching the existing format: what shipped, why, and the decisions a future reader would otherwise re-litigate — the bigram/Shannon split as one detector rather than two, ASCII-lower byte pairs, the map-via-`CAST` parameter shape, zero (not smoothed) probability for unseen pairs, and that the live test asserts the gap in both directions. Update the "Last updated" line at the top of the file.

- [ ] **Step 4: Add the `CHANGELOG.md` entry**

Under Added, in the file's existing voice: the bigram method, what it catches that the character-entropy method does not, the new setting, and that the default is unchanged.

- [ ] **Step 5: Verify the doc claims against the code**

Re-read §6 next to `find_entropy_outliers`. Every number in the doc (0.05, 20 000, the 6-character minimum, the 20-value floor) must match a constant or default in the code. Fix the doc, never the code, if they disagree.

- [ ] **Step 6: Commit**

```bash
git add docs CHANGELOG.md
git commit -m "docs: D11 bigram entropy method"
```

---

### Task 8: End-to-end verification

**Files:** none modified unless a defect turns up.

- [ ] **Step 1: Full backend suite with ClickHouse up**

```bash
podman compose up -d
uv run pytest -q
```
Expected: PASS, no skips in the `clickhouse` marker set.

- [ ] **Step 2: Full frontend suite**

```bash
cd frontend && npm run test && npm run typecheck && npm run lint
```
Expected: PASS

- [ ] **Step 3: Drive it in the real app**

Use the project's `/verify` skill (`Skill(verify)`) — it has the isolated-database launch recipe. In the Investigate panel of the seeded demo case: select the entropy detector, run it in **Character mix**, switch to **Character order**, confirm the run completes, the findings render their pair explanation, and Normal/Dismiss/Confirm work on a bigram finding. Then reopen the persisted run and confirm its params show `entropy_method: bigram`.

- [ ] **Step 4: Report**

State plainly what passed, what was skipped and why. If the demo case yields no bigram findings, that is a finding to report, not a failure to hide — the demo's hostnames may be too uniform to learn from, in which case say so and note it as a possible follow-up for `demo/scenario.py`.

---

## Self-Review Notes

Spec coverage checked section by section: §1 statistic → Tasks 1-2; §2 carried-over machinery → Task 2 (reuses the existing loop rather than re-implementing); §3 query shape → Task 2, with one deliberate deviation from the spec recorded below; §4 threshold setting → Task 1 (setting) + Task 4 (effective-value stamping); §5 finding payload → Task 2 (backend) + Task 6 (mirror); §6 API → Tasks 4-5; §7 frontend → Task 6; §8 tests → Tasks 1, 2, 3, 4, 6, 8; §9 docs → Task 7.

**Deviation from the spec, deliberate:** the spec passes the learned table as a `Map(String, Float64)` *query parameter* and computes `rare_pairs` in SQL with a lambda over that map. This plan instead passes two parallel arrays and builds the map with `CAST((keys, values), 'Map(String, Float64)')` inside a `WITH`, and computes `rare_pairs` in Python from the table already in memory. Reason: `clickhouse-connect`'s binding of a dict to a `Map` parameter is not something this codebase already does anywhere, and a lambda capturing a `WITH` alias inside a higher-order function is the kind of construct the D14 test file was written because it fails at execution time. Both replacements use constructs the codebase already relies on. The observable behavior is identical; the `_ascii_lower_bigrams` helper exists precisely so the Python-side pair computation matches ClickHouse's byte-level, ASCII-only-lowercase semantics exactly, and Task 1 tests that helper directly.
