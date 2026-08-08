# Investigate panel — redesign

Date: 2026-08-07
Status: design agreed, not yet planned

## Why

The Investigate panel has three defects that compound.

**It is slow to first result.** `useDetectorSweep` (`frontend/src/components/analysis/detector-hooks.ts:49`)
fires all eleven statistical detectors on mount and renders a single spinner until the slowest
returns. Heavy scans serialize through `HEAVY_SCAN_GATE`, so the wait is the sum of the heavy
detectors divided by the gate width, not the max. Nothing is cached server-side; the client's
`staleTime` of 60 s is the only reuse, so a colleague opening the same case pays the full cost
again.

**It runs detectors that cannot produce a finding.** Nothing checks preconditions. `numeric_range`
scans timelines with no numeric field. `proportion_shift` and `value_distribution_drift` are
two-window methods that run in the self-baseline frame, where they are structurally empty — two of
eleven scans wasted by construction on the default frame. A zero from a method that could not have
found anything is indistinguishable from a zero that clears the data.

**It is organized by subsystem, not by intent.** Five tabs — Anomalies, Patterns, Sigma,
Similarity, Method — split fourteen tools that all produce the same artifact: a finding with a
timestamp, a score and an explanation. The analyst must guess which subsystem would have noticed a
thing before they can look for it. The methodology prose is excellent and sits one tab away from
the findings it explains.

A fourth, structural: the panel is a `shrink-0` flex sibling alongside the filter rail, the event
grid and `EventDetailPanel` (`frontend/src/pages/ExplorerPage.tsx:1402`). The sum of their minimum
widths exceeds a laptop viewport, so the panel is pushed off screen.

## Goals

1. First findings visible in well under a second on reopen; nothing heavy runs unasked without the
   analyst being told what it costs.
2. The panel states what it examined, what it did not examine, under what scope, and with what
   result — including methods that found nothing and rules that matched nothing.
3. Which tool to use is answered against the analyst's own data, not in a documentation tab.
4. The panel cannot overflow the viewport.

## Non-goals

- No change to the detectors in `db/anomaly_stats.py`. They work; this is a delivery problem.
- No change to `GET /anomalies`, `agent/tools.py`, the MCP surface, or `DetectorRun`.
- No live-stream or incremental detection. Batch SQL over ClickHouse, as today.

## Decisions

Reached by walking three interactive mockups.

| Decision | Choice |
|---|---|
| First open | Relevance gate resolves free, applicable methods auto-run cheapest-first, results stream in |
| Skipped methods | Never locked — collapsed row with the reason and a "Run anyway" control |
| Layout | Rail holds the findings inbox; all detail renders in one wide overlay sheet |
| Grouping | By evidence weight: named techniques → statistical outliers → exploration |
| Analyst questions | Survive as preset filters over the stream, not as containers |
| Caching | Server-side fingerprint cache; `DetectorRun` untouched as the audit diary |
| Stream membership | Sweep-able methods only (11 detectors + log templates); Sigma joins when it has a run |
| Non-sweep surfaces | One Tools sheet, plus "Mark baseline" as a histogram gesture |
| Params | Per-method params object on the new endpoint only |

## Architecture

### The relevance gate

`GET /cases/{case_id}/timelines/{timeline_id}/analysis/plan`

Pure Postgres. Inputs: the merged `field_stats` inventory (`db/field_stats.py`), source metadata,
one cached min/max-timestamp probe, and the active scope. No ClickHouse scan.

Per method it returns:

```json
{
  "method": "numeric_range",
  "status": "not_applicable",
  "reason": "no field parses as numeric",
  "reason_facts": {"numeric_fields": 0, "sampled": 19, "threshold": 0.9},
  "cost_class": "heavy",
  "estimated_seconds": 1.8,
  "cache": "miss"
}
```

`reason_facts` carries the numbers behind the verdict so the UI renders the arithmetic rather than
a canned sentence. Thresholds live in `core/config.py` with `SettingSpec` entries in
`core/settings_registry.py`, so an operator can move them and the displayed reason follows.

**A method is `not_applicable` if and only if it cannot produce a finding on this data.** Never
"unlikely to be interesting". The preconditions:

