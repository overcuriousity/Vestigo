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
| `inputs` | what the figure asks for, from the fixed vocabulary `INPUT_KEYS` — `field`, `second_field`, `fields`, `pairing`, `start_filter`, `end_filter`, `columns` — each `required` or `optional` |
| `derives` | derivations admitted on the primary field: `bins`, `time_part` (§4); only figures that admit the ordinal scale may declare one |
| `reads_options` | the `ChartOptionsSpec` keys the figure consumes; an unread option is a warning |
| `supports_compare`, `supports_marks` | whether a second layer / instants-and-windows can be drawn honestly |

`requires_second_field`, `accepts_second_field` and `multi_field` are read-only views over
`inputs`, kept because the generated TypeScript and `propose_chart` already speak in those
terms.

Two things the design listed as registry columns live in the frontend instead, keyed by
`chartType` with a completeness test each: the gallery glyph (`primitives/FigureThumbnail.tsx`,
`figureThumbnail.test.tsx`) and the "how to read" line (`lib/explainers.ts`,
`vizExplainers.test.ts`); captions are one facts-driven builder (`lib/caption.ts`) rather
than a template id per figure. And the design's "`bar pie waffle heatmap` gain `bins`,
`timePart`" ships as bar, heatmap, pivot, sankey, change and table only: a derivation yields
the ordinal scale, and `pie` / `waffle` admit nominal alone (a ranked-bins pie would order
its wedges by a scale the mark cannot show), so by the rule above they declare none — a
numeric field reaches them through the bar.

The rail renders **one control per declared input key and nothing else**. Two tests hold the
two sides together: `tests/test_chart_meta.py::test_declared_inputs_have_a_rail_renderer`
(a row may only declare keys the rail renders — `RAIL_RENDERED_INPUTS`) and
`frontend/src/test/chartRail.test.tsx` (for every figure, exactly the declared inputs
appear). Every key is declared by a shipped figure —
`tests/test_chart_meta.py::test_every_input_key_is_declared_by_some_shipped_figure` keeps it
so (`lane_key` left the vocabulary when the lane key became the charted `field`,
§"Interval lanes").

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
- **`options` is resolved, not trusted.** `c_opts` is `JSON.parse`d without validation, so a
  shared or hand-edited link carries whatever it says. `resolveChartOptions` is the one place
  that turns it into concrete values, and every option that reaches a `Literal`-typed query
  parameter or drives a `switch` is coerced onto its allowlist there (`oneOf`), the way `topN`
  is clamped and `highlight` is filtered: `{"tableSortBy":"Count"}` otherwise reached the
  endpoint as a 422 and a permanently blank chart with nothing on screen to explain it.
- **Normalized on the way in.** Every entry point — a URL (`paramsToChartConfig`), a saved
  chart and a Story snapshot (`parseStoredChartConfig`) — runs `normalizeChartConfig`, which
  drops a `compare` layer the figure cannot draw and a `derive` its registry row does not
  admit. Both are invisible in the rail (the Compare radios render unchecked *and* disabled;
  the Derive section is absent) and unreachable to clear there, but the caption reads the
  *config*, not the request — so a pie carrying a stale `compare: baseline` printed
  "comparison: all timeline events" over one layer it never fetched, and a cumulative step
  carrying a stale `derive` claimed a binning it never sent. The rail drops both at the moment
  the figure is picked and says so in `autoNotice`; the normalizer is what makes that not the
  only thing keeping them honest.
  It also drops an `options.quantity` the `(field, treat-as)` pair no longer admits — the
  same shape of dead state reached through a control that *is* on screen, but not for the
  figure the analyst is looking at when they change either: switching Treat-as under a
  running sum left `quantity: "sum"` behind, `/viz/cumulative` knows no scale and drew it,
  and the same chart failed to execute as a Story block with `quantity="sum" needs
  scale="ratio"`. `resolveChartOptions` masks it as well, for a config that reaches it
  without the page's URL round trip (the agent's proposal card).
- **Cleaned on the way out, too.** `marks` and `inputs` are deliberately *carried* across a
  figure switch so switching back loses nothing, which is why the normalizer leaves them
  alone — but `execute_chart_spec` refuses a mark or an `inputs` key the figure does not
  declare *by name*. So `chartConfigToStored` drops what the target figure cannot honour at
  the boundary where the config stops being editable state and becomes a record, and
  `_stored_chart_to_spec` does the same on the way back for a chart saved before that (both
  from the same registry). A spec the agent writes by hand still gets the refusal, which is
  the right answer there; a persisted chart resolves as the figure it was saved as.
- **`scale` is "treat as".** The Stevens term is the wire format the agent, the URL and
  saved charts share; the plain phrase (`lib/scaleDisplay.ts`) exists only in the UI and the
  caption.

## 4. The rail (`components/viz/ChartRail.tsx`)

Read top-down, in dependency order:

