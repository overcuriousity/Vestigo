# Anomaly Detection — Method Reference

Audience: forensic analysts using the Analysis tab. No math background assumed —
every formula is explained in plain language before the notation.

This document covers every detector actually running in the codebase today. If
a detector described here changes (formula, default, field name), update this
file and the "Method" tab copy in the same commit — see
[Reality check](#reality-check-2026-07) at the bottom for the audit that
produced this file and what was fixed as part of it.

There are three independent analysis tools in TraceSignal:

1. [Value novelty](#1-value-novelty-rare--first-seen-values) — rare/new field values (ClickHouse, no ML)
2. [Frequency anomalies](#2-frequency-anomalies-volume-spikes--silences) — volume spikes/silences (ClickHouse, no ML)
3. [Semantic similarity search](#3-semantic-similarity-search) — "find events like this one" (embeddings + Qdrant)

The first two are **statistical detectors**: pure counting and arithmetic over
already-ingested events, no machine learning, no network calls, work the
instant ingestion finishes. The third needs an explicit embedding step first.

Code: `src/tracesignal/db/anomaly_stats.py` (detectors 1–2),
`src/tracesignal/db/similarity.py` (detector 3). UI: `frontend/src/components/analysis/`.

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

## 3. Semantic similarity search

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
