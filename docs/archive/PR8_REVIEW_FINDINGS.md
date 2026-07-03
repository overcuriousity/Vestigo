# Review: PR #8 — per-value histograms and visualization page

*Reviewed 2026-07-02 against branch `feat/viz-histograms-and-visualization-page` (PR #8, base
`main`). 8 finder angles (3 correctness, 3 cleanup, 1 altitude, 1 CLAUDE.md conventions), key
correctness candidates independently re-verified against the code.*

The PR adds a large new frontend viz module (9 D3-based chart types, shared axis/tooltip/legend
primitives, export/color/stats libs, a `VisualizePage`, and a per-field histogram modal) plus
three new backend aggregation endpoints (`field-terms`, `field-numeric`, `field-timeseries`)
built on new `EventQueryService` methods in `db/queries.py`. The PR is purely additive — no
existing behavior, tests, or CSS rules were removed, and no CLAUDE.md rule violations were
found. Verification did surface real correctness bugs, mostly at the edges of the new
aggregation queries and the D3 scale/color logic, plus a set of cleanup opportunities.

**Status: fully resolved.** 5 of 7 correctness bugs fixed 2026-07-02; bugs 6–7 plus design
items 8/10/12/13 fixed 2026-07-03; the remaining design items 9/11/14/15/16 fixed later on
2026-07-03. Nothing from this review is open.

## Resolution summary

