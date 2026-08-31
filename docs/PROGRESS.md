# Vestigo Implementation Progress

Append-only session log — what changed and why, newest first. This file keeps the recent
sessions only; older ones live in git history, and every release is summarized in
`CHANGELOG.md`. Plans belong in `ROADMAP.md`, not here.

Last updated: 2026-08-31 (session 214 — scenario presets return to Visualize, bound to roles rather than fields).

## Session 214 — 2026-08-31: scenario presets, bound to roles rather than fields

The 2026-08-29 Visualize round folded the task presets into `ChartMeta.question`, which kept
their prose and dropped what they did — the metric, the compare mode, the options a preset set
in one click. This brings them back one level up, as **scenarios**: six investigations named
the way an analyst names them (DDoS / flood, data exfiltration, SQL injection, RDP
interaction, lateral movement, off-hours activity), each resolving to exactly one legal
`ChartConfig`.

The design problem was that "SQL injection" is domain knowledge and the page's first standing
rule is that the core knows nothing about what a field is. The answer is that **a scenario
names roles, not fields**: "the field holding the request text", bound by the analyst in a
modal that pre-fills what it can from their own timeline's tokens and reports what it could
not. Two scenarios also suggest a filter — injection syntax, the RDP event IDs — keyed on the
field the analyst bound, shown as a pre-checked droppable row, and merged per field into the
page's URL filters, so it arrives as removable chips and as caption prose rather than as a
silent narrowing.

Applying a scenario adds no render path: it is the same `takeOver(config, filters)` the rail's
own controls make. A scenario is never withheld from the gallery over a role its timeline
cannot fill — it opens and says which role that is. `vizScenarios.test.ts` holds every
scenario against the figure registry (scale legal, options read, required inputs covered by
required roles), so a registry change cannot leave one emitting a config the rail refuses.

New: `frontend/src/components/viz/lib/scenarios.ts`, `ScenarioModal.tsx`. `docs/VISUALIZE.md`
§4a records it; §5 records why there is no scenario tool for the agent.

## Session 213 — 2026-08-31: `/mcp` gets the chart, not a description of one (1.17.1)

Three defects on the external transport, all the same shape: tools written for the agent
panel, advertised to a client that has no panel.

- **`propose_chart` returned no data.** In-app it drops `result` because the analyst's card
  fetches its own copy and the model only needs `summary`. Over `/mcp` there is no card, so a
  client asking for a time histogram got `{"buckets": 48, "interval_seconds": 3600}` — a
  figure it could not draw, tabulate or quote, leaving it to re-derive the same numbers
  through `histogram`/`field_terms` by hand. That surface now keeps `result` (and `marks`),
  through a new generic `_columnar_deep` plus the existing `_compact_timeseries`, so it reads
  like every other tabular result. `_columnar_deep` walks the structure instead of naming row
  keys the way `_columnize` does: one tool covers twenty chart types over a dozen aggregation
  shapes, and a named list would silently stop covering a new figure. Sizes were never the
  problem once the payload is columnar — `AGENT_CHART_LIMITS` already bounds every shape to
  ~1–3 KB typical, ~12 KB worst case (a 1,000-point scatter), the same order as a routine
  `field_timeseries` call.