0. **Scenarios** — §4a, above the field picker because a scenario answers *which field?* as
   well as *which figure?*. Collapsed once the URL already describes a chart.
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
3. **Derive** — shown only when the treat-as admits a change of scale *and* the figure that
   would result actually sends one (`lib/derive.ts` `deriveOptionsForChart`, over
   `deriveOptionsFor` and `resolveDeriveTarget`): *Measure* offers ranges; *Number or time*
   offers ranges or a calendar part; a `time:` field, already a calendar part, offers nothing
   and the section is absent. The figure gate is not simply `CHART_META[c].derives` —
   deriving is a change of scale and the rail re-picks the figure when the current one is
   illegal at the derived one, so a histogram (which admits none) still offers *Group into
   ranges* and lands on a bar. Withheld from the figures that admit none *and* stay selected
   because they are legal at every scale (cumulative, calendar, punchcard, time, corr): those
   would carry a `derive` they never put on the wire while the caption named it.
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
   on the table, *Count distinct of (optional)*), `fields` (the correlation list),
   `columns` (the table's checklist: count, share, first seen, last seen, distinct <second
   field>), and the interval lanes' three: `pairing` (two radios, *First to last* / *Start →
   next end*, each with its one-sentence rule under it), `startFilter` and `endFilter`
   (*Start events* / *End events* — a `CompareFilterEditor` each under *Start → next end*,
   and the note "Not used — first-to-last pairing needs no filters." under *First to
   last*, so the sections never disappear and reappear as the pairing flips). With nothing
   ticked the table shows count, share, first seen and last seen, plus *distinct* whenever
   a second field is set; *distinct* is disabled until one is, and a stored choice that
   names it without a second field is drawn without it.
6. **Compare · Metric · Options** — as before; Compare is always rendered and states its
   reason when a figure has no honest two-layer encoding. Compare has a third state beside
   supported and unsupported — **required** (`requiresCompare`, the ranked change): *Off*
   is disabled with the reason under the radios, and picking the figure from the gallery
   while Compare is off switches Compare to *Baseline* together with the figure and says so
   in `autoNotice` ("Compare set to Baseline — Ranked change … needs two windows."). The
   cumulative step adds **Quantity** (running event count · running sum (measure) · distinct
   values seen so far) beside *Buckets*; the dishonest choices are greyed with the reason
   (sum needs *Measure*, distinct needs *Categories* or *Ordered categories*). The ranked
   change adds *Top values per window* and *Layout* (dumbbell · slope). The interval lanes
   add *Lanes* (the lane cap, bound to `options.limitY`; slider to 50). The calendar has no
   options.
