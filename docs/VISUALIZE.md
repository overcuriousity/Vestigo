# Visualize — the report-figure builder

Reference for the Visualize page (`frontend/src/pages/VisualizePage.tsx`,
`frontend/src/components/viz/`, `api/routers/viz.py`, `agent/chart_meta.py`). Update it in
the same commit as any change to those.

## 1. Purpose

The Visualize page builds **figures for a report**: statistically honest, visually clear
descriptions of what a filtered slice of a timeline contains, for an external investigation
report or a Story (`docs/STORIES.md`). It is deliberately a near-dead end — a figure's
marks can filter the Explorer (`ChartActionPopover`), but the page exists to *present*, not
to drill.

Standing rules, all of which the code below enforces:

- **Fields are generic.** A field is nominal, ordinal, interval or ratio scaled (Stevens),
  and nothing in the core knows what an IP address, a URL or a port *is* — that is what the
  enrichers are for. A derivation is a change of scale, never a domain-specific parse.
- **The analyst decides what a field is, per chart.** The probe suggests; the "treat as"
  chips are the decision; nothing is persisted per timeline or per user for it.
- **Honesty disclosures are caption lines.** Whatever the image cannot show — the pairing
  rule, a cap that cut values, the denominator of a share — is on the page and in the
  export (`lib/caption.ts`).
- **No shared axes, no small multiples.** Two quantities with different scales on one axis
  is a correctness trap; figures are assembled side by side in the report instead.
  (Facetting was declined for this reason and removed from the roadmap.)
- **One legality table** binds the analyst, the agent, external MCP clients and the Stories
  export resolver (§2).

## 2. The figure registry

`src/vestigo/agent/chart_meta.py` is the source of truth; `scripts/gen_chart_meta.py`
generates `frontend/src/components/viz/lib/chartMeta.ts` from it and
`tests/test_chart_meta.py::test_generated_typescript_is_up_to_date` asserts the mirror is
current. Each figure (`ChartType`) is one `ChartMeta` row:

