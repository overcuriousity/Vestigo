# D11 — bigram entropy variant — design

*2026-08-06*

## Goal

Close the capability gap `docs/ANOMALY_DETECTION.md` §6 currently documents against
itself: the shipped `entropy` detector measures each value's own Shannon character
entropy, so a value built from perfectly ordinary characters in an unusual *order* —
a lowercase-latin DGA domain among English hostnames — scores unremarkably and is
never flagged. AMiner's `EntropyDetector` learns a character-**bigram** transition
table and flags values whose mean pair probability is low; that statistic catches the
case ours misses.

D11 adds that statistic as a second **method** on the existing entropy detector. Not
a fifteenth tool: same field selection, same findings envelope, same disposition and
allowlist wiring, one more control in the UI.

## Decisions

| Question | Decision |
|---|---|
| Surface | A `method` on the existing `entropy` detector, not a new detector id |
| Request parameter | `entropy_method` = `shannon` (default) \| `bigram` |
| Persisted `method` | `iqr` / `temporal-iqr` (unchanged) or `bigram` / `temporal-bigram` |
| Transition table | Learned in ClickHouse, materialized into Python, passed back as a `Map(String, Float64)` query parameter |
| Casefolding | On — learn and score over `lower(val)`, stamped into run params |
| Unseen pair | Probability 0.0 (no smoothing) |
| Threshold | New setting `stat_entropy_bigram_prob_thresh`, default 0.05 (AMiner's) |
| Finding payload | Same `type: "entropy"`, new `mode` discriminator; per-mode fields nullable |
| Score | `(thresh − mean_prob) / thresh`, already 0–1 |
| Default behavior | Unchanged — an existing run, saved view or agent call with no `entropy_method` gets Shannon exactly as today |

## 1. The statistic

For a field, over the **baseline population** of *distinct* values:

```
pairs(v)      = ngrams(lower(v), 2)                    -- consecutive character pairs
count(p)      = how many baseline values contain pair p, summed with multiplicity
P(c₂|c₁)      = count(c₁c₂) / Σ_x count(c₁x)
```

For each distinct value `v` in the detect population:

```
mean_prob(v)  = arrayAvg(arrayMap(p -> table[p], pairs(v)))     -- missing key ⇒ 0.0
flagged       ⇔ mean_prob(v) < prob_thresh
score(v)      = (prob_thresh − mean_prob(v)) / prob_thresh
```

`mean_prob` is a probability in `[0, 1]`, **not** bits. That is why it does not reuse
the `entropy` field of the finding (§5).

**Why conditional and not joint probability.** `P(c₂|c₁)` asks "given this character,
is the next one ordinary?", which is the question that separates `mail.corp.example`
from `qxvbnrtplkjhgfdsa`. A joint `P(c₁c₂)` would additionally penalize values built
from rare *characters*, which is the charset detector's job and would double-count.

**Populations, per mode.** Identical to Shannon mode, so nothing about field
selection or window handling is new:

| | Self-baseline (`bigram`) | Temporal (`temporal-bigram`) |
|---|---|---|
| Learned from | distinct values of the whole corpus | distinct values in the baseline window |
| Scored | every distinct value | suspect-window distinct values only, attributed to their window |

Self-baseline mode learns and scores over the same population — that is not
degenerate here. A handful of DGA domains among thousands of hostnames contribute a
negligible share of the transition mass, exactly as the Tukey fence tolerates the
outliers it is about to flag. It is the same construction Shannon mode already uses,
with the same caveat: a field that is *mostly* generated learns generated statistics
and flags nothing.

## 2. Carried over unchanged from Shannon mode

Everything below is existing machinery the bigram path reuses rather than
re-implements — `find_entropy_outliers` keeps one signature and one field loop:

- `@_gated_scan` and `HEAVY_SCAN_SETTINGS` (scan-cost gate, memory caps).
- Auto field selection via `_auto_string_fields`, or explicit `fields`.
- `_col_expr` + `field_mappings`, so canonical fields work identically.
- `_MIN_ENTROPY_VALUE_LEN` (6 codepoints) — a value shorter than that has too few
  pairs for a mean to mean anything, so the same floor applies for the same reason.
- `_MIN_ENTROPY_BASELINE` (20 qualifying distinct baseline values) → `insufficient_data`
  when every scanned field skips.
- Distinct-value weighting: a hot value repeated a million times contributes its pairs
  **once**, so traffic volume cannot train the table toward itself.
- Window predicates, sentinel exclusion, per-window attribution, `first_seen`/`evt_id`
  stubs, `_finalize_findings` (allowlist, dispositions, exclusions, limits).

## 3. Query shape

Two queries per field, mirroring the existing learn/detect split.

**Learn** (parameters: case, sources, min length, baseline window clause):

```sql
SELECT pair, cnt, sum(cnt) OVER (PARTITION BY substring(pair, 1, 1)) AS head_cnt
FROM (
    SELECT arrayJoin(ngrams(lower(val), 2)) AS pair, count() AS cnt
    FROM ( SELECT DISTINCT <col> AS val FROM events WHERE … )
    GROUP BY pair
)
ORDER BY cnt DESC
LIMIT <cap>
```

Python divides `cnt / head_cnt` into the probability map. Ordering by `cnt DESC`
means a hit on the cap drops the *rarest* pairs, whose probability is closest to the
zero that a missing key already yields — the cheapest possible truncation error, and
it is reported: exceeding the cap adds a per-field warning, the same way the grouped
charset scan reports its row ceiling.

`ngrams` is a ClickHouse builtin over bytes; `lower()` is applied first. Multi-byte
UTF-8 characters therefore contribute byte pairs rather than codepoint pairs. This is
acceptable and documented — byte pairs of a consistent script are still consistent,
so the statistic holds; it only means the "pairs" a finding displays may render as
partial codepoints for non-Latin values. Rejected the alternative
(`extractAll(val, '(?s).')` + `arrayZip` for true codepoint pairs) as materially more
expensive per row for a cosmetic gain on a detector already near its memory ceiling.

**Detect** (adds the map parameter and the threshold):

```sql
SELECT val, mean_prob, rare_pairs, cnt, first_seen, evt_id [, win_idx]
FROM (
    SELECT val,
           arrayAvg(arrayMap(p -> {tbl:Map(String, Float64)}[p], ngrams(lower(val), 2))) AS mean_prob,
           arraySlice(arraySort(p -> {tbl:Map(String, Float64)}[p],
                                arrayDistinct(ngrams(lower(val), 2))), 1, 5)            AS rare_pairs,
           cnt, first_seen, evt_id [, win_idx]
    FROM ( SELECT <col> AS val, count() AS cnt, min(ts) AS first_seen,
                  toString(argMin(event_id, ts)) AS evt_id [, win_idx]
           FROM events WHERE … GROUP BY val [, win_idx] )
)
WHERE mean_prob < {thresh:Float64}
ORDER BY mean_prob ASC, first_seen ASC
LIMIT {plim:UInt32}
```

`rare_pairs` is the five distinct pairs with the lowest learned probability — the
explanation, computed in the same pass rather than recomputed per finding. Python
attaches each pair's probability from the map it already holds.

**Map parameter size.** The cap is `_ENTROPY_BIGRAM_MAX_PAIRS = 20_000` (module
constant, not a setting — it is a memory bound, not an analyst-facing knob). Worst
case that is ~20k × ~16 bytes of parameter text per query, which is small next to the
scan it accompanies.

## 4. Threshold setting

`stat_entropy_bigram_prob_thresh: float = 0.05` in `core/config.py`, with a
`SettingSpec` in `core/settings_registry.py` (group: the one the other `stat_*`
detector knobs use; not `env_only`, not `secret`, no restart). The coverage test that
enforces "every setting is editable in the UI" requires this.

The **effective** value — request override or server default — is stamped into
`resolution` as `entropy_prob_thresh`, so a persisted `DetectorRun` records the
number it actually used. Same pattern as `shift_fdr_q` / `shift_min_ratio`.

## 5. Finding payload

`EntropyFinding` gains a `mode: "shannon" | "bigram"` discriminator. Per-mode fields
become optional, populated for their own mode and `None` for the other:

| Field | shannon | bigram |
|---|---|---|
| `entropy`, `lower`, `upper`, `direction` | set | `None` |
| `mean_prob`, `prob_thresh`, `rare_pairs` | `None` | set |
| `field`, `value`, `count`, `score`, `first_seen`, `event_id`, `event`, `details` | set | set |

`rare_pairs` is a list of `{pair, prob}` objects.

**Why not overload `entropy`.** A probability in `[0, 1]` and a Shannon entropy in
bits are different quantities on different scales. Writing a probability into a field
named `entropy` — with `lower` = threshold and `upper` = 1.0 to keep the band shape —
would make every existing consumer (the finding row, the marker text, the story
export, the agent's rendering, a user's own script against the API) silently render a
wrong sentence. The discriminator costs one branch in one component and one line in
`finding-normalize.ts`; the overload costs correctness.

`details` (which drives allowlisting and dispositions) keeps
`allowlist_field` / `allowlist_value` exactly as today, so a value allowlisted from a
Shannon run stays allowlisted in a bigram run and vice versa — the analyst judged the
*value*, not the statistic.

## 6. API

`entropy_method` is a new optional parameter on both anomaly surfaces
(`GET` query param and the `POST` body model in `api/routers/events.py`), validated
against `{"shannon", "bigram"}` with a 422 on anything else. Absent ⇒ `shannon` ⇒
byte-identical behavior to today.

`resolution["entropy_method"]` and `resolution["entropy_prob_thresh"]` are stamped for
every entropy run, including Shannon ones (where the threshold is `None`), so a
persisted run says which statistic produced it.

The agent's `run_detector` tool gains the same optional argument, documented in its
docstring next to `group_field`'s.

## 7. Frontend

- **`EntropyView`**: a two-option mode control in the toolbar, alongside the field
  picker. It is part of the query key, so switching modes re-queries.
- **`EntropyRow`**: branches on `finding.mode`. The bigram branch replaces the
  bits-and-band line with
  `mean pair probability 0.004 < 0.05 — rarest: "qx" 0.000, "vb" 0.000, "nr" 0.000`,
  and swaps the up/down arrow for a single "unlikely" indicator (bigram findings have
  no direction — there is no such thing as too *ordinary* a value).
- **Empty state**: today's hint advertises the gap ("Character entropy alone misses a
  lowercase-only generated name"). In shannon mode it becomes a pointer to the bigram
  mode; in bigram mode it says no value scored below the threshold.
- **Methodology note + `MethodologyPanel`**: rewritten to describe both methods in the
  same plain register as the existing fourteen — what each measures, when to reach for
  which, and that both are syntactic.
- **Markers** (`useAnomalyMarkers`) get bigram-appropriate detail text, since that
  string is what a tagged annotation carries into the timeline and the story.
- **`api/types.ts`**: `EntropyFinding` mirrored (the hand-maintained duplication that
  Milestone 3's OpenAPI item exists to delete — follow the existing convention here,
  do not fix that problem in this branch).

## 8. Tests

- **`tests/test_entropy_bigram_clickhouse.py`** (live ClickHouse, following
  `test_charset_group_field_clickhouse.py`): a corpus of English-word hostnames plus
  injected lowercase DGA values, asserting the DGA values flag under `bigram` and
  **not** under `shannon`. That is the exact claim the docs used to overclaim, so it
  is the test that proves D11 closed it — and it fails on today's code.
- Temporal mode: DGA values only in the suspect window, baseline clean; assert window
  attribution and that a baseline-window DGA does not flag.
- Pair-cap warning: a synthetic high-cardinality field trips the cap and the run
  reports it.
- Unit tests without ClickHouse: the probability-map construction and the score
  formula, both pure Python.
- **`tests/test_demo_detector_coverage_clickhouse.py` must stay green** — the default
  mode is unchanged, so this asserts D11 broke nothing.

## 9. Docs

`docs/ANOMALY_DETECTION.md` §6 is rewritten in the same commit: the paragraph that
currently reads "The bigram variant is roadmap D11" becomes a two-method section — the
measurement, the two population modes, the score, and a caveat block covering
casefolding, byte-pair behavior on non-Latin scripts, the pair cap, and the fact that
a mostly-generated field trains generated statistics. The comparison to AMiner stops
being a disclaimer and becomes a statement of what we now ship, without claiming
parity on anything not built (their model is online and self-updating; ours is a
batch table learned from a declared baseline — that difference stays stated).

`docs/ROADMAP.md` loses the D11 item; `docs/PROGRESS.md` gains the session entry.

## Out of scope

- Trigram or higher orders — no evidence they earn their cost here.
- Smoothing (Laplace/Good-Turing) for unseen pairs. Zero is the honest value for
  "never observed" and it is what makes the statistic decisive; smoothing would only
  matter with a much smaller baseline than the 20-value floor already permits.
- Automatic mode selection. The analyst picks; the run records which was used.
- Touching Shannon mode's behavior in any way.
