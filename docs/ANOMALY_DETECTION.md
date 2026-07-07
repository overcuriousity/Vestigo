# Anomaly Detection — Method Reference

Audience: forensic analysts using the Analysis tab. No math background assumed —
every formula is explained in plain language before the notation.

This document covers every detector actually running in the codebase today. If
a detector described here changes (formula, default, field name), update this
file and the "Method" tab copy in the same commit — see
[Reality check](#reality-check-2026-07) at the bottom for the audit that
produced this file and what was fixed as part of it.

There are seven independent analysis tools in TraceSignal:

1. [Value novelty](#1-value-novelty-rare--first-seen-values) — rare/new field values, single field or [combinations](#value-combinations-the-value_combo-variant) (ClickHouse, no ML)
2. [Frequency anomalies](#2-frequency-anomalies-volume-spikes--silences) — volume spikes/silences (ClickHouse, no ML)
3. [Timestamp order](#3-timestamp-order-out-of-order-records) — timestamps running backwards in record order (ClickHouse, no ML)
4. [Numeric range](#4-numeric-range-out-of-band-values) — numeric values outside a learned band (ClickHouse, no ML)
5. [Charset novelty](#5-charset-novelty-never-seen-characters) — values containing characters outside a field's learned character set (ClickHouse, no ML)
6. [Entropy outliers](#6-entropy-outliers-random-looking-values) — values whose character entropy falls outside the field's learned band (ClickHouse, no ML)
7. [Semantic similarity search](#7-semantic-similarity-search) — "find events like this one" (embeddings + Qdrant)

The first six are **statistical detectors**: pure counting and arithmetic over
already-ingested events, no machine learning, no network calls, work the
instant ingestion finishes. The seventh needs an explicit embedding step first.

Code: `src/tracesignal/db/anomaly_stats.py` (detectors 1–6),
`src/tracesignal/db/similarity.py` (detector 7). UI: `frontend/src/components/analysis/`.

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
- **`_HEAVY_SCAN_SETTINGS` on every whole-corpus scan** (`max_threads = 8`,
  `max_bytes_before_external_group_by = 4 GB`, `max_memory_usage = 12 GB`):
  large GROUP BY states spill to disk, a runaway query fails alone instead of
  taking the server with it, and concurrent panel scans can't oversubscribe
  the box. Any new detector query that touches the whole corpus must carry it.
- **No-timestamp events are stored as a sentinel, not NULL.** `timestamp` is
  a non-Nullable sort-key column; events without a parseable timestamp carry
  `2299-12-31 23:59:59.999 UTC` (`db/_dt.py NULL_TS_SENTINEL`) and are
  presented as `null` by the API. Every aggregate/bucket over `timestamp`
  must exclude them via `TS_NOT_SENTINEL_SQL` — exactly where the old
  Nullable schema used `timestamp IS NOT NULL`.

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
| **Temporal** | Value is absent before a split timestamp but present after it | Incident response — "what showed up *after* the incident started that wasn't there before" |

### Self-baseline mode

Count how many times each value of a field occurs across every event in
scope. Any value occurring `rarity_floor` times or fewer (default: 3) is
flagged. The **rarity floor is a count, not a percentage** — on a
10-event timeline, "≤ 3 occurrences" is not rare at all; on a 10-million-event
timeline it very much is. Analysts should sanity-check the floor against
timeline size, or switch to temporal mode when investigating a specific
incident window rather than tuning the floor.

### Temporal mode

You (or the UI, defaulting to the timeline's midpoint) supply a split
timestamp. Everything before it is the **baseline window**; everything at or
after it is the **detect window**. A value is flagged only if it appears **zero
times in the baseline** and **at least once in the detect window** — i.e.,
genuinely new activity after the split, not just uncommon activity in
general. The rarity floor is **ignored entirely** in this mode — it's a
different question (first-seen vs. rare-overall), not a stricter/looser
version of the same one.

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

Auto-selection is capped at 15 fields per scan (`_MAX_AUTO_SCAN_FIELDS`) —
each field is a separate sequential ClickHouse query, so an uncapped
recommendation set (up to ~54 candidate fields) could turn one panel-open
into dozens of round-trips. The highest-coverage recommended fields win the
cap; you can always override with an explicit field list via the Fields
picker to scan something the auto-selector skipped.

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
| **temporal-z-score** | Only buckets before the baseline/detect split | Incident response |

**Self-baseline uses leave-one-out scoring.** Each bucket is compared against
the mean/std of *every other* bucket in that series — not against a mean/std
that includes itself. This matters: if you included the spike bucket in its
own baseline, a big enough spike would drag the mean and standard deviation up
enough to make itself look *less* anomalous, potentially hiding the very
event you're looking for. Leave-one-out avoids that self-suppression. (This
document and the in-app Methodology panel previously stated the opposite —
that the self-baseline "includes this window" — which was wrong; fixed as
part of the audit that produced this file.)

**Temporal mode** computes mean/std from the baseline window only, then scores
every detect-window bucket against that fixed, independent baseline — no
leave-one-out needed since the scored buckets were never part of the
baseline. A series with zero activity in the baseline but some activity in
the detect window is still scored (against a floored minimum standard
deviation, see below) rather than silently skipped — "this thing didn't exist
before and now it does" is exactly the kind of finding temporal mode exists to
surface.

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
| Baseline | the whole corpus | values before `baseline_end` |
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
`bytes` value in the 300-event baseline was 500; this detect-window value is
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
| Flags | values containing a character almost no other value has | detect-window values containing a character the baseline never had |

**Why self-baseline can't use the plain charset.** The whole corpus's
character set trivially contains every character in the corpus — nothing could
ever be novel (the same degeneracy as an exact min/max band in the numeric
range detector). So self-baseline mode inverts it: a character is *rare* when
it appears in at most `rarity_floor` distinct values, and any value containing
a rare character is flagged. "Of 5,000 distinct usernames, exactly one
contains a NUL byte" is precisely the finding this mode exists for.

**Temporal mode is the AMiner-faithful one:** learn the baseline window's
alphabet, flag detect-window values whose characters step outside it.

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
| Baseline population | entropies of every distinct value in the corpus | entropies of distinct values before `baseline_end` |
| Band | Tukey fence `[q1 − 1.5·IQR, q3 + 1.5·IQR]` | same fence, learned from the baseline window |
| Flags | statistical entropy outliers anywhere | detect-window values outside the baseline's band |

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

## 7. Semantic similarity search

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