| # | Status | What happened |
|---|--------|----------------|
| 1 | ✅ Fixed | `field_value_timeseries` now derives `all_starts` from `[min_ts, max_ts]`/`interval` (replicating ClickHouse's `toStartOfInterval` epoch alignment in Python) instead of from the sparse query result — quiet buckets are zero-filled, not dropped. Added `test_field_value_timeseries_zero_fills_buckets_with_no_top_value_events` covering a bucket with zero events across every top-N value. |
| 2 | ✅ Fixed | Added `TOP_LEVEL_NON_STRING_COLUMNS` (`db/_columns.py`) and had `_field_column_expr` wrap those columns (`timestamp`) in `toString(...)` before the caller does string comparisons/grouping — `field_terms`/`field_value_timeseries` on `field=timestamp` no longer throws a ClickHouse type error. Added `test_field_terms_on_timestamp_column_casts_to_string`. |
| 3 | ✅ Fixed | Added `numericDomain(min, max)` to `lib/stats.ts` — pads a degenerate (`min === max`) domain by a small symmetric epsilon instead of leaving it zero-span. Applied to `NumericHistogram`, `EcdfChart` (previously unguarded), and also `BoxPlot`/`ViolinPlot` (their existing `.nice()` call turned out **not** to fix a zero-span domain either — confirmed by reading d3-array's `nice()` source, which early-returns `[start, stop]` unchanged when the tick step is 0 — so those two had the identical latent bug). Added `numericDomain` unit tests in `vizStats.test.ts`. |
| 4 | ✅ Fixed | `buildSeriesColorMap` now folds any series past the 8-slot categorical palette into the neutral `OTHER_COLOR` instead of wrapping `% 8` and silently reusing an earlier series' hue. |
| 5 | ✅ Fixed | Bucketed under the same code change as #4: `buildSeriesColorMap` now takes `(string \| {key, isOther})[]` and a reserved sentinel `OTHER_KEY` (a NUL-prefixed string no real ClickHouse value can produce) identifies the synthesized "outside top-N" row structurally, not by comparing display text to the literal string `"Other"`. `BarChart`/`PieChart` rows now carry a `key` distinct from `label`, used for the `scaleBand`/`pie` domain, React `key`, and color-map lookups — a real field value literally named `"Other"` no longer collides with the synthesized bucket. Extended `vizColors.test.ts` with folding and non-collision cases. |
| 6 | ✅ Fixed | `AxisBottomBand` grew a `maxLabelChars` prop (default 14) plus density-aware label thinning (every band keeps its tick, only every Nth gets a label). `Heatmap` now feeds it an adaptive **UTC** tick formatter (time-only within one day, month-day within one year, full date otherwise) with `maxLabelChars={17}`. While here: `Heatmap`/`LineChart`/`TimeHistogram` all formatted ticks/tooltips with `timeFormat` (browser-local) while labeling them "UTC" — switched to `utcFormat`. |
| 7 | ✅ Fixed | New shared `lib/download.ts` with `sanitizeFilename` (strips `/ \ : * ? " < > \|` + control chars, collapses runs, falls back to "download") applied inside `triggerDownload`, used by both chart exports and event exports. Covers design item 12 too — the duplicated blob-download helpers in `viz/lib/export.ts` and `api/export.ts` are now one implementation (with the DOM-attach Firefox/Safari fix). Tests in `download.test.ts`. |

## Design-item resolution (2026-07-03)

| # | Status | What happened |
|---|--------|----------------|
| 8 | ✅ Fixed | The numeric probe query is now gated: it runs once per field change (scale auto-suggestion, tracked via `autoProbedField` state) and while a numeric chart is displayed — no longer on every bins change on a terms chart. |
| 9 | ✅ Fixed | `field_terms` fused into one scan — the GROUP BY now carries pre-LIMIT totals via window aggregates (`sum(count()) OVER ()` for the event total, `count() OVER ()` for distinct-values-as-group-count). Pre-LIMIT semantics verified against live ClickHouse (LIMIT 2 over 3 groups returns the full total/distinct on every row). This also removes the wasted totals scan inside `field_value_timeseries`'s `field_terms` call (3 scans → 2). `field_numeric_stats` deliberately keeps two scans — bin edges depend on the first scan's min/max; documented in its docstring. |
| 10 | ✅ Fixed | `VisualizePage` clamps the shared `topN` via `effectiveTopN = min(topN, maxTopN)` per data kind — request, slider position, and label always agree after a chart-type switch. |
| 11 | ✅ Fixed | `_field_column_expr` is now the single implementation (grew a lazy `param_name: str \| Callable` and a `cast_non_string` flag); `_ParameterizedQueryBuilder._column_expr` is a one-line delegate passing its bound `_param_name`, so `pN` numbering is unchanged. |
| 12 | ✅ Fixed | Folded into bug 7 (shared `lib/download.ts`). |
| 13 | ✅ Fixed | New `serializeEventFilterParams` in `lib/queryParams.ts` (scalar fields + JSON-stringified `filters`/`exclusions`); `api/viz.ts` (3×) and `api/events.ts` (2×) now use it instead of hand-rolled stringify blocks. |
| 14 | ✅ Fixed | New `primitives/useChartRef` + `primitives/ChartEmptyState` replace the identical ref-fallback and empty-state markup in all 9 charts; `lib/pointer.ts::svgLocalPoint` extracts the `getBoundingClientRect`+margin math from the 3 hover-strip charts. Deliberately no `useChartHover` — it would wrap a bare `useState`, and `LineChart`'s index-based hover state genuinely differs. |
| 15 | ✅ Fixed | The chartType-validity sync effect (with its exhaustive-deps suppression) is gone — the clamp happens in the scale radio's change handler, sharing one module-level `chartTypesFor(scale)` helper with `availableChartTypes`. |
| 16 | ✅ Fixed | New unfiltered `GET .../viz/fields` endpoint (`{fields:[{token,distinct,coverage}]}`, coverage-desc/token-asc) backed by `StatisticalAnomalyService.field_inventory` — the raw enumeration extracted out of `recommend_novelty_fields`, which now just layers classification on top (output unchanged). `VisualizePage` switched to it; default field is now the highest-coverage one. `AnomalyFieldPicker`/`FrequencyView` keep the anomaly endpoint. |

Verified after each fix: backend `uv run pytest` (274 passed) and `uv run ruff check .` clean;
frontend `npm run test` (59 passed), `npm run typecheck`, and `npm run lint` clean (lint's 3
warnings are pre-existing, unrelated to this PR).

2026-07-03 session: backend 276 passed, frontend 66 passed, ruff/tsc/oxlint clean. Bugs 6
and the new ingestion progress bar were additionally verified end-to-end against the running
app (real ClickHouse/Postgres/Qdrant, Playwright-driven UI): the heatmap axis now renders
distinct, unclipped, thinned UTC tick labels, and a 300k-event upload shows live byte-based
progress in the job tray, completing with the source list auto-refreshing to the final count.

2026-07-03 later session (items 9/11/14/15/16): backend 281 passed, frontend 72 passed,
ruff/tsc/oxlint clean (lint's 3 warnings still the pre-existing ones). The fused
`field_terms` SQL was additionally validated against the live ClickHouse instance — window
aggregate totals match the old two-scan reference exactly, including under LIMIT.

## Correctness bugs (verified against the code)

1. **`src/tracevector/db/queries.py:1151` — `field_value_timeseries` silently drops
   fully-quiet time buckets instead of zero-filling them.** `all_starts` is built only from
   `bucket_result.result_rows` (rows that had ≥1 event for a top-N value), contradicting the
   method's own comment ("filling buckets with zero rows so every series has an entry for every
   bucket"). A time interval where none of the top-N values fired is simply absent from every
   series. `Heatmap.tsx`/`LineChart.tsx` build their x-axis domain directly from the returned
   bucket starts, so a quiet interval doesn't show as a gap — it visually compresses out,
   misleading an analyst about event timing on a forensic timeline. Fix: derive `all_starts`
   from `[min_ts, max_ts]`/`interval` directly (the method already computes `interval`), not
   from the sparse query result.

2. **`src/tracevector/db/queries.py:955,967` — `field_terms`/`field_value_timeseries` can be
   called with `field=timestamp`, producing a ClickHouse type error.** The `field` query param
   (`api/routers/viz.py:122`) has no server-side whitelist. `resolve_column_token("timestamp")`
   returns the bare `DateTime64(3)` column, and `field_terms` builds
   `WHERE ... AND {col_expr} != ''` — comparing a datetime column to the empty-string literal.
   `GET .../viz/field-terms?field=timestamp` (or any bar/pie/heatmap/line chart pointed at the
   timestamp field) throws a ClickHouse parse error, surfacing as a 500 instead of a graceful
   response. Contrast with `field_numeric_stats`, which safely wraps its cast in
   `toFloat64OrNull`. Fix: either reject non-string top-level columns at the router/service
   boundary, or cast defensively like `field_numeric_stats` does.

3. **`frontend/src/components/viz/charts/NumericHistogram.tsx:51` / `EcdfChart.tsx:44` —
   degenerate x-scale for single-value numeric fields.** `scaleLinear().domain([stats.min!,
   stats.max!])` with `min === max` is a real, valid response (`field_numeric_stats` returns
   `count > 0` with `min === max` for a constant-valued field, and non-degenerate bin edges
   spanning `mn` to `mn + bin_count`). d3's `normalize()` doesn't produce `NaN` here — it falls
   back to a constant `0.5` when the domain span is 0 — but that means **every bar's x0 and x1
   map to the same horizontal-midpoint pixel**, collapsing all bars to zero width at one point.
   The chart renders as visually empty for any field where all matching events share one value
   (e.g. a filtered `exit_code`). `BoxPlot`/`ViolinPlot` call `.nice()` on their scale; these two
   don't, and neither guards the degenerate-domain case. Fix: detect `min === max` and render a
   single full-width bar/point, or pad the domain.

4. **`frontend/src/components/viz/lib/colors.ts:30-40` — `buildSeriesColorMap` doesn't fold
   9th+ series into "Other" despite the file's own header comment promising it** ("A 9th+
   series folds into 'Other' rather than generating a new hue"). `idx` increments unboundedly
   and `seriesColorVar` wraps via `index % 8`, so series index 8 reuses the exact CSS var as
   series index 0. `field-timeseries` defaults `series_limit=12`, so any field with ≥9 distinct
   top values (e.g. HTTP status codes) rendered as a line chart or heatmap shows two different
   values in the identical color, indistinguishable in the chart and legend. Fix: cap the
   series actually plotted at 8 and fold the remainder into the `Other` bucket, matching the
   documented design.

5. **`frontend/src/components/viz/lib/colors.ts:22,34` + `BarChart.tsx`/`PieChart.tsx` — the
   synthetic "Other" bucket is merged into real data by string equality with the literal
   `"Other"`.** If a charted field has an actual value that is the string `"Other"` (plausible
   for a category/status field), it collides with the synthesized "outside top-N" row in
   `buildSeriesColorMap`'s `Map` — one overwrites the other, silently merging two distinct rows
   into one with the wrong count/color. Fix: carry an `isOther: true` flag on the synthesized
   row instead of a string sentinel.

6. **`frontend/src/components/viz/primitives/Axis.tsx:76` (`AxisBottomBand`) vs.
   `Heatmap.tsx:71` — hard 13-char tick-label truncation is incompatible with `Heatmap`'s only
   real use of it.** `Heatmap` feeds `AxisBottomBand` full timestamp labels
   (`"2024-01-01 00:00 UTC"`, ~20 chars) which all truncate to an identical, unreadable
   `"2024-01-01 0…"` prefix for every hour of a given day — the time axis becomes useless on
   exactly the chart that needs it. Fix: raise/remove the truncation cap for `AxisBottomBand`,
   or give `Heatmap` a shorter label formatter.

7. **`frontend/src/components/viz/FieldHistogramModal.tsx:143` + `lib/export.ts` — exported
   filenames aren't sanitized against path separators.** `filename={`${fieldKey}_${activeValue}_histogram`}`
   is used verbatim as `a.download`. Since this is a forensic log tool, field values are
   frequently file paths (e.g. `attr:file_path` = `/var/log/audit/audit.log`, or a Windows path
   with `:`), so the export can silently land in an unexpected subdirectory (`/` in the name) or
   fail outright (`:` is illegal on Windows). Fix: strip/replace filesystem-illegal characters
   before use as `a.download`.

## Design / robustness issues (lower severity, still concrete)

8. **`frontend/src/pages/VisualizePage.tsx` — the numeric-probe query (`vizApi.fieldNumeric`)
   fires unconditionally for every field/bin change**, not gated by whether the analyst is
   actually viewing a numeric chart type — every field selection pays for two full ClickHouse
   scans (quantiles + histogram) purely to drive a one-time scale auto-suggestion.
9. **`src/tracevector/db/queries.py` — `field_terms` and `field_numeric_stats` each scan the
   filtered event set twice** (a totals/quantile scan plus a separate terms/histogram scan over
   the identical subquery), and `field_value_timeseries` calls `field_terms` purely for its
   top-values list while discarding the `total`/`distinct` scan it also performs — three scans
   where two would do.
10. **`frontend/src/pages/VisualizePage.tsx` — shared `topN` state has different valid ranges
    for terms charts (max 50) vs. timeseries charts (max 20) but isn't clamped on chart-type
    switch** — e.g. set to 45 on a bar chart, then switching to Heatmap sends `series_limit=45`
    while the slider visually clamps to 20, so the requested series count no longer matches
    what's displayed as selected.
11. **`src/tracevector/db/queries.py:188` — `_field_column_expr` is a hand-duplicated copy of
    `_ParameterizedQueryBuilder._column_expr`** (own docstring: "Mirrors ... but is deliberately
    a free function"), rather than extending the existing method. A future change to
    column-resolution logic (new alias, escaping fix) has to be applied in both places by hand.
12. **`frontend/src/components/viz/lib/export.ts:100` (`triggerDownload`) duplicates the
    blob-download pattern already in `frontend/src/api/export.ts:5` (`downloadExport`)**, and
    the two have already drifted — the viz version appends/removes the anchor from the DOM for
    Firefox/Safari compatibility, the older one doesn't.
13. **`frontend/src/api/viz.ts` re-implements the `filters`/`exclusions` JSON-stringify block
    per endpoint** instead of extending `lib/queryParams.ts::serializeEventFilterFields`, which
    was built specifically to stop this pattern from being copy-pasted (its docstring already
    names 4 prior duplicates) — this PR grows it to 7.
14. **9 chart components (`BarChart`, `BoxPlot`, `EcdfChart`, `Heatmap`, `LineChart`,
    `NumericHistogram`, `PieChart`, `TimeHistogram`, `ViolinPlot`) each hand-roll identical
    `svgRef` fallback boilerplate, empty-state markup, and hover-tooltip state wiring** — a
    shared `useChartRef`/`ChartEmptyState`/`useChartHover` would collapse ~9x duplication to one
    implementation each.
15. **`VisualizePage.tsx` — `chartType` is kept in sync with `scale` via two separate
    `useEffect`s** rather than derived with `useMemo`, making update ordering harder to reason
    about and fragile to future `CHART_META` additions.
16. **`VisualizePage.tsx` — the Visualization page's field picker reuses `/anomalies/fields`**,
    an anomaly-detection endpoint (`recommend_novelty_fields`) that excludes fields it deems
    unsuitable for novelty detection (constant/identifier/sparse). This couples an unrelated
    feature — charting — to anomaly-detection heuristics, so tuning novelty detection later can
    silently change what fields the Visualization page offers.

## Angles that came back clean

No CLAUDE.md rule violations, no removed/broken existing behavior (PR is purely additive), and
no cross-file (frontend↔backend) param/shape mismatches beyond what's listed above.