| Method | Precondition | Skipped when |
|---|---|---|
| `value_novelty` | ≥1 categorical field | — (effectively always applicable) |
| `timestamp_order` | ≥1 source | — |
| `value_combo` | ≥2 categorical fields | only one usable field |
| `numeric_range` | ≥1 field whose sampled values parse ≥90 % numeric | no numeric field |
| `charset` | ≥1 free-form string field | every field is enum-like |
| `entropy` | ≥1 free-form string field | every field is enum-like |
| `frequency` | span ≥ N buckets | span shorter than the bucket minimum |
| `interval_periodicity` | series field with enough events per group; span covers several periods | too few repeats per group |
| `sequence_novelty` | series field with distinct count in the workable range | no usable series field |
| `proportion_shift` | two windows exist (baseline frame with an active definition) | self-baseline frame |
| `value_distribution_drift` | two windows exist | self-baseline frame |
| log templates | ≥1 message-bearing field | no message field |

`needs_setup` is a distinct status from `not_applicable`: it means the analyst can make the method
applicable by an action (define a baseline, anchor an event), and the UI offers that action.

The plan is advice plus an audit record. It never withholds a method. Every skipped row carries
"Run anyway", and running it produces exactly what the unconditional sweep produces today.

The plan endpoint is an optimisation, not a dependency: if it fails, every method renders as
`applicable, cost unknown` and the panel falls back to running everything.

### The findings endpoint and its cache

`GET /cases/{case_id}/timelines/{timeline_id}/analysis/findings?method=X&params={...}`

`params` is one canonical JSON object per method. An adapter in the new router unpacks it into the
existing `StatisticalAnomalyService` calls via the helpers already exported from
`api/routers/events.py`. `GET /anomalies` keeps its exact current signature.

New table `analysis_cache`:

- **Key** — `sha256` over: `timeline_id`, the sorted set of source SHA-256 hashes, the enrichment
  generation, `frame` + `baseline_id`, `method`, the canonical params JSON, and `dispositions_hash`.
- **Value** — the serialized response, plus `computed_at`.

Sources are immutable after ingestion except for enrichment applies, and both ingestion and
enrichment apply already refresh `source_field_stats`. The **enrichment generation** is therefore
defined as the maximum `SourceFieldStats.computed_at` (`db/postgres.py:599`) across the timeline's
sources, paired with their `stats_version`. Both events that can mutate `events.attributes` already
bump that row, so no new bookkeeping is introduced and no existing invalidation path changes.

`dispositions_hash` (`db/postgres.py:1055`) is in the key because `normal` verdicts are
detection-affecting: marking a value normal must invalidate the cached findings.

Because every key input is content-addressed, **a cache hit is proof the answer still holds**.
There is no staleness heuristic and no "scanned 2 h ago" judgement for the analyst to make. Adding
a source changes the fingerprint, which misses, which rescans.

Eviction is LRU by `computed_at`, bounded per case. A dropped row costs a rescan and nothing else.

`DetectorRun` is unchanged and keeps its current role: the accumulating forensic diary, written
when the analyst acts on a finding, not on automatic sweeps.

### Scope provenance

A finding is meaningless without the comparison that produced it, and today nothing records it.

1. **Responses carry scope.** `/analysis/findings` returns a `scope` object — `frame`,
   `baseline_id`, `baseline_name`, `baseline_window`, `suspect_windows`, `dispositions_hash`,
   `computed_at` — echoed from the cache key. Identical for every finding in a response. The rail
   prints it once per group; the sheet prints it in full.

2. **Dispositions record the scope they were made under.** New nullable `scope` JSON column on
   `finding_dispositions`. Nullable so existing rows stay valid and read as "scope not recorded" —
   honest, rather than a fabricated backfill.

   `dispositions_hash` is **not** extended. It hashes detection-affecting facts; scope-at-verdict-
   time is provenance, not a detection input. Extending it would invalidate the reproducibility
   record of every existing `DetectorRun` for no gain.

3. **Scope changes confirm before they take effect.** Changing frame or baseline invalidates every
   method's cache at once, so it opens a dialog naming the consequence: how many methods re-run,
   and how many existing verdicts were made under the outgoing scope. Confirm proceeds; cancel
   leaves scope untouched.