- **`open_url` was a relative path.** It is the one thing that shows a human the real,
  interactive figure, and an MCP client has no origin to complete it against. New optional
  `VESTIGO_PUBLIC_BASE_URL` makes it absolute; unset, nothing changes. Not taken from the
  request `Host` header on purpose — behind a proxy that is whatever the proxy forwarded, and
  a confidently wrong link is worse than a relative one. Worth recording why nothing had to be
  persisted for this: the figure lives entirely in the query string (`agent/deep_link.py`
  mirrors the page's codec), so a link costs no row, needs no write permission, and works for
  a read-only case member — which a save-the-chart-first design would not.
- **`propose_finding` was advertised there at all.** Its whole product is a card; over `/mcp`
  it wrote nothing, showed nothing, and returned a hit count `search_events` already gives,
  under a docstring telling the model an analyst was looking at it. Now `requires_conversation`
  like the other two proposal tools.

`propose_chart` keeps its name on both surfaces (renaming breaks existing clients) but is
*described* differently where there is no card and no Save button — `EXTERNAL_CHART_DESCRIPTION`.

**Considered and rejected: rendering the chart server-side.** The obvious read is that the
Visualize page's SVG/PNG export could be called from the server, but it is not a renderer —
`downloadChartSvg`/`downloadChartPng` take an `SVGSVGElement` the browser has *already*
painted, and every step after that is browser-bound (`getComputedStyle` to resolve the
`var(--viz-*)` palette, `XMLSerializer`, a `<canvas>` to rasterize). The drawing is the 22
React components in `components/viz/charts/`. They *can* render outside a live page —
`renderToStaticMarkup` plus `ChartStaticWidthContext` is exactly how story exports work
(#197) — but only in a JS runtime, which would mean Node at runtime, a second SSR bundle, a
build-extracted palette, and a rasterizer. That is a deployment-story change
(`docs/DEPLOYMENT.md`), not a patch. The alternative, a Python re-implementation, puts two
renderers that can disagree about the same `ChartConfig` in a product where the card is
evidence. Neither belongs in 1.17.1; the deep link is what shows a human the figure.

## Session 212 — 2026-08-30: every embeddings entry point is gated on the capability

`EmbedWizard` and `ToolsSheet` already hid themselves where `capabilities.embeddings` is
false; five other surfaces did not, and on an instance with no embedding backend they each
described a subsystem that does not exist there:

- `TimelineList` — the per-row badge read `Embedding 0/N` forever (vector counts never move),
  as if a job were still running, plus the staleness hint telling the analyst to re-embed.
- `CaseOverviewPage` — the `0/N sources embedded` header badge, which also kept a 15 s
  `["sources"]` poll alive waiting for a count that can never change.
- `FilterRail` — the Keyword/Semantic switch, with Semantic permanently disabled behind a
  "No embeddings for this timeline **yet**" tooltip. The whole switch is gone now; the regex
  toggle beside it (keyword-only) stays.
- `EventDetailPanel` — the find-similar (vector search) icon, which could only ever answer
  with an error.
- `lib/guidance.tsx` — the "Optionally: embeddings" workflow step. `CASE_OVERVIEW_STEPS`
  entries now carry an optional `capability`, and the list renders through a small
  `CaseOverviewSteps` component so the remaining steps renumber themselves.

All of them gate on `useCapabilities()` — the same `GET /api/health` answer, one place — per
`core/capabilities.py`: an unconfigured subsystem shows no entry point at all, and every
endpoint still refuses on its own, so hiding is never the only enforcement.

## Session 211 — 2026-08-30: the converter excerpt shrinks to a few dozen records

A converter generation over a 348 KB / 3006-line nginx access log failed four times in a
row with `TimeoutError`, then `502 ... upstream command exited prematurely`. The llama.cpp
logs name it precisely: the prompt was **50,039 tokens**, prefill took 91 s at ~546 tok/s,
generation ran at ~18.8 tok/s, and the process was killed at exactly `4.00.000` —
`srv operator(): cleaning up before exit...` — on all three server-side attempts. A 240 s
cap upstream (not ours: `converter_generation_timeout_seconds` is 180 and the client's read
timeout was 300) that the request could never fit inside.

The prompt was that big because the excerpt was 64 KiB of a log that says the same thing
3000 times. A first cut of this fix condensed the excerpt to distinct line *shapes* (masked
quoted runs / hex / digits, five lines per shape per block). Review found eight defects in
that machinery — a clamp that produced a phantom empty line for files just over budget, a
tail walk that orphaned stack-trace continuations, per-line request ids defeating the
masking entirely, quoted delimiters and apostrophes confusing the shape, a sample-run input
skewed toward unparseable lines, and two tests that never exercised the path they named —
and the conclusion was that the cleverness was the problem. Generated converters are best
effort by design: a frictionless start on a standard log, or a first try at a format nothing
else covers, and if that fails the analyst takes the longer route anyway.

**So the fix is a smaller sample.** `converter_sample_bytes` defaults to **4 KiB** (also the
floor, as before) — split 70/15/15 across the head, a middle window and the tail so the newest
timestamps are in it. The system message says the excerpt is deliberately short and that the
converter is for the whole file (`SYSTEM_PROMPT_VERSION` → `"4"`), and `sample_as_file` writes
**every** block rather than the head alone, so the guarded sample run sees the middle and the
tail too. On the offending log the task message drops from ~23k tokens to ~1.4k (35 lines).

Review of that cut found the byte split had no notion of a record: at 4 KiB the blocks are
2867/614/615 bytes, so a 1.5 KB JSONL line came out as a fragment with an absolute line number
and no marker, a 3 KB line made even the head a fragment (0/2 valid records — no converter
could ever pass the 50 % floor), a last line longer than 615 bytes meant no tail at all, and a
middle block could start inside a quoted multi-line CSV field. **The budget now bounds what is
shown, not what a block may hold.** `converters/sample.py` reads whole *records* — a marker
(`\n`; `\n<indent>{` for pretty-printed JSON objects and array elements; a quote-parity-aware
newline when the probe shows multi-line quoted fields) found at C speed in the same O(1)
streaming scan — at least one per block however long, with the last record as the tail
fallback, and a one-line `[{…},{…}]` array yields a head of decoded elements. `Sample.raw_blocks`
are those records (the sample-run input, a top-level array written back as one);
`Sample.blocks` are what the model sees: a JSON record with every key but long strings and
arrays cut as `…[N more chars]` / `…[N more items]`, any other line cut at its block's share.
The task header names the real block line ranges and that rule instead of a literal in the
system message. Measured on an 85-record Claude session transcript (2.8 KB median, 20 KB max
line): head/middle/tail all whole records, prompt ~4k tokens.

Docs and copy followed: `INPUT_FORMATS.md` §"The loop" steps 1 and 4 state the size, the
record rule and that the sample run sees the whole excerpt; `DEPLOYMENT.md` and the setting
help say "small on purpose"; the upload dialog's disclosure waits for the server's
`sample_bytes` (submit is disabled until then) instead of rendering a guessed default.

**Not fixed here:** the 240 s cap that killed the requests is in the llama.cpp/llama-swap
deployment, not in this repo; a file of very wide lines (minified JSON) can still be a large
prompt at 4 KiB, and the setting cannot go below it — 4 KiB is both the default and the floor,
because below it the 70/15/15 split cannot hold a whole ordinary line per block, and a block
holds at least one record however long. Such a file is what the longer route is for.

A second review of the excerpt found three more defects, all in how a leading `[` was read.
`_detect_layout` treated *any* file whose first non-space byte was `[` as a JSON array, so a
bracket-prefixed plain log — Apache `error_log`, `[2026-03-01T10:00:00Z] INFO …`, the
commonest shape there is — had `raw_decode` called at the byte after it, which happily read
`2026` as the array's first element: the whole excerpt became five numbers and the sample run
got `[2026,-0,3,-0,1]`, so generation could never succeed and it *looked* like it had sampled
the file. A leading `[` is now only an array when the next non-space character opens a
container; anything else is lines, which is what it is. When nothing decodes after that (a
truncated export), the head is raw text, and `sample_as_file` no longer wraps it in a second
pair of brackets the file never had. And `record_lines` assumed shown line *j* was raw line
*j*, which is false for a pretty-printed record re-dumped with its arrays and strings
shortened: every line after the first shortening carried a number pointing at unrelated text,
under a header that promised the numbers were absolute. A re-formatted record now numbers only
its own first line and leaves the rest of the gutter blank, the elements of a multi-line array
carry their own line numbers, and the header states the file lines each block spans
(`Sample.line_spans`) rather than counting shown lines against file lines.

## Session 210 — 2026-08-30: twelve review findings on the Visualize round (#332)

A third `/code-review 332` pass. All twelve fixed; the ones with a testable surface carry
the test that catches them.

**Two figures asserted in the caption what they never sent to the server.** A Cumulative step
offered *Group into ranges*, stored the derivation and never put `derive` on the wire — while
the caption (and therefore the PNG/SVG export and any Story snapshot) printed `derived:
grouped into 8 log-spaced ranges`. Switching away from Ranked change onto a pie left
`compare.mode: "baseline"` in the config, where all three Compare radios render unchecked
*and* disabled — invisible and unreachable — under a caption naming a comparison layer that
was never fetched. Both are now gated twice: the rail offers a derivation only when the figure
that would *result* sends one (`deriveOptionsForChart` / `resolveDeriveTarget` — a histogram
still offers it and lands on a bar), drops a comparison the newly picked figure cannot draw
with its own notice, and `normalizeChartConfig` runs on every way a config enters the app —
a URL, a saved chart, a Story snapshot — so the rail is not the only thing keeping the two
honest.

**A Table figure fired the grouped-numeric scan on a categorical X.** `groupedOn` read
`acceptsSecondField && fieldY`, and `table` declares an optional second field, so every table
with a "count distinct of" field also ran `viz/field-numeric-grouped` — a heavy ClickHouse
scan on every render and every knob change, whose result and error nothing on the page reads.
It is a modifier on the *numeric* kind only.

**Two options outlived the field they needed and answered 422.** "No field — count every
event" cleared `field` and left `options.quantity: "sum"`; clearing a table's grouping field
left `tableSortBy: "distinct_second"`. Both preconditions are now enforced where the value is
resolved (`resolveChartOptions`), which is also what the request and the Sort-by control read,
rather than at the one mutation site that happened to be found.

**Bin-edge labels named numbers that were not the edges.** `_fmt_edges` escalated precision
only on *collisions*, so edges `4000.125` / `4000.875` labelled `4,000` / `4,001` — and the
label is what a reader checks a value against, so 4000.05 sat in the bin below a label saying
it was above it. Precision now rises twice for two reasons: until every label names its own
edge (relative 1e-6, capped at six decimals — a log-spaced edge is irrational), then uncapped
until no two collide. The echo carries `edge_labels` so the caption prints the server's text
instead of rounding the floats a second time client-side.

**The calendar clipped its most recent weeks.** A `Math.max(3, …)` floor on the cell broke the
`weeks × step ≤ innerWidth` guarantee: 53 columns needed 265px, and below that the `<svg>`
clipped the overflow — which, weeks running left→right, is the newest days, under a caption
still claiming 53 weeks. The step is derived from the width, so the fit is exact at every size.

**The lanes caption mixed two scopes in one sentence.** `starts`/`ends` are counted over the
whole union; `unpaired_starts`/`orphan_ends` come from the pairing over the *kept* lanes. Two
sentences now, each naming its own scope.

**Four smaller ones.** An open interval starting within 7px of the panel's right edge drew its
arrowhead to the left of its own start, pointing at time it does not cover (the arrowhead sits
at the bar's end now, never wider than half of it). A negative bucket delta on a signed sum
rendered as `+-3.5`. Two baseline suspect windows with identical bounds collided on a React
key and one band vanished. And the Highlight box split on commas, so a DN or a user-agent
could never be highlighted — it is one value per line.

## Session 209 — 2026-08-30: five more review findings on the Visualize round (#332)

A second `/code-review 332` pass; all five fixed, each with the test that catches it.

**Marks off the drawn axis were counted and never disclosed.** `layoutMarks` computed
`offscreen` and documented it as "disclosed, not silently dropped", but nothing read it: the
overlay drew only what fit, and `markCaptionLines` built its lines from the unfiltered
server response — a chart windowed to August captioned four July marks it never drew. The
axis a figure draws is now one pure function per figure (`lib/timeDomain.ts`), which the
chart builds its x scale from *and* the caption reasons about, so the two cannot drift; each
source's caption line ends in `; N outside the drawn time axis, not drawn`. Ranges count
too — a baseline whose windows fall outside the slice drew nothing and said nothing.

**A number range that named other sources' marks.** Instant numbering is global and by time,
so two interleaved sources give one of them `#1,#3,#5,#7,#9,#11` — captioned `marks #1–#11`,
which names five rules belonging to the other source. Non-contiguous runs now read `marks 6
of #1–#11`.

**A derived table sorted by value came back in lexical order.** `field_table` ordered on the
raw `multiIf` output, and `<`, `≥` and `≤` all sort after the digits, so the ranges
interleaved (`1,000 – 2,000` before `< 1,000`) — and the `LIMIT` kept the wrong rows.
`derive.label_order_expr` orders by the label's rank in the derivation's own list, in SQL.

**The RE2 guard on `POST …/viz/marks` covered one of three regex paths.** It inspected
`m.filters.q_regex` only, so a per-field `regex` match mode, and a `view` mark whose saved
view carries a regex, ran unguarded — an RE2-only rejection surfaced as a 500. The guard is
now decided per mark, off the query about to run.

**The cumulative step's y axis assumed a monotonic total.** `yMax` was the *final* value, but
`quantity="sum"` over a signed measure is not monotonic: a series peaking at 500 and settling
at 20 was drawn against a `[0, 20]` axis. The domain is the running series' own min and max.

`docs/VISUALIZE.md` updated alongside (table sorting, marks, the cumulative axis).

## Session 208 — 2026-08-30: eleven review findings on the Visualize round (#332)

`/code-review 332` verified twelve candidates (ten confirmed, one plausible, one refuted);
all ten confirmed ones and the plausible one's neighbour are fixed here, each with the test
that would have caught it.

**Two 500s.** `field_lanes(pairing="next_end")` merged the start/end layers' parameters
with `_with_params`, which copies only the primary's `.external` — a >512-id list on the
start layer registered `vestigo_ext_0` and shipped no table for it ("Unknown table"). The
shared registry is now the source of truth after the merge. And `POST …/viz/marks` ran
`resolve_marks` on a bare `run_in_threadpool` with no `validated=`: a full scan lane
escaped as a 500 the page's busy-retry never sees, and a bad regex reached ClickHouse. It
goes through `_run_regex_guarded` with the same pre-checks as every viz GET.

**`scale` is the treat-as, on both sides of the agent boundary.** The spec demanded
`scale="ordinal"` with a derive while the page stores the treat-as (ratio/interval) and needs
it to offer the Derive control — so an agent-proposed binned chart opened with no Derive
section, and a page-saved one was *rejected* on Stories export. `chart_exec` now accepts the
source scale (`DERIVE_SOURCE_SCALES`; ordinal kept for compatibility) and resolves the
effective ordinal itself; `spec_to_stored_chart_config`, `deep_link.py` and `agent.ts`
(`deriveSourceScale`) all carry the treat-as. `docs/AGENT.md`, `docs/VISUALIZE.md`.

**The rest.** `propose_chart` warns when `open_url` cannot carry `event_ids` / `run_id` /
`collapse_routine` (the link is wider than the result). The `ChartSpec` prose is generated
from the registry (derive and mark takers) and names every figure, pinned by a test. Bin
labels take the precision the edges need instead of merging bins under one label. The
gallery drops a derivation the picked figure cannot take, and says so. The numeric probe
keeps cumulative / calendar / lanes / change and moves only the treat-as, only where the
figure admits it. `useResolvedMarks` hands out no data for a figure that draws none (the
caption printed marks under a bar). `columns: []` — the value-only table — survives the URL,
storage and the spec crossing. `compare_field_terms` resolves width/log edges once.

## Session 207 — 2026-08-30: interval lanes and the docs consolidation (viz plan D)

Plan D of the Visualize design round (steps 8 and 9 of the original nine, the last); the
reference is `docs/VISUALIZE.md` §"Interval lanes", and that document is now the durable
form of the whole round (§"History"). Stacked on ranked change (#329).

**The lane key is `field`.** The design listed a `laneKey` input beside a field-free
figure; on the field-first rail that would have put a "No field" control over a second
"Lane key" picker with the treat-as chips and the field probe applying to nothing.
Declaring `inputs={"field": "required", …}` gives the lane key the scale check, the probe
and the greyed-tile reasons for free, exactly like the bar — and `lane_key` left
`INPUT_KEYS`, so a new test pins that every key in the vocabulary is declared by a shipped
figure.

**One pairing rule, stated as the rule.** The design's two sentences about `next_end`
disagree when two starts precede one end; the shipped rule is *an end closes the most
recent open start in its lane* — nested activity draws as nested bars — and the caption
prints it verbatim. An open start runs to the slice end under an arrowhead; an orphan end
is counted, never drawn. `first_last` is one `argMin`/`argMax` scan per lane.

**Python pairing over a capped, ordered scan.** LIFO matching where orphans consume nothing
is a stack, not a running sum, so it is the pure, unit-tested `pair_intervals` over a
`ORDER BY ts, is_start DESC, event_id LIMIT rows_cap + 1` scan of the kept lanes rather
than a window function; the cap (`ChartLimits.lanes_rows`, 2,000 agent / 50,000 analyst)
is disclosed in the response and the caption, as are the lane cap (`ChartLimits.lanes`, 10
default; 20 agent / 100 analyst), the starts and ends, the open-ended and orphan counts and
the undated rows.

**`POST …/viz/lanes`.** The design named a GET; three filter sets do not fit query params.
The start and end layers are resolved as Compare layers are and pinned to the primary's
window; both are **ANDed with the current filters** — an analyst filtering to one host
expects that host's starts — which needed a `param_prefix` on `_build_where` so three
clauses can share one statement without their `p0…` colliding.

**Schema budget.** `ChartInputsSpec.pairing` / `start_filter` / `end_filter` took the tool
schemas from 42,115 to 42,285 chars; the ceiling stays at 42,500 (`docs/AGENT.md`).

## Session 206 — 2026-08-29: ranked change (viz plan C)

Plan C of the Visualize design round (step 6 of the original nine); the reference is
`docs/VISUALIZE.md` §"Ranked change". Stacked on the time figures (#328).

**The first figure that requires Compare.** `change` is *defined* by two windows, so the
registry row gains `requires_compare=True` (generated as `requiresCompare`) with three
consequences: the rail disables Compare's *Off* and says why, picking the figure with
Compare off switches Compare to Baseline together with the figure and says so in
`autoNotice`, and `propose_chart` refuses a `change` without a comparison layer by name.
A config that still arrives without one (a URL, a saved chart) gets an empty state that
names the fix rather than a blank chart.

**Share of window, never count.** The two windows are rarely the same size, so the encoded
quantity is each value's share of its own window; the pure `rank_change_rows` turns two
count maps and two totals into `rose | fell | new | vanished | same` rows ranked by |Δ
share|, and the live test pins the case the figure exists for — the same count in both
windows *fell* when the second window doubled. `field_change` is four scans in two
parallel pairs: top-N per window (both on the primary's derived expression, as
`compare_field_terms` does), the union, and one count scan per window over the union that
also yields the window's non-empty total. The union is capped (`ChartLimits.change_union`,
30 agent / 200 analyst) and the omitted count is a caption line, like every other honesty
disclosure here (both totals, the per-window top-N, the union size, new/vanished counts).

**Endpoint decision.** The design named `GET …/viz/field-change`; it ships as
`kind="change"` on `POST …/viz/compare`, because two filter sets do not fit query params
and the two-layer resolution, window pinning and derive rule already live there.

**Schema budget.** `ChartOptionsSpec.layout` took the tool schemas from 42,044 to 42,115
chars; the ceiling stays at 42,500 (`docs/AGENT.md`).

## Session 205 — 2026-08-29: cumulative step and calendar heatmap (viz plan B)

Plan B of the Visualize design round (steps 5 and 7 of the original nine); the reference is
`docs/VISUALIZE.md` §"Cumulative step" and §"Calendar heatmap". Stacked on marks (#327).

**A third field state.** `cumulative` and `calendar` are the first figures that chart every
event without a field and a field's values with one: `inputs={"field": "optional"}` beside
field-free (`time`, `punchcard`) and field-required. `requires_field` is false for both,
the rail offers *No field* and keeps the figure whichever way the analyst goes, the page's
"pick a field" empty state stays out of the way, and `propose_chart` accepts either.

**One quantity rule on both sides.** The cumulative step accumulates `events`, a `sum` or
`distinct` values, chosen by `options.quantity` or resolved from field and scale by the same
rule in `resolveChartOptions` and `chart_exec`; the registry refuses `sum` over anything but
a measure and `distinct` over a measure by name, and a field under `events` is a warning.
The `distinct` quantity merges per-bucket `uniqExactState`s through a window function
rather than adding per-bucket distinct counts — 2/3/3/4 users, not 2/4/4/6 — and the live
test pins the difference. Values that could not be summed or counted are `unparsed` and
captioned. The step is `curveStepAfter` and never interpolates; marks draw on it, Compare
does not.

**UTC by decision.** The calendar's day boundary is UTC and the caption says so: no
timeline or user carries a display timezone, and the punch card already pins UTC. The figure
keeps the latest 53 ISO weeks (`ChartLimits.calendar_weeks`, the same for the agent and the
analyst — a display truth, not a context budget), discloses the weeks and the events it
dropped, and draws an empty day as an outlined cell distinct from the ramp's lowest step.

**Schema budget.** `ChartOptionsSpec.quantity` took the tool schemas from 41,947 to
42,044 chars; the ceiling moved to 42,500 (`docs/AGENT.md`).

## Session 204 — 2026-08-29: marks, `open_url`, the schema budget (viz plan A)

Plan A of the Visualize design round (step 4 of the original nine); the reference is
`docs/VISUALIZE.md` §"Marks". Stacked on the table figure (#326).

**One resolution, three callers.** A mark is stored as a *source* (`MarkSource`: an
events filter, a saved view, a baseline definition, or a typed instant/range) and
`agent/marks.py::resolve_marks` is the only place a source becomes drawable marks — behind
`POST …/viz/marks` (the page and the agent's card, through one hook), `execute_chart_spec`
(the agent) and, through it, the Stories export. An `events`/`view` source goes through the
new `EventQueryService.mark_instants`: the earliest N dated events under the filter plus a
`countIf` pair, so an undated event is counted and disclosed rather than drawn at the
sentinel year. A baseline's windows are drawn as declared; nothing is derived from them.

**Provenance on every mark, the cap in every caption.** Each resolved mark carries
`{kind: event | view | baseline | analyst, …ids}`; each source reports `count`, `shown`,
`overflow`, `undated`. The per-source cap is `viz_marks_max` (setting, default 50) on the
page and `ChartLimits.marks_per_source = 20` for the agent, and `markCaptionLines` writes
one line per source naming its provenance, its instant numbers, the cap and the undated
count. `lib/marks.ts` numbers instants in time order across sources and lays them out
(alternating label tiers when crowded, stacked bands), and both `MarksOverlay` and the
caption read it, so `#3` on the figure is `#3` in the text.

**The rail's eight sources.** `MarksEditor` lists the chart's marks with their resolved
status and adds one from event id · tag · confirmed findings · custom filter · baseline
definition · saved view · instant · range. *Confirmed findings* is an `events` mark over
disposition event ids, which is why the marks codec now carries `eventIds` inside the
filter payload while the Explorer's `ids` stay session-only. Rendered after Metric (Compare
and Metric belong together), before the per-chart options.

**Agent parity and `open_url`.** `ChartSpec.marks` is one `ChartMarkSpec` with a
kind-validator rather than a five-member union — the tool schema is budgeted. Every
`propose_chart` result now carries `open_url`, the Visualize deep link for that exact
figure, for external `/mcp` clients that get no card: `agent/deep_link.py` mirrors the
page's URL codec and a shared fixture is asserted from both sides (backend produces it,
`agent.test.ts` parses it back). The "treat as" vocabulary note joins both prompts. The
schema budget was measured 40,953 → 41,947 and the ceiling moved to 42,000, recorded in
`docs/AGENT.md`.

## Session 203 — 2026-08-29: the table figure (viz step 3/9)

Step 3 of 9 from the Visualize design round; the reference is `docs/VISUALIZE.md` §"Table
figure". Stacked on step 2 (#325).

**The value inventory made bounded.** `EventQueryService.field_table` is the top-N of
`iter_field_inventory` (#295) on the *same* SELECT core — `_inventory_select_core`, lifted
out so a row's count and seen range can never mean one thing in a streamed inventory and
another in a table; a live test asserts the two agree cell for cell. It adds `share` against
the slice's non-empty total, `uniqExactIf` of a second field per row, any column as the sort
(`NULLS LAST`, `val ASC` tie-break), and derivations. Two parallel scans as `field_terms`.

**The remainder-row rule.** Whenever the top-N cut anything the response carries a
`remainder` and the figure draws it as a final italic row — count and share only, because a
seen range for "everything else" would be a third scan for a row that exists to say "there
is more". Absent exactly when nothing was cut; the shown shares plus the remainder's sum to
one, and the caption names the share denominator.

**Two renderings over one row model.** `lib/tableRows.ts` decides columns, cell text,
highlighting and the remainder once; `TableFigure` draws an `<svg>` (page, PNG/SVG export,
in-cell bar encoding count only) and `TableHtml` a real `<table>` that `ChartMarks
tableAs="html"` selects for Stories and the HTML export. CSV is built client-side from the
same model — caption as `#` lines, raw shares — and offered as a third export format only
while a table is on the canvas.

**The rail's first `columns` renderer** — a checklist, *distinct* disabled until a second
field ("Count distinct of (optional)") is set — plus sort, direction and highlight options.
`RAIL_RENDERED_INPUTS` grew in the same commit. Agent parity: `ChartSpec.inputs.columns`,
`options.table_sort_by/table_sort_dir/highlight`, two refusals, `ChartLimits.table_rows`;
Stories round-trip `inputs` unchanged.

## Session 202 — 2026-08-29: derivations — ranges and calendar parts (viz step 2/9)

Step 2 of 9 from the Visualize design round; the reference is `docs/VISUALIZE.md`
§"Derivations". Stacked on step 1 (#324).

**A derivation is a change of scale and nothing else.** `db/derive.py` owns the one
`DeriveSpec` (two kinds: `bins` — number → ordered ranges, `width`/`log`/`custom`; and
`time_part` — a timestamp-valued attribute → hour/weekday/day/week/month), the edge maths,
the human labels and the SQL (one `multiIf` over `_finite_float_cast`, or the existing
`TIME_FIELD_SPECS` expression over `parseDateTimeBestEffortOrNull(…, 'UTC')`, so a derived
hour can never disagree with `time:hour_of_day`). Nothing knows what an IP or a URL is — that
stays with the enrichers. Both kinds yield the ordinal scale.

**Resolved once, in ClickHouse, before aggregation.** `EventQueryService._resolve_derive`
runs the one-scan min/max pre-flight `width`/`log` bins need, under the same WHERE as the
aggregation that follows, and returns the expression plus a `ResolvedDerive` whose `echo()`
(`labels`, `edges`, `negative_bin` / `part`, `timezone`) rides on every response — a chart of
ranges that did not say where its bins are would be uncheckable. `log` gives values ≤ 0
their own disclosed bin; unparseable values map to `''` and fall out through the existing
guard, counted nowhere, and the caption says so. Threaded through `field_terms`,
`compare_field_terms` (both layers on the **primary's** resolved expression, which is also
part of the baseline-cache key), `field_value_timeseries`, and `field_pivot`, where a derived
x axis is a bounded domain like a cyclical `time:` axis — every label in order, empty or not.

**Endpoints and parity.** `derive` on `field-terms` / `field-timeseries`, `derive_x` on
`field-pivot`, `CompareRequest.derive`; malformed → 422 in the validator's words, a `time:`
field → 422 rather than a 500; a derived terms request bypasses the M24a stats cache.
`ChartSpec.derive` is the same model in nested-argument position (`ChartDeriveSpec`, an
`ObjectArgModel`, so the schema-slimming and stringified-object tolerance both apply); four
registry-driven rejections; `describe_field.derivations`. The tool-schema budget moved
39,382 → 40,213 chars, ceiling now 41,000. Stories export round-trips `timePart` ↔
`time_part`.

**The rail.** A **Derive** step between *Treat as* and *Figure*, present only when the
treat-as admits a change (`lib/derive.ts`). The gallery and every legality check run at the
effective scale; a bar over ranges defaults to value order and `BarChart.valueOrder` follows
the echoed labels rather than the alphabet; the caption gains a `derived:` line with the
resolved edges. The greyed-figure fix deferred from step 1: clicking a greyed figure that
exactly one derivation would light applies it and says so; with two candidates the tile
stays inert. A derived chart also suppresses the numeric auto-probe's re-pick, which would
otherwise have dropped it silently.

## Session 201 — 2026-08-29: figure registry, `ChartConfig` v2, the field-first rail

Step 1 of 9 from the Visualize design round (durable form: the new `docs/VISUALIZE.md`).
Behaviour-preserving for every existing chart; the foundation the six new figures land on.

**The chart table became a figure registry.** `agent/chart_meta.py` rows now declare
`inputs` (what the figure asks for, from a fixed eight-key vocabulary), `derives` (which
change-of-scale derivations it admits), `question` (the forensic question it answers) and
`supports_marks`. `requires_second_field` / `accepts_second_field` / `multi_field` are
read-only views over `inputs`, so the generated `chartMeta.ts` and `propose_chart` keep their
vocabulary. Two tests pin the registry to the rail from both sides: a row may only declare
keys the rail renders (`RAIL_RENDERED_INPUTS`), and the rail renders exactly the declared
keys for every figure.

**`ChartConfig` v2.** Three new slots — `derive`, `inputs`, `marks` — all empty until their
figures ship, plus `c_derive` / `c_inputs` / `c_marks` in the URL codec (JSON, dropped
field-by-field when malformed). A stored `v: 1` row upgrades losslessly on read
(`upgradeChartConfig`); the backend writes `CHART_CONFIG_VERSION = 2`. Saved charts, story
blocks and links keep meaning what they meant.

**The rail reads field first.** `ChartRail.tsx` leaves `VisualizePage.tsx` (2218 → 1358
lines) as its own component: Field (with *No field — count every event* as the first entry,
so the top control is never inert) → **Treat as** (four plain-language chips — Categories,
Ordered categories, Number or time, Measure — with the Stevens term in the tooltip) →
**Figure** (a thumbnail gallery; illegal figures greyed with their reason) → the figure's
declared inputs → Compare → Metric → Options. The scale radio and the presets drawer are
gone; each preset's question is its figure's `question`. Every automatic re-pick still
names itself, now in the rail's own words ("`src_port` looks numeric — treating it as a
measure; change this if its values are categories to you").

Declined and removed from the roadmap: facetting / small multiples — a shared axis across
panels is a correctness trap, and figures are assembled side by side in the report instead.

Next, in order: derivations (bins, calendar part), the table figure, marks, cumulative
step, ranked change, calendar heatmap, interval lanes.

## Session 200 — 2026-08-29: the top-values list stops being a dead end (#296, #297)

Two limits an analyst kept hitting, both in the terms path.

**#296 — "+ N more in other values" is now a button.** The per-value histogram modal
(`FieldHistogramModal.tsx`) fetched a hardcoded top-50 and rendered the tail as inert text.
It now holds a `termsLimit` in state, expanding by 50 per click up to 500 — the
`field-terms` endpoint's own `limit` cap, and the point past which a scroll container full of
rows stops helping. The query re-asks for a longer prefix rather than appending a page (the
window aggregation in `_field_terms_impl` recomputes `other_count` for whatever limit it is
given, so the tail count stays truthful after every expansion), and `keepPreviousData` keeps
the rows already on screen while the bigger answer is in flight. No backend change: the first
load at 50 still resolves from the `field_stats` cache, and `merged_field_terms` already
returns `None` above its cached top-N, so every expansion falls through to the live path
rather than being answered from a cache that cannot see that far.

**#297 — per-chart-type Top-values ceilings, with an escape hatch.** The slider capped at
20 (timeseries) / 50 (terms) and `resolveChartOptions` clamped to the same numbers, so a
config from a URL, a saved chart or an agent could not exceed what the controls allowed.
Both now read `TOPN_MAX`, keyed by chart type rather than by data kind: bar reaches the
backend's 500, the timeseries charts stop at `series_limit`'s 50, and pie/waffle stop at 50
for legibility rather than for anything fetchable — a pie past a few dozen slices is
unreadable whatever the slider permits. These match `ANALYST_CHART_LIMITS` in
`agent/chart_exec.py`, so an exported chart freezes what the analyst could ask for by hand.
The slider keeps a shorter `TOPN_SLIDER_MAX` travel for the common range and a numeric input
beside it reaches the ceiling, with a line naming it — the old behaviour silently clamped
instead. A test asserts no slider ceiling can exceed the hard one it escapes from, which is
the exact failure mode the issue was filed for.

**Review follow-ups (same session).** Six findings from the review of #323, all in the
surfaces the two issues touched.

- `keepPreviousData` was keyed on the whole query key, but the modal's `filters` change
  *while it stays open* — a row's Filter IN/OUT narrows the Explorer without closing it. The
  kept rows then showed pre-filter values, counts and `distinct` as if they were scoped to
  the new filter, with nothing on screen saying otherwise. The placeholder now compares the
  previous query's key against the current *scope* (everything but `termsLimit`) and blanks
  to the spinner on anything else.
- The numeric Top-values box could not be cleared: `Number("")` is `0`, which is finite, so
  the first Backspace committed `topN: 1` and the digits of "300" landed on top of it —
  breaking the one thing the control exists for. It is now a small `TopNInput` holding its
  own draft string; an empty or out-of-range entry stays a draft until blur.
- Slider `min` (3) and box `min` (1) disagreed, so a typed 1 left the DOM clamping the thumb
  to 3 while state said 1, and dragging to exactly 3 fired no change event at all. Both read
  a shared `TOPN_MIN`.
- The line naming the escape hatch only rendered on the terms branch. The gap is *widest* on
  heatmap/line (slider 20, ceiling 50), where the number box was therefore undiscoverable.
- "These match `ANALYST_CHART_LIMITS`" held only for bar: `chart_exec` caps by `data_kind`,
  so an agent-proposed or exported pie/waffle could carry 500 slices the UI clamps to 50.
  `TERMS_TOP_N_BY_CHART` now narrows the ceiling per mark (`docs/STORIES.md`).
- Raising bar to 500 made the *vertical* orientation degenerate — fixed 300px frame, so
  sub-pixel bands and overdrawn labels — while only the horizontal branch grows with the row
  count. `barReadabilityWarning` gives it the advisory pie already had, with a one-click
  switch to horizontal.

**Second review round (same session).** Six more, all in the controls the first round built.

- The slider's `value` was clamped with `Math.min(topN, TOPN_SLIDER_MAX)`, which reintroduced
  at the ceiling exactly what unifying `TOPN_MIN` had just fixed at the floor: with a typed
  300 the thumb already sits at bar's 50, so dragging it there changes nothing in the DOM and
  fires no change event — the chart went on drawing 300. It now also commits on release
  (`onPointerUp`, and `onKeyUp` for the keys that actually move a range input — a bare Tab
  into the slider fires keyup on it too), since a range input's value follows the pointer, so
  what the thumb reads when the analyst lets go is the answer they gave.
- The vertical-bar readability warning counted *categories*, but compare mode draws two
  half-width sub-bars per band. The threshold was therefore twice as permissive exactly where
  the crowding is worst, and the text named half the bars on screen. It counts bars now, and
  the parameter is named for it.
- `resolveChartOptions` capped `topN` but never floored it, and `c_opts` arrives from the URL
  as `JSON.parse`d, unvalidated data. A shared or hand-edited link carrying `{"topN": 0}`
  resolved to `0`, reached `/viz/field-terms` as `limit=0`, and came back 422 — a permanently
  blank chart with nothing on screen to explain it; `"x"` did the same as `NaN`. `clampTopN`
  now floors, caps, rounds, and falls back to the default for anything that is not a number.
- The exact-value box committed every in-range keystroke, so typing "500" spent three gated
  ClickHouse scans (5, 50, 500) on the way to one answer. It debounces by 400 ms, which keeps
  the live preview the control is built around; blur and Enter still commit immediately.
- Enter did nothing in that box, so an entry above the ceiling sat on screen — uncommitted and
  unclamped — until the analyst happened to click elsewhere. It now commits with the same
  clamp as blur.
- `FieldHistogramModal` kept an expanded `termsLimit` across a Filter IN/OUT, which already
  blanks the rows on purpose. The first fetch of the new scope then asked for more than 50
  values, and `merged_field_terms` serves nothing above 50 out of the `field_stats` cache — so
  the cheap path stayed skipped for as long as the modal was open. A scope change resets to
  the first page.

**Third review round (same session).** Five more, all downstream of the raised ceiling.

- PNG export failed on exactly the charts the 500 ceiling makes reachable. A horizontal bar
  chart sizes itself to its row count, so 500 rows is a ~13,000px `<svg>` (~21,000px in
  compare mode) and `downloadChartPng` rasterized at `height * scale` — past the 16,384px a
  canvas may be at *any* resolution, at which point `toBlob` yields `null` and the analyst
  reads "PNG export failed", or worse gets a blank image. `effectiveExportScale` now lowers
  the requested scale to what the canvas limits allow (dimension *and* Chrome's 2^28-pixel
  area cap), and `ExportControls` says which scale it actually used and points at SVG for
  full detail. A silently downscaled export would make the resolution picker lie.
- Expanding the top-values list crosses a computation boundary the analyst could not see. An
  unfiltered first page comes from the `field_stats` cache, whose per-value counts are exact
  but whose cross-source top-N merge is approximate and whose `distinct` is a
  max-across-sources; the cache answers nothing above 50, so the first "+ N more" click falls
  through to an exact live scan that may reorder or replace values *inside* the 50 already on
  screen. The response has carried a `cached` flag all along and the modal ignored it. It now
  names the cache while it is serving, marks the approximate distinct count with `≈`, and
  says the list was re-read once the answer becomes live.
- The Top-values slider committed on every intermediate step while the box beside it
  debounced for the very same reason — dragging a bar chart's full travel was ~50 gated
  ClickHouse scans and ~50 history entries for one gesture. The slider now keeps a live draft
  (the label still tracks the thumb) and commits once, on release.
- Committing on release also meant a press-and-release on the *pinned* thumb — no drag, no
  change event, nothing moved — rewrote a typed 500 down to the slider's 50. A release now
  only commits if something moved on the way to it; above the slider's range the label and
  the exact box stay authoritative.
- The expanded list renders every row it is given, and each carried two Radix `Tooltip`
  roots: ~1,000 of them mounted for the ~14 rows the scroll container shows. The three
  per-row icon buttons use native `title` (plus `aria-label`) now, which is what the row's
  focus button already did.

## Session 199 — 2026-08-28: the release workflow could never reach ClickHouse

CI on `main` has been green since 1.15.2, but the **Release** workflow has failed on every tag
since v1.13.0 — so no tag has published an image or a GitHub release in four versions. The
backend job died at `pytest` in ~50s with `ClickHouse — HTTP 401 ... Code: 194 ...
Authentication failed`.

`ci.yml` sets `CLICKHOUSE_SKIP_USER_SETUP: 1` on its ClickHouse service: the image's `users.d`
drop-in restricts `default` to localhost, the job connects across the docker bridge, and every
query is refused with 194 while `/ping` — which takes no user — still answers Ok, so the
container looks healthy right up to the first query. `release.yml` gates on "the same checks as
CI" but never got that env var when ci.yml did. Added it, plus `POSTGRES_HOST_AUTH_METHOD:
trust` for the connection-cost reason ci.yml documents. Both are throwaway service-container
settings; neither is a deployment.

Remaining drift, deliberately left: release.yml has no `ruff format --check` step and no
`if: !cancelled()` gating, so a tag can pass checks CI would fail on.

Also adopted nine of the ten open dependabot bumps — frontend `@tanstack/react-query`,
`vite`, `@vitejs/plugin-react`, `oxlint`, three `@types/*`; backend `clickhouse-connect` and
`ruff` (to 0.16.5, two patches past the PR).

The tenth, `mcp` 1.28.1 → 2.1.1, cannot land, and not because of our code. mcp 2.x renames
`FastMCP` to `mcp.server.mcpserver.MCPServer`; that migration is small here — the internals
`agent/tools.py` reaches for (`_tool_manager.list_tools()`, `Tool.parameters`,
`Tool.fn_metadata`, `remove_tool`) are unchanged, and `server.settings.{stateless_http,
streamable_http_path,transport_security}` simply became `streamable_http_app()` kwargs. But
the chain is `pydantic-ai-slim[mcp]` → `fastmcp-slim[client]` → `mcp`, and the `<2.0` cap is
**fastmcp-slim 3.x's**, one hop further down than uv's error message attributes it —
pydantic-ai itself already allows `fastmcp-slim<5`. So the resolver refuses no matter what we
rewrite. Pinned `mcp<2` and told dependabot to skip 2.x rather than have it reopen an
unmergeable PR weekly.

Because an ignore rule is a thing you stop seeing, the trigger is recorded twice: a standing
decision in `ROADMAP.md`, and `tests/test_dependency_guards.py`, which asserts the installed
fastmcp-slim *still* caps `mcp<2` and fails with the migration steps the day it does not.
fastmcp-slim 4.0.0b5 already requires `mcp>=2.0`, so 4.0 going stable is what fires it — and
it forces the move rather than merely allowing it, since our own pin would make that bump
unresolvable.

## Session 198 — 2026-08-28: PR #308 review, second pass (four more true-statement bugs)

A second review of the same branch. One in `FieldCombo`, three in the Visualize rail — and all
four are the branch's own theme: a control that moved without saying so, or said something that
was not true of the chart on screen.

- **Enter committed a label as a token.** The list filters on the label *and* the token, but
  Enter's exact-match checked only the token — so typing what a row displays narrowed the list
  to that one row and then committed the display text as free entry: `series_field = "Display
  name"`, `user_agent` where the option is `attr:user_agent`, the literal `No grouping` where
  the row means "clear this". A unique label match now selects its row; an ambiguous one still
  falls through to free text rather than guessing which row was meant.
- **The Y picker claimed the wrong problem, in the other direction.** Session 197 fixed X→Y by
  clearing Y and saying so. Y→X had no guard: the Y list drops whatever X holds, but the box
  takes free text, so typing X's token into Y committed it and disclosed it as "not in this
  timeline's reported fields" — about a field that plainly is. There is no mirror of the
  takeover to make here (X is the axis the chart is built on, and clearing it would only have
  the defaulting effect refill it with a field nobody picked), so the pick is refused and the
  reason is said at the picker that refused it.
- **The auto-notice outlived the chart it was about.** The invariant "never shown under
  `chartRefLive`" was in the docstring but not in the render. The page does not remount when a
  saved chart is opened — `c_chart` is a param on the same route — so a notice from the chart
  the analyst was building survived onto a stored chart the rail re-picked nothing for.
- **The non-numeric probe moved two controls in silence and wiped a standing notice.** Its
  branch set the notice to `null`: a ratio Box plot became a nominal Bar with no word about it,
  and the "Group by cleared" line the analyst's own edit had put there a few hundred
  milliseconds earlier went with it. It now names exactly which of the two controls moved, and
  returns early when neither did — landing on the scale and type the chart already has is not a
  change and must not claim to be one.

Six new tests; the suite is 1010 green. The Visualize page tests now mock `dispositionsApi`:
every chart query waits on `scopeReady`, so without it the numeric probe never fired and the
existing "never probes a time field" assertion was passing on a page that probed nothing at all.

## Session 197 — 2026-08-28: PR #308 review fixes (the one field picker, and what it says)

Eleven findings from the review of the session-195/196 branch. Nine are in `FieldCombo` itself,
which is now on five surfaces at once — so each of them was one bug in five places.

- **The list was mouse-dead inside the export dialog.** A modal Radix layer sets
  `body { pointer-events: none }` and re-enables it on its own node only, so a list portaled to
  `document.body` swallowed every row click and hover; the keyboard was the sole way through.
  `pointer-events-auto`, and a z-index above the dialog's.
- **Escape closed the surface around the combo.** `preventDefault()` without `stopPropagation`
  let the key reach `InvestigateSheet`'s `window` listener — reverting a half-typed token threw
  away every knob value beside it — and Radix's capture-phase `document` handler shut the export
  dialog before the combo saw the key at all. The documented "Escape reverts a draft" contract
  was unreachable on both surfaces. It now listens on `window` in the capture phase, the one
  position ahead of both, and only for the key pressed inside its own container.
- **Two disclosures that named the wrong problem.** The unknown-field note fired while the
  options query was still in flight (every Visualize load carrying `c_field`), and again on the
  Y picker whenever X was set to the token Y already held — a field that plainly *is* in the
  timeline, reported as not. The first is gated on the list being loaded; for the second, taking
  Y's token into X now clears Y and says so, which is the same disclosure the rest of #298 makes.
- **Enter on an emptied box committed `""`** to callers with no empty option — `logTemplates`
  and the sequence-motif query both issued a fieldless request, a state the `<select>`s this
  replaced could not reach. The empty commit now needs an empty option to commit *to*; the
  method knobs, which offer one as the method's own default, are unchanged.
- **The keyboard walked an invisible highlight.** These lists run to hundreds of tokens — the
  reason the control exists — inside a `max-h-48` box, so arrowing past the sixth row moved a
  highlight off-screen and Enter committed something the analyst could not see. Also
  `aria-activedescendant`/`aria-controls`, absent since the Radix `Select` was replaced.
- **A 150 ms blur timer with no owner** could wipe a draft typed after a refocus. Tracked and
  cleared, on focus and on unmount.
- **Two more notices that were not true.** The scale radio blamed the scale for a clamp the
  *field* forced (`time:date` excludes the numeric marks at any scale), and wrote "on a interval
  scale"; and a preset — an explicit pick of type, scale and field at once — left the previous
  auto-notice standing under the chart type the analyst had just chosen, which is the one thing
  that notice must never do.
- **The correlate picker accepted free text with no way to disclose it.** Its `value` is
  permanently empty, so the unknown-token note could never fire, and a typo became a matrix chip
  that came back empty. The set there is closed, so it is now closed.

Nine new tests; the suite is 1004 green.

## Session 196 — 2026-08-28: the Visualize rail reads in dependency order (#298)

The rail read Field → Scale → Chart type while the dependency ran the other way. Landing on
the default Time histogram — which charts no field — left the *topmost* control inert, and the
only way to discover why was to change a dropdown below it. Two more controls moved on their
own with no explanation.

- **Reordered:** scale of measurement → chart type → field → second field → Compare → metric →
  options. Scale gates which chart types are legal; the chart type decides whether a field
  means anything. Top-down, that is now a sentence. The page header comment, which promised
  the old field-first model, was rewritten in the same commit rather than left stating the
  opposite of the code.
- **The inert field state explains itself.** Instead of a bare greyed `— event count —`, it
  says the time histogram counts every event so it charts no field, and offers a one-click
  "Chart a field instead (Bar)". This is the contract Compare already kept — always rendered,
  disabled with the reason — now applied to the control that needed it most.
  `firstFieldChartingType` picks the target: `defaultChartTypeForScale` alone would not do,
  because its preference list ends in `time`, which is itself field-free.
- **Every automatic re-pick names itself.** One `autoNotice` under Chart type, set by the scale
  radio when it clamps an illegal chart type and by both field probes when they choose a scale
  ("`attr:src_port` looks numeric — scale set to ratio, chart set to Histogram"). Cleared by
  the analyst's next explicit chart-type or field pick, since their own choice needs no excuse;
  never shown under `chartRefLive`, where the config is the analyst's, not a probe's.

Six new tests; the suite is 987 green. `components/viz/ChartRail.tsx` is still worth
extracting from this 1,500-line page — deliberately not folded in here, since it would bury a
UX fix inside a large-diff refactor.

## Session 195 — 2026-08-28: one field picker everywhere

Six surfaces asked "which field?" six different ways, and none of them let an analyst type.
Two native `<select>`s in Investigate (log templates, sequence patterns), one in the export
dialog, two Radix `Select`s in Visualize and the compare-filter editor, and a bare inline
`<select>` for the method knobs. On a timeline carrying a few hundred `attr:*` tokens, a
dropdown is the wrong control — and none of the six could reach a field the cardinality
inventory had not caught up with yet.

- **`components/ui/FieldCombo.tsx`** — the one control. A text input that opens its full list
  on focus (browse, exactly as the dropdowns did), filters as you type against both the token
  and the label, walks with ↓/↑, and commits whatever you type when nothing matches. The box
  shows the raw token because that is what every caller stores and what an analyst would type;
  the pretty label and its hint (cardinality, `(time field)`, distinct counts) live on the
  rows. Escape reverts a draft without the caller ever seeing it.
- **Free entry, disclosed.** A committed token that is not in `options` renders a muted "not
  in this timeline's reported fields" note. It never blocks — the inventory legitimately lags
  a source ingested a minute ago — but a typo used to commit silently and come back as an
  empty chart or a scan that found nothing, with nothing naming the cause.
- **`lib/useAnchoredDropdown.ts`** — the portaled fixed-position anchoring (flip above when
  there is no room below, re-place on scroll/resize) extracted verbatim from `TagInput`, which
  had the only copy. `TagInput` now calls the hook, so there is one implementation rather than
  two to keep in step.
- **Six call sites converted.** `PatternsView`'s `<optgroup>` Standard/Dynamic split survives
  as the combo's section headers; `MethodFieldSelect` uses the borderless `inline` variant so
  it still reads as part of the knob's sentence; `ExportDialog`'s placeholder still names which
  of its three states the list is in. `CompareFilterEditor`'s *value* control stays a `Select`
  on a bounded `time:` field — its domain is complete and its canonical values are opaque
  ("1" is Monday), which is the one place free text builds a filter matching nothing.

Thirteen new tests pin the contract; the suite is 981 green.

## Session 193 — 2026-08-28: PR #305 review fixes, and a CI job that could not pass

### PR #305 review fixes (the chart lane's last unwired surface)

Three findings from the review of the session-192 branch.

- **The Visualize page never opted into `busyRetry`.** Its eleven gated aggregations — more
  than the rest of the app together — fell back to the global `retry: 1`, so a busy lane
  (a story export holding foreground slots, or three other panels) turned a 5-second wait
  into an error toast where the old unbounded queue had always produced an answer. Every
  *smaller* surface had been wired up; the biggest one was missed, which is the shape of
  gap a per-call-site opt-in with no type behind it produces. `busyRetryCoverage.test.ts`
  now fails any component that issues a `_foreground_scan` call through `useQuery` without
  it.
- **The busy badge only rendered under `isLoading`.** False the moment a key has data, so a
  filter change on a chart that was already drawn retried silently for four minutes behind
  stale marks and then failed. `TimelineHistogram` had added a `waiting && isFetching`
  overlay for exactly this; `ChartCanvas`, `FieldHistogramModal` and the Visualize canvas
  had not. Also names the Visualize page's active query once — the spinner, the badge and
  the grouped box/violin case (which had no spinner at all, since `numericQuery` is
  disabled while `groupedOn`) now read the same one.
- **The chart lane's thread reservation is a bound, not a reservation.**
  `detect_scan_memory_budget` divides by `N + 2` so the lane's memory comes out of the
  detectors' share; `detect_scan_max_threads` still divides by `N` alone, so the lane's
  threads are *added* to a box a full heavy gate already saturates — `2 x 10 + 4 x 5 = 40`
  on 20 cores. Kept as is (closing it means halving every sweep on a box where no chart is
  open, and it is a quarter of the 8x it replaced) and the prose corrected instead:
  `_scan.py`'s two docstrings, `ANOMALY_DETECTION.md`, `DEPLOYMENT.md` and the sizing
  calculator now state the additive bound and point at
  `VESTIGO_STAT_SCAN_MAX_THREADS = cores / (N + 2)` for operators who want the strict
  version.

### CI's ClickHouse answered `/ping` and refused every query

The backend job had been red since 2026-08-26 and burned the runner's 6-hour default
timeout on every run, on `main` and on PR #305 alike. Not a regression in the branch under
review: `bcc3683` (PR #304) moved CI from `clickhouse-server:24` to `26.6.1.1193` to match
the tag `docker-compose.yml` pins, and images from ~25.x on ship a
`users.d/default-user.xml` that restricts the `default` user to `::1`/`127.0.0.1`. The job
reaches the service container through a published port, so its packets arrive from the
docker bridge and every query came back `401` / exception code 194 (REQUIRED_PASSWORD).
`docker-compose.yml` has mounted `deploy/clickhouse/allow-default-network.xml` against
exactly this since the image bumped; a service container starts before `actions/checkout`,
so there is no repo file to mount and CI never got the equivalent.

- **`CLICKHOUSE_SKIP_USER_SETUP: 1`** on both jobs' ClickHouse service. The entrypoint
  drops the drop-in entirely, which is the mount's effect without a file. Test-only —
  nothing in `.github/` is a deployment.
- **The reachability probe asks a question the failure could answer.** `/ping` is served
  before any user is resolved, so it said `Ok.` to a server that would refuse every
  statement — and `tests/conftest.py::pytest_configure` exists precisely so a run that
  cannot pass stops in a second instead of ending in a wall of red. It now runs `SELECT 1`
  as the configured user and reports ClickHouse's own diagnosis on failure.
- **`timeout-minutes: 60` on the backend job.** The suite runs in ~25 minutes; a hung test
  should not hold a runner for six hours to teach nobody anything.


## Session 192 — 2026-08-27: PR #306 review fixes (the chart lane's other half)

Seven findings from the review of the session-191 branch. The cancellation plumbing came
through clean; every real problem was in the *sizing* half of the change — the reservation
had been re-derived for memory and not for anything else.

- **The chart lane's threads were unaccounted.** `foreground_scan_settings()` reused the
  heavy `max_threads`, which is auto-sized as `cores ÷ N` precisely so a *full* heavy gate
  saturates the box (#301). Four chart slots at that width, each able to fan out two
  queries, put 8× a detector's threads on top of a box already fully committed — 100
  threads on 20 cores at the default concurrency. So four charts rendering during a sweep
  made every query slower, including the sweep, which is the opposite of what #300 is for.
  `detect_foreground_max_threads()` applies the same reservation the memory cap uses: the
  two slots the heavy divisor holds back, split across the gate. Not divided again by the
  fan-out width — two waves at half the heavy width is one heavy slot's threads, which is
  what the reservation already allows.
- **The busy retry window was seven times what it claimed.** `BUSY_RETRY_LIMIT` was sized
  against the 5 s `Retry-After` alone, but each attempt *also* parks for
  `FOREGROUND_WAIT_SECONDS` server-side before the 503 exists — so "about two minutes" was
  really about fourteen, and a wedged lane spent a quarter of an hour looking like a slow
  one. The wait is 5 s now rather than 30, which also fixes the second half of it: a parked
  caller holds one of anyio's threadpool tokens for the whole wait, so several panels
  retrying against a full lane could occupy the pool with *waiters* — including the poll
  `run_scan` uses to notice a disconnect, the very path `_kill_detached` avoids the pool for.
  Answering "busy" quickly frees the token and lets the (cheap) retry do the waiting.
- **The filter rail's autocomplete could raise a toast.** `vizApi.fieldTerms` became
  foreground-gated, so it can answer 503 — and that one query had neither `busyRetry` nor
  `silentError`, so typing in a filter field during a busy lane popped "scan lane busy" from
  a background suggestion nobody asked for. It usually never reaches a scan at all (an
  unfiltered call is served from the `field_stats` cache); this is the cache-gap path.
- **…and it paid for a scan it discarded.** The totals wave exists so the tail beyond the
  top-N can be reported *spillably*, but the autocomplete maps `values[].value` and throws
  `total`/`distinct`/`other_count` away. `field_terms(..., totals=False)` runs the top-N
  alone and reports the rows in hand; `other_count` is 0 rather than a subtraction against a
  total nobody measured, so nothing that renders an "Other" slice may use it.
- **`scan_exec._client()` built its store unlocked** from the bare daemon threads `_kill`
  runs on — and simultaneous disconnects are exactly the scenario that path exists for, so
  two of them each built a store and all but one leaked. One `threading.Lock`.
- **The halved heavy cap is documented, not reverted.** Moving the divisor to N + 2 cut
  every detector query's `max_memory_usage` in half at the default concurrency, permanently
  and whether or not anyone opens Visualize. That is the deliberate price of a lane that
  never queues behind a sweep, but it can turn a whole-corpus sweep that used to spill into
  one that hits `MEMORY_LIMIT_EXCEEDED` — so `docs/DEPLOYMENT.md` now carries an upgrade
  note saying so and pointing at the ceiling rather than at N.
- Sizing calculator: the chart lane's derived thread width is shown alongside its cap, and
  the cores line no longer implies a full box covers both lanes at once.

## Session 191 — 2026-08-27: PR #305 review fixes (scan admission classes)

Six findings from the review of the session-190 branch, plus the sizing calculator rework
that came out of the first one.

- **The chart lane could not spill.** The foreground cap was one heavy slot split four
  ways — with the default `stat_scan_concurrency=2`, `budget/12` against a detector's
  `budget/2` on `main`. That would be fine if a chart query could always spill, but two
  could not: `field_terms` carried `sum(count()) OVER ()` / `count() OVER ()` (a frameless
  window materialises every group after the GROUP BY) and `field_numeric_grouped` counted
  groups with `uniqExact` (a full in-memory hash set). Both died at `max_memory_usage` on
  exactly the high-cardinality field a chart is for. Both are now spillable `GROUP BY`
  aggregates — the trade `count_field_inventory` already documents — issued in parallel
  under the slot the caller holds, and the lane now reserves **two** heavy slots
  (divisor N + 2), so a chart is capped at half a detector's cap rather than a sixth.
- **`_run_parallel` dropped the scan context.** A bare `ThreadPoolExecutor.submit` starts
  with an empty context, so every fanned-out chart scan went out untagged and a disconnect
  could not `KILL` it. It runs under `copy_context()` now — which is also what keeps the
  two new parallel scans cancellable.
- **A dead ClickHouse turned a 499 into a 500.** `_kill` built its store lazily *outside*
  the best-effort `try`, so a connect failure on the first disconnect propagated out of
  `run_scan` and left the scan task with nobody to retrieve its result.
- **Nothing handled `run_scan`'s own cancellation.** On shutdown (or a failing `gather`
  sibling) the threadpool scan ran on holding its gate slot and its ClickHouse process.
  The `CancelledError` path now sets the flag and fires the `KILL` from a plain daemon
  thread — no event loop required, since the loop is what is going away.
- **The queue depth an analyst is shown.** `_waiting` was keyed by `id(gate)` and never
  pruned, so a collected gate's count could resurface under a recycled id; it is a
  `WeakKeyDictionary` now. And `ahead` was sampled at *entry* and reported up to 30 s
  later — it is counted where the deadline expires instead.
- **The frontend retried a busy lane forever.** No cap, no toast (`isScanBusy` was excluded
  from the error handler) and, in `TimelineHistogram`, no waiting text either, because
  `placeholderData` keeps `isLoading` false. A wedged lane therefore looked like a silently
  stale panel — the symptom #300 set out to remove. Retries stop after `BUSY_RETRY_LIMIT`
  (24 ≈ two minutes) and the 503 then surfaces as an error; the histogram shows "waiting
  behind N scans" over its stale bars while it retries.

The [sizing calculator](docs/sizing/index.html) now takes the hardware in hand — host RAM
and cores — and answers whether it is enough, then gives every memory value and setting
twice: the **minimum** that serves the described workload, and what the hardware supports
at **full spend** (as many scan slots as its cores and RAM allow without shrinking a query
below what a whole-corpus GROUP BY needs). A host that cannot host the stack at all, and
one that runs but spills every scan, are different verdicts and say so.

### Second review round

Six more findings on the same branch, all of them the *edges* of the two-class model rather
than its shape.

- **A fan-out spent its slot's budget twice.** The per-query cap is sized per gate *slot*,
  but `field_terms`' new totals wave, `field_numeric_grouped`, the violin's second wave and
  the compare layers each put two queries in flight under one slot at the full cap — a
  fully admitted foreground gate committing 2x what the heavy divisor reserved for it. The
  over-commit factor is exactly the fan-out width, so no gate sizing absorbs it:
  `_run_parallel` now declares its width to `scan_fanout` and the queries divide the slot's
  share (nested waves multiply). The `_run_serial` the first round added for the pivot
  stays, but as a latency/thread-count trade rather than the budget's only defence.
- **The disconnect KILL queued behind the scans it was freeing.** It went out through
  `run_in_threadpool`, and the situation a disconnect exists for is precisely the one where
  every anyio thread token is held by a scan parked on a gate. It uses the same plain
  daemon thread the `CancelledError` path already did.
- **Agent and MCP tools had no `ScanBusy` guard.** `chart_exec` got one for `propose_chart`;
  the thirteen other tool calls into gated aggregations did not, so a busy lane escaped as
  an unhandled `RuntimeError` — a 500 on the MCP surface. `chart_exec._scan` is now the
  shared `run_gated_scan` and every one of them goes through it.
- **A story export could fail a chart block.** An export is a job: no spinner, no request to
  answer 503 to, no retry. The bounded wait turned "the lane was busy for 30 s" into a
  failed block in an attested report. `unbounded_foreground_wait()` lets such a caller queue
  as it did before the gate existed.
- **The sizing calculator recommended hardware its own numbers said was too small.**
  `minimumPlan`'s working-set ceiling still divided by `concurrency` after `budgetFor` moved
  to `concurrency + foreground_slots` — at the default concurrency, half the working set the
  page had just computed the deployment needs.
- **`detect_scan_memory_budget` hardcoded `+ 2`** where everything else keys off
  `_FOREGROUND_SLOTS`, so changing that constant would have broken the "both gates fully
  admitted still fit" identity silently — and `scan_budget_report` would have gone on
  reporting the old total as fact.

## Session 194 — 2026-08-27: docs/ is reference documentation again

*(Landed on `main` as #306 while this branch was open, which is why it is numbered after sessions 190–193 despite its date — both branches claimed 190 in parallel.)*

`docs/` had accumulated three audiences in one directory: operator guides next to an
internal backlog, a 4,356-line session log, thirteen archived plans and PR-review dumps, and
twenty-four dated design records. An operator opening it could not tell which files were for
them.

- **Deleted the process records.** `docs/archive/` (superseded plans, per-PR review finding
  dumps, archived roadmap phases and progress splits) and `docs/superpowers/` (specs and
  execution plans) are gone — 36k lines. Git history keeps them. Every pointer into them was
  repointed at the reference doc that describes the shipped behavior, including seven
  docstrings under `src/` and three `CHANGELOG.md` entries; a link check over every markdown
  file passes.
- **`PROGRESS.md` 4,356 → ~740.** Sessions 1–169 trimmed; the header now says how to recover
  them and that this file is deliberately not exhaustive.
- **`ROADMAP.md` 646 → 394**, refreshed against the real issue list (its "verified" stamp was
  a month and four releases old, and #296–#298 were missing from it). Every open item and
  standing decision survives; the rationale essays around them do not.
- **Reference docs trimmed** where prose had drifted from contract into narrative:
  `DEPLOYMENT.md` 987 → 781 (post-mortem sections and a duplicated worked sizing example
  out, every operational fact kept), `AGENT.md` 907 → 811, `ANOMALY_DETECTION.md` 2,271 →
  2,103 — mostly the shared-machinery preamble, whose gate/mute/overrides/cache/scope
  sections carried the same fact three ways. The fourteen method sections are left alone:
  they are the peer-review contract this project claims, and shortening the explanation of a
  detector is not a docs improvement.
- **`MODEL_REFINEMENT.md`** loses its 2026-07-05 storage-placement audit — all three of its
  cleanups shipped in M21 — and states the current placement instead.
- **`README.md`** groups its documentation index by audience (running it / using it / why it
  is built this way) and drops the stale screenshot-grid TODO comment.
- **`CLAUDE.md`** records the new rule: `docs/` is reference documentation, not a working
  area — design rationale goes in the subsystem's reference doc, review findings in the PR
  thread or as a condensed roadmap item, history in git.

## Session 190 — 2026-08-26: scan admission classes (#300)

Production evidence on a 700M-event timeline: an analyst opening a per-value histogram
during an Investigate sweep got a spinner that never resolved, and a reload turned every
chart on the page into the same spinner. Three defects, none of them tuning:

- **Charts shared the detector lane.** `queries.py` gated `histogram`, `field_terms` and
  every other chart aggregation on the same `HEAVY_SCAN_GATE` as fourteen whole-corpus
  detectors; a sweep is 4 cheap plus 9 heavy requests fired in parallel, and a histogram
  joined the back of that queue. Now `FOREGROUND_SCAN_GATE` (4 slots) admits charts in
  their own lane. The heavy cap divides the budget by N + 1 and the reserved slot is split
  four ways, so both gates fully admitted still fit the total — the calculator and
  `scan_budget` report follow.
- **A queued request could never say so.** Every gate acquire was unbounded and blind. A
  chart now waits at most 30 s and answers 503 with `queued_ahead` and `Retry-After`; the
  histogram, per-value modal and chart canvas render "waiting behind N scans" and retry
  at the server's pace instead of toasting a failure or spinning.
- **A disconnect orphaned the work and the reload doubled it.** Nothing cancelled a
  threadpool scan or its ClickHouse query when the client left. `api/scan_exec.py::run_scan`
  binds a `ScanContext` (every scan's `SETTINGS` clause now carries
  `log_comment = 'vestigo-scan/<token>'`), polls `request.is_disconnected()`, and on
  disconnect sets the cancel flag — a parked `acquire_scan_slot` notices within a second —
  and issues `KILL QUERY` by tag. Verified against the reference ClickHouse.

The other half of the #300 report — the derived-range histogram scanning the corpus
twice — had already shipped in `6c69855`; the roadmap entry claimed otherwise and is gone.
The design round and execution plan for this session are in git history (commits `fdaf4d5`
and `5ab8d6c`); what shipped is described in `ANOMALY_DETECTION.md` §"Two admission classes"
and `DEPLOYMENT.md` §"Resource sizing".

## Session 189 — 2026-08-26: PR #304 review fixes (scan-budget accounting)

Five findings from the review of the session-188 branch, four of them in the new cache
accounting itself.

- **The fallback could raise the budget.** With the caches at or over the ceiling,
  `_resolve_scan_memory_budget` fell back to the 12 GB constant — larger than the ratio
  would have given on any ceiling under 15 GB. An 8 GiB app container went from 6.4 GB
  to 12 GB *because* its configuration was found to be over-committed. The fallback is
  now clamped to `ratio × detected`, so subtracting the caches can only ever lower the
  budget, and the result can never be 0 (which ClickHouse reads as unlimited).
- **The caches were subtracted from the wrong ceiling.** `scan_memory_ceiling()` returns
  local detection — the *app* container's RAM — whenever the probe never ran or an
  unbounded ClickHouse ceiling was capped by it. ClickHouse's cache maxima do not live
  under that number; `_cache_bytes_under` now applies them only to ClickHouse's own.
- **`uncompressed_cache_size` is never allocated.** It is gated on
  `use_uncompressed_cache`, which is off by default, so a stock server reports an 8 GiB
  maximum it never touches. Counting it took 8 GiB off the budget of every externally
  managed ClickHouse and could invent an `over_budget`. Both uncompressed caches are out
  of `_COUNTED_CACHES` and out of the probe query; `memory.xml` still pins them to 0 so
  the premise survives an upgrade.
- **A server-pinned `max_threads` is not a core count.** `auto(N)` is; a plain integer is
  an operator's thread limit, and dividing it by the gate ran scans at a quarter of the
  configured width while reporting it as `detected_cores`. `parse_max_threads_setting`
  now returns `(value, is_auto)`, a pin is honoured verbatim, and the report says
  `clickhouse_pinned` with `detected_cores: null`.
- **The airgap `memory.xml` guard was unreachable.** Under `set -e` the `cp` aborted
  first, on both failure shapes, with a `cp:` line naming no cause. The guard now runs
  ahead of the copy (bundle side and mount-target side). Its test passed anyway — twice
  over: it asserted only `returncode != 0` and the string `memory.xml`, and the bundle it
  built failed the manifest check before ever reaching the installer.

CI and the release workflow now run ClickHouse `26.6.1.1193` (the glibc build of the tag
`docker-compose.yml` pins) instead of `24`, which is what surfaced this: `24` has no
`primary_index_cache_size`, so the probe test asserted a name the server under test did
not have.

## Session 188 — 2026-08-25: Scan-budget truthfulness (#301, #302, #303)

`scan_budget.risk` reported `ok` for a configuration that does not fit, including the one
we ship. Three defects, one probe.

- **Caches were never counted** (#302). The ratio is now taken of
  `ceiling − caches`, which is what `stat_scan_memory_ratio`'s own help text always
  claimed, and `risk` compares `scans + caches` against the ceiling. The issue's own
  table understated it: at 26.6 defaults `index_mark_cache_size` and
  `primary_index_cache_size` are 5 GiB *each*, so the reference stack's caches (12 GiB)
  exceeded its whole 9.5 GiB ceiling. `memory.xml` now pins both; caches total 3.5 GiB
  and the stack fits, with 1.2 GiB of merge headroom, asserted by
  `tests/test_reference_stack_budget.py`.
- **Thread width was a constant** (#301). `stat_scan_max_threads` defaults to 0 = auto:
  cores ÷ concurrency, floor 2. The issue proposed reading CPU counts from
  `system.asynchronous_metrics`; 26.6 has none — no `CGroupMaxCPU`, no `OSNProcessors`.
  The real source is `system.settings.max_threads`, which reports `auto(N)` and *is*
  cgroup-quota aware (verified: `--cpus=2` → `auto(2)`).
- **The airgap install never had a ceiling** (#303). `install.sh` copied
  `allow-default-network.xml` and not `memory.xml`, so the compose bind-mount source did
  not exist and Docker made an empty directory of it. Fixed, plus a `-f` pre-flight
  assertion, `--check` coverage, and a parity test between the bundle script, the
  installer and the compose mounts. `risk` is now rendered on the admin Settings page,
  since a startup log line is what nobody reads.


Alongside: a **sizing calculator** at `docs/sizing/` (GitHub Pages, linked from the README).
It turns an expected dataset size, analyst count and deployment shape into recommended RAM,
cores and `VESTIGO_*` values, using the same arithmetic `db/_scan.py` uses — its constants are
*generated* from `core/config.py`, `db/_scan.py` and `memory.xml` by
`scripts/gen_sizing_constants.py`, with a parity test, so a public sizing page cannot recommend
values the app stopped using. Sizing numbers only, deliberately: an operator who pastes a
generated config has skipped the file that explains what the numbers mean.

Issue #300 (foreground histograms queuing behind detector sweeps in the shared gate)
stays open: it changes hot-path query behaviour and gets its own round.

## Session 187 — 2026-08-25: PR #299 review — the scan-budget half

The value-inventory export (#295) itself came through review clean. Everything below
is the scan-budget refactor that shipped alongside it, plus one concurrency hole in
the new export gate.

**The budget and the gate had drifted apart.** Session 186 moved the per-query budget
to query-build time so the ClickHouse probe could reach it. `HEAVY_SCAN_GATE` stayed
sized at import — it has to be, it is imported by value — so the divisor followed the
live setting while the semaphore did not. An admin lowering "Concurrent heavy scans"
from 4 to 2 in the console would double every query's `max_memory_usage` while the
gate kept admitting 4: twice the total budget, i.e. the exact OOM the pair exists to
prevent. `restart_required=True` on the spec is advisory text, not enforcement. Both
halves now read one frozen `_GATE_CONCURRENCY`, and `/api/health` discloses a pending
value waiting for a restart.

**An "unbounded" ceiling was still believed outright.** `resolve_clickhouse_ceiling`
classifies a ceiling ClickHouse merely derived (0.9 x RAM a limit-less container does
not own) as `bounded=False` — and the budget used it anyway. App in a 4 GiB container
against an unlimited ClickHouse on a 64 GiB host went from 1.6 GiB per query to ~23,
on the strength of a number the module had just called a guess. `scan_memory_ceiling()`
now caps an unbounded ceiling by local detection: two guesses, take the lower. An
explicit `max_server_memory_usage` is a decision and still stands as written — but it
is now clamped by `max_server_memory_usage_to_ram_ratio` exactly as the server clamps
it, which the reference stack needed: 10 GiB pinned against `mem_limit: 12g` and a 0.8
ratio is really 9.6, and the probe reported the optimistic number. `memory.xml` now
pins 9.5 GiB so the file says what it means.

**The export gate could stall the app.** `EXPORT_SCAN_GATE` is one slot held for the
whole client-paced drain, and it was acquired with an untimed blocking `acquire()`
inside the generator — which Starlette runs on an anyio worker thread. One analyst
backgrounding a large download blocked every other inventory export in the process,
each queued one parked on a thread from the pool every `run_in_threadpool` query also
needs. The slot is now taken by the endpoint *before* the response begins, bounded by
`VESTIGO_EXPORT_SCAN_QUEUE_WAIT_SECONDS` (default 30), and a wait that runs out is a
clean 503 rather than a truncated 200. Acquiring it after the headers are gone could
never have been anything else. The start-of-export audit row moved below the
acquisition while we were in there: a refused request produced no file, and a row
saying an export ran is the kind of claim this trail exists not to make.

**The merge wait was waiting on other people's merges.** `_await_merges` polled
`system.merges` for *any* merge on `events`, holding the admission slot, for up to 300
seconds. An instance with concurrent ingest always has one in flight, so it burned the
full five minutes every apply and stalled every detector sweep behind it. It now polls
the partition ids the apply actually staged, read off the scratch table before the
swap — no parts staged, no wait.

**One pre-existing test bug, found on the way.** `tests/test_scan_budget.py`'s
local-detection tests read module state that startup recovery writes once, so any
earlier test booting the app left the dev ClickHouse's real ceiling behind and three
tests failed by *ordering* — on `main` too, not from this work. An autouse fixture now
starts each of them from "the probe has not run".

**Two small ones.** `/api/health` re-detected local memory on the event loop on every
poll (three blocking reads); it runs in the threadpool now. And the export dialog's
field picker rendered neither the loading nor the error state of `viz/fields`, so a
failed request read as an empty list with a permanently disabled Download button.

## Session 186 — 2026-08-25: nothing was bounding ClickHouse

A production instance had been losing clickhouse-server to the kernel for months. The
enrichment apply was blamed, twice, and gated twice (sessions 52 and 56). It was never
the enrichers.

**Three ceilings, all off.** The airgapped compose — the file that actually reaches
production — carried no `mem_limit` on any service. The repository's own
`docker-compose.yml` at least carried them commented out; the bundle template never had
them. With no container limit, ClickHouse derived its own ceiling from
`max_server_memory_usage_to_ram_ratio` (0.9) times detected RAM, which in an unlimited
container is the whole 64 GiB host, so it never self-throttled. And
`deploy/clickhouse/memory.xml` shipped as a `.example` nobody is told to copy.

**The app made it worse, and reported success while doing it.**
`detect_scan_memory_budget()` measured the memory of whatever host *the app* process
sat on. Full-docker, no limits: 64 GiB detected, x 0.8 = 51.2 GiB total, / 2 slots =
**25.6 GiB authorized for a single query** — while Postgres, Qdrant and the app shared
the same RAM and ClickHouse's own ceiling sat at ~57.6 GiB. Every guardrail was
honoured exactly. The sum simply did not fit, the kernel picked the largest RSS, and
SIGKILL is not something a process gets to write a log line about. `restart:
unless-stopped` then erased the outage: the only visible trace was `docker compose ps`
showing ClickHouse up two hours against five days for everything else.

**The fix is to stop having two numbers.** The budget is now derived from *ClickHouse's
own reported ceiling* (`system.server_settings`, falling back to
`system.asynchronous_metrics`), probed once at startup. An operator sets
`max_server_memory_usage` and the app follows it. Local detection survives only as the
pre-probe fallback, and landing there is now logged as a warning naming the
misconfiguration.

That required the SETTINGS clause to stop being a module constant. It was frozen at
import — the one moment at which the app cannot ask ClickHouse anything, which is
precisely why the budget could only ever be sized from the app's own host. It is now
`heavy_scan_settings()`, called per query (69 call sites, mechanical). The side effect
is worth as much as the fix: every `stat_scan_*` value except `concurrency` is now
genuinely live, so four settings lost a `restart_required` badge that had been telling
the truth about an implementation detail nobody wanted.

`resolve_clickhouse_ceiling` distinguishes a ceiling an operator *set* from one
ClickHouse *derived* from RAM it does not own. Both are usable for sizing; only the
first is a limit. That distinction is the whole finding, so it is what `risk` reports:
`ok` / `over_budget` / `unbounded`, served on `/api/health` because a startup warning is
exactly what nobody reads.

**The merge window.** `finalize_enrichment_apply` held its admission slot "across the
INSERT *and* the REPLACE" on the stated grounds that the swap queues merges and merge
memory is not covered by the per-query cap. Both halves of that sentence were right and
the conclusion did not follow: `ALTER TABLE ... REPLACE PARTITION` returns as soon as
the swap is done, with the merges still ahead of it. The slot was being released into
the expensive part. It now waits on `system.merges`, bounded and non-fatal — the
partition is durable by then, so a slow merge must never fail a completed apply.

**Shipped, not documented.** Both compose files now set memory limits and mount
`memory.xml`, overridable per service via `VESTIGO_*_MEM_LIMIT`. A guardrail that ships
opt-in is a guardrail production runs without; that is the actual lesson, and it is why
none of these are opt-in any more.

Two test bugs surfaced on the way, both latent races rather than fallout.
`_SeqFakeClient` keyed canned results to a FIFO under one marker while `compare_*` runs
its two layers through `_run_parallel` — which layer got which counts was a thread race
that happened to land right. Markers can now be tuples that must all match, so a test
keys on what distinguishes the layers (`q=` compiles to `ILIKE`) instead of on arrival
order. And `test_other_database_errors_are_not_swallowed` used `code: 241` as its
example of an untranslated error; 241 is now deliberately translated, so it moved to
`code: 62`.

## Session 185 — 2026-08-25: what the review found in the value inventory (#299)

Four findings against session 184's export, all correctness, and two of them changed a
decision that session had recorded as deliberate.

**The scan gate is not one gate.** Session 184 wrote that `iter_field_inventory` holds
"its scan-gate slot for the whole drain" as though that followed from streaming. It does
not. Every other holder of `HEAVY_SCAN_GATE` is bounded by ClickHouse; this one is bounded
by the analyst's browser, and the gate has two slots, process-wide. Two people downloading
a large inventory over a slow link — or one who backgrounds the download — hold both, and
every detector on the box blocks behind them. The generator cannot even time itself out,
because while it is suspended waiting on the consumer its own code is not running.

The split that resolves it: the aggregation is what `HEAVY_SCAN_GATE` exists to admit, and
a sorted aggregate cannot emit its first row until every group exists — so **the first
block proves the heavy scan is over**. The detector slot is handed back there, and a new
one-slot `EXPORT_SCAN_GATE` covers the drain. Exports queue behind each other rather than
stacking two live result streams; detectors stop paying for a slow browser. No setting for
the export gate: the supported answer to "I want more concurrent exports" is that you don't.

**The pre-flight could not survive its own use case.** `uniqExact` builds the full distinct
set in a hash table and does not spill — `HEAVY_SCAN_SETTINGS`' external-aggregation
thresholds simply do not apply to it. So the count died at `max_memory_usage` on exactly
the high-cardinality field the export was built for, and surfaced as a bare 500. This is
the same trap session 184 avoided for the *stream* (window sorts cannot spill either) and
then walked into for the count. It is now `count()` over a `GROUP BY` subquery: the same
grouping the stream does, so exact for the same reason, through the spillable path. The
grouping runs twice now. That is the price of the export working on the fields it exists
for. A `QueryMemoryExceededError` maps any remaining `code: 241` to a 413 that says narrow
the scope, alongside the older `QueryRequestTooLargeError` — the cap is ours, set so one
scan dies instead of the server, which makes hitting it explainable rather than a 500.

**Two smaller ones.** `iter_field_inventory` yielded ClickHouse cells straight through, so
a `content_hash` or `file_hash` inventory — both legal field tokens, and reachable from the
agent and the CLI even though the viz picker does not list them — wrote `b'3f2a…\x00'` into
the CSV. `decode_fixed_string` is now factored out of `decode_fixed_string_columns` for the
paths that yield a bare cell rather than a row. And in the export dialog only the mode
toggle was frozen mid-download: flipping the separator to tab made the progress row read
"Downloading .tsv" over an in-flight comma-separated request that would still save as
`.csv`. Every control that shapes the request is frozen now, events-mode format included.

Each of the four has a test that fails without its fix — checked by reverting each one.

## Session 184 — 2026-08-25: the value inventory export (#295)

An analyst wanted three columns out of a timeline — each distinct `attr:src_ip`, when it
was first seen, when it was last seen — and the only way to get them was to export every
event and aggregate the file elsewhere. `QueryService.field_terms` already grouped by the
value; `min`/`max` over the timestamp were two more aggregates in the same scan.

The aggregation is a *new* method rather than a flag on `field_terms`, for a reason worth
recording: `field_terms` can afford its `sum() OVER ()` because it takes a top-N, and the
inventory cannot — it is unbounded in group count, and window sorts are the one sort
ClickHouse will not spill to disk (`db/_scan.py`). `iter_field_inventory` is therefore
plain `GROUP BY`/`ORDER BY` under `HEAVY_SCAN_SETTINGS`, streamed to the client one block
at a time through a new `_select_row_blocks` (the streaming sibling of `_select`), holding
its scan-gate slot for the whole drain.

Two details are the difference between a file an analyst can rely on and one they cannot.
The no-timestamp storage sentinel is nulled out *inside* the aggregate, not filtered out
of the scan — so a value seen only on undated events keeps its true count and reports no
times, instead of claiming it was first seen in the year 2299. And the pre-flight
`uniqExact` is the same construction as the events export's `count()`: the number the
completeness trailer is proven against, and the last point at which a query failure can
still pick a status code rather than truncate a 200.

Columns and separator are the analyst's choice (issue thread). The one rule the server
imposes: the column a file is *sorted by* is always written, even when unticked — a file
ordered by a column it does not contain reads as shuffled. The UI says so rather than
silently adding it.

`lucide-react` went 1.32.0 → 1.34.0 (upstream latest) in the same branch. It was not a
planned bump: the installed copy in this checkout was missing its ESM entry and its type
declarations, so thirty-odd untouched files failed `tsc` and `vite build` could not resolve
the package at all — a broken install that reads exactly like a code error. Reinstalling
fixed it; taking the current release rather than re-pinning the old one is the cheaper end
state. Pinned exact, as every other frontend dependency here is, and the lockfile diff
touches nothing else.

The dialog's field and order pickers are native `<select>`s, not the Radix one. A Radix
Select inside a Radix Dialog puts two focus scopes in a loop under jsdom (the test hung on
`Maximum call stack size exceeded` in `react-focus-scope`); `UploadDialog` already had the
native precedent, and typing a prefix to jump is worth more than styling on a field list
that runs to hundreds of entries.

## Session 183 — 2026-08-24: upstream branch triage, and 1.14.0

Forty remote branches, and the question was which of them still meant anything. Only
sixteen were ahead of `main` at all; twenty-three were fully contained in it and had
simply never been deleted. That left five branches carrying real work plus eleven
dependabot bumps.

Two checks in this triage were wrong the first time, both worth recording.

The first: `git merge-tree` output was grepped for `CONFLICT`, but git on this machine
reports in German — `KONFLIKT`. Every branch therefore read as merging cleanly. Redone
by exit code, two did not: `docs/pr182-review-followups` conflicted in `ROADMAP.md`,
and `feat/d11-entropy-bigram` conflicts in `anomaly_stats.py`. A locale-dependent
predicate that fails open is worse than no predicate — it produces confident wrong
answers. Match on exit status, not on translated prose.

The second: the ten green dependabot PRs were green *individually*, against a `main`
that predated the batch. `vitest` and `@vitest/ui` each failed alone with `ERESOLVE`
because `@vitest/ui@4.1.10` peer-pins `vitest@4.1.10` exactly; neither bump is
satisfiable without the other, and only merging them together resolves. `mcp` 2.0.0
stays out for the same class of reason — every `pydantic-ai-slim[mcp]` release pins
`mcp>=1.24.0,<2.0` — and that PR was closed rather than left to be re-triaged monthly.
Two branches also had to be re-verified after the batch landed, because their green CI
had run against ruff 0.16.2 while `main` moved to 0.16.3.

`#293` looked like a merge and was not. Its backend job was failing on
`test_manifest_hashes_match_committed_assets`: both nginx converters had been edited
and neither `manifest.json` entry updated. That is not a test detail. The manifest is
the integrity record an analyst checks a downloaded converter against, so a stale hash
means the published checksum does not describe the script actually served. It then
turned out to be unmergeable for a second reason — its original commit was unsigned and
authored as `mstoeck3@hs-mittweida.de`, the global identity leaking past the repo-local
config, exactly the PR #139 regression. Signed and re-authored; `BLOCKED` became
`CLEAN`, which is the confirmation that the signature was the cause.

Released as **1.14.0**, not 1.13.1. The version files already said 1.13.1 from the
timeout branch, but by the time the release was cut it carried a new converter and a
new `vhost` attribute — added functionality, and the changelog claims SemVer. A patch
number would have had to file two `Added` items under `Fixed` to stay honest about
itself. The unreleased 1.13.1 section was folded into 1.14.0 rather than left standing,
since no tag was ever cut for it.


## Session 182 — 2026-08-20: ClickHouse strangled itself on a debug log

A production instance went fully unresponsive. The output was one stack trace repeating
forever, nested inside itself:

```
Cannot log message in OwnAsyncSplitChannel channel: Cannot log message in ...
Poco::Exception. Code: 1000, e.code() = 0, File access error: .../clickhouse-server.log
0. Poco::RotateBySizeStrategy::mustRotate(Poco::LogFile*)
```

`df` inside the guest reported 390 GB free, the log directory was writable, and every file
was present — so the three obvious causes were all wrong.

- **`e.code() = 0` was the clue.** A real filesystem fault carries an errno and Poco throws
  a specific subclass. A bare `FileException` with no errno comes from one place:
  `LogFileImpl::sizeImpl()` when the `ofstream` flush fails. The handle was already dead,
  not the filesystem. C++ streams latch their failbit, so it never recovered — and
  ClickHouse stats the log before *every* message to decide about rotation, then tries to
  log the failure, which needs the same stat. The logging thread span until the server
  stopped answering. Nothing crashed; a restart cleared it in seconds.
- **The trigger was `EDQUOT`, not `ENOSPC`.** `err.log` had it in plain text —
  `Disk quota exceeded` on a rename, and `Cannot reserve 1.00 MiB` against the system log
  tables. A quota enforced above the container, which `df` inside it cannot see. On ZFS
  `quota` counts snapshots and `refquota` does not, and `df` shows the refquota view, so a
  snapshot backlog exhausts the real ceiling while the guest looks healthy.
- **Our compose file helped it along.** The stock image logs at `trace` with a 1000M x 10
  rotation into the container's *writable layer* — we mount a volume for
  `/var/lib/clickhouse` but none for `/var/log/clickhouse-server`. Measured on the dev box:
  ~1.1 GB of log files, plus 1.32 GiB `trace_log` and 992 MiB `text_log` on the data volume.
  ~11 GB of ceiling nobody reads.

`scripts/clickhouse-log-recovery.sh` caps the logger (`information`, 100M x 3), disables the
unbounded telemetry tables, puts a 14-day TTL on `query_log`/`part_log` — kept deliberately,
they are what you want when an ingest misbehaves — and recreates the container, which is what
reclaims the space: the writable layer goes, the named volume stays. It refuses outright if
`/var/lib/clickhouse` is *not* on a volume, since a recreate would then delete every case.

Airgap-safe by construction (`--pull never`, image verified present first — a stalled
registry pull on an isolated host is its own outage), engine-agnostic across Docker and
Podman, and it backs up and validates its `docker-compose.yml` edit before applying it.

Verified rather than assumed: the drop-in was booted on a throwaway container running the
same image, confirming `logger.level=information`, zero `<Trace>` lines, the telemetry tables
absent and the TTLs applied. `tests/test_clickhouse_log_recovery.py` (16 cases) guards what
can rot silently — the XML's meaning, and that the compose anchor the script splices against
still exists.

Not fixed here, and it is the actual root cause: the quota lives on the hypervisor. Capping
the logs lowers how fast you reach the ceiling; it does not raise it.
`docs/DEPLOYMENT.md` §"ClickHouse log growth" documents both halves.

## Session 181 — 2026-08-20: haproxy2vestigo, and measuring a timezone instead of assuming it

A 1.2 GB Docker `json-file` log of a HAProxy 2.6 frontend had no converter. Now it has one:
`src/vestigo/assets/converters/haproxy2vestigo.py`, built on the `nginx2vestigo.py` template
(same CLI, `.gz`, directory input, parallel chunking, `--split`, `--since/--until`).

- **Two detected layers, no flags.** Envelope — Docker `json-file`, BSD syslog, or bare —
  then payload: HTTP log, TCP log, connection error, startup/reload. On the real file:
  4,207,331 events in 35 s, 0 skipped, 323 MB Parquet.
- **A catch-all needs a gate.** An unmodelled shape still becomes an event
  (`haproxy:message`) rather than being dropped, so the converter sniffs the first 200 lines
  for a *structured* shape and refuses the file if it finds none. Without the gate it would
  have "successfully" converted any text file at all.
- **The timezone is measured.** `accept_date` carries no offset, so the Docker envelope's
  RFC 3339 `time` sets the timestamp and each row records which clock it used in
  `timestamp_desc`. The observed `envelope − accept_date` skew goes in the footer as evidence.
- **The median was the wrong estimator, and the real file proved it.** First run reported a
  9999 ms skew — not clock drift but HAProxy's 10 s **tarpit** on the `PT--` sessions that are
  92% of that log. The difference is session duration plus write latency (accept stamped at
  session start, logged at session end), so only its **minimum** is bounded by the clock
  offset. A 5th percentile is not enough either — a log where every session is tarpitted
  leaves it nothing fast to land on, which is what a fixture written for exactly that case
  caught. The footer reports `_min` (the conclusion) beside `_p05`/`_median` (the context).

`tests/test_haproxy_converter.py` (24 tests, synthetic fixtures only), a `manifest.json`
entry, and `docs/INPUT_FORMATS.md` §"`haproxy2vestigo.py`" follow. The parser downloads panel
is manifest-driven, so no frontend change. Verified end to end: 200k events through
`vestigo ingest` carry `parser_name=haproxy2vestigo`, and the column advisor picks
`src_ip`/`http_path`/`client_real_ip` on its own.

## Session 180 — 2026-08-18: every LLM wall clock is an operator setting

A converter job on a slow local endpoint failed all four attempts with
`model call failed: ` — an empty reason, because a bare `TimeoutError` (and httpx's)
stringifies to `""` and `job.py` interpolated only `{exc}`. Behind it: three hardcoded
model timeouts nobody could reach from the admin console.

- **`converter_generation_timeout_seconds`** (default 180, was `generate_script`'s parameter
  default that the single call site never overrode). `timeout_s` is now a required keyword —
  a default there is a ceiling no operator can turn.
- **`agent_request_timeout_seconds`** (default 300, was `runtime.LLM_TIMEOUT`). The
  stranded-turn bound in `api/routers/agent.py` became `_turn_stale_after()`, computed per
  call, so an edited timeout does not need a restart to be respected by the sweep.
- **`column_advisor_timeout_seconds`** (default 45, was `ADVISOR_TIMEOUT_SECONDS`). The
  constant stays as the no-settings fallback.
- **Attempt errors name the exception type** (`model call failed: TimeoutError`). An
  attempt trail that cannot distinguish "refused" from "stalled" is not a forensic record.

`.env.example`, `docs/INPUT_FORMATS.md` §"Generated converters" step 2 and the settings
registry follow; the console renders all three with no frontend change.

## Session 179 — 2026-08-18: fourth review pass, the dependabot batch, 1.13.0

- **Two findings from a fourth `/code-review 277`.** `source_hash_in_use` asked
  `scalar_one_or_none()` of the converter-script lookup, so it raised as soon as two scripts
  shared a raw file — which is what every *regeneration* produces. All four callers swallow the
  exception, so the damage was silent and wrong rather than loud: an ingest rollback reported
  "source-row removal failed" after removing the row, startup reconciliation skipped its
  `source.ingest_interrupted` audit row and logged a retry that never comes, and retained blobs
  leaked. Second: the AST guard flagged `input`/`help` in *any* context, so `input = args.input`
  — the spelling the prompt's own `-i/--input` mandate invites — was rejected before the script
  ran and cost one of four attempts, with the enforced-constraints paragraph never naming them.
  They are now a separate shadowable set, refused only where the script never binds the name.
- **Eleven dependabot PRs merged** (numpy, pyarrow, alembic, pydantic-settings,
  sentence-transformers, pydantic-ai-slim → 2.31.1, and the frontend's Radix/lucide/
  `@types/node`/jest-dom). `mcp` 2.0.0 (#269) is **not** takeable and was left open: every
  `pydantic-ai-slim[mcp]` release pins `mcp>=1.24.0,<2.0`, so the bump makes the requirements
  unsatisfiable. Full suite green at 2681 with the embeddings extra installed.
- **1.13.0 released** — generated converters is the headline; see `CHANGELOG.md`.

## Session 178 — 2026-08-18: PR #277 third review — every finding fixed, minor ones included

A third `/code-review 277` (ten ranked findings, three cut by the cap, four cleanups); all
fixed (each with its reasoning in the PR #277 review thread).
What changed shape:

- **The static guard closes dynamic lookup and rebinding.** `pydoc.locate`, `__builtins__[…]`,
  `f = eval`, `os.__dict__[…]`, `x = os`, `f(os)`, `sys.path.insert`, `().__class__.__base__
  .__subclasses__()` all passed before. Now an imported module may only be the receiver of an
  attribute access, the object-graph dunders are refused on every receiver, and the deny-list
  covers import-by-string, deserialisers and `gc`/`inspect`. Docs call it best-effort, which it is.
- **The trail is complete.** Attempts append under a row lock (`append_converter_attempt`;
  two reuse jobs no longer clobber each other), model errors before a row exists are buffered
  and land on the row (or on a `failed` row named from the file when no draft ever arrived),
  a failure after the full run passed is an `ingest` attempt + audit, and every entry that
  sent a prompt carries that prompt's hash — the row's `prompt_hash`/`model` follow the draft
  that became the code.
- **Retention is lazy and reclaimed.** The raw file is kept only once a row is about to
  reference it; a job that fails before that takes its copy back; `source_hash_in_use`
  counts `converter_input_hash`.
- **Duplicate conversion is refused everywhere**: the job pre-checks (CLI, concurrent
  submits), registration re-checks, and migration 0032 makes the index a partial unique
  index. 0032 also adds `converter_scripts.raw_mtime`: the evidence file's own mtime (browser
  `lastModified`, CLI `stat`) is what the model is told and what the script sees on `-i`;
  absent, the model is told "unknown".
- **Reuse needs only the switch** (`converter_reuse` capability): saved converters run with
  the model down or never configured; the dialog offers *Use a saved converter* alone then.
- Startup reconciliation of `generating` rows runs before the lifespan yields; the upload
  dialog freezes its mode switch mid-transfer and its progress row follows the running
  transfer; the converters panel disables Regenerate while its job runs and polls the list;
  the prompts panel explains a failed load with a Retry.

## Session 177 — 2026-08-18: PR #277 second review — ten more findings fixed

A second `/code-review 277` on the branch after session 176; all ten findings addressed
(each with its reasoning in the PR #277 review thread). The
ones that changed shape rather than just code:

- **The script only ever sees a private copy of its input.** The runner hardlinked the
  retention copy into the workdir, so its `chmod` and any script writing to `-i` reached
  the evidence itself. `shutil.copyfile` now — a large log is copied per run, deliberately.
- **`check_script` is an allow-list.** Stdlib (minus the deny-list) plus `pyarrow`/`numpy`,
  import aliases resolved, `from x import *` refused, `sys.modules`/`getattr(module, …)`
  refused, destructive method names refused on any receiver (`Path.unlink`/`.chmod`
  included). Prompt and docs restate it.
- **The retention store knows converter rows own blobs** (`source_hash_in_use` unions
  `converter_scripts.raw_file_hash`), and `delete_case` cascades the rows.
- **No row stays `generating`**: the job's catch-all fails a row it created; startup
  reconciliation (`_reconcile_stale_converter_generations`) fails rows a restart orphaned
  and records the interruption as an attempt.
- **Same saved script over the same raw file is refused** (409 naming the existing source)
  via the new `sources.converter_input_hash` (migration 0031); a duplicate Parquet outcome
  is now said in the tray and the case jobs panel.
- `build_sample` streams (no per-line offset list; `count_lines` for the reuse path), the
  stderr partial-line buffer is capped, a non-gzip `.gz` upload is a 400 not a 500, and the
  disclosure copy names head/middle/tail rather than "the first N bytes".

## Session 176 — 2026-08-17: PR #277 review — every finding fixed

`/code-review 277` on the generated-converters branch; all findings addressed in one pass
(the PR #277 review thread has the full set):

- **Path traversal via the multipart filename** — the upload's `filename` was joined onto
  temp dirs unsanitised (sample file, regenerate copy, later `unlink()`). `sample.safe_filename`
  reduces it to a basename everywhere it is used; `ConvertJobInputs` applies it on construction.
- **Sample and full run now see the same file** — the runner stages the retention copy under
  the evidence filename (`input_name`), and a `.gz` upload's head sample is re-gzipped, so a
  suffix-driven script behaves identically in both phases and `source_file` names the
  evidence, not the hash.
- **`build_sample` on a one-line file** no longer indexes past `line_count`; the tail block is
  capped at its budget so one huge last line cannot exceed the disclosed size.
- **`register_source_for_ingest` rolls back** the freshly created `ingesting` row (and an
  unshared retention copy) when the timeline add fails, closing the orphan-row gap for both
  the upload endpoint and the converter job.
- **`validate_output` streams** the Parquet in Arrow batches (`map_lookup` for the unparsed
  flag, per-batch null counts, an offset-order tracker) — bounded memory in the API process.
- **Generation loop** — the "name already exists at v>1" redraft moved inside the loop and is
  recorded as a `generate` attempt without costing one; a lost `(case, name, version)` race
  retries with the next version instead of an unhandled `IntegrityError`.
- **Ingest failure after a valid conversion** is recorded as an `ingest` attempt plus a
  `converter.run` audit row and fails the job with the reason.
- **Runner**: rlimits are applied by a `-c` bootstrap inside the child (no `preexec_fn`
  from a threaded process); stderr EOF while the child runs now waits out the deadline and
  kills the group; `posix`/`_socket`/`_ssl`/`_posixsubprocess`/`_multiprocessing` denied,
  and the prompt reads the runner's list rather than a hand copy (system prompt v2).
- **Converter name is enforced**: the task header declares it once known, and
  `validate_output` checks the footer's `converter_name` against the row.
- **UI**: the tray's "View converter attempts" link is followed while the panel is mounted;
  invalidation fires on `failed` too; timestamps render in UTC like the rest of the app.
- Cleanups: `typed_completion` (`agent/oneshot.py`) shared by the column advisor and the
  generator; list endpoint defers the two large Text columns; scratch dirs removed on every
  path; CLI commands registered before the `__main__` guard; duplicate TS interfaces gone.
  One suggested cleanup was declined: generalising the importer's missing-stem rule would
  let a truncated archive restore silently (`test_incomplete_archive_fails_the_job`), so
  `_OPTIONAL_STEMS` stays explicit, now with the reason on it.

## Session 175 — 2026-08-17: generated converters — the model writes the script, the harness runs it

The upload dialog gains *Let AI write the converter* (and the CLI `vestigo convert-ingest`):
an analyst uploads any plain-text, time-annotated log, the configured model writes a
converter to the Parquet contract, the server runs it in a guarded subprocess, validates the
output, repairs on failure, and ingests the result — the produced Parquet *is* the source, so
nothing downstream changes. Scripts are case-bound rows (`converter_scripts`) the analyst can
download (with a provenance header), reuse on later uploads (no model call) and regenerate
with a hint (a new version, never an edit).

Decisions worth remembering:

- **Off by default, gated twice.** `converter_generation_enabled` plus the agent probe; the
  capability hides the UI, `convert`/`regenerate` answer 503 (house style, not the 404 the
  spec first said), list/download stay available because rows are records.
- **Guard is stdlib only** (no bwrap/containers, so uv and image deployments are unchanged):
  AST deny-list, `python -I`, scrubbed env, private cwd, `RLIMIT_AS/CPU/FSIZE/NOFILE`, own
  session. Measured: pyarrow imports at 2048 MB `RLIMIT_AS` and fails at 1024, so the setting
  floors at 2048. `RLIMIT_NPROC` was dropped — on Linux it counts the *user's* processes and
  starved OpenBLAS thread creation on a busy host; `OPENBLAS_NUM_THREADS=1` in the child env
  instead. What the guard does not stop is written down in `docs/DEPLOYMENT.md`.
- **The prompt is data.** `converters/prompt.py` renders the generation, repair and human
  copy-paste prompts from `ingestion/parquet_format.py`; the frontend now fetches the
  copy-paste text from `GET /api/converters/prompt` (the drift bug #204 cannot recur), and
  the assertions moved from `guidancePrompts.test.ts` to `test_converter_prompt.py`.
- **Egress is exactly** the head/middle/tail excerpt, filename, size, line count, mtime,
  version to declare and hint; the dialog names the model, endpoint host and byte count.
- **No repair on the full-file run** — a script that passed the sample and fails the whole
  file met a format change the sample did not show; regenerate with a hint instead of the
  harness sending more evidence than disclosed.
- **No job cancellation** (`JobStore` has none); timeouts bound every subprocess.
- `upload_source` lost ~120 lines to `register_source_for_ingest`, which the job reuses so
  both register a file identically (dedup, format detection, footer validation, retention,
  the row, the default timeline).
- Transfer carries `converter_scripts.ndjson` and the raw inputs as blobs; an older archive
  without the stem imports fine (`_OPTIONAL_STEMS`).

Tests: `test_converter_{prompt,sample,validate,runner,generator,scripts_store,scripts_api}.py`,
`test_converter_job_clickhouse.py` (real subprocess + real ingest with a fake model: happy
path, repair round, exhausted attempts, denied import, reuse, regenerate), transfer round
trip, CLI, plus `uploadDialogGenerate` and `generatedConvertersPanel` on the frontend.

## Session 174 — 2026-08-14: a declaration that steers nothing must not claim to

A second review pass over the same branch. Nothing here breaks a scan; every finding is a
place where the declaration *described* itself wrongly, which for shared, audited state is
the same class of bug.

`DetectorRun.params["field_overrides"]` was recorded unconditionally — including for a run
that named its fields explicitly (which bypasses the declaration in every detector) and for
methods that never receive it. That is the "auto does not describe what was scanned" problem
the key was added to fix, pointed the other way: the diary claiming a decision steered a scan
it never touched. It is now recorded only where it applied.

The PATCH endpoint validated method ids against all twelve, but four select no fields to
steer: `frequency` and `sequence_novelty` take one named `series_field`, `timestamp_order`
reads none, `log_template` clusters message text. A declaration against those was accepted,
audited, and rendered under "Declared fields" while the detector scanned exactly as before.
`FIELD_OVERRIDE_METHOD_IDS` (`db/analysis_plan.py`) is now the one list, shared by the
endpoint's 422 and by `_resolve_field_overrides`, which also keeps it out of the cache key.

Two disclosure bugs. `_drift_split_fields` cut its categorical branch by whatever the numeric
branch consumed, so a timeline with fifteen recommended numeric fields sliced the categorical
list to `[:0]` and dropped a pinned field with no note — a held-back field indistinguishable
from one that found nothing, which is the one thing this must never look like. And
`_auto_string_fields` derived its "held back" count from the whole candidate universe rather
than from what its quota would have scanned, so excluding a field ranked 22nd of 40 reported
a narrowing of a scan that was byte-identical to the undeclared one.

Frontend: the picker's identifier branch (charset/entropy) never re-applied the 15-field cap
after prepending pins, so it previewed 17 checked chips for a run that scans 15. The write
chain's serialization was per-hook-instance while two instances are mounted at once — the
method sheet's picker and the Tools summary — so declaring in one and resetting in the other
before the PATCH landed rebuilt the payload from the stale cache and dropped the in-flight
declaration from the timeline and the audit pair alike; it is now keyed by timeline at module
scope. A failed write is surfaced rather than swallowed: the chip returns to the server's
answer either way, which on its own reads as "nothing happened" rather than "not saved".

Released as 1.12.2.

## Session 173 — 2026-08-14: review of PR #264, the two halves that did nothing

A review of session 172's branch found the pin half broken in two places, both of which made a
control look present and do nothing — and one of which produced a false forensic claim.

`apply_field_overrides` built its pin list as "declared `true` and *not already selected*", so
only pins on fields the recommender had left out were promoted. A pin on a field it ranked 18th
kept rank 18 and was cut by the caller's `[:15]`; for `value_combo`, whose cap is 2, any pin
below third was cut — the exact case the code's own comment said it existed for. Nothing was
disclosed, so the field was neither scanned nor mentioned. Pins are now every `true`
declaration, promoted out of the kept list rather than skipped, and the per-detector cap is
re-applied afterwards (`charset`/`entropy` never re-cut, so a stored declaration could double
a heavy scan under one `HEAVY_SCAN_GATE` slot).

`_drift_split_fields` passed each branch's own selection as its `known` universe. A pin is by
construction a field the branch did not select, so every pin fell into the "not present in this
timeline" branch: pins never applied, and the numeric branch announced a categorical field as
absent while the categorical branch scanned it — a run that scans a field and disclaims it in
the same breath. The declaration is now resolved once against both recommenders' candidates,
and a pin neither of them selected is classified by the same syntactic numeric probe the
explicit-`fields` path uses, so it lands in the branch that probe indicates.

Two smaller things: `DetectorRun.params` now records the method's slice as it stood at run
time — `fields: auto` does not describe what was scanned once a declaration is edited, and an
applied pin leaves no trace in `warnings` the way an exclusion does — and `/analysis/findings`
hands the declaration it already read for the cache key to `_run_stat_detector` instead of
letting it re-read the timeline (24 redundant round-trips per 12-method sweep).

Frontend: `useFieldOverrides.declare` closed over the query-cache snapshot, which only refreshes
on the mutation's `onSuccess`, so two chip clicks in quick succession both built on the
pre-mutation state and the second PATCH — a full replace — dropped the first, including from
the audit row's `previous`/`new` pair. Edits now build on what is in flight and the requests are
chained so they cannot land out of order.

## Session 172 — 2026-08-14: per-timeline, per-method field overrides

Session 171 gave the field knobs back their pickers, which made the correction possible but
not durable: the picker's selection is per-run React state, so an analyst who takes a field
away from a detector takes it away again on the next sweep, and the next analyst never learns
they did.

The miss it leaves is semantic, not statistical. `recommend_numeric_fields` types fields
syntactically and says so in its own docstring: an HTTP status code parses as a number, so
`numeric_range` offers it, learns a band over `{200, 404, 500}` and reports the 500s as
outliers forever. No probe discovers that it is a categorical field wearing digits — only the
analyst does.

`Timeline.field_overrides` (migration `0029`, nullable) is where they say it:
`{method_id: {field_token: bool}}` — `true` pins a field into a method's automatic selection,
`false` takes it out, absent leaves the recommender's answer standing. Per method rather than
per field, because the same status code is meaningless to `numeric_range` and an excellent
`value_novelty` field. Written through `PATCH .../timelines/{id}/field-overrides` (contribute
access, unknown method ids and empty tokens 422, every change audited), shared per timeline on
`muted_methods`' contract rather than held per browser.

One helper does the work: `apply_field_overrides` sits between a recommender's answer and the
scan list of all eight detectors that pick their own fields, and returns what it held back for
the run's `warnings`. That keeps the shape "advice, never a lock" on every axis — an explicit
`fields=[…]` never reaches the helper and still scans an excluded field, the analysis plan
does not consult it, a pin naming a field the timeline lacks is dropped rather than scanned as
an always-empty column, and a held-back field is disclosed rather than silently narrowing a
scan into something that reads as "clean". Pins are applied before each detector's
`_MAX_AUTO_SCAN_FIELDS` slice, since being ranked below the cut is why a field gets pinned.

The findings cache key gains the method's slice: an answer computed before a field was
declared off is an answer to a different question.

In the UI the control is a small pin/exclude button beside each chip in `AnomalyFieldPicker`,
deliberately separate from the checkbox — scoping this run and deciding what the method reads
are different questions — and the picker's auto preview applies the declaration exactly as the
backend does, so the checked set keeps previewing what actually runs. The Tools sheet's Methods
tab summarizes what a timeline declares and resets it per method.

Not done: the agent has no tool for this. Every agent write today is a proposal an analyst
confirms, and a direct-write tool would be a new precedent that needs its own argument rather
than a side effect of this change.

## Session 171 — 2026-08-13: field knobs are choices again, not typing

Reported as a regression in the Investigate panels: the `fields` knob asks the analyst to
type field names. It does — and the same refactor took the single-field knobs with it.

`cadaa5c` replaced eleven per-detector views with one generic knob renderer that types every
knob as `<input type="text">`. Eight of those views mounted `AnomalyFieldPicker` (cardinality-
ranked candidates, Standard vs Dynamic grouping, coverage and distinct counts per chip,
`value_combo`'s 2–4 floor and ceiling); four more offered `series_field`, `group_field` and the
log-template `field` as `<select>`s built from the same `/anomalies/fields` inventory. A field
name is not free text — it is a fixed set of columns plus whatever `attr:` keys the timeline
happens to carry, and nobody can spell those from memory for a source they ingested an hour
ago. The knobs were reachable but unusable, which is the worse failure: the sheet's method mode
is what keeps the analysis gate advice rather than a lock, and that argument only holds if
running a method with your own parameters is actually possible.

Both controls are back. `kind: "fields"` renders the picker, configured per method from the
registry with the values the deleted views used (auto counts, `value_combo`'s 2–4, charset and
entropy's identifier-inclusive auto set, numeric-range's numeric candidate list) — so the
checked set previews what the backend will really scan. New `kind: "field"` renders
`MethodFieldSelect`, the single-field counterpart, which merges each knob's standard options
with this timeline's attribute keys. A picked selection travels as a list, which
`_FieldsParams._join_fields` already accepted alongside the comma-joined string; an untouched
picker still sends nothing, so "auto" stays the method's own default rather than an empty
string dressed up as one.

`MethodFieldSelect` renders only the `<select>` and inherits the labelled chrome from the
caller, so the fix adds no new arbitrary font size to the design-system budget.

