# PR #262 review findings — `redesign/investigate-panel`

Point-in-time record of the review rounds on the Investigate redesign (analysis gate,
fingerprint cache, scope provenance, the rail and its sheet). Ten defects from the fourth
round, all resolved in session 166 unless noted, plus three from the seventh round in session
169 (see the last section). Kept in full here so `ROADMAP.md` carries pointers rather than
prose.

Verified clean at review time: `uv run ruff check .`, `npx tsc -b --noEmit`, `npm run lint`.
The `analysis_cache` payload path is JSON-safe (`_normalize_event_datetimes` stringifies
timestamps before they reach the `sa.JSON` column), `enrichment_generation`'s premise holds
(`enrichers/jobs.py` does refresh `SourceFieldStats`), and `delete_case` was correctly extended
to reap `AnalysisCache`.

## HIGH — scope provenance was inert end to end

**1. `frontend/src/hooks/useDisposition.ts` — `confirmed` verdicts never recorded a scope.**
`analysisScope` was attached to the two `dispositionsApi.create` branches — `normal` /
`dismissed` / `routine`, exactly the kinds whose `disposition_identity` *excludes* scope. The
`confirmed` branch returned early through `anomaliesApi.persistFinding`, which sent only
`{detector, content, details}`.

**2. `src/vestigo/api/routers/events.py` — `persist_anomaly_finding` could not accept one.**
`PersistAnomalyFindingRequest` had no `analysis_scope` field and the
`create_disposition(kind="confirmed", …)` call omitted it. This is the only endpoint the UI's
Confirm button reaches, so even a client that wanted to stamp the scope had no way to.

Consequence chain: every confirmed row in a real deployment carried `analysis_scope = NULL`;
`create_disposition`'s `scope_in_identity` arm always compared `None == None`, so the "two
confirmed verdicts on one finding, one per baseline" case that migration 0026 and the dedupe
change exist for could never occur; and `useScopeChange`'s `affectedVerdicts` could never count
a confirmed verdict — the one class of verdict the scope-change dialog warns about.

**Fixed:** the request model and the API client both carry `analysis_scope`; `useDisposition`
passes it on the confirmed path. Covered by
`tests/test_disposition_scope.py::test_the_confirm_button_records_the_scope_it_was_pressed_under`
and `::test_confirming_one_finding_under_two_baselines_makes_two_claims`.

## MEDIUM

**3. Re-confirming under a new scope was unreachable.** `_apply_confirmations` matched confirmed
rows by `event_id` alone, so a finding confirmed against baseline A came back `confirmed: true`
under baseline B and `FindingRowActions` rendered Pin disabled with "Already confirmed".

**Fixed:** the applier takes the request's scope. Same-scope rows badge `confirmed`; other-scope
rows set `confirmed_other_scope`, which the rail renders as a muted "confirmed elsewhere" marker
with Confirm live — which also makes good on the scope-change dialog's previously unkept promise
that out-of-scope findings "are marked so you can re-examine them". A row with no recorded scope
still badges everywhere; demoting it would silently unbadge existing evidence.

**4. The `sequence_novelty` gate violated the module's own contract.** `series_distinct >= 3`
marked the method `not_applicable` on a timeline whose series field had two distinct values.
Two values yield 2³ = 8 distinct trigrams and a rare one is perfectly scoreable, so a timeline
with exactly two artifact types — an ordinary two-source case — silently lost n-gram novelty.
The same argument applied more weakly to the `entropy` half of the `enum_only` gate: a field
with ≤5 distinct values can still hold one value far outside the learned entropy band.

**Fixed:** the series floor is 2 (only a single value is structurally barren); `entropy` is
ungated; `charset` keeps the enum gate, where the impossibility is genuinely structural.

**6. `log_template` reported a `total_findings` already truncated by `limit`.** Every other
method returns `result.total_findings`, counted before the cap, which is what the rail's count
and the "showing N of M" copy assume. A timeline with 400 distinct templates reported exactly
50, with nothing indicating anything had been cut. **Fixed:** it returns
`LogTemplatesResult.total_templates`.

## LOW

**5. `numeric_range`'s `reason_facts` quoted a denominator from a different field set.**
`"sampled": len(inputs.inventory)` counted merged-inventory tokens (capped at
`max_attr_keys=50`, plus appended canonical entries) while the numerator came from
`numeric_tokens_from_stats`, which scans every key in the raw `field_stats` payload. The UI
renders this verbatim as "0 of N sampled ≥ 90%", and checkable arithmetic is the stated point.
**Fixed:** `numeric_tokens_from_stats` returns a `NumericTokenScan(tokens, examined)` and the
gate quotes `examined`.

**7. Cache eviction was least-recently-*computed*, not LRU, contradicting its own comment.**
`_cache_put` only ran on a miss, so `computed_at` was never refreshed on a hit. **Fixed as
documentation plus a cheaper write path, not as LRU:** adding a write to the hit path would turn
`require_case_read`'s one bounded write exception into a per-request one. The comments now state
what the code does and why it is safe (one rescan, never a wrong answer); eviction is skipped on
the replace branch, and the `NOT IN (<up to 500 ids>)` delete is guarded behind a row count so it
runs only when an insert actually exceeds the cap.

**8. The plan's span probe ignored `source_offsets`.** `query_timestamp_range` was called with
the default `ts_expr="timestamp"` while every detector runs against the offset-corrected
effective timestamp, so on a timeline with a declared clock-skew correction the gate and the
frequency detector could disagree about the same data. **Fixed:** `get_analysis_plan` threads
the offsets through to `effective_ts_sql`.