| key | meaning |
|---|---|
| `label` | the figure's name |
| `question` | the forensic question it answers — shown under the gallery, and the successor of the retired task presets |
| `scales` | which "treat as" values the primary field may have for this figure to be legal |
| `data_kind` | which backend aggregation feeds it; pivot and sankey share one, so switching refetches nothing |
| `default_scale` | scale assumed when a request states none (mirrors the page's numeric probe) |
| `inputs` | what the figure asks for, from the fixed vocabulary `INPUT_KEYS` — `field`, `second_field`, `fields`, `lane_key`, `start_filter`, `end_filter`, `pairing`, `columns` — each `required` or `optional` |
| `derives` | derivations admitted on the primary field: `bins`, `time_part` (§4); only figures that admit the ordinal scale may declare one |
| `reads_options` | the `ChartOptionsSpec` keys the figure consumes; an unread option is a warning |
| `supports_compare`, `supports_marks` | whether a second layer / instants-and-windows can be drawn honestly |

`requires_second_field`, `accepts_second_field` and `multi_field` are read-only views over
`inputs`, kept because the generated TypeScript and `propose_chart` already speak in those
terms.

The rail renders **one control per declared input key and nothing else**. Two tests hold the
two sides together: `tests/test_chart_meta.py::test_declared_inputs_have_a_rail_renderer`
(a row may only declare keys the rail renders — `RAIL_RENDERED_INPUTS`) and
`frontend/src/test/chartRail.test.tsx` (for every figure, exactly the declared inputs
appear). Keys in the vocabulary that no shipped figure declares yet belong to figures listed
in §6.

## 3. `ChartConfig` v2

`frontend/src/components/viz/lib/chartConfig.ts` — the one serializable description of a
figure. URL state, saved charts (`SavedChart.config` in Postgres), story `chart_ref` blocks
and export captions all derive from it.

```ts
{
  v: 2,
  chartType, scale, metric, compare, options,   // as in v1
  field, fieldY, fields,                        // as in v1
  derive: null | { kind: "bins", mode: "width" | "log", count }
               | { kind: "bins", mode: "custom", edges }
               | { kind: "timePart", part: "hour" | "weekday" | "day" | "week" | "month" },
  inputs: { laneKey?, startFilter?, endFilter?, pairing?, columns? },
  marks: MarkSource[]
}
```

- **v1 upgrades losslessly.** `upgradeChartConfig` turns a stored `v: 1` row into v2 with
  `derive: null, inputs: {}, marks: []`; every other key kept its meaning. `parseStoredChartConfig`
  refuses any other version. The backend writes `CHART_CONFIG_VERSION = 2`
  (`stories/export.py::spec_to_stored_chart_config`); the demo case's seeded chart still
  carries `v: 1` and loads through the upgrade.
- **URL codec.** `c_derive`, `c_inputs`, `c_marks` travel JSON-encoded inside the `c_*`
  namespace (like `c_fields`), so `chartConfigToParams` clears them by construction and a
  malformed value is dropped field-by-field, never guessed at. Filters inside `inputs` and
  `marks` use the View payload shape, like `compare.filters`.
- **`scale` is "treat as".** The Stevens term is the wire format the agent, the URL and
  saved charts share; the plain phrase (`lib/scaleDisplay.ts`) exists only in the UI and the
  caption.

## 4. The rail (`components/viz/ChartRail.tsx`)

Read top-down, in dependency order:

1. **Field** — `FieldCombo`, with *No field — count every event* as the first entry, so the
   field-free figures (events over time, punch card) are reached from the picker itself and
   the topmost control is never inert (#298). Picking a field while on a field-free figure
   lands on the first figure that charts it, and says so. Two figures are **field-optional**
   (`inputs: {field: "optional"}` — the cumulative step and the calendar heatmap): the picker
   offers *No field* and keeps the figure whichever way the analyst goes — every event without
   a field, the field's values with one — and a hint under the picker says so. That is a
   third state beside field-free (`time`, `punchcard`, which never take a field) and
   field-required (everything else); `requires_field` is false for both of the first two.
2. **Treat as** — four chips; the Stevens term sits in the tooltip:

   | chip | tooltip | Stevens |
   |---|---|---|
   | Categories | Names with no order — identity only | nominal |
   | Ordered categories | Categories with a rank but no distance between steps | ordinal |
   | Number or time | Distances mean something, zero is arbitrary | interval |
   | Measure | A number with a true zero — differences and ratios both mean something | ratio |

   The field probe pre-selects one and explains itself (`treatAsNotice`); it never overrides a
   `scale` that arrived from the URL, a saved chart or the agent.
3. **Derive** — shown only when the treat-as admits a change of scale (`lib/derive.ts`
   `deriveOptionsFor`): *Measure* offers ranges; *Number or time* offers ranges or a calendar
   part; a `time:` field, already a calendar part, offers nothing and the section is absent.
   Three radios — *Use the value as is* / *Group into ranges* (equal-width, log-spaced, or my
   own edges) / *Calendar part* (hour of day, day of week, day of month, ISO week, month —
   all UTC). With one active the rail states "now treated as ordered categories": the figure
   gallery and every legality check run at that *effective* scale (`effectiveScale`), the bar
   axis defaults to value order so ranges read in order, and a treat-as or field change that
   no longer offers the derivation drops it. Clicking a **greyed** figure that exactly one
   derivation would light applies that derivation, switches to the figure and says so in
   `autoNotice` ("Bar needs categories — grouped attr:bytes into 8 log-spaced ranges."); when
   two could (a *Number or time* field: ranges or a calendar part) the tile stays inert — the
   rail never guesses. See §"Derivations" below.
4. **Figure** — every figure as a thumbnail (`primitives/FigureThumbnail.tsx`). Lit when legal
   for (effective treat-as, field) by the registry plus the `time:` field guard; greyed with
   the reason in its tooltip (`lib/figureGallery.ts`: "Histogram needs Number or time or
   Measure."), plus "Click to …" when a single derivation would light it. The selected
   figure's `question` and "how to read" line sit under the gallery.
5. **The figure's inputs** — one block per declared key: `secondField` (Field (Y) / Group by;
   on the table, *Count distinct of (optional)*), `fields` (the correlation list), and
   `columns` (the table's checklist: count, share, first seen, last seen, distinct <second
   field>). With nothing ticked the table shows count, share, first seen and last seen, plus
   *distinct* whenever a second field is set; *distinct* is disabled until one is, and a
   stored choice that names it without a second field is drawn without it.
6. **Compare · Metric · Options** — as before; Compare is always rendered and states its
   reason when a figure has no honest two-layer encoding. The cumulative step adds
   **Quantity** (running event count · running sum (measure) · distinct values seen so far)
   beside *Buckets*; the dishonest choices are greyed with the reason (sum needs *Measure*,
   distinct needs *Categories* or *Ordered categories*). The calendar has no options.
7. **Marks** — only on figures whose registry row says `supportsMarks` (events over time,
   value over time): the chart's mark sources, each with the status the server resolved it
   to (`N drawn`, `N of M drawn — capped at C`, `; U undated not drawn`; a baseline names
   its windows), a remove button per source, and an eight-entry **Add mark** menu — event
   id · tag · confirmed findings · custom filter · baseline definition · saved view ·
   instant · range (§"Marks"). Rendered after Metric, before the per-chart options.

Every automatic re-pick names itself in one `autoNotice` (set by the treat-as chips when they
clamp an illegal figure and by the field probes), is cleared by the analyst's next explicit
pick, and is never shown under a live saved-chart reference (`chartRefLive`).

What was removed: the scale radio (Nominal / Ordinal / Interval / Ratio), and the presets
drawer — each preset's question is now its figure's `question`.

## Derivations

A derivation is a **change of scale and nothing else**: number → ordered ranges, or a
timestamp-valued attribute → calendar part. Both yield the ordinal scale. There are exactly
two kinds, and nothing in the subsystem knows what an IP address, a URL or a port *is* —
domain parsing belongs to the enrichers, not to a chart's axis. `ChartConfig.derive` holds
one (`{kind: "bins", mode: "width"|"log", count}` / `{kind: "bins", mode: "custom", edges}` /
`{kind: "timePart", part}`); on the wire the kind is `time_part` (`lib/derive.ts`
`deriveToParam`). `CHART_META[c].derives` says which figures admit one — bar, heatmap, pivot
and sankey today, the terms-fed marks — and the registry is the only rule.

**Computed in ClickHouse, before aggregation** (`db/derive.py`, threaded by
`EventQueryService._resolve_derive`):

- `width` / `log` bins come from a one-scan pre-flight (`min` / `max` / `minIf(v > 0)` over
  the float-cast column under the same WHERE as the aggregation that follows), so the edges
  are the *slice's* edges and the response echoes them — a chart of ranges that did not say
  where its bins are would be a chart nobody could check. `log` cannot place a value ≤ 0, so
  those get their own, disclosed bin (`negative_bin`). `custom` takes the analyst's edges,
  open-ended at both ends. Labels are human (`< 1,024` · `1,024 – 10,240` · `≥ 10,240`;
  three significant digits for non-integers); the bins SQL is one `multiIf` over
  `_finite_float_cast`.
- `time_part` reuses `TIME_FIELD_SPECS` over `parseDateTimeBestEffortOrNull(…, 'UTC')`, so a
  derived hour and a `time:hour_of_day` chart can never disagree — same zero-padding, same
  ISO weekday, same UTC. A `time:` field itself is refused (HTTP 422 / `propose_chart`
  rejection): it is already a calendar part.
- Unparseable values (a `bytes` of `n/a`, a `logon_at` of `yesterday`) map to `''` and fall
  out through the callers' existing `!= ''` guard, so they are in no bin and counted
  nowhere; the caption says so.

Where it is threaded: `field_terms` (`derive=`), `compare_field_terms` (`derive=` — **both
layers are counted on the primary's resolved expression**, and that expression is part of
the baseline-cache key, so a derived request never reads an underived layer),
`field_value_timeseries` (`derive=`), `field_pivot` (`derive_x=` — a derived x axis is a
**bounded domain** like a cyclical `time:` axis: every label, in order, empty or not; an
empty range is a finding). The endpoints take `derive` (`field-terms`, `field-timeseries`),
`derive_x` (`field-pivot`) as a JSON query parameter, and `CompareRequest.derive` (`kind:
"terms"` only); malformed → 422 with the validator's words. A derived `field-terms` request
never answers from the M24a field-stats cache, which holds raw values.

The response echo — `derive` (or `derive_x`): `{kind, labels, mode, edges, negative_bin}` for
bins, `{kind, labels, part, timezone: "UTC"}` for a calendar part. The frontend uses
`labels` as the bar axis's value order (`BarChart.valueOrder`) and `edges` /
`negative_bin` for the caption's `derived:` line, which always names what was done and what
could not be counted (`lib/caption.ts`).

## Table figure

The one figure that is a table: the top-N values of a field with their count, share, first
and last seen, and — given a second field — how many distinct values of it each row saw. It
is the value inventory (#295) made bounded, and it is built to agree with it cell for cell.

**One SELECT core.** `EventQueryService.field_table` (`db/queries.py`) and the streamed
`iter_field_inventory` share `_inventory_select_core` — `val, count(), min(dated),
max(dated)` over one `GROUP BY val`, with the no-timestamp sentinel nulled *inside* the
aggregate so a value seen only on undated events keeps its count and reports no time rather
than the year 2299. `tests/test_table_clickhouse.py` asserts the two agree on every shared
cell. The table adds `uniqExactIf(second, second != '')` as `distinct_second`, and runs two
scans in parallel under one slot as `field_terms` does: the sorted top-N, and a spillable
totals grouping (`sum(c)`, `count()`) from which `total`, `distinct` and the remainder are
derived — no window functions, for the reasons `iter_field_inventory` gives.

**Share and the remainder.** `share = count / total`, where `total` is the number of events
with a non-empty value under the current filters — the caption names that denominator on
every table. Whenever the top-N cut anything (`distinct > len(rows)`) the response carries a
`remainder` — `{count, share, distinct_values}` — and the figure draws it as a final,
italic row: "Remainder (N more values)". It carries count and share only; its seen range and
distinct-second count would be a third scan for a row that exists to say "there is more".
Absent exactly when nothing was cut. The shown shares plus the remainder's sum to one.

**Sorting.** `sort_by` is any column — `value`, `count`, `share` (orders as `count`),
`first_seen`, `last_seen`, `distinct_second` (needs a second field; refused otherwise) —
and `sort_dir` is `asc` or `desc`. Time sorts are `NULLS LAST` in either direction, as the
inventory's are, and every ordering breaks ties on `val ASC`, so a re-run over unchanged
sources reproduces the same rows in the same order. The default is count, descending.

**Derivations** are accepted (`bins`, `time_part`) exactly as on a bar: the rows are the
ranges or calendar parts, and the response echoes `derive`.

**Endpoint.** `GET …/viz/field-table?field=…&second_field=…&limit=1..500&sort_by=…&sort_dir=…&derive=…`
plus the shared filter parameters. 422 when `second_field == field`, when
`sort_by=distinct_second` has no `second_field`, and for a malformed `derive`. Registry row
`table`: `data_kind="table"`, nominal or ordinal, `inputs` field (required), second field
(optional), `columns` (optional); options `top_n`, `table_sort_by`, `table_sort_dir`,
`highlight`. `ChartLimits.table_rows` caps the rows — agent (20, 30), analyst (50, 500).

**Options.** `topN` (the slider stops at 50, the exact box reaches 500); `tableSortBy` /
`tableSortDir`; `highlight` — a list of values whose rows get a faint band. Highlighting is
presentation only and the caption says which rows were highlighted, so a report reader can
tell an analyst's emphasis from the data's.

**Two renderings, one row model.** `lib/tableRows.ts` decides which columns show
(`effectiveColumns`), how each cell reads (`tableRowModels`: `en-US` integers, `45.5%`,
times trimmed to seconds, `—` for none), which rows are highlighted and what the remainder
row says. `charts/TableFigure.tsx` draws it twice: `TableFigure` is an `<svg>` (through
`ChartFrame`, so the page and the PNG/SVG export treat it like every other figure; the
count column carries an in-cell bar that encodes count only), and `TableHtml` is a real
`<table>` — headers, `data-highlighted`, `data-remainder` — which `ChartMarks tableAs="html"`
selects for a Story snapshot and the HTML export, where a table should be a table.

**CSV.** Built client-side (`tableCsv`) from the response the figure already holds, under
the same row model: the caption lines as `#` comment rows, a header (`value` plus the
effective columns), one row per value with **raw** values (a share is `0.4545…`, not
`45.5%`, so the file computes), and the remainder row last. Offered as a third export format
beside PNG and SVG only while a table is on the canvas.

**Caption lines** (after the field line): `sorted by <column> (ascending|descending)`;
`share = count / N events with a non-empty <field>`; `showing top K of D distinct values; R
events across M more values in the remainder row` (only when a remainder exists); and
`highlighted rows: a · b — presentation only` (only when set). The generic "(capped…)" line
does not fire for a table — the remainder row *is* the disclosure.

## Marks

A **mark** is an instant or a window drawn over a time-axis figure to say *this is when*.
What the analyst stores is never a pixel: `ChartConfig.marks` is a list of **sources**
(`MarkSource`, `c_marks` in the URL, `marks` in a saved chart and a Story block), and the
figure resolves them again on every draw. Five kinds:

| `kind` | stored fields | resolves to |
|---|---|---|
| `events` | `filters` (a view payload, plus `eventIds` — see below), `label?` | one instant per **dated** event matching the filter, capped |
| `view` | `viewId` | one instant per dated event of the saved view's filter, capped |
| `baseline` | `definitionId` | the definition's baseline window and every suspect window, as ranges, labeled as declared |
| `instant` | `at`, `label` | itself |
| `range` | `start`, `end`, `label` | itself |

**One resolution.** `agent/marks.py::resolve_marks(scope, marks, …)` is the only place a
source becomes marks, and three callers share it: `POST …/viz/marks` (the page and the
agent's card, through one hook `useResolvedMarks`), `execute_chart_spec` (the agent), and
through it the Stories export — so a report can never disagree with the page about which
instant a mark stands for. An `events`/`view` source goes through
`EventQueryService.mark_instants`: the earliest N dated events under the filter (ascending
by the offset-corrected timestamp, then `event_id`, so a re-run draws the same instants)
plus a `countIf` pair that says how many matched with and without a real timestamp — an
undated event cannot be placed on a time axis and is **counted, never drawn at the
sentinel year**. Nothing is derived from a baseline definition; its windows are drawn
exactly as declared.

**Provenance.** Every resolved mark carries `provenance`: `{kind: "event", event_id,
source_id}`, `{kind: "view", view_id, event_id, source_id}`, `{kind: "baseline",
definition_id, window_id}` (`"baseline"` or the suspect window's id), or
`{kind: "analyst"}` for a typed one. The response is `{marks, sources, cap}`, where
`sources[i]` is the per-source status — `count` (dated matches), `shown`, `overflow`,
`undated` — and `cap` the ceiling that applied.

**The cap is per source and always disclosed.** `viz_marks_max` (setting, group *Scan
guardrails*, default 50, 1–500) bounds what one source may draw on the page; the agent's is
`ChartLimits.marks_per_source = 20`, because every resolved mark is summarised into the
model's context. Past it the earliest N are drawn and the caption says how many were not.

**Rendering** (`primitives/MarksOverlay.tsx`, inside `CompareHistogram` and `LineChart`):
instants are numbered dashed rules with `#n` beside them, ranges tinted bands with their
label; both use `--color-warning` and its dim, never a Compare layer colour, so a mark cannot
be mistaken for a series. `lib/marks.ts` is the pure module both the overlay and the caption
read — instants are numbered in time order across every source, labels alternate a tier
when two rules are closer than 48px, overlapping bands stack, an instant outside the drawn
axis is counted as `offscreen` — so `#3` on the figure is `#3` in the caption.

**Caption lines** (`markCaptionLines`, pinned in `marks.test.ts`), one per source, in
source order: `marks #1, #3, #4: "beacons" — 3 events matching a filter; 1 undated event
not drawn`; `marks #1–#7: "Beacons" — 40 events of saved view; the earliest 7 drawn (cap
7), 33 not drawn`; `marks: baseline "Quiet" — its baseline window and 1 suspect window, as
declared`; `mark #2: "first" at 2026-07-20 01:30:00Z — analyst-placed`; `mark: "w"
… → … — analyst-placed`. More than five instants from one source are listed as a range of
numbers rather than one by one.

**Confirmed findings are an `events` mark over ids.** The rail's *Confirmed findings*
entry lists the timeline's `kind="confirmed"` dispositions and writes one
`{kind: "events", filters: {ids}, label: "confirmed findings"}`; *Event id* writes the
same shape for one id. That is why the marks codec carries `eventIds` inside the filter
payload — a mark's event ids are its provenance and travel with the chart — while the
Explorer's own `ids` stay session-only, as before.

**The endpoint.** `POST /{case}/timelines/{tl}/viz/marks` takes `{marks: MarkSource[]}`
— the stored shape verbatim, so the page posts what it holds — under `require_case_read`,
writes nothing, and answers 422 for a malformed mark (the `ChartMarkSpec` validator's
message) or an unknown baseline definition / saved view (`marks[i]: … not found`).

## Cumulative step

`chart_type="cumulative"` (`data_kind="cumulative"`, `EventQueryService.cumulative`,
`GET …/viz/cumulative?field=&quantity=&buckets=`) draws a running total over time. Three
**quantities**, chosen by `options.quantity` or resolved from the field and its treat-as by
one rule that `resolveChartOptions` and `chart_exec` both apply: no field → `events` (a
running event count); a *Measure* (ratio) → `sum` (a running `sum()` of the field parsed as a
number); categories (nominal/ordinal) → `distinct` (the number of distinct values seen so
far). The registry refuses the dishonest combinations by name — `sum` over anything but a
measure ("a running sum over anything but a measure is not a quantity"), `distinct` over a
measure, and either without a field — and warns, without refusing, when a field is set under
`events` (the field is ignored). The endpoint knows no scale, so it never assumes `sum`: the
page asks for it, and a `quantity` that needs a field without one is a 422.

**The SQL.** Buckets are `toStartOfInterval(ts, INTERVAL n second)` on the histogram's
epoch-aligned grid (`bucket_interval_seconds` + `aligned_bucket_starts`, the range being the
query's explicit `start`/`end` or the dated events' min/max), and the running value is a
window function over the bucketed subquery: `sum(delta) OVER (ORDER BY bucket ROWS BETWEEN
UNBOUNDED PRECEDING AND CURRENT ROW)` for `events` and `sum`. For `distinct` each bucket
yields a `uniqExactState(field)` and the window merges the states cumulatively
(`uniqExactMerge(st) OVER (…)`) — **never a sum of per-bucket distinct counts**, which would
count a value once per bucket it appears in: alice,bob | alice,carol | — | alice,dave is
2, 3, 3, 4 distinct users, where a sum of per-bucket distincts would say 2, 4, 4, 6
(`tests/test_cumulative_clickhouse.py` pins this). The result is zero-filled so every
aligned bucket is present and a flat step is a flat step; each bucket carries its own
`delta` (for `distinct`, the values first seen in it) beside the running `value`. A `sum`
skips values that do not parse as a number and a `distinct` skips empty values; both are
counted in `unparsed` and the caption says so ("1 event with no numeric attr:bytes value not
summed" / "… with an empty attr:user not counted"). `buckets` is bounded by
`ChartLimits.time_buckets` like the histogram (4–200 on the endpoint).

**Drawing** (`charts/CumulativeStep.tsx`): a `curveStepAfter` path — the value holds until
the next bucket changes it and the line never interpolates, because a diagonal would assert
growth inside a bucket nobody measured (`cumulativeStep.test.tsx` parses the path and
forbids a diagonal segment). The tooltip shows the running value and the bucket's own
contribution. Marks are supported (`MarksOverlay` on the same time axis); Compare is not —
two cumulatives on one axis is a shared-axis trap — and there is no metric. The caption
names the quantity and the field in its header line ("cumulative sum of attr:bytes (measure)
over time", "distinct values of attr:user seen so far", "cumulative event count over
time"), the bucket width ("1 h buckets, UTC") and the final value over the events scanned.

## Calendar heatmap

`chart_type="calendar"` (`data_kind="calendar"`, `EventQueryService.calendar`,
`GET …/viz/calendar?field=`) draws one cell per day, weeks as columns and ISO weekdays as
rows. **Day boundaries are UTC**, and the caption says so ("day boundaries UTC"): no timeline
or user carries a display timezone today, a boundary that moved with the viewer would redraw
the figure between two analysts, and the punch card already pins UTC and states it. The scan
is one `GROUP BY toDate(ts, 'UTC')`; with a field, a day counts the events whose field is
non-empty (`countIf(field != '')`), and a `time:` field is refused ("a calendar part is
always present — a calendar over it counts every event; omit field").

**The cap.** The figure keeps the latest `ChartLimits.calendar_weeks` = 53 ISO weeks
(Monday-start; `start` is the Monday of the first shown week, `end` the last day with data);
when the data spans more, `truncated` is true, `dropped` counts the events on earlier days,
and the caption states both ("53 weeks, 2025-07-21 → 2026-07-22, day boundaries UTC" /
"latest 53 of 61 weeks drawn; 9 earlier events not drawn"). The cap is a display truth
rather than a context budget, so it is the same 53 for the agent and the analyst.

**Drawing** (`charts/CalendarHeatmap.tsx`): cell shade is the count on the shared sequential
ramp (`lib/colors.ts`); a day with no events is an outlined empty cell (`fill="none"`,
`--viz-grid` stroke), deliberately distinct from the ramp's lowest step so "few" and "none"
never look alike; month labels above the first column of each month, every other weekday
label on the left, and the tooltip names the weekday, the date and the count. No marks, no
Compare, no options.

## 5. Parity

- **Agent.** `propose_chart` validates against the same registry (`docs/AGENT.md`
  §"`propose_chart`"); the chat card renders through the same `ChartCanvas`. `ChartSpec.derive`
  is the same `DeriveSpec` the endpoints parse, with four rejections — a figure whose
  `derives` lacks the kind (naming the figures that take it), a `time:` field, a `scale`
  other than `ordinal` (an omitted one resolves to ordinal), and empty bins after the scan —
  and `describe_field` reports `derivations` (`bins` when numeric, `time_part` always,
  nothing for a virtual field). The card's `specToChartConfig` carries `derive` across.
  For the table, `ChartSpec.inputs.columns` mirrors the rail's checklist and
  `options.table_sort_by` / `table_sort_dir` / `highlight` its options; two refusals —
  `inputs.columns` on any other figure, and `distinct_second` (as a column or a sort)
  without `field_y`; rows capped by `ChartLimits.table_rows` (agent 20/30, analyst 50/500);
  the echo carries `resolved.inputs`, and `summary` the first five rows and the remainder.
  `ChartSpec.marks` is a list of one `ChartMarkSpec` — one model with a kind-validator
  rather than a five-member union, because the tool schema is budgeted and five `$defs`
  would spend most of a step's headroom on prose the validator states once; refused on a
  figure whose `supports_marks` is false (naming the figures that draw them); the echo
  carries `resolved.marks` (the sources) and `summary.marks` the per-source status.
  For the cumulative step `options.quantity` is resolved by the same field-and-scale rule as
  the rail and echoed in `resolved.options`; the summary carries `total`, `events`,
  `unparsed`, `buckets` and `interval_seconds`. For the calendar the summary carries
  `total`, `max_count`, `weeks`, `weeks_total`, `truncated` and `dropped`.
- **External MCP.** The tool server is one FastMCP instance served in-app and on `/mcp`, so
  the schema an external client sees is the in-app one. Every `propose_chart` result also
  carries `open_url`, the Visualize page link for that exact figure
  (`agent/deep_link.py`, a Python mirror of `filtersToParams` + `chartConfigToParams`;
  `frontend/src/test/fixtures/viz-deep-link.json` is asserted from both sides) — an
  external client gets no card, so it gets the link.
- **Stories.** A `chart_ref` block stores the config beside the filters it was drawn under;
  the export resolver crosses the casing boundary once (`_stored_chart_to_spec`) — for
  `derive` only the kind's casing differs (`timePart` ↔ `time_part`), tested as a round trip;
  `inputs.columns` and the table options cross unchanged. A table block freezes the
  `field_table` response and is drawn as a real `<table>` in the snapshot and the HTML
  export (§"Table figure"). A time-axis chart with marks freezes the resolved `marks`
  (`{marks, sources, cap}`, provenance included) beside `chart`, and the snapshot draws
  them without re-resolving. A cumulative or calendar block freezes the `CumulativeResponse`
  / `CalendarResponse` as `chart` like every other kind, and `snapshotToChartResult` rebuilds
  both without a fetch.

## 6. Not yet shipped

Designed in the 2026-08-29 round and tracked as follow-up steps, in this order: ranked
change between two windows; interval lanes.
Until each lands, its `INPUT_KEYS` entries are vocabulary only, and this document does not
describe it. Geo/choropleth remains its own roadmap round.