7. **Marks** — only on figures whose registry row says `supportsMarks` (events over time,
   value over time, the cumulative step, the interval lanes): the chart's mark sources, each with the status the server resolved it
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
`deriveToParam`). `CHART_META[c].derives` says which figures admit one — bar, heatmap, change,
pivot, sankey and table today, the terms-fed marks — and the registry is the only rule.
`ChartConfig.scale` stays the **treat-as** the derivation was computed from (a measure or a
number-or-time), never the ordinal it yields: that is what decides whether the rail offers the
Derive control at all (`deriveOptionsFor`), so the agent contract, a saved chart and a deep
link all carry it the same way (`lib/derive.ts` `deriveSourceScale`, Python
`chart_meta.DERIVE_SOURCE_SCALES`; an omitted or "ordinal" scale resolves to the derivation's
natural one). Picking a figure that admits no derivation drops the active one and says so —
legality is judged at the effective scale, so such a figure lights up under a derivation it
cannot carry. Picking a figure that cannot draw a *comparison* drops that the same way, for
the same reason and with its own notice ("Pie / Donut charts one layer — the comparison was
dropped.") — and so does **every** other path that re-picks the figure: a Treat-as change that
clamps it, a field picked on a field-free figure, and a derivation applied by clicking a greyed
tile (which conversely *bootstraps* Compare to Baseline for a figure that requires two
windows). Dropping the derivation moves the treat-as to the effective scale the gallery judged
the figure at, since the raw one may not admit it. Bin labels are cut by `_fmt_edges`, which raises the precision twice for two
different reasons. First until every label *names its own edge* — three significant digits
called a boundary at 4000.125 `4,000`, which puts a value of 4000.05 in the bin below a label
saying it is above it — capped at six decimals, because a log-spaced edge is irrational and
readability wins past a relative 1e-6. Then, uncapped, until no two labels collide: a label is
also the `multiIf` literal rows are grouped by, so two edges under one label were one bin in
the result and two in the caption.

**Computed in ClickHouse, before aggregation** (`db/derive.py`, threaded by
`EventQueryService._resolve_derive`):

- `width` / `log` bins come from a one-scan pre-flight (`min` / `max` / `minIf(v > 0)` over
  the float-cast column under the same WHERE as the aggregation that follows), so the edges
  are the *slice's* edges and the response echoes them — a chart of ranges that did not say
  where its bins are would be a chart nobody could check. `log` cannot place a value ≤ 0, so
  those get their own, disclosed bin (`negative_bin`). `custom` takes the analyst's edges,
  open-ended at both ends, at most `BINS_MAX - 1` = 49 of them — a ceiling the rail states at
  the Edges box (`EDGES_MAX`) rather than leaving a long pasted list to 422 with nothing on
  screen to explain why the chart stopped redrawing. Labels are human (`< 1,024` · `1,024 – 10,240` · `≥ 10,240`); the
  bins SQL is one `multiIf` over `_finite_float_cast`. The edges are **strictly increasing
  or absent**: over a range narrow relative to its magnitude — an epoch-nanosecond attribute
  binned across a few hours — float64 absorbs `lo + k * step` back to `lo` and then repeats
  it, so `bin_edges` drops the collapsed ones and the chart carries fewer ranges than were
  asked for. It cannot do otherwise: every consumer takes the edges to be increasing (two
  edges under one label are one bin in the result and two in the caption, `bins_expr` emits
  arms no row can reach, `label_order_expr` collapses the repeats onto one rank). The caption
  names the ranges there are and says how many were asked for.
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

The response echo — `derive` (or `derive_x`): `{kind, labels, mode, edges, edge_labels,
negative_bin}` for bins, `{kind, labels, part, timezone: "UTC"}` for a calendar part. The
frontend uses `labels` as the bar axis's value order (`BarChart.valueOrder`, which applies
it only under `options.sort: "value"` — so `resolveChartOptions` *resolves* `sort` to "value"
for every derived bar rather than the rail writing one into the config, and the page, the
agent's card, a Story snapshot and the HTML export order the same ranges the same way) and
`edge_labels` / `negative_bin` for the caption's `derived:` line, which always names what was
done and what could not be counted (`lib/caption.ts`). `edge_labels` is `edges` run through
the same `_fmt_edges` the bin labels are cut by — the caption naming a boundary and the axis
naming the bin either side of it are then the same text, and rounding the floats a second time
client-side (which said `4,000 – 4,001` over bins starting at 4000.125) cannot happen.

Because `labels` advertises the derivation's *whole* domain, a figure that draws fewer series
than the domain has must say so. `field_value_timeseries` caps at `series_limit` (≤ 50) and a
derivation routinely exceeds it — 49 custom edges are 51 bin labels, an ISO-week part is 53 —
so the response carries `series_truncated`, `distinct` and `other_count` beside `series`. The
cap is detected by fetching one row past it; the exact `distinct`/`other_count` cost a second
aggregate and are only paid for when the probe says the cap was actually hit. Both consumers
read them: the page's caption says `showing top 12 of 53 distinct values (capped; N events
across the 41 values not drawn)` — *not* `in "Other"`, since a value-over-time figure draws
one series per value and has nowhere to roll the rest into — and `execute_chart_spec` carries
them in its `summary` plus a warning, so a model reasoning over twelve series knows whether
that was all of them.

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
On a **derived** field, `value` means *value order*, not lexical order: `<`, `≥` and `≤` all
sort after the digits, so the string order would interleave the ranges (`1,000 – 2,000`
before `< 1,000`). The rank in the derivation's own label list is the key
(`derive.label_order_expr`), in SQL, because it also decides which rows the `LIMIT` keeps.

**Derivations** are accepted (`bins`, `time_part`) exactly as on a bar: the rows are the
ranges or calendar parts, and the response echoes `derive`.

**Endpoint.** `GET …/viz/field-table?field=…&second_field=…&limit=1..500&sort_by=…&sort_dir=…&derive=…`
plus the shared filter parameters. 422 when `second_field == field`, when
`sort_by=distinct_second` has no `second_field`, and for a malformed `derive`. Registry row
`table`: `data_kind="table"`, nominal or ordinal, `inputs` field (required), second field
(optional), `columns` (optional); options `top_n`, `table_sort_by`, `table_sort_dir`,
`highlight`. `ChartLimits.table_rows` caps the rows — agent (20, 30), analyst (50, 500).

**Options.** `topN` (the slider stops at 50, the exact box reaches 500); `tableSortBy` /
`tableSortDir`; `highlight` — a list of values whose rows get a faint band, entered **one per
line** (a field value carries commas of its own — a DN, a user-agent — and splitting on them
turned one value nothing matches into two). Highlighting is presentation only and the caption
says which rows were highlighted, so a report reader can tell an analyst's emphasis from the
data's.

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

`showing top K` only when the sort is **count descending**; otherwise `showing the first K in
this order`. The server applies the analyst's `sort_by`/`sort_dir` *before* the `LIMIT`, so
the sort decides which values survive and not merely how the survivors are arranged — sorted
by count ascending the drawn rows are the K *rarest* and the remainder holds nearly every
event, which "top" would misdescribe with nothing else on the canvas to correct it.

**CSV escaping.** Every cell — the `#` caption rows included — goes through `csvCell`, which
quotes on `"`, `,`, `\n` *and a lone `\r`*: a bare CR is routine in a field taken from a
Windows-sourced log, and RFC-4180 parsers (Excel among them) read it as a record terminator,
splitting the row and shifting every column after it.