Existing verdicts are never discarded or rewritten by a scope change. They persist, tagged with the
scope they were made under; any whose scope differs from the active one render with a marker and a
"re-examine under current scope" action. The analyst decides; the tool records.

**Consequence:** a case can hold two `confirmed` verdicts on one finding under two scopes. That is
correct — they are two assertions about two comparisons — but the triage burndown must count
distinct findings rather than disposition rows, or coverage inflates.

## The panel

### Layout

`InvestigatePanel` becomes `InvestigateRail`: resizable, 300–420 px, the **only** fixed-width
sibling. Finding detail, method detail and the Tools sheet all render into one overlay `Sheet`,
absolutely positioned inside the stage at `right: <rail width>`. An overlay contributes nothing to
the flex row's minimum width, so the overflow is structurally impossible rather than tuned away.

`EventDetailPanel` becomes the same overlay species — it is the third fixed sibling that made the
overflow worse.

### Rail

Top to bottom:

1. **Scope strip** — one line: `Compare baseline · Feb 24 – Mar 1 → suspect Mar 3–4`. Clicking
   opens the Tools sheet at its Scope section. Replaces `FrameBar`.
2. **Preset chips** — the analyst questions as filters: Everything, Known-bad, Changed vs. baseline,
   Repeating, Unusual values.
3. **Findings in three fixed groups**, each with its plain-language note:
   - **Named techniques** — "a rule author named this" (Sigma)
   - **Statistical outliers** — "odd, not necessarily bad" (the eleven detectors)
   - **Exploration** — "leads, not verdicts" (log templates, similarity)

   Within a group, per-method rank interleave as today (`lib/finding-normalize.ts`).
4. **Progress** — a thin line, not a spinner. Methods land as they finish, cheapest first.

Evidence weight is the grouping because it encodes the one thing the old panel never said: a Sigma
hit and a rare value are not the same kind of claim.

### Sheet

One component, three modes.

- **Finding** — the verdict sentence in plain language, an evidence chart where the method has one,
  score with its unit, evidence class, *how this method works* (the methodology prose, per method),
  the params knobs with a rerun control, and the compiled SQL. Actions: confirm, dismiss, mark
  normal, filter grid.
- **Method** — the same explanation, params and query, without a specific finding.
- **Tools** — sections in order: **Methods** (every method considered, ran and skipped, with
  `reason_facts` and "Run anyway") → **Signatures** (the Sigma rule list with per-rule hit counts
  *including the zeros*, plus run state and a rerun control) → **Explore** (similarity anchor state,
  motif mining) → **Scope** (the canonical scope record).

The Tools sheet is the answer to "what did you examine and what did you not". That question has
evidentiary weight, so it is one surface, reachable without losing the findings list, and it never
sits behind an unlabeled icon.

`MethodologyPanel` is deleted as a tab; its content moves into the method registry, attached to the
findings it explains.

### Baseline gesture

"Mark baseline" is available directly on the histogram — the baseline's input is the timeline, so
the gesture belongs there. The Tools sheet's Scope section remains the canonical record. Gesture on
the timeline, record in the sheet.

### Stream membership

The stream carries the eleven statistical detectors plus log templates. Sigma is a background job
with run history, so its hits join the stream when a run exists and its group otherwise reads
"not run" with the run control — never an implied all-clear. Similarity is anchor-driven: it
appears once an event is anchored from a grid row, in the Exploration group.

## Compatibility

Three subsystems share code with the panel and must keep working.

**Agent.** `agent/tools.py:1165` imports seven private helpers from `api/routers/events.py`:
`_get_query_service`, `_get_similarity_service`, `_persist_detector_run`, `_run_stat_detector`,
`_serialize_stat_result`, `_validate_field_modes`, `_validate_regex`. Those signatures are frozen.
The new endpoints build on top of them; the params adapter lives in the new router. A test asserts
the import surface still resolves so a later refactor cannot break the agent silently.

**Visualizations.** `api/routers/viz.py` imports from `events.py` and reads
`ensure_source_field_stats`. The plan endpoint reads that same cache read-only, with no payload
change and no `STATS_VERSION` bump, so nothing recomputes and viz behavior is unchanged. Fingerprint
data lives in the new table, never in `source_field_stats.payload`.

