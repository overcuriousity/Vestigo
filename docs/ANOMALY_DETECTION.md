# Anomaly Detection — Method Reference

Audience: forensic analysts using the Analysis tab. No math background assumed —
every formula is explained in plain language before the notation.

This document covers every detector actually running in the codebase today. If
a detector described here changes (formula, default, field name), update this
file and the "Method" tab copy in the same commit — see
[Reality check](#reality-check-2026-07) at the bottom for the audit that
produced this file and what was fixed as part of it.

There are eleven independent analysis tools in TraceSignal:

1. [Value novelty](#1-value-novelty-rare--first-seen-values) — rare/new field values, single field or [combinations](#value-combinations-the-value_combo-variant) (ClickHouse, no ML)
2. [Frequency anomalies](#2-frequency-anomalies-volume-spikes--silences) — volume spikes/silences (ClickHouse, no ML)
3. [Timestamp order](#3-timestamp-order-out-of-order-records) — timestamps running backwards in record order (ClickHouse, no ML)
4. [Numeric range](#4-numeric-range-out-of-band-values) — numeric values outside a learned band (ClickHouse, no ML)
5. [Charset novelty](#5-charset-novelty-never-seen-characters) — values containing characters outside a field's learned character set (ClickHouse, no ML)
6. [Entropy outliers](#6-entropy-outliers-random-looking-values) — values whose character entropy falls outside the field's learned band (ClickHouse, no ML)
7. [Proportion shift](#7-proportion-shift-value-share-changes-between-windows) — values whose *share* of events changed significantly between the baseline and a suspect window (ClickHouse + a real significance test, no ML)
8. [Interval cadence](#8-interval-cadence-arrival-rhythm-changes-between-windows) — values whose inter-arrival rhythm broke (missed/silent heartbeat) or newly regularized (beaconing) between the baseline and a suspect window (ClickHouse + significance tests, no ML)
9. [Event sequences](#9-event-sequences-never-seen-orderings) — time-ordered n-grams of a field's values that occur in a suspect window but never in the baseline (ClickHouse, no ML)
10. [Value-distribution drift](#10-value-distribution-drift-whole-field-shape-changes-between-windows) — fields whose *whole value distribution* changed between the baseline and a suspect window (ClickHouse + significance tests, no ML)
11. [Semantic similarity search](#11-semantic-similarity-search) — "find events like this one" (embeddings + Qdrant)

The first ten are **statistical detectors**: pure counting and arithmetic over
already-ingested events, no machine learning, no network calls, work the
instant ingestion finishes. The eleventh needs an explicit embedding step first.

Code: `src/tracesignal/db/anomaly_stats.py` (detectors 1–10),
`src/tracesignal/db/similarity.py` (detector 11). UI: `frontend/src/components/analysis/`.

### Query-cost discipline (all statistical detectors)

Three cross-cutting rules keep detector scans survivable on 100M+-row cases
(added 2026-07 after a 300M-row nginx case took a production box down):

- **Two-phase representative events.** Detector scans aggregate only
  `argMin(event_id, timestamp)` per group — never `argMin(message, …)`, which
  forces decompressing the fat `message` column for *every scanned row*
  (~136 GiB per field on the 300M case). The ≤`limit` findings that survive
  ranking are hydrated afterwards in one batched `get_events_by_ids` call
  (`_hydrate_finding_events` / `_hydrate_freq_findings`); a finding whose
  event vanished mid-flight keeps a minimal `_stub_event` shape.
- **`HEAVY_SCAN_SETTINGS` on every whole-corpus scan** (`max_threads = 8`,
  spill thresholds for GROUP BY and plain ORDER BY at min(4 GB, half the
  per-query cap), `max_memory_usage` = total budget / concurrency — the
  budget auto-sizes to `TS_STAT_SCAN_MEMORY_RATIO` (0.8) of detected RAM,
  cgroup-aware; pin it with `TS_STAT_SCAN_MAX_MEMORY_BYTES` when ClickHouse
  is on a different host, ~70% of *that* host's RAM): large GROUP BY states
  and plain ORDER BY sorts spill to disk, and a runaway query fails alone
  instead of taking the server with it. Any new detector query that touches
  the whole corpus must carry it. ClickHouse's own 90%-of-RAM server limit
  is no substitute — containerized servers misdetect total memory (observed
  503 GiB on a 128 GiB VM), so this per-query cap is the real bound.
- **`HEAVY_SCAN_GATE` admission control on every `find_*` detector**: at most
  `TS_STAT_SCAN_CONCURRENCY` (2) heavy scans run against ClickHouse at once;
  surplus scans queue in the app. `max_memory_usage` is per *query* — without
  the gate, N parallel detector requests (one anomaly-panel load fires
  several) each carry a full cap and stack past the ClickHouse host's RAM;
  a correctly-pinned 8 GiB cap OOM-killed a 12 GiB host exactly this way.
  Nested helpers (`recommend_*`, `*_inventory`) are not gated — gated scans
  call them while holding the slot.
- **Window-function sorts cannot spill** (verified empirically on ClickHouse
  26.6: the `MergeSortingTransform` feeding a window function runs into
  `max_memory_usage` regardless of `max_bytes_before_external_sort`, code
  241). Consequences for any `lagInFrame`-style scan: bound the sort — the
  timestamp-order detector scans **per source** (one query per source, no
  case-wide `PARTITION BY source_id`) — and keep the sorted rows slim
  (fixed-width columns only; `message` is hydrated afterwards for just the
  reported rows). The 300M-row case OOMed on both counts before this.
- **No-timestamp events are stored as a sentinel, not NULL.** `timestamp` is
  a non-Nullable sort-key column; events without a parseable timestamp carry
  `2299-12-31 23:59:59.999 UTC` (`db/_dt.py NULL_TS_SENTINEL`) and are
  presented as `null` by the API. Every aggregate/bucket over `timestamp`
  must exclude them via `TS_NOT_SENTINEL_SQL` — exactly where the old
  Nullable schema used `timestamp IS NOT NULL`.
- **Per-source clock-skew offsets (W2) are honored.** When a source carries a
  nonzero `time_offset_seconds`, every window predicate, bucket, representative
  first-seen/last aggregate, and timeline-range query runs over the *effective*
  (offset-corrected) timestamp via `effective_ts_sql` (`db/_offsets.py`), so
  windows and cadences are judged on the corrected timeline. The applied offset
  map is stamped into `DetectorRun.params` for reproducibility. The fast path
  (no in-scope source has an offset) is byte-identical to pre-W2 SQL. The one
  exception is **timestamp order**: its `lagInFrame` skew math stays on the raw
  column (a uniform per-source shift cancels within a source), and only the
  reported timestamps are shifted for display.

### Baseline definitions, suspect windows, and the normality model

Every temporal detector (value novelty, value combos, frequency, numeric
range, charset, entropy, proportion shift, interval cadence, event sequences —
everything except the mode-less timestamp-order detector; proportion shift,
interval cadence and event sequences are temporal-*only*, having no
self-baseline mode) answers the same shape of question: *given a period I know was
normal, what stands out in the periods I'm suspicious of?* Two persistent,
analyst-declared primitives express "normal", and it is worth being precise
about which is which:

- **Baseline definitions = time-based normality.** A named, per-timeline
  Postgres object (`baseline_definitions`, model in `db/postgres.py`) holding
  one **baseline window** (the known-normal reference period) plus **1..N
  labeled suspect windows** (the ranges under investigation). Windows are
  half-open `[start, end)`, need **not** be adjacent (gaps are fine), and the
  baseline must be disjoint from every suspect window (the API rejects overlap
  with 422 — "absent from baseline" is meaningless if they share events).
  Suspect windows *may* overlap each other (a burst examined two ways) with a
  warning. Cap: 10 suspect windows. Mark them by dragging ranges on the
  timeline histogram in "mark" mode; manage them in the Baselines panel.
- **Dispositions = the analyst's verdicts on findings**
  (`finding_dispositions`, router `api/routers/dispositions.py`). One unified
  taxonomy replaces the former detector allowlist, per-event `normal`
  annotation and `pinned` flag; every write is audited. `kind` carries the
  verdict, and the distinction is deliberate:
  - **`normal`** — "this is expected behavior"; the manual extension of the
    baseline. The only kind that affects detection: value-scoped rows
    (`field` + `value`, timeline-scoped) are dropped post-detection on
    *every* event; event-scoped rows (`source_id` + `event_id`) exclude the
    event from scans (the positional case, e.g. timestamp order). For the
    frequency detector the field is the series field, so a row suppresses a
    whole known-noisy series. `detector` may be the wildcard `"*"` — normal
    for **every** value-shaped detector at once, written by the field-value
    **Normal** action (an event-detail attribute row has no detector
    context) versus a finding row's Normal which scopes to one detector. A
    detector run applies rows whose detector is its own **or** `"*"`.
  - **`dismissed`** — "noise for this investigation"; presentation-only.
    Detectors keep scoring; findings are filtered at response time with an
    explicit `dismissed_count` (never silently) and an
    `include_dismissed=true` escape hatch that returns them flagged
    `dismissed: true`. Never enters the reproducibility hash and never
    rewrites what a persisted run found.
  - **`confirmed`** — "escalated true positive"; durable. Event-scoped with a
    concrete detector; written by the **Confirm** action together with the
    system `anomaly` annotation. Bulk "Tag N as anomaly" re-runs preserve
    confirmed `(event, detector)` pairs instead of clearing them.

  Undecided is the absence of a row. Rows are freely deletable (the
  Dispositions list under Windows & normality); removing one makes the
  finding flaggable/visible again.

**How a suspect window scores.** Each temporal detector restricts its scan to
the union of the baseline and suspect windows (events outside every window are
ignored), learns its reference from the **baseline** window, and reports one
finding per (value, suspect window) with the suspect window named in
`details.window_label`. The "surprise" family of detectors (value novelty,
combos, charset) use the **suspect window's own event count** as the score
denominator — not the whole corpus, which overstated rarity whenever the
windows covered only part of the timeline. A suspect window with fewer than 50
events gets a `warnings` entry (scores over tiny samples are unstable); it is
warned about, never silently dropped.

**Frequency's bucket rule (the subtle one).** The bucket interval is derived
from the **baseline** window (`baseline span / 60`), and the *same*
epoch-aligned interval buckets every suspect window, so counts are comparable.
Mean/std come from the baseline window's buckets only, **zero-filled** — a
bucket with no events is a real `0` in the distribution, not a missing sample,
or a silent period would inflate the mean. Because `toStartOfInterval` aligns
buckets to the Unix epoch, a window edge can cut a bucket; **partial buckets
are excluded** from both the baseline stats and suspect-window scoring (a
half-covered bucket reads as a fake spike/drop). A suspect window too short to
contain one full bucket yields a warning rather than a bogus single-bucket
z-score — the fix is to widen the window or shrink the baseline (a shorter
baseline → finer interval).

**Forensic reproducibility.** Baseline definitions and dispositions are
freely editable — reproducibility does **not** depend on them surviving.
Every `DetectorRun` snapshots into its `params` the resolved `baseline_id`,
the full `windows` payload, a `windows_hash`, and the `dispositions_hash` +
row count it was filtered through (`dispositions_hash` covers the
detection-affecting `normal` rows only, value- **and** event-scoped — the
old `allowlist_hash` never recorded the per-event exclusions). So a persisted
run stays fully self-describing — "why is this value not flagged?" and "what
exactly did this scan compare?" stay answerable — even after the definition
or dispositions are later changed or deleted. Saved baseline definitions
(`baseline_id`) are the only temporal input — the legacy single-`baseline_end`
split point and `temporal=true` midpoint fallback were removed once nothing
depended on them (runs persisted with those params still render: stored
`params` are displayed, never replayed).

The ways an event can be "normal" (or otherwise dispositioned), to keep them
straight:

| Mechanism | Scope | Created from | Effect |
|---|---|---|---|
| Baseline window | time range | histogram "mark" mode | detectors learn "expected" here |
| Disposition `normal` | `(detector\|"*", field, value)` or one event | Normal on a finding / field row | suppresses detection; hashed into runs |
| Disposition `dismissed` | `(detector, field, value)` or one event | Dismiss on a finding | hidden from view only; counted, revealable |
| Disposition `confirmed` | one event + detector | Confirm on a finding | durable escalation; survives re-scans |

---

## 1. Value novelty (rare / first-seen values)

**What it answers:** "Which field values in this timeline barely ever occur,
or never occurred before now?" A rare `user_agent`, a `process_name` seen only
once, a `status_code` that appears for the first time after an incident
started — these are the kind of things forensic analysts hunt for manually by
eyeballing a `GROUP BY`. This detector automates that scan across every
plausible field at once and ranks the results by how surprising each value is.

**Why it's useful:** malicious activity frequently *is* the rare value — a
one-off admin login from an unfamiliar host, a process name that shows up once
in ten million events. Frequency-based rarity is a cheap, explainable, and
fast first pass before reaching for embeddings or manual review. It is also
the only anomaly detector that works the moment ingestion finishes — no
embedding job required.

**Two independent modes** — pick based on what you're investigating:

| Mode | What "rare" means | When to use |
|---|---|---|
| **Self-baseline** | Value appears ≤ *rarity floor* (default 3) times across the *whole* timeline | General triage — "what's unusual in this dataset overall" |
| **Temporal** | Value is absent from the baseline window but present in a suspect window (see [the normality model](#baseline-definitions-suspect-windows-and-the-normality-model)) | Incident response — "what showed up in the window I'm investigating that wasn't in the known-normal period" |

### Self-baseline mode

Count how many times each value of a field occurs across every event in
scope. Any value occurring `rarity_floor` times or fewer (default: 3) is
flagged. The **rarity floor is a count, not a percentage** — on a
10-event timeline, "≤ 3 occurrences" is not rare at all; on a 10-million-event
timeline it very much is. Analysts should sanity-check the floor against
timeline size, or switch to temporal mode when investigating a specific
incident window rather than tuning the floor.

### Temporal mode

You select a [baseline definition](#baseline-definitions-suspect-windows-and-the-normality-model)
— a baseline window plus one or more suspect windows. A value is flagged only
if it appears **zero times in the baseline window** and **at least once in a
suspect window** — genuinely new activity in the period you're investigating,
not just uncommon activity in general. You get one finding per suspect window
the value appears in, each carrying its `window_label`. The rarity floor is
**ignored entirely** in this mode; the surprise denominator is the suspect
window's own event count, not the whole corpus (see [the score](#the-score-surprise)).

### The score: "surprise"

Every flagged value gets a **surprise score**:

```
surprise = −log(count / total_events)
```

Read it as: *how many bits of "huh, that's odd" does this value carry.*
Concretely:
- A value that's half the corpus (count/total = 0.5) scores ~0.7 — not
  surprising at all.
- A value that's 1 in 1,000 scores ~6.9.
- A value that's 1 in 1,000,000 scores ~13.8.

Higher = rarer = ranked higher in the findings list. It is intentionally the
same "self-information" quantity used in information theory — the log makes
a value that's 1000× rarer than another score linearly further away rather
than needing to compare ratios by eye. You never need to compute it yourself;
it's carried in `details.surprise` on every finding for citation in a report.

**Caveat, stated honestly:** this score ranks *rarity*, not *maliciousness*.
A rare value is exactly as often a broken parser, a one-off maintenance
script, or a misconfigured log source as it is an attacker. Treat every
finding as a triage lead, not a verdict — this is exactly what the in-app
`ShieldCheck` banner and the "Rare ≠ malicious" note say, and it is worth
repeating in any case report that cites these findings.

### Which fields get scanned

You can name specific fields, or let the auto-recommender pick. The
recommender classifies every candidate field (`artifact`, `timestamp_desc`,
`display_name`, `parser_name`, and every `attr:<key>` seen in the data) purely
by *cardinality* — no assumptions about field meaning, so it works on any log
type:

| Classification | Rule | Recommended? |
|---|---|---|
| `constant` | 1 or fewer distinct values | No — no signal |
| `sparse` | fewer than 5% of events have any value | No — too little coverage |
| `identifier` | distinct values ÷ non-empty events ≥ 0.9 (hashes, UUIDs, free-text messages — nearly every value is unique) | No — nothing repeats, so nothing can be "rare" relative to a peer group |
| `categorical` | everything else — moderate cardinality, decent coverage | Yes |

One exception overrides the cardinality rule: pipeline-synthesized fields
(`artifact`, `display_name`, `parser_name`, `parser_version`, `source_file` —
`_SYNTHETIC_FIELDS` in `db/anomaly_stats.py`) are never auto-recommended and
are hidden from the Fields picker. These values are stamped on by
normalization, not present in the raw log data, so "rare" values there
reflect ingestion metadata rather than analyst-relevant behavior. They remain
valid tokens for explicit `fields=` API selections.

The attribute-key inventory behind this classification counts distinct values
with ClickHouse's approximate `uniq()` (~1% error), not `uniqExact` — the
thresholds above are coarse ratios, and exact per-key hash sets over
near-unique values on multi-million-event sources were a server-killing
memory blowup. The scan also ignores empty attribute values entirely (they
are treated as "field absent", matching the coverage semantics everywhere
else).

When no explicit fields are given, both the `/anomalies/fields` endpoint and
the value-novelty detector itself resolve this inventory from the per-source
field-stats cache (`db/field_stats.py`) instead of running the live `uniq()`
scan on every request — the cache is computed once per source (at ingest and
after each enrichment apply) and merged across a timeline's sources at read
time, `distinct` approximated as max-across-sources. Only the exact
canonical-mapping aggregates (`field_mappings`) still require a small live
query, since deduping raw keys mapped to one canonical field isn't derivable
from per-source counts.

This is a real, useful filter: scanning an `identifier`-classified field like
a raw message string for "rare values" would flag almost every row (each
message is close to unique) and bury real signal in noise. Restricting to
`categorical` fields (status codes, artifact types, usernames, hostnames,
event IDs, parser names) is what makes the score meaningful.

Auto-selection is capped at 15 fields per scan (`_MAX_AUTO_SCAN_FIELDS`).
All plain-attribute fields in a scan share **one** ARRAY JOIN pass over the
`attributes` map (M23b — the map column dominates scan cost, and the former
one-query-per-field loop re-read it once per field); the cap bounds that
pass's expansion width plus the residual per-field queries for top-level
columns and mapped canonical fields. The highest-coverage recommended fields
win the cap; you can always override with an explicit field list via the
Fields picker to scan something the auto-selector skipped.

### Value combinations (the `value_combo` variant)

The **Value combos** detector (AMiner `NewMatchPathValueComboDetector`) is the
multi-field extension of rare values: instead of scoring one field's values, it
scores *combinations* of two or more fields together.

**Why a separate detector:** a combination can be rare even when each field's
individual values are common. `login_ok` is a common action; `03:00` is a common
hour; but `(login_ok, 03:00)` — a successful login at 3am — may be a combination
that has never occurred before. Single-field novelty can't see this; it only
knows each value in isolation.

**How it works:** exactly like rare values, but the ClickHouse `GROUP BY` spans
every selected field expression instead of one, and the count/first-seen and
surprise score are computed per *combination*. Both modes carry over unchanged —
self-baseline flags combinations appearing ≤ the rarity floor; temporal flags
combinations absent from the baseline window but present after the split. The
score is the same `−log(count / total events)`.

**Field selection differs in one way:** you must give it at least two fields (the
picker enforces 2–4). Auto mode does **not** enumerate every pair — with 15
candidate fields that would be 105 combinations, 105 queries, and a result set no
analyst can triage. Instead auto mode combines exactly the two highest-coverage
recommended fields into a single tuple. Pick fields explicitly for any other
combination.

---

## 2. Frequency anomalies (volume spikes & silences)

**What it answers:** "Is there a time window where some category of event
happened a lot more — or a lot less — than usual?" A burst of failed logins,
a process that suddenly starts firing 50× its normal rate, a service that
suddenly goes silent when it should be logging heartbeats — this detector
finds those windows automatically instead of requiring you to eyeball a
histogram bar by bar.

**Why it's useful:** volume is one of the oldest and most robust anomaly
signals in security monitoring (this is the same basic idea SIEM "threshold"
and "spike" rules use) — and unlike value novelty, it also catches **silences**
(a service that stops logging is often as suspicious as one that starts
flooding). It requires no embeddings, no model, and works immediately.

### How it works, step by step

1. Pick a **series field** — the field whose values you want separate
   timelines for (default: `artifact`, e.g. "webhistory", "prefetch",
   "eventlog"). Every distinct value of this field gets scored as its own
   independent time series.
2. Split the timeline into **time buckets**. The bucket width is computed the
   same way the histogram computes its bars: `(max_timestamp − min_timestamp)
   / bucket_count` (default 60 buckets). This is deliberately the *same*
   formula the Explorer's histogram uses, so the anomaly-window markers
   overlaid on the histogram line up with what the histogram is actually
   showing — but note the anomaly scan itself always covers the *whole*
   timeline for the case/source, regardless of any `q`/`artifact`/`tag`/time
   filters currently applied to the Explorer view. A filtered histogram and
   the anomaly overlay on top of it can therefore represent different spans;
   this is intentional (the detector needs the full picture to baseline
   correctly) but worth knowing if the two visually disagree.
3. Count events per (series value, time bucket).
4. Compute a **z-score** for each bucket: how many standard deviations away
   from "normal for this series" this bucket's count is.
5. Flag any bucket where `|z| ≥ z_threshold` (default 2.5).

### The z-score, explained without the formula first

Every series (e.g., every distinct `artifact` value) has its own "normal
rhythm" — an average event count per bucket, and how much that count
naturally wobbles bucket to bucket. The z-score answers: *for this one
bucket, how many "normal wobbles" away from the average is it?*

- z ≈ 0: perfectly typical for this series.
- z ≈ +3: this bucket had about 3 standard deviations *more* events than
  usual — a spike.
- z ≈ −3: about 3 standard deviations *fewer* — a silence.

The sign tells you the direction (`TrendingUp`/`TrendingDown` icon in the UI);
the magnitude tells you how extreme.

Formula, for completeness: `z = (observed − mean) / std`, where `mean` and
`std` come from the rest of the series (see leave-one-out note below, and the
temporal variant).

### Two sub-modes, matching value novelty's structure

| Mode | Baseline (mean/std source) | When to use |
|---|---|---|
| **z-score** (self-baseline) | Every bucket in the series, computed **leave-one-out** | General triage |
| **temporal-z-score** | The [baseline window's](#baseline-definitions-suspect-windows-and-the-normality-model) full, zero-filled buckets; suspect-window buckets scored against them | Incident response |

**Self-baseline uses leave-one-out scoring.** Each bucket is compared against
the mean/std of *every other* bucket in that series — not against a mean/std
that includes itself. This matters: if you included the spike bucket in its
own baseline, a big enough spike would drag the mean and standard deviation up
enough to make itself look *less* anomalous, potentially hiding the very
event you're looking for. Leave-one-out avoids that self-suppression. (This
document and the in-app Methodology panel previously stated the opposite —
that the self-baseline "includes this window" — which was wrong; fixed as
part of the audit that produced this file.)

**Temporal mode** computes mean/std from the baseline window's buckets only
(zero-filled and full-buckets-only — see the [normality model](#baseline-definitions-suspect-windows-and-the-normality-model)
for the bucket-interval and partial-bucket rules), then scores every
suspect-window bucket against that fixed, independent baseline — no
leave-one-out needed since the scored buckets were never part of the
baseline. A series with zero activity in the baseline but some activity in a
suspect window is still scored (against a floored minimum standard
deviation, see below) rather than silently skipped — "this thing didn't exist
before and now it does" is exactly the kind of finding temporal mode exists to
surface. A suspect window too short to contain one full baseline-interval
bucket is warned about instead of scored.

### Two numerical floors — why they exist and what they mean for interpretation

- **Minimum bucket count (`_MIN_FREQUENCY_BUCKETS = 3`):** a series needs at
  least 3 data points before z-scoring is attempted at all. Standard
  deviation over 1–2 points is meaningless or undefined; series with fewer
  buckets are skipped rather than producing a misleading z-score.
- **Minimum standard deviation (`_MIN_FREQUENCY_STD = 0.5`):** a series that's
  almost perfectly constant (say, exactly 10 events every bucket, forever)
  has a standard deviation near zero. Dividing by a near-zero number would
  either blow the z-score up to a meaningless extreme or produce `NaN`/`inf`.
  The standard deviation is floored at 0.5 events so that any real deviation
  from a near-constant baseline still produces a sane, finite z-score instead
  of an infinite or undefined one.

**Honest statistical caveat:** z-scoring assumes something close to a normal
(bell-curve) distribution of counts per bucket. Event counts — especially for
low-volume series — are often closer to Poisson-distributed (bursty, skewed,
never negative), and a series with only 3–5 data points does not have enough
samples for "standard deviation" to be a stable, trustworthy number in the
first place. Treat z-scores on short or sparse series as a rough triage
signal, not a rigorous statistical test. This is a real, known limitation —
not a bug — but the Method tab does not currently say so explicitly; consider
that a documentation gap when defending a finding that rests on a short
series.

### Choosing the z-threshold

Default is 2.5 (`stat_z_threshold` in config, and the app default). Roughly:
in a truly normal distribution, |z| ≥ 2.5 happens by chance about 1.2% of the
time per bucket — a reasonable, if not rigorous, "unusual enough to look at"
cutoff. Lower it to catch more/subtler deviations at the cost of more false
positives; raise it to see only the most extreme windows. The severity
color-coding in the UI (low/medium/high) scales relative to *your chosen*
threshold, not a fixed number — a window at 2× your threshold reads as high
severity regardless of whether your threshold is 2 or 6.

---

## 3. Timestamp order (out-of-order records)

**What it answers:** "Within a source file, does any event's timestamp jump
*backwards* compared to the record physically before it?" A log line whose
timestamp is earlier than the line above it did not arrive in chronological
order — a strong indicator of log tampering, a clock reset, or two writers
appending to one file.

Adapted from AMiner's `TimestampsUnsortedDetector`.

**Why it's useful:** Most log formats are append-only and monotonic — each new
record is stamped no earlier than the last. A backwards jump is therefore
anomalous by construction, and unlike "rare value" it needs no baseline to be
meaningful. Deleting or editing lines in the middle of a log, or resetting a
system clock, leaves exactly this signature.

### This detector has no baseline/detect modes

Value novelty and frequency both split time into a "normal" baseline and a
"suspect" detect window. Timestamp order does not: the violation is *positional*
(a record is out of place relative to its neighbour), not *temporal* (something
changed after a point in time). The Method line reports `sequential`, and the
UI shows no mode toggle.

### What "record order" means

Record order is the position of the raw record **in the source file**, not its
parsed timestamp — that would be circular. Concretely the detector orders by:

1. **byte offset** — where the raw record starts in the file. Monotonic per
   file, so it is the natural record sequence.
2. **line number**, then **event id** — deterministic tie-breaks only.

Each event's timestamp is compared to its *immediate predecessor* in that order
(ClickHouse `lagInFrame`), not to a running maximum. The distinction matters:
if one event is stamped far in the future, comparing against a running maximum
would flag every later event until the clock "caught up" — a cascade. Comparing
against the predecessor flags just the two boundaries (the jump up, and the jump
back down), which is what an analyst wants to triage.

### Plain-language rule, then the notation

Flag a record when its timestamp is at least θ seconds earlier than the record
immediately before it in file order:

```
flag event i  when  ts(i) < ts(i-1) − θ        (records ordered by byte offset)
skew(i)       =     ts(i-1) − ts(i)  in seconds  (the backwards jump, > 0)
```

`θ` is `min_skew_seconds` (config `stat_order_min_skew`, default 1.0s). It
suppresses sub-second logger jitter — two events in the same millisecond bucket
written "out of order" by a fraction of a second are almost never interesting.
Set it to 0 for AMiner-strict behaviour (any backwards step flags).

The **score** is the skew in seconds — larger backwards jumps rank first.

### Caveats

- **NULL timestamps are excluded.** A record with no parsed timestamp carries no
  order signal and is skipped, not treated as position zero.
- **Interleaved multi-writer logs legitimately jump.** Two processes appending
  to one file (or a merged/rotated log) can interleave timestamps without any
  tampering. Read a cluster of small-skew violations in one source as "this is a
  multi-writer log", not "this was edited" — the byte offsets and per-source
  violation count in `details` help tell the two apart.
- Findings are grouped by source in the UI, each with the source's total
  violation count and worst skew, so a systematically-unsorted source reads
  differently from a single sharp jump.

---

## 4. Numeric range (out-of-band values)

**What it answers:** "For fields that hold numbers, is any value far outside the
range the field normally takes?" A `response_bytes` of 5 GB when the field
normally sits in the kilobytes, a negative `duration`, a port number where one
never appeared before.

Adapted from AMiner's `ValueRangeDetector`.

**Why it's useful:** Numeric fields have a natural notion of "too big" / "too
small" that categorical novelty can't express — 9,999 is not "a rare value" the
way a new username is, it's *out of range*. Data exfiltration (huge byte
counts), malformed records (negative or absurd values), and scanning (ports
outside the usual set) all show up here.

**Field selection is syntactic, never semantic.** A field qualifies if at least
90% of its non-empty values parse as numbers (`toFloat64OrNull`). The detector
never interprets what the number *means* — a field of HTTP status codes
qualifies exactly like a field of byte counts. That's a strength (works on any
log) and a caveat (see below).

### Two modes

| | Self-baseline (`iqr`) | Temporal (`temporal-range`) |
|---|---|---|
| Baseline | the whole corpus | values in the baseline window |
| Band | Tukey fence `[q1 − 1.5·IQR, q3 + 1.5·IQR]` | exact `[min, max]` of the baseline |
| Flags | statistical outliers | anything outside the historical range |

**Why self-baseline needs the IQR fence.** An exact min/max over the whole
corpus flags nothing by construction — every value is within [min, max] because
min and max *are* corpus values. So self-baseline mode instead uses the Tukey
fence: the interquartile range (IQR = q3 − q1, the spread of the middle 50% of
values) extended 1.5× past each quartile. This is the standard boxplot-outlier
rule, and it's fully explainable ("the middle 50% of `bytes` sat between 100 and
300; this value of 9,000 is far past the q3 + 1.5·IQR fence of 426").

**Why temporal uses exact min/max.** With a real baseline/detect split, the
AMiner-faithful behaviour is exactly right: learn the range the field took
*before* the incident window, flag anything outside it *after*. "The largest
`bytes` value in the 300-event baseline was 500; this suspect-window value is
9,999."

### The score

```
score = distance outside the band ÷ band width
```

A value one band-width past the edge scores 1.0; ten band-widths past scores
10.0. It normalises severity across fields with very different scales, so a
`bytes` outlier and a `duration` outlier rank comparably. Findings group by
distinct violating value.

### Caveats

- **Numeric-looking identifiers.** Ports, status codes, PIDs, and error codes
  all parse as numbers but have no meaningful "range" — an IQR fence over status
  codes is nonsense (404 is not an outlier of 200). For these, prefer **temporal
  mode** (did a code appear that was never seen in the baseline?) or just don't
  select them. The picker shows each field's numeric parse ratio to help you
  judge.
- **Baseline size floor.** A field with fewer than 20 numeric baseline samples
  is skipped — quartiles and min/max over a handful of points are too noisy to
  trust. If every scanned field is skipped, the status is `insufficient_data`.
- Rare ≠ malicious, as everywhere: a legitimate large file transfer and an
  exfiltration both produce a large `bytes` value.

---

## 5. Charset novelty (never-seen characters)

**What it answers:** "Does any value of this field contain a *character* that
this field's values never otherwise contain?" A null byte inside a username, a
Cyrillic homoglyph in a hostname, a `'` or `;` in a field that is otherwise
plain alphanumerics.

Adapted from AMiner's `CharsetDetector`.

**Why it's useful:** Injection payloads, encoding-smuggling, and homoglyph
spoofing change a value's *alphabet* before they change anything a value- or
frequency-level detector can see. `admin` and `аdmin` (Cyrillic а) are two
different rare values to the novelty detector — but only the charset detector
says *why* the second one is suspicious. Detection is purely syntactic: the
detector compares character identities, never what a value means.

**Character sets are learned over distinct values, not rows.** A character
carried by one hot value that repeats a million times counts once. This keeps
a field's reference alphabet a property of its vocabulary, not of its traffic
volume.

### Two modes

| | Self-baseline (`rare-chars`) | Temporal (`temporal-charset`) |
|---|---|---|
| Reference set | characters appearing in **more than** `rarity_floor` (3) distinct values | every character seen in baseline-window values |
| Flags | values containing a character almost no other value has | suspect-window values containing a character the baseline window never had |

**Why self-baseline can't use the plain charset.** The whole corpus's
character set trivially contains every character in the corpus — nothing could
ever be novel (the same degeneracy as an exact min/max band in the numeric
range detector). So self-baseline mode inverts it: a character is *rare* when
it appears in at most `rarity_floor` distinct values, and any value containing
a rare character is flagged. "Of 5,000 distinct usernames, exactly one
contains a NUL byte" is precisely the finding this mode exists for.

**Temporal mode is the AMiner-faithful one:** learn the baseline window's
alphabet, flag suspect-window values whose characters step outside it.

### The score

```
score = Σ over the value's novel characters of −log(values_with_char / distinct_values)
```

The same "surprise" family as value novelty, summed per novel character — a
value containing two rare characters outranks a value containing one, and a
character shared by 3 values scores lower than a character in exactly 1. In
temporal mode a novel character was never seen at all, so each contributes the
+1-smoothed maximum `log(distinct_values + 1)`. Findings carry the novel
characters *and their unicode codepoints* (`U+0000`, …) in `details`, so
invisible characters are visible in the report.

### Caveats

- **Free-text fields in large scripts.** A field whose reference alphabet
  exceeds 5,000 characters (CJK prose, base64 blobs mixing full alphabets) is
  skipped — "novel character" is meaningless there. Fields with fewer than 20
  distinct baseline values are skipped too (an alphabet learned from a handful
  of values flags everything). If every scanned field skips, the status is
  `insufficient_data`.
- **Characters are extracted with re2 (`extractAll(val, '(?s).')`) in UTF-8
  mode.** Codepoints — including NUL — are handled; byte sequences that are not
  valid UTF-8 may be skipped by the regex engine rather than surfaced as
  findings. (A byte-level fallback exists as a documented option if this bites
  in practice.)
- **Auto field selection differs from value novelty:** identifier-kind fields
  (URLs, filenames, user agents — near-unique values) are *included*, since
  that's exactly where injected metacharacters live. Constant and sparse
  fields stay excluded. Within the 15-field auto cap, up to 5 slots are
  reserved for identifier fields so a source with many categorical columns
  can't crowd them out (categoricals otherwise sort first); the Fields picker's
  "auto" preview mirrors this selection.
- **Tuning:** the rare-character floor is its own setting,
  `stat_charset_rarity_floor` (`TS_STAT_CHARSET_RARITY_FLOOR`, default 3),
  separate from value novelty's `stat_rarity_floor` so the two detectors can be
  tuned independently — they count different things (distinct-values-per-char
  vs. value occurrences).
- Rare ≠ malicious, as everywhere: a legitimately imported UTF-8 name and a
  homoglyph attack look identical to this detector. It ranks for triage.

---

## 6. Entropy outliers (random-looking values)

**What it answers:** "Does any value of this field look *statistically unlike*
the field's normal values — too random, or too repetitive?" A DGA domain
(`kq3v9xz2m8w1.com`) among human-named hosts, a base64 payload in a field of
plain words, a padding string of one repeated character.

Adapted from AMiner's `EntropyDetector`.

**Why it's useful:** Randomness is a fingerprint of machine-generated content
— DGA domains, encoded/encrypted payloads, session keys dropped into the wrong
field. None of these are "rare values" in a useful sense (every DGA domain is
unique, so all of them are maximally rare) — what distinguishes them is
*character-level* statistics. The inverse signal matters too: near-zero
entropy means degenerate stuffing (`AAAA…`, `xxxxxxxx`) that often marks
overflow padding or sanitizer artifacts.

**The measurement: Shannon character entropy.** For one value, count how often
each character occurs, turn the counts into frequencies, and sum
`−f·log₂(f)` over the characters. The result is bits per character: English
words sit around 2.5–4 bits, uniformly random alphanumerics near 5–6, a single
repeated character at exactly 0. The detector never interprets the value —
only its character histogram.

**Entropies are per distinct value, not per row.** A hot value repeated a
million times contributes one point to the field's entropy distribution, so
traffic volume can't drag the band. Values shorter than 6 characters are
excluded outright (baseline and detect): a 3-character string's entropy is
degenerate and would flood the band with false lows.

### Two modes

| | Self-baseline (`iqr`) | Temporal (`temporal-iqr`) |
|---|---|---|
| Baseline population | entropies of every distinct value in the corpus | entropies of distinct values in the baseline window |
| Band | Tukey fence `[q1 − 1.5·IQR, q3 + 1.5·IQR]` | same fence, learned from the baseline window |
| Flags | statistical entropy outliers anywhere | suspect-window values outside the baseline window's band |

Unlike the numeric-range detector, *both* modes can use the fence directly:
quartiles are not degenerate over their own population the way an exact
min/max is, so self-baseline mode needs no special construction.

### The score

```
score = distance outside the band ÷ band width
```

Identical to the numeric-range score — a value one band-width past the fence
scores 1.0. `direction` says which side: **above** = random-looking, **below**
= degenerate/repetitive. Findings carry the entropy, band, quartiles, and
baseline size in `details`.

### Caveats

- **Legitimate high-entropy fields.** Hashes, UUIDs, and tokens are *supposed*
  to be random — a field of session IDs will produce a high, tight band and
  flag nothing (good), but a mixed field (URLs that sometimes embed tokens)
  will flag the tokens. That's usually the interesting case anyway; deselect
  the field if not.
- **Entropy is length-insensitive beyond the minimum.** A short random string
  and a long random string score similarly; this detector finds *character
  randomness*, not payload size — pair with numeric range over a length field
  if size matters.
- **Baseline size floor.** Fields with fewer than 20 qualifying distinct
  baseline values are skipped; if every scanned field skips, the status is
  `insufficient_data`.
- Rare ≠ malicious, as everywhere: a CDN hostname and a DGA domain can score
  identically. It ranks for triage.

---

## 7. Proportion shift (value-share changes between windows)

**What it answers:** "Is this value significantly more (or less) frequent in
the window I'm investigating than in the known-normal period?" Windows event
ID `4625` (failed logon) exists in every baseline — a brute-force attempt is
not a *new* value, it's the same value at 50× the rate. Did `status=failed` go
from 0.5% of events to 8% after the incident started? Did a service's
heartbeat vanish entirely?

**Why it's useful — and why it isn't the frequency detector:** the frequency
detector compares *absolute counts per time bucket* against a z-score band —
it fires when a series gets louder or quieter in some bucket. Proportion shift
compares a value's *share of the window's events*, whole window against whole
window. Three consequences:

- A value whose rate triples but is *spread evenly* across the suspect window
  never breaches z in any single bucket — frequency misses it; proportion
  shift is built for exactly that.
- Because it tests shares, a benign global volume change (more users, verbose
  logging turned on) flags nothing by itself — every value's share is
  unchanged. Frequency, comparing absolute counts, would flag everything.
- In temporal mode, frequency's top findings are dominated by series with zero
  baseline activity (scored against a floored standard deviation) —
  effectively re-reporting "first seen". Proportion shift deliberately
  excludes those (see next paragraph), so its findings are the genuinely new
  signal neither existing detector surfaces.

**Why it isn't value novelty:** temporal value novelty flags values **absent**
from the baseline (`baseline_cnt = 0`). Proportion shift requires
`baseline_cnt ≥ 1` by construction — the two detectors partition the space:
"never seen before" belongs to value novelty, "seen before but its rate
changed" belongs here. No finding appears in both.

**Temporal-only.** A share can only "shift" between two populations, so this
detector has no self-baseline mode — it always needs a
[baseline definition](#baseline-definitions-suspect-windows-and-the-normality-model).
Without one it reports `insufficient_data` with a warning rather than guessing.

### The test: a 2×2 G-test per (value, suspect window)

For each candidate value and each suspect window, build the 2×2 table: how
many baseline events are this value vs. not, and how many suspect-window
events are this value vs. not. The **G-test** (log-likelihood ratio test,
Dunning 1993 — the standard test for "is this term significantly more frequent
in corpus B than corpus A", built precisely for skewed count data with rare
values) asks: *how surprised should we be by this table if the value's share
hadn't actually changed?*

```
G = 2 · Σ over the four cells of  observed · ln(observed / expected)
p = P(χ²₁ ≥ G)      (computed exactly via erfc(√(G/2)) — no scipy needed)
```

The **score is the G statistic** — evidence strength, same ranking role as
surprise/z elsewhere. `direction` says which way the share moved (`up` = more
frequent in the suspect window, `down` = less). A value **present in the
baseline but absent from a suspect window is a maximal "down"** — a classic
tampering/silencing signature (log source disabled, service killed). Its rate
ratio uses Haldane–Anscombe +0.5 smoothing (so the ratio is finite); the test
itself always uses the raw counts, and its representative event is the value's
*last baseline occurrence* (`details.last_seen_baseline`).

### Multiple testing: Benjamini–Hochberg FDR

One run performs one test per value per suspect window across up to 15 fields
— easily thousands of tests. At p < 0.05, ~5% of perfectly normal values would
look "significant" by chance alone; without correction the findings list would
be mostly noise. All tests in a run are therefore corrected together with the
**Benjamini–Hochberg procedure**, and each finding carries its adjusted
`q_value`. Read q as: *of everything this run flagged, at most about this
fraction is expected to be a false alarm* — q ≤ 0.05 (the default,
`TS_STAT_SHIFT_FDR_Q`) means at most ~5% of the flagged set, not 5% per test.

### The effect floor: significant ≠ meaningful

On a 100M-event baseline, a shift from 1.00% to 1.02% is overwhelmingly
"significant" — and completely uninteresting. A finding therefore also needs
the share to change by at least a minimum **rate ratio** (default 2×, either
direction; `TS_STAT_SHIFT_MIN_RATIO`). Both thresholds are echoed in every
finding's `details` (`q_threshold`, `min_ratio`, `m_tests`) and snapshotted
into the persisted `DetectorRun`, so a run stays reproducible after the
defaults change.

### The candidate cap, honestly

Per field, ClickHouse returns at most `TS_STAT_SHIFT_MAX_CANDIDATES_PER_FIELD`
(default 2000) candidate values, highest total volume first — a power-based,
direction-neutral ordering (low-volume values almost never had the statistical
power to reach significance anyway). The BH correction runs over exactly the
tests performed, so when a field hits the cap the test count `m` is understated
for that field; the run attaches a warning saying so rather than hiding it.
Treat marginal q-values on a capped field as exploratory.

### Caveats

- **Events are not independent.** Log events arrive in bursts, retries, and
  sessions; the G-test assumes independent observations, so p-values (and
  therefore q-values) are somewhat overconfident. Treat q as a *ranking aid*
  backed by a principled correction, not an exact false-positive probability —
  same honesty note as the z-score's normality assumption.
- **Composition effects.** Shares must sum to 1: if one source goes quiet in
  the suspect window, every *other* value's share rises mechanically. A page
  of correlated "up" findings across unrelated fields often means one thing
  went silent — check the "down"/vanished findings first.
- A shifted share is not malicious by itself — a deploy, a crawler, or a
  config change all shift proportions. Rank for triage, as everywhere.
- **Fields:** same categorical auto-selection as value novelty
  (identifier-like fields have no repeating shares to test); override via the
  Fields picker. First-seen exclusion (`baseline_cnt ≥ 1`) is enforced in SQL.

---

## 8. Interval cadence (arrival-rhythm changes between windows)

**What it answers:** "Did something that arrived on a *clock* stop arriving —
or start?" A host beacons to its collector every 60 seconds; a cron job fires
hourly; an agent heartbeats. Proportion shift (detector 7) sees a value's
*share* of events; it cannot see *timing*. Interval cadence learns each value's
inter-arrival rhythm in the baseline and flags two things:

- **A regular value that breaks rhythm** — the 60-second heartbeat that goes
  missing (a killed agent, a suppressed log source) or suddenly runs 6× hot.
  The extreme case — the value goes fully **silent** in the suspect window — is
  the per-value silence signal (formerly roadmap item D6, now merged here).
- **A bursty value that becomes suspiciously regular** — *beaconing*. Traffic
  to one destination that was sporadic in the baseline but arrives every 60
  seconds ± a jitter in the suspect window is the canonical C2-callback
  signature. No count-based or share-based detector can see this: the volume
  and the share can both be unremarkable while the *spacing* screams.

**Why it isn't the frequency or proportion-shift detector:** both of those
compare *how many* (absolute counts, or share of the window). Interval cadence
compares *how evenly spaced*. A heartbeat that slips from 60 s to 90 s barely
moves its count and not at all its share, but it is a rhythm break; a beacon
train has the same modest volume as the baseline's sporadic traffic but a
radically different regularity. This detector owns the *spacing* axis the way
proportion shift owns the *magnitude* axis.

**Why it isn't value novelty:** like proportion shift, it requires
`baseline_cnt ≥ 1` — a value must exist in the baseline to have a learned
cadence. First-seen values belong to value novelty; no finding appears in both.

**Temporal-only.** Cadence can only *change* between two populations, so there
is no self-baseline mode — it always needs a
[baseline definition](#baseline-definitions-suspect-windows-and-the-normality-model).
Without one it reports `insufficient_data`.

### Two tests, one per direction, gated on baseline regularity

The rhythm is summarized per value per window by the **coefficient of
variation** of its inter-arrival gaps (CV = standard deviation ÷ mean). CV ≈ 0
is a metronome; CV ≈ 1 is a memoryless (Poisson) process; CV > 1 is bursty.
Which test a value gets is decided *entirely by its baseline CV*, so the
suspect window never selects its own test:

- **Regular baseline** (CV ≤ 0.5, and at least 5 inter-arrival gaps learned) →
  the **cadence-break test**. A two-sample Poisson-rate likelihood-ratio test
  compares the value's arrival *rate* in the baseline vs. the suspect window,
  using each window's duration as the exposure:

  ```
  Given a events over baseline seconds d_b and c events over window seconds d_w,
  under "same rate": E_a = (a+c)·d_b/(d_b+d_w),  E_c = (a+c)·d_w/(d_b+d_w)
  G = 2·[ a·ln(a/E_a) + c·ln(c/E_c) ]        p = P(χ²₁ ≥ G)   (erfc, no scipy)
  ```

  `direction` is `missed` (rate dropped; `count = 0` is the maximal case, and
  its representative event is the last baseline occurrence,
  `details.last_seen_baseline`) or `accelerated` (rate rose). This test is
  **conservative for genuinely periodic values** — their counts vary *less*
  than Poisson assumes — so a flagged deficit is at least as significant as its
  p-value says.

- **Bursty or sparse baseline** (CV ≥ 0.8, or fewer than 5 baseline gaps) →
  the **beaconing test**. Greenwood's spacing statistic asks whether the
  suspect window's arrivals are *too evenly spread* to be random. Normalize the
  gaps by the value's active span `S` (last − first arrival in the window):

  ```
  G = Σ (gap/S)²   over the N window gaps
  Under "random arrivals":  E[G] = 2/(N+1),  Var[G] = 4(N−1)/((N+1)²(N+2)(N+3))
  z = (G − E[G]) / √Var[G]     p = Φ(z)   (left tail = suspiciously regular)
  ```

  Only the *left* tail is scored — burstiness (the right tail) is the frequency
  detector's territory. Needs at least 10 window gaps before the normal
  approximation is trusted. `direction` = `new_regularity`.

The CV band **0.5 < CV < 0.8 is a deliberate dead zone** — those values are
neither clearly periodic nor clearly bursty, and get no test.

**Score = −log10(p)** for both directions, so the two different statistics rank
on one comparable scale (this differs from proportion shift, whose score is the
raw G).

### Multiple testing and effect floors

Every test in a run — both directions, all fields × values × windows — shares
one **Benjamini–Hochberg** FDR pool; each finding carries its adjusted
`q_value` (read q exactly as for proportion shift). Significance alone never
flags — each direction adds an effect floor:

- Cadence break: the arrival **rate must change by at least `min_ratio`×**
  (default 2, `TS_STAT_INTERVAL_MIN_RATE_RATIO`; Haldane +0.5 smoothing on a
  zero count for the ratio display only).
- Beaconing: the window CV must be **≤ 0.3** (`TS_STAT_INTERVAL_BEACON_CV_MAX`
  — a real cadence, not merely "eviction-order regular") **and** the active
  span must cover **≥ 50%** of the window (`TS_STAT_INTERVAL_BEACON_MIN_SPAN`),
  so a short dense burst of eleven evenly spaced events never reads as
  beaconing.

Thresholds are echoed into every finding's `details` and snapshotted into the
persisted `DetectorRun`, so a run stays reproducible after the defaults change.

### The candidate cap, honestly

Same treatment as proportion shift: per field ClickHouse returns at most
`TS_STAT_INTERVAL_MAX_CANDIDATES_PER_FIELD` (default 2000) values, highest total
volume first; when a field hits the cap the BH test count is understated for
that field and the run attaches a warning. Treat marginal q-values on a capped
field as exploratory.

### Caveats

- **Inter-arrival gaps are computed strictly within one window** — the
  `lagInFrame` that produces each gap is partitioned by (value, window index),
  so a gap can never straddle the baseline/suspect boundary and corrupt both
  windows' statistics. A value's first arrival in each window has no predecessor
  and contributes no gap (that is why a regular value needs ≥ 5 baseline gaps,
  i.e. ≥ 6 baseline arrivals, to be tested).
- **The Greenwood normal approximation is rough for small N** — mitigated by
  the 10-gap floor, but a beaconing q near the threshold on a short train is
  weaker evidence than the same q on a long one.
- **Events are not independent** (bursts, retries) — as everywhere, q is a
  ranking aid backed by a principled correction, not an exact false-positive
  probability.
- A broken or new rhythm is **not malicious by itself** — a scheduled
  maintenance window silences heartbeats; a new monitoring agent legitimately
  beacons. Rank for triage.
- **Fields:** same categorical auto-selection as value novelty; override via
  the Fields picker. First-seen exclusion (`baseline_cnt ≥ 1`) is enforced in
  SQL.

---

## 9. Event sequences (never-seen orderings)

**What it answers:** "Did events start happening in an *order* never seen
before?" The AMiner `EventSequenceDetector` analog (roadmap D8). A login, a
privilege change and a log clear may each be individually common — value
novelty sees nothing — but the three in that order, back to back, may never
have happened in the baseline. This detector owns the *ordering* axis the way
proportion shift owns magnitude and interval cadence owns spacing.

**How it works.** Per source, events are ordered by (effective) timestamp —
with record-order tie-breaks (`byte_offset`, `line_number`, `event_id`) so the
ordering is deterministic — and every run of **n consecutive values** of one
grouping field (default `artifact`, n = 3) forms one **n-gram**. An n-gram
that occurs in a suspect window but **never in the baseline window** is
flagged, once per suspect window it appears in.

Sequences are assembled entirely in SQL: a `lagInFrame` chain over a window
`PARTITION BY source_id, window-index`, so

- an n-gram never mixes events from different sources,
- an n-gram never spans a window boundary or the gap between windows (all n
  events sit inside one window), and
- the whole run is reproducible from the recorded queries — no Python-side
  sequence assembly.

Every scan runs **once per source** — window-function sorts cannot spill to
disk (see [query-cost discipline](#query-cost-discipline-all-statistical-detectors)),
so the sort must be bounded by one source. Counting stays case-wide:
per-source counts are summed per window in Python, and — because each
source's scan can only rule out its *own* baseline — a cross-source
verification pass drops any candidate n-gram that occurs in **any** source's
baseline window.

**Temporal-only.** "Never seen before" needs a before — there is no
self-baseline mode. Without a
[baseline definition](#baseline-definitions-suspect-windows-and-the-normality-model)
it reports `insufficient_data`; likewise when the baseline window holds no
complete sequence of length n.

**Score = −log(count / window_ngram_total)** — the same surprise scale as
value novelty, but the denominator is the suspect window's own count of
*complete n-grams* (not its event count; the first n−1 events of each
(source, window) run contribute no complete n-gram). The representative event
is the **first** event of the n-gram's earliest occurrence in the window. A
suspect window with fewer than 50 complete n-grams gets a `warnings` entry.

**Parameters.**

- `ngram_size` (request) / `TS_STAT_SEQUENCE_NGRAM` (server default 3, the
  AMiner default sequence length) — validated 2–5; the effective n is
  snapshotted into the persisted `DetectorRun`.
- `series_field` (request, default `artifact`) — the single grouping field the
  sequence is built over (shared with the frequency detector's group-by; not
  the multi-field `fields` picker). Any field token works, including
  `attr:<key>` and mapped canonical fields.
- `TS_STAT_SEQUENCE_MAX_CANDIDATES` (default 2000) — cap on novel n-grams
  fetched per run, lowest suspect volume (rarest) first; hitting it attaches a
  warning.

**Allowlist key:** `(series_field, "a → b → c")` — the finding's `value` is
the " → "-joined n-gram, so **Mark normal** suppresses that exact ordering on
every event.

### Caveats

- **Interleaved multi-writer sources.** A source whose records interleave many
  independent streams (one syslog file carrying fifty hosts) produces
  n-grams that cross stream boundaries — a "new sequence" may be two unrelated
  streams shuffling differently. Choose a grouping field that is meaningful
  across the whole source, or scope the timeline to per-stream sources. A
  per-stream secondary partition field is a possible follow-up, deliberately
  not implemented yet.
- **Tiny baselines make everything novel.** A baseline with few complete
  n-grams vouches for almost nothing; the <50-n-gram warning fires, and n = 4
  or 5 on a short baseline mostly measures the baseline's poverty. Prefer
  longer baselines for larger n.
- **Order is judged per source on the corrected timeline** — per-source clock
  skew offsets (W2) shift a source's events uniformly, so intra-source order
  is invariant, but which *window* an event falls in follows the corrected
  timestamp.
- A new ordering is **not malicious by itself** — a software update legitimately
  changes startup sequences. Rank for triage.

---

## 10. Value-distribution drift (whole-field shape changes between windows)

**What it answers:** "Did this field's *mix of values* change between the
known-normal period and the window I'm investigating?" Response sizes that
quietly doubled, a status-code mix that tilted from 200s toward 500s, a
user-agent population that gained a new majority — none of these is one rare
value (value novelty), one value's share (proportion shift), or a volume
change (frequency). The unit of finding here is the **field**, not a value:
one finding says "this field's whole distribution is different in this
window." Adapted from AMiner's `VariableTypeDetector`, reduced to two
field-agnostic tests.

**Why it isn't proportion shift:** proportion shift tests each value's share
separately and needs that single value's change to clear an effect floor. A
drift of many small shifts — every category moving a little, or a numeric
field's whole curve sliding — never produces one significant value, but the
aggregate shape change is exactly what a whole-distribution test sees.
Conversely, one value spiking hard is proportion shift's territory and will
usually fire both; the drift finding then names the same culprit in
`top_contributors`.

**Why it isn't numeric range:** the range detector flags individual events
outside a learned band. A distribution can drift substantially while every
single value stays inside the old min/max (e.g. the median doubling within an
unchanged range). Drift tests the population, range tests the outliers.

**Temporal-only.** A distribution can only drift between two populations, so
there is no self-baseline mode — it always needs a
[baseline definition](#baseline-definitions-suspect-windows-and-the-normality-model).
Without one it reports `insufficient_data` with a warning rather than
guessing.

### Two tests, one per field kind

The branch is chosen **syntactically** (never by field meaning, per the
field-agnostic rule):

- **Numeric fields** (≥ 90% of non-empty values parse as numbers — the same
  probe the numeric-range detector uses): a **two-sample Kolmogorov–Smirnov
  test**, computed *inside ClickHouse* via the
  `kolmogorovSmirnovTestIf('two-sided')` aggregate over `toFloat64OrNull`
  values — baseline sample vs. suspect-window sample, one conditional
  aggregate per suspect window, one scan per field. The KS statistic **D** is
  the largest gap between the two cumulative distribution curves — directly
  readable as "at least D of the probability mass sits on a different side of
  some threshold." The finding's `direction` (up/down) comes from the median
  shift, and its representative event is the window's most extreme value in
  the drifted direction (`argMax`/`argMin` in the same scan).
- **Categorical fields** (the novelty recommender's categorical class): a
  **k-category G-test** — the 2×k generalization of proportion shift's 2×2 —
  over the top **50** baseline categories plus one exact `__other__` bucket
  (folded in Python from the full GROUP BY, so no mass is dropped;
  `details.k_truncated` says whether folding happened). The p-value uses the
  chi² survival function with df = buckets − 1, computed with a pure-`math`
  regularized incomplete gamma (`_chi2_sf`) — no scipy, airgap-safe. The
  finding carries `top_contributors`: the ≤ 5 categories with the largest
  share change, which is usually the whole story. The representative event is
  the most-shifted category's first window occurrence (or its last baseline
  occurrence if it vanished).

Auto field selection blends both recommenders — numeric-recommended fields to
the KS branch, remaining categorical fields to the G branch — under the usual
15-field cap; the Fields picker overrides, and explicitly picked fields are
branch-classified by the same numeric-ratio probe. The classification probe
is restricted to the baseline + suspect-window union: the drift tests only
ever read windowed rows, so classifying fields never pays a whole-case scan.

### Multiple testing and the effect floors

Every (field × suspect window) test from **both branches** lands in one
Benjamini–Hochberg pool (`TS_STAT_DRIFT_FDR_Q`, default 0.05) — same q-value
reading as proportion shift. Because both branches must rank on one scale and
their raw statistics live on different scales (D vs. G), the **score is
`−log10(p)`**, the interval-cadence convention.

Significance alone never flags: each branch has its own effect floor, both in
the *fraction-of-probability-mass* family so one intuition covers both —
numeric findings need **D ≥ 0.1** (`TS_STAT_DRIFT_MIN_KS_D`), categorical
findings need a **total-variation distance ≥ 0.05** (`TS_STAT_DRIFT_MIN_TVD`;
TVD = 0.5·Σ|share difference|, the categorical analog of D). Sides with fewer
than `TS_STAT_DRIFT_MIN_SAMPLES` (20) field-bearing events are skipped
entirely — excluded from the FDR pool, with a warning — rather than tested on
noise. Effect floors are server config only; the request can override `fdr_q`
but the floors' units are branch-specific.

Findings are per field, so the disposition/allowlist key is `(field, "*")` —
"this field's drift is expected" suppresses the field, not one value.

### Caveats

- **Heavy ties make KS conservative.** The KS test assumes continuous data;
  log-derived numerics (ports, sizes, durations rounded to seconds) are full
  of ties, which *lowers* the true significance of a given D — flagged
  findings are at least as real as their p-value claims, but subtle drifts in
  very tied fields may be missed.
- **Composition effects, again.** Shares sum to 1: one source going quiet
  tilts every categorical mix it participated in. A page of categorical drift
  findings across unrelated fields often has one silence at the root — read
  `top_contributors` before treating each finding as independent.
- **`__other__` is a bucket, not a value.** When a field has more than 50
  baseline categories, changes *inside* the folded tail partially cancel;
  the test is exact for the named categories and conservative for the tail.
- Events are not independent (bursts, retries, sessions) — the same
  p-values-are-overconfident honesty note as every other significance test
  here. Rank with q, verify by looking.

---

## 11. Semantic similarity search

Not a statistical anomaly detector — no baseline, no score threshold, no
z-score — but it lives in the same Analysis tab and Method panel, so it's
documented here for completeness.

**What it answers:** "Show me other events that read like this one, even if
the wording differs." Useful for finding variants of a known-bad log line, or
building intuition about a cluster of related events, when exact-match or
keyword search would miss paraphrased or differently-formatted duplicates.

**How it works:** at embed time, selected fields of each event are encoded
into a 384-dimension vector by a sentence-embedding model (default
`all-MiniLM-L6-v2`) and stored in Qdrant, one vector per event, normalized to
unit length. A search computes **cosine distance** between the query vector
and every candidate — for unit-length vectors this reduces to `1 − dot
product`, which is why the code skips renormalizing at search time. Results
are ranked by similarity (closer = more alike).

**Why it's explainable, not a black box:** the exact field selection used to
build the embedding is hashed into an `embedding_config_hash`. Two runs with
the same config hash are guaranteed to have used the same fields and model —
reproducible for forensic citation. Changing which fields get embedded
changes the hash and therefore starts a fresh Qdrant collection; old and new
embeddings are never silently mixed.

**Caveat:** semantic similarity is a *relevance* signal, not a *rarity* or
*anomaly* signal — it will happily return 50 near-identical "similar" events
if that's what's in the data. Use it to build context around a suspicious
event, not as a standalone anomaly detector.

---

## Reality check (2026-07)

This document was written alongside an audit of every statistical detector's
implementation against its own module docstring, its Method-tab description,
and its formula's mathematical soundness. Findings:

- **Confirmed correct:** the surprise score (`−log(count/total)`), the
  self-baseline rarity floor semantics, the temporal "absent from baseline,
  present in detect" semantics, the field cardinality classifier, the
  leave-one-out variance formula (`(Σx² − n·mean²)/(n−1)` computed over the
  n−1 remaining points — standard sample-variance algebra, verified by hand),
  and the shared bucket-interval formula between the histogram and the
  frequency detector.
- **Bug fixed:** `FrequencyView.tsx`'s self-baseline explanation text said the
  per-window "expected" baseline *includes* the flagged window itself. The
  backend does the opposite on purpose (leave-one-out, to avoid a spike
  suppressing its own detection) — the UI copy had drifted out of sync with
  the implementation. Corrected in both `FrequencyView.tsx` and
  `MethodologyPanel.tsx`.
- **Bug fixed:** `ValueNoveltyView.tsx`'s footer note unconditionally
  mentioned the "rarity floor," even in temporal mode, where the backend
  explicitly ignores the rarity floor entirely. Copy now branches on the
  active mode.
- **Bug fixed:** frequency-finding severity color bands (low/medium/high)
  were hardcoded at fixed |z| constants (3, 5) regardless of the analyst's
  chosen `z_threshold`. Raising the threshold above 5 made every returned
  finding read as "high" trivially; lowering it below 3 made every finding
  read as "low" regardless of how extreme relative to the chosen cutoff.
  Severity now scales off the active threshold.
- **Known limitation, not fixed (documented above instead):** z-scoring's
  normality assumption is shaky for short (3–5 bucket) or low-count series,
  and the Method tab doesn't say so. Flagged in this doc's z-score section
  rather than papered over — the fix is honest documentation, not a code
  change, since the alternative (a different statistical test per series
  length) would be real added complexity for a niche edge case.

## Explicit baseline + suspect windows (2026-07)

The single-`baseline_end` split point was replaced by explicit
[baseline definitions](#baseline-definitions-suspect-windows-and-the-normality-model):
a named baseline window plus 1..N labeled suspect windows per timeline, marked
on the histogram. Every temporal detector now scores each suspect window
against the baseline with per-window statistics (surprise denominators are the
suspect window's own event count; frequency derives its bucket interval from
the baseline and excludes partial/edge buckets), attributes each finding to its
window, and warns on windows too small to score. The old whole-corpus surprise
denominator and whole-timeline frequency buckets — both of which overstated
significance when the analysis covered only part of the timeline — are gone.
Per-event "mark normal" was unified into the value-level detector allowlist
(roadmap D11); the legacy `normal` annotation was still honored but no longer
created outside timestamp-order findings. Schema for both new tables is managed
by Alembic (`src/tracesignal/db/migrations`), which this change also adopted.

## Unified disposition taxonomy (2026-07)

The remaining fragmentation — `detector_allowlist` table, per-event `normal`
annotation (the timestamp-order fallback), and the `pinned` flag on system
annotations — was replaced by the single `finding_dispositions` table
(migration `0004`, which moves all legacy rows) and the audited
`/dispositions` endpoints. See
[the normality model](#baseline-definitions-suspect-windows-and-the-normality-model)
for the taxonomy (`normal` / `dismissed` / `confirmed`). Two behavior gaps
closed with it: per-event normality is now audited and hashed into
`DetectorRun.params` (`dispositions_hash` replaces `allowlist_hash`), and
"hide as noise without blessing it into the baseline" exists at all
(`dismissed`, with an explicit `dismissed_count` so nothing is silently
hidden). Annotation types tightened to `tag`/`comment` (user) and `anomaly`
(system); the `/allowlist` endpoints are gone.
