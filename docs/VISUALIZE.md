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
   lands on the first figure that charts it, and says so.
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
5. **The figure's inputs** — one block per declared key: `secondField` (Field (Y) / Group by),
   `fields` (the correlation list).
6. **Compare · Metric · Options** — as before; Compare is always rendered and states its
   reason when a figure has no honest two-layer encoding.

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

## 5. Parity

- **Agent.** `propose_chart` validates against the same registry (`docs/AGENT.md`
  §"`propose_chart`"); the chat card renders through the same `ChartCanvas`. `ChartSpec.derive`
  is the same `DeriveSpec` the endpoints parse, with four rejections — a figure whose
  `derives` lacks the kind (naming the figures that take it), a `time:` field, a `scale`
  other than `ordinal` (an omitted one resolves to ordinal), and empty bins after the scan —
  and `describe_field` reports `derivations` (`bins` when numeric, `time_part` always,
  nothing for a virtual field). The card's `specToChartConfig` carries `derive` across.
- **External MCP.** The tool server is one FastMCP instance served in-app and on `/mcp`, so
  the schema an external client sees is the in-app one.
- **Stories.** A `chart_ref` block stores the config beside the filters it was drawn under;
  the export resolver crosses the casing boundary once (`_stored_chart_to_spec`) — for
  `derive` only the kind's casing differs (`timePart` ↔ `time_part`), tested as a round trip.

## 6. Not yet shipped

Designed in the 2026-08-29 round and tracked as follow-up steps, in this order: the table
figure; marks (instants and windows from events, tags, confirmed findings, baseline
definitions, saved views, or typed); the cumulative step; ranked change between two windows;
the calendar heatmap; interval lanes.
Until each lands, its `INPUT_KEYS` entries are vocabulary only, and this document does not
describe it. Geo/choropleth remains its own roadmap round.