**9. `analysis_gate_min_frequency_buckets` (12) was compared against seconds while the detector
buckets with `stat_frequency_buckets` (60).** Fails open, so no answer was wrong, but the two
settings were silently uncoupled and the reason string quoted the wrong number. **Fixed as
truth-telling rather than a rename** (renaming is a breaking `VESTIGO_*` change): `reason_facts`
now carries both `required_seconds` and `bucket_count`, and both `SettingSpec` help texts name
the unit.

**10. `_run_log_templates` re-resolved the timeline scope and discarded the result.** A second
`get_timeline` + `list_timeline_sources` round-trip per request, and two independently-resolved
source lists equal only by construction. **Fixed:** the caller passes `field_mappings` and
`source_offsets` down.

## UI drift from the design round (same session, not review findings)

Found while comparing the shipped surface against the mockup the redesign was chosen from:

- **The "Named techniques" group was structurally always empty.** No method carried
  `evidenceClass: "named"`, and Sigma hits never joined the rail stream, so the rail reserved
  its strongest slot for something nothing could fill — and the "Known-bad" preset shipped as
  "Evidence integrity" because it would have filtered to a guaranteed-empty list. Fixed with a
  `useSigmaFindings` hook feeding the latest completed run's per-rule hits into the group
  through `FindingGroup`'s `extraRows` slot, and the Known-bad preset restored. A row is a rule
  and its match count, because that is what a run records.
- **The finding sheet was a definition list.** The spec, the mockup and the file's own docstring
  all described a verdict sentence, an evidence figure, the query and a verdict action row;
  none had been built. All four now exist (`lib/finding-verdict.ts`,
  `components/analysis/FindingEvidence.tsx`, `querySketch` on `MethodMeta`, and a sticky footer
  reusing `FindingRowActions`), and the finding-mode knobs gained the Run button they lacked.
  Two deliberate departures from the mockup: the evidence figure renders **nothing** for shapes
  whose payload carries no second number (rare values, value combos, templates) rather than
  drawing the mockup's cadence chart from per-event timestamps the API does not return; and the
  query block is labeled "Query shape", not "the query it ran", because the detectors — unlike
  the Sigma runner — do not return compiled SQL.
- **Copy pointing at deleted surfaces.** The Tools sheet told analysts to click a search icon on
  a grid row (it is on the expanded event detail panel); `NeedsBaselinePrompt` pointed at
  "Windows & normality below" and was itself unreferenced; several docstrings still said
  "the Patterns tab". Fixed; `NeedsBaselinePrompt` deleted. `AnomalyFieldPicker` is also
  unmounted but was **kept** — the `fields`-knob roadmap item wires that exact component back in.

## Seventh review round — three defects, all in the new UI (session 169)

Reviewed against `main...HEAD` once the mute strip had landed. The backend halves of the
redesign — the fingerprint cache's key coverage, the gate's advice-not-lock property, and
`scope_identity`'s narrowing across the hit and miss paths — were re-read and held. All three
findings were in the surface itself, and each was verified failing before the fix.

**11. The Investigate sheet had no positioned ancestor.** `InvestigateSheet` renders
`absolute top-0 …` over a scrim of `absolute inset-0`, and its docstring says it is positioned
inside the grid stage — but the stage row, its parents, `ExplorerPage`'s root and `AppShell`
were all `position: static`. The only `relative` on the page is on the rail, which is the
sheet host's *sibling*; `overflow-hidden` establishes no containing block. Both elements
therefore resolved against the viewport: the sheet covered the top bar and toolbar instead of
the stage, and the full-viewport scrim swallowed every click in the application. **Fixed:**
`relative` on the stage container, with a comment saying it is load-bearing — this is the kind
of class a later tidy-up deletes as decorative.

**12. The "Known-bad" preset disclaimed the hits it was showing.** That preset is
`methods: []` by construction — it draws entirely on Sigma — so `visibleRunnable` is
necessarily empty, and the "No method applies to this data yet" state was not guarded by
`anyFindings` the way the "No findings under this scope" state above it is. With Sigma
configured and a completed run, the rail listed the rule hits and then denied them in the
next paragraph. **Fixed:** both `visibleRunnable.length === 0` states now require
`!anyFindings`, which already counts Sigma.

**13. Muting a detector mid-session left its histogram and grid marks.** The marker publisher
iterated all of `METHODS` with no `muted` check, unlike `visible`. Muting only flips the
sweep's queries to `enabled: false`, and react-query keeps serving the data it already cached
for a disabled query, so the findings stayed live for the rest of the session. The rows left
the feed and the strip said "1 muted" while every mark stayed on the timeline — the exact
contradiction of the mute's own promise, in the one scenario it exists for (the analyst mutes
`timestamp_order` *after* its findings have littered the histogram). The existing
`detectorMute.test.tsx` only covered muted-at-mount, where nothing was ever fetched.
**Fixed:** `markers` filters on `muted`, with a regression test that mutes through the strip
after the marks are on screen.

Not counted as findings, both noted for the follow-up queue: `lib/triage-coverage.ts`'s
`computeDetectorCoverage` still counts disposition *rows* per kind rather than distinct
findings, which one finding carrying two `confirmed` rows (one per baseline) now makes wrong —
harmless only because `useTriageCoverage` lost its last consumer when `TriageBurndown` was
deleted, i.e. it is dead code that should either be revived correctly or removed. And
`useMutedMethods`'s `write` callback depends on the whole mutation object, so `toggle` and
`unmuteAll` change identity every render; no correctness impact today.