## Marks

A **mark** is an instant or a window drawn over a time-axis figure to say *this is when*.
What the analyst stores is never a pixel: `ChartConfig.marks` is a list of **sources**
(`MarkSource`, `c_marks` in the URL, `marks` in a saved chart and a Story block), and the
figure resolves them again on every draw. Five kinds:

| `kind` | stored fields | resolves to |
|---|---|---|
| `events` | `filters` (a view payload, plus `eventIds` — see below), `label?` | one instant per **dated** event matching the filter, capped |
| `view` | `viewId` | one instant per dated event of the saved view's filter, capped |
| `baseline` | `definitionId`, `label?` | the definition's baseline window and every suspect window, as ranges, labeled as declared |
| `instant` | `at`, `label` | itself |
| `range` | `start`, `end`, `label` | itself |

`label` is required on `instant` and `range` and optional on the other three, where it
*replaces* the name the source would otherwise carry — the view's name, or the baseline
definition's — as the prefix on every window the source draws. Provenance still names the
definition or view, so a renamed mark never hides where it came from.

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

**Rendering** (`primitives/MarksOverlay.tsx`, inside `CompareHistogram`, `LineChart`,
`CumulativeStep` and `IntervalLanes`): instants are numbered dashed rules with `#n` beside
them, ranges tinted bands with their
label; both use `--color-warning` and its dim, never a Compare layer colour, so a mark cannot
be mistaken for a series. `lib/marks.ts` is the pure module both the overlay and the caption
read — instants are numbered in time order across every source, labels alternate a tier
when two rules are closer than 48px, overlapping bands stack, an instant outside the drawn
axis is counted as `offscreen` — so `#3` on the figure is `#3` in the caption. The axis
itself comes from `lib/timeDomain.ts`, one pure function per time-axis figure, which the
chart builds its x scale from *and* the caption reasons about, so the two cannot drift.

**Caption lines** (`markCaptionLines`, pinned in `marks.test.ts`), one per source, in
source order: `marks #1, #3, #4: "beacons" — 3 events matching a filter; 1 undated event
not drawn`; `marks #1–#7: "Beacons" — 40 events of saved view; the earliest 7 drawn (cap
7), 33 not drawn`; `marks: baseline "Quiet" — its baseline window and 1 suspect window, as
declared`; `mark #2: "first" at 2026-07-20 01:30:00Z — analyst-placed`; `mark: "w"
… → … — analyst-placed`. More than five instants from one source are listed as a range of
numbers rather than one by one — but only when that source's numbers are actually
contiguous: numbering runs across every source in time order, so two interleaved sources
read `marks 6 of #1–#11`, never `marks #1–#11`.