**Frontend cross-imports.** `detector-registry.ts` is consumed by `lib/finding-normalize.ts`,
`lib/triage-coverage.ts`, `hooks/useDisposition.ts`, `hooks/useTriageCoverage.ts` and three tests.
It survives and is extended — evidence class, precondition id, params schema, knob definitions,
methodology prose — rather than replaced. `useDisposition` recognises caches by scanning query keys
for named segments (`SHOW_DISMISSED_KEY`); the new cache-backed keys keep that convention.

## Files

**New (backend)**

- `src/vestigo/api/routers/analysis.py` — the two endpoints and the params adapter.
- `src/vestigo/db/analysis_plan.py` — one precondition predicate per method, each returning a
  verdict and `reason_facts`. Pure function of inventory, source metadata and scope.
- `src/vestigo/db/analysis_cache.py` — fingerprint computation, get/put, LRU eviction.
- One Alembic revision: `analysis_cache` table, `finding_dispositions.scope` column. Dialect-
  portable, since migrations run against SQLite in tests.

**Edited (backend)**

- `core/config.py` + `core/settings_registry.py` — gate thresholds and cache bounds, one
  `SettingSpec` each (the coverage test fails otherwise).
- The ingestion and enrichment-apply call sites — bump the enrichment generation input.

**Unchanged (backend):** `db/anomaly_stats.py`, `api/routers/events.py`, `agent/tools.py`,
`DetectorRun`.

**New (frontend):** `InvestigateRail`, `InvestigateSheet`, `ToolsSheet`, `ScopeStrip`,
`FindingGroup`, `MethodRow`, `useAnalysisPlan`, `useMethodFindings`; `detector-registry.ts`
extended into the method registry.

**Deleted (frontend):** `InvestigatePanel`, `DetectorAccordion`, `FindingsFeed`, `FrameBar`,
`MethodologyPanel`, and the eleven per-detector view components. Their knobs and charts move into
the sheet's method mode, driven by the registry.

## Failure modes

| Situation | Surface |
|---|---|
| One method errors | Its row shows the error and a retry; the rest of the stream is unaffected |
| Plan endpoint fails | Every method renders `applicable, cost unknown`; panel runs everything |
| Cache miss | Normal streaming path — a miss is slower, never wrong |
| Empty timeline / mid-ingest | The existing two-arm `NoEventsState` treatment, unchanged |
| Sigma configured, never run | "Not run" with the run control — never an implied all-clear |
| Scope changed mid-triage | Confirm dialog first; existing verdicts kept and marked |

## Testing

**Backend**

- Precondition predicates, unit-tested per method against synthetic inventories, including the
  boundary each threshold names.
- Fingerprint: same inputs produce the same key; each input independently changes it (source added,
  enrichment applied, baseline switched, params changed, disposition added).
- Cache hit returns a response byte-identical to a fresh compute.
- `tests/test_demo_detector_coverage_clickhouse.py` stays green: every analysis tool still finds
  something in the demo case. The gate must not skip a method the demo case proves applicable, so
  that test doubles as the gate's regression guard.
- The seven agent imports from `events.py` still resolve.

**Frontend**

- Rail renders groups in evidence-weight order.
- Every skipped method exposes "Run anyway".
- The scope-change dialog blocks until confirmed.
- A verdict made under an outgoing scope renders its marker.
- `useDisposition` optimistic updates still hit the new query keys.

## Rollout

One branch, no feature flag. The old panel is deleted in the same change: running two investigation
surfaces side by side would fork the disposition semantics, which must stay single-sourced.

Documentation in the same commit — `ANOMALY_DETECTION.md` gains the precondition table and the
scope-provenance rules; `ROADMAP.md` loses the items this closes; `PROGRESS.md` gets the session
entry.

## Open items for the plan

- Exact bucket-count minimum for `frequency` and period-count minimum for `interval_periodicity` —
  pick values the demo case satisfies, then assert that in the coverage test.
- Whether the rail's width preference stays in `useUiStore` (it does today) or moves to a per-user
  server-side setting.