**Marks off the drawn axis are disclosed, never silently dropped.** Given the figure's
domain, each source's line ends in `; N outside the drawn time axis, not drawn` (`;
outside the drawn time axis, not drawn` for a single-mark source) — an instant outside the
axis or a range clipped away to nothing, exactly what `layoutMarks` declines to draw. A
chart windowed away from its marks would otherwise caption rules the reader cannot find.
The two agree at the edges by construction: a range meeting the axis at a single instant is
*drawn*, as a minimum-width band widened inwards (a range starting on the domain's last
instant has no room to its right), and `outsideDomain` is `end < lo || start > hi` to match.

**A filter mark has to narrow something.** The rail's *Custom filter* entry refuses to add
a source whose filter is empty (the same question `hasActiveFilters` answers for the filter
chips) and says why. An empty filter is not an empty mark: it matches every event in the
timeline and draws one rule per event, up to the cap.

**Confirmed findings are an `events` mark over ids.** The rail's *Confirmed findings*
entry lists the timeline's `kind="confirmed"` dispositions and writes one
`{kind: "events", filters: {ids}, label: "confirmed findings"}`; *Event id* writes the
same shape for one id. That is why the marks codec carries `eventIds` inside the filter
payload — a mark's event ids are its provenance and travel with the chart — while the
Explorer's own `ids` stay session-only, as before.

**The endpoint.** `POST /{case}/timelines/{tl}/viz/marks` takes `{marks: MarkSource[]}`
— the stored shape verbatim, so the page posts what it holds — under `require_case_read`,
writes nothing, and answers 422 for a malformed mark (the `ChartMarkSpec` validator's
message) or an unknown baseline definition / saved view (`marks[i]: … not found`). It reads
the body with `_stored_marks_to_spec(..., strict=True)`: the lenient reading that lets a
chart saved by an older build still render what it can would here draw fewer marks than the
caller asked for, with nothing in `sources` or the caption to say so. Each
`events`/`view` scan runs under the foreground gate with the same pre-checks every viz GET
applies, and the RE2 guard is decided **per mark, off the query about to run** — a pattern
can sit in `qRegex`, in a per-field `regex` match mode, or inside the saved view, and only
the first is visible on the request body, so an RE2-only rejection answers 400 on all three.

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

The figure is legal at **nominal, ordinal and ratio, never interval**: accumulating needs the
true zero that separates the two, so a running total of temperatures or calendar years totals
nothing. The scale was briefly advertised and reachable by no quantity at all — `sum` demanded
ratio, `distinct` demanded nominal/ordinal, `events` discarded the field — so the refusals
cycled with no way through. Accumulating the *event count* over an interval-scaled field is
`quantity="events"`, which takes no field. `chart_exec` runs the scale check *before* the
per-figure rules for this reason: those are phrased in terms of the scale, so on a scale the
figure does not admit they answer the wrong question.

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
forbids a diagonal segment). The y axis spans the running series' own minimum and maximum,
not `[0, total]`: `sum` over a *signed* measure is not monotonic, so a series that peaks at
500 before settling at 20 would otherwise be drawn against a `[0, 20]` axis, most of it above
the plot area. The floor stays 0 unless the running total actually goes below it. The
tooltip shows the running value and the bucket's own contribution. Marks are supported
(`MarksOverlay` on the same time axis); Compare is not —
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

## Ranked change

`chart_type="change"` (`data_kind="change"`, `EventQueryService.field_change`) answers
"which values gained or lost share between the reference window and this one — and which
appeared or disappeared?" for one categorical field (nominal or ordinal; a numeric or time
field reaches it through a derivation, bins or a calendar part, exactly as the bar does).

**Two windows, fixed vocabulary.** The *primary* window is the current filters — the suspect
slice, drawn in the accent colour; the *comparison* window is the Compare layer — Baseline
(the whole timeline over the same time range) or Custom filters (the time range pinned to
the primary's, as every Compare kind does) — the reference, drawn grey. The pair is the
one the grouped bar already uses, so a reader moving between figures never relearns which
layer is which. The figure is the first with `requires_compare=True` (generated as
`requiresCompare`): it is *defined* by two windows, so the rail disables Compare's *Off*
and says why, picking it with Compare off switches Compare to Baseline and says so, and
`propose_chart` refuses a `change` without a comparison layer by name (§5). The page shows
"Ranked change needs a second window — turn on Compare" rather than an empty chart if the
config ever arrives in that state (a URL, a saved chart).

**The scans.** Both windows are resolved by `POST …/viz/compare` with `kind="change"`;
`field_change` then runs four scans in two parallel pairs under one foreground gate slot:
the top-N values of each window (`GROUP BY … ORDER BY count() DESC, val ASC LIMIT n`, both
on the **primary's** derived expression — the comparison layer never resolves edges of its
own, or the two windows would be counted on different bins), the union of the two lists
(primary order first, duplicates dropped), and one count scan per window restricted to the
union (`has(Array(String))`) that also yields the window's non-empty total. The pure,
unit-tested `rank_change_rows` turns the counts into the rows.

**Share of window, never count.** Each row carries `primary_share = primary /
primary_total` and `comparison_share` (0.0 when that window's total is 0), `delta_share =
primary_share − comparison_share`, and a status: `new` (present in the primary window only),
`vanished` (comparison only), else `rose` / `fell` / `same` by share. The windows are rarely
the same size, so a count comparison would mostly measure the window sizes: a value with
the same count in both windows *fell* when the second window is twice the size, and the
live test pins exactly that. Rows are ranked by |Δ share| descending, ties by value, and
capped at `ChartLimits.change_union` — 30 for the agent, 200 for the analyst — with
`truncated`, `rows_shown`, `omitted` (the smallest changes) and `union_size` in the
response. The per-window top-N is `options.top_n`, clamped to `ChartLimits.change_top_n`
(agent 10 default / 20 max; analyst 10 / 100, slider to 20).

**Why not `GET …/viz/field-change`.** The design named a GET; it ships as a kind on the
existing Compare POST because two filter sets do not fit query params — the same reason
Compare is a POST — and because the two-layer resolution, the window pinning, the baseline
token and the derive rule already live in that handler. The baseline cache token is not
passed: `field_change` has no cache path, and the token's superset invariant belongs to
the terms and time kinds.

**Drawing** (`charts/RankedChange.tsx`). *Dumbbell* (default, `options.layout`): one row per
value, ranked; the grey dot at the comparison share, the accent dot at the primary share,
a link between them on a shared 0…max-share axis in percent; the value at the left, the
status at the right in words (`new`, `vanished`, `same`) or percentage points (`+30.0 pp`,
`−40.0 pp`, a real minus sign). *Slope*: two columns, reference left and this window
right, one line per value from its comparison share to its primary share, labelled at both
ends, accent when it rose or is new and muted otherwise. Both carry the legend ("Reference
window (comparison)" / "This window (primary)"), a tooltip with both counts, both totals
and both shares, and the empty state "No values in either window". No marks, no metric.

**Caption.** The header is `share-of-window change of <field> between two windows — <label>`
(with `(<scale> → ordered categories)` after the field when derived). Beneath the usual
`primary:` / `comparison:` lines with both window totals: `ranked by |Δ share of window|,
not by count; top N values per window, U in the union — X new, Y vanished`, and when the
cap bit `union capped at S of U values; the O with the smallest change not drawn`.

## Interval lanes

`chart_type="lanes"` (`data_kind="lanes"`, `EventQueryService.field_lanes`,
`charts/IntervalLanes.tsx`) answers "how long did each value's activity run — which runs
overlap, which never ended, which ended without a start?" for one categorical field
(nominal or ordinal). One **lane** per value of the field; each **interval** is one bar
from its start to its end.

**The lane key is `field`.** The design listed a `laneKey` input beside a field-free
figure. On the field-first rail that would have meant a top *Field* control reading "No
field — count every event" over a second "Lane key" picker, with the treat-as chips, the
field probe and the gallery legality applying to nothing. Declaring `inputs={"field":
"required", …}` gives the lane key the scale check, the probe and the greyed-tile reasons
for free, exactly like the bar. `lane_key` left `INPUT_KEYS` with it, so no dead
vocabulary survives (§2).

**Two pairings, one rule.** `pairing="first_last"` (the default): one bar per lane from
its first to its last dated event under the current filters. `pairing="next_end"`:
`start_filter` and `end_filter` name the events that open and close an interval, and **an
end closes the most recent open start in its lane** — the rule the caption prints
verbatim. The design's two sentences ("each start pairs with the next end after it" and
"an end claims the most recent unclaimed start") disagree when two starts precede one end;
the second is the rule shipped, because it draws nested activity as nested bars rather than
crossing ones. A start never closed is **open-ended** — drawn to the slice end under an
arrowhead, muted, so "still open" never reads as "ended at the edge"; an end with no open
start before it in its lane is an **orphan** — counted, never drawn. An interval never
crosses a lane: the stack is per lane.

**Start and end filters are ANDed with the current filters**, never a separate window — an
analyst filtering to one host expects the start events to be that host's. That needs
`_build_where` to build a second and a third clause whose parameter names cannot collide
with the first: `param_prefix` on `_ParameterizedQueryBuilder` (`s_p0…`, `e_p0…`), one line
each, unit-tested for disjointness; the fixed names (the offset arrays, `field_key`) are
bound once with the same values by every clause.

**The scans.** `first_last` is one grouped scan — `argMin`/`argMax` of the event id by
time plus `count()` per lane, `ORDER BY count DESC, lane ASC LIMIT lane_cap` — in parallel
with the whole's counts (distinct lanes, undated rows, the earliest and latest dated
instant). `next_end` builds one parameterised subquery, the `UNION ALL` of the
start-matching rows (`primary ∧ start`, `is_start = 1`) and the end-matching rows
(`primary ∧ end`, `is_start = 0`); two parallel scans rank the lanes by their row count
and count the whole (distinct lanes, starts, ends, undated); then one ordered scan fetches
the kept lanes' rows (`has(Array(String))`) `ORDER BY ts, is_start DESC, event_id LIMIT
rows_cap + 1` — a start before an end at the same instant — and the pure, unit-tested
`pair_intervals` turns them into intervals with one stack per lane. **The pairing runs in
Python, not in a window function**: LIFO matching where orphans consume nothing is a stack,
and a stack is not a running sum — expressing it in SQL needs a self-join on a derived
depth, and the one-sentence rule would be buried in it. The scan is capped instead
(`ChartLimits.lanes_rows`: 2,000 for the agent, 50,000 for the analyst), and when the cap
bites the response says so (`rows_truncated`, `rows_paired`) and the caption prints "first
N start/end events (by time) paired — the row cap; later ones not drawn". The lane cap is
`options.limit_y`, clamped to `ChartLimits.lanes` (agent 10 default / 20 max; analyst 10 /
100, slider to 50). Undated rows never feed a bar and are counted.

**Every cap and count is in the response** (`LanesResponse`): `lanes[{key, count,
intervals[{start, end | null, start_event_id, end_event_id | null}]}]` ranked by event
count then key, `lane_cap`, `lanes_total`, `lane_cap_hit`, `other_lanes`, `starts`, `ends`
(the whole, before the caps; `0, 0` under `first_last`), `unpaired_starts`, `orphan_ends`
(over the rows that were paired), `rows_cap`, `rows_truncated`, `rows_paired`, `undated`,
and `slice_start` / `slice_end` — the query's own bounds when it has them, else the
earliest and latest dated row; open-ended bars run to `slice_end`.

**`POST …/viz/lanes`, not the design's `GET …/viz/field-lanes`.** Three filter sets do not
fit query params — the same reason Compare and `viz/marks` are POSTs. `LanesRequest` is
`{field, pairing, primary, start_filter, end_filter, limit_y}` with the three layers as
`CompareFilters`; the start and end layers are resolved by the same `_resolve_body_query`
and pinned to the primary's time window exactly as a custom Compare layer is. `next_end`
without both filters is a 422 that names them; the lane cap is clamped to the analyst
ceiling and the row cap is the analyst's, both echoed.

**Drawing.** A `scaleBand` of the kept lanes down the left with the value's label, a
dashed lane line, and a `scaleTime` of `[slice_start, slice_end]` along the bottom. A
closed interval is a solid accent bar (at least 2 px wide); an open-ended one is muted and
runs to the right edge under an arrowhead; nested intervals in one lane are both drawn.
The legend names both ("interval" / "no end seen — runs to the slice end"); the tooltip
carries the lane, the start and its event id, and the end and its event id or "no end
seen". Marks draw across every lane (`MarksOverlay`, the same primitive the time figures
use). The empty state is "No intervals to draw." with a pairing-specific hint. No Compare,
no metric.

**Caption.** The header is `intervals of <field> over time — <label>`. Beneath the usual
layer lines: the pairing rule verbatim (`pairing: start → next end — an end closes the most
recent open start in its lane; an open start runs to the slice end; an end with no open
start before it is an orphan, counted and not drawn`, or `pairing: first to last — one bar
per lane, from its first event to its last`); `lanes: N shown of M (top by event count); K
more not drawn` when the cap bit, else `lanes: N` — *N is what the figure draws*, since a
lane that survived the cap can still pair no interval (under `next_end`, one whose events are
all orphan ends) and `IntervalLanes` filters it out, so the difference is disclosed as `; K
lanes with no interval to draw`; under `next_end` **two** further lines, because they are two
scopes and one sentence carrying both is arithmetic nobody can reconcile —
`starts: S · ends: E — matched across all M lanes, before the caps` (the whole union) and
`paired over the P lanes kept: U open-ended (no end seen, drawn to <slice end>), O orphan
ends not drawn` (every lane that survived the cap, drawn or not — an orphan end in an empty
lane is counted here and nowhere on screen); then, when the row cap bit, the
`first N start/end events (by time) paired` line; and `U undated events not drawn` whenever
any were.

## 4a. Scenarios (`components/viz/lib/scenarios.ts`, `ScenarioModal.tsx`)

Six investigations named in the analyst's language — DDoS / flood, data exfiltration, SQL
injection, RDP interaction, lateral movement, off-hours activity — each of which resolves to
exactly one legal `ChartConfig`. They are the second descendant of the retired task presets:
`ChartMeta.question` kept the prose (§2), and this table keeps what the presets actually did,
which was to set the *rest* of the config in one click.

The standing rule that the core knows nothing about what a field is (§1) survives intact,
because **a scenario names roles, never fields.** A `ScenarioRole` is a label, a hint, which
chart input it fills (`field` / `fieldY` / `filter`), and an optional `suggest` pattern over
field *tokens*. `suggestBindings` pre-fills what it can from the timeline's own
`VizFieldInfo` list (which arrives sorted by coverage, so the first match is also the
best-covered one) and leaves the rest empty — a role it cannot fill is reported to the
analyst, never guessed at. The analyst binds each role in `ScenarioModal`, and the modal is
where every piece of the scenario's domain knowledge is on screen before anything renders.

Two scenarios also carry a **suggested filter**, which is what makes them more than a figure
choice: SQL injection wildcard-matches injection syntax in the bound request field, and RDP
interaction narrows to the remote-desktop event IDs (4624, 4625, 4778, 4779, 1149, 21, 25) in
the bound event-ID field. The filter is built *from the bindings*, so it is keyed on the
analyst's own field token; it is a pre-checked, described, droppable row in the modal; and
`VisualizePage::applyScenario` merges it per field into the page's URL filters rather than
replacing them. It therefore reaches `InheritedFiltersBar` as removable chips and
`lib/caption.ts` as caption prose by the existing paths — nothing about a scenario is hidden
from the figure it produced, and a shared link reproduces it.

Applying a scenario adds no render path. It is one `takeOver(config, filters)` — the same
call the rail's own controls make — with `buildScenarioConfig` writing *every* member of the
patch, including the ones the scenario does not use: a scenario laid over a chart the analyst
had already built must not leave that chart's derivation, marks or figure-specific inputs
behind for a figure that never declared them.

A scenario is never withheld. One whose roles find no candidate field still appears in the
rail, still opens, and names the role it could not fill (the analysis gate's rule — advice
plus a record, never a lock). `tests/vizScenarios.test.ts` checks every scenario against the
registry — its scale is in the figure's `scales`, its options are all in `readsOptions`, a
required input is covered by a required role, and no role binds an input the figure never
asks for — so a registry change cannot leave a scenario emitting a config the rail would
refuse.

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
  A `field_y` that repeats `field` is refused for every figure that takes one: the HTTP
  endpoints answer 422 and the rail refuses it at the picker, but `execute_chart_spec` calls
  the query service directly, and one field named twice does not fail — it draws a diagonal
  pivot, a `y=x` scatter, one group per value, or a table whose `distinct field_y` column is
  1 on every row, each presented as a real answer.
  `ChartSpec.marks` is a list of one `ChartMarkSpec` — one model with a kind-validator
  rather than a five-member union, because the tool schema is budgeted and five `$defs`
  would spend most of a step's headroom on prose the validator states once; refused on a
  figure whose `supports_marks` is false (naming the figures that draw them); the echo
  carries `resolved.marks` (the sources) and `summary.marks` the per-source status.
  For the cumulative step `options.quantity` is resolved by the same field-and-scale rule as
  the rail and echoed in `resolved.options`; the summary carries `total`, `events`,
  `unparsed`, `buckets` and `interval_seconds`. For the calendar the summary carries
  `total`, `max_count`, `weeks`, `weeks_total`, `truncated` and `dropped`. For the ranked
  change `propose_chart` refuses a `change` without a comparison layer by name
  (`chart_type="change" needs a comparison layer — … set compare.mode to "baseline" or
  "custom".`); `resolved.options` carries `top_n` (clamped to `change_top_n`) and `layout`;
  the summary carries both totals, `union_size`, `rows_shown`, `truncated` and the first
  five `top_rows` (`{value, status, delta_share}`).
  For the interval lanes `propose_chart` resolves `inputs.pairing` (default `first_last`),
  refuses `next_end` without both filters by name (`pairing="next_end" needs
  inputs.start_filter and inputs.end_filter — the events that open and close an interval;
  pairing="first_last" needs neither.`), refuses `inputs.pairing` / `start_filter` /
  `end_filter` on any other figure (`… are chart_type="lanes" only.`), and warns when
  `first_last` carries filters it will not read; `options.limit_y` is clamped to
  `ChartLimits.lanes`, the start and end layers are pinned to the primary's window as the
  endpoint pins them, and the summary carries `pairing`, `lanes_shown`, `lanes_total`,
  `lane_cap_hit`, `intervals`, `unpaired_starts`, `orphan_ends`, `rows_truncated`,
  `undated` and the first five `top_lanes` (`{key, count, intervals}`). Marks draw on it.
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
  both without a fetch. A ranked-change block freezes the `ChangeResponse` as `chart` and
  is redrawn the same way — the shares are in the response, so the snapshot never recounts
  a window.
  An interval-lanes block freezes the `LanesResponse` as `chart` beside its resolved
  `marks` (it is a time-axis figure) and `snapshotToChartResult` rebuilds it; the casing
  boundary crosses `inputs.pairing` (`nextEnd` ↔ `next_end`) and `inputs.startFilter` /
  `endFilter` (view payloads ↔ `FilterSpec`s) through the same two helpers the compare
  filters use, tested as a round trip.
- **Scenarios are analyst-side only** (§4a). There is no scenario tool and no backend
  registry: a scenario resolves to a `ChartConfig` the agent could already have proposed
  through `propose_chart`, so exposing it would add a second vocabulary for the same
  figures — and the field binding it exists for is a question to ask a person looking at
  their own timeline, not one to answer by pattern-matching on the model's behalf.

## 6. Out of scope, by decision

Every figure the 2026-08-29 round designed has shipped; what it deliberately left out:

- **Small multiples / facets and shared axes** — rejected outright in the round. A figure
  answers one question over one slice; a grid of them is a Story, and Stories already hold
  many charts under one set of filters.
- **Geo / choropleth** — its own round (`docs/ROADMAP.md` Milestone 2): the offline
  basemap, the projection and the count-vs-rate rule are a design problem this round did
  not take on.
- **A `db/viz_aggregations.py` module** — the design named one; the aggregations live on
  `EventQueryService` (settled in step 1), where the WHERE builder, the field-column
  resolution, the scan gate and the parallel-scan helper already are.
- **A demo-case chart-coverage test** — none exists for any figure. A sibling of
  `tests/test_demo_detector_coverage_clickhouse.py` asserting `execute_chart_spec` draws
  every figure over the demo case is the natural follow-up and is listed in `ROADMAP.md`.

## History

Scenarios (§4a) landed after that round, on 2026-08-31.

This document is the durable form of the 2026-08-29 Visualize round, which landed as nine
steps in a stacked series of PRs: the figure registry and the field-first rail (#324),
derivations (#325), the table figure (#326), marks and `open_url` (plan A, #327), the
cumulative step and the calendar heatmap (plan B, #328), ranked change (plan C, #329), and
interval lanes with this consolidation (plan D). Each step's decisions and the reasons for
deviating from the design's letter are in the section that describes the figure.
