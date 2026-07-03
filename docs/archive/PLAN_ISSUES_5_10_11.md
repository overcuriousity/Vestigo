# Implementation Plan — Issues #5, #10, #11

Status: **done** — all three issues (#5 rename, #10 field aggregation, #11 converters) shipped;
see `docs/ROADMAP.md` and `docs/PROGRESS.md`. Archived here per repo convention on plan docs
rather than deleted, since it records the design rationale.

Confirmed decisions:

| Issue | Decision |
|---|---|
| #5 rename | New name **TraceSignal**, full rename (package, CLI, env prefix, DB defaults, docs, UI, repo) |
| #10 field aggregation | **Hybrid**: query-time field mapping now; optional physical materialization later |
| #11 converters | **Commit snapshot in-repo**, vendored from `overcuriousity/2timesketch` |

Additional decisions (session 2026-07-03):

- #5 env vars: **hard cutover** `TV_` → `TS_`, no fallback shim; `docs/MIGRATION_RENAME.md`
  covers existing deployments. Frontend `localStorage` keys renamed `tv-*` → `tsig-*`.
- #10 merge suggestions: **name similarity + value-shape heuristics** (extend
  `db/field_recommend.py`). No embedding-based semantic proximity — embeddings are optional
  and usually absent at timeline-creation time; possible future enhancement. Sample values
  of actual data are always visible in the mapping table.

Recommended implementation order — three separate PRs:

1. **#5 rename** first. It is mechanical but touches ~71 files; doing it before the
   feature work avoids painful rebases and lets #10/#11 land under the new name.
2. **#11 guidance + converters** second. Small, self-contained, no schema changes.
   The in-case guidance text references timeline creation (#10) — ship it with a
   forward-looking phrasing, adjust one string when #10 lands.
3. **#10 timeline wizard + field aggregation** last. Largest change, cuts across the
   query layer.

---

## Issue #5 — Rename TraceSignal → TraceSignal

Rationale (from the issue): anomaly detection is primarily statistical, not
vector-based; "Signal" describes the actual value proposition (traces → actionable
signals) while keeping the "Trace" root.

### Scope of the full rename

**Backend**
- `src/tracesignal/` → `src/tracesignal/`; update every `from tracesignal...` import
  (mechanical, `git mv` + search/replace, verified by the test suite).
- `pyproject.toml`: `name = "tracesignal"`, authors unchanged.
- CLI entry points: `tsig` → `tsig`, `tsig-web` → `tsig-web`
  (`ts` is avoided — too generic and collides with Timesketch tooling mentally;
  `tsig` stays short and unambiguous).
- Env prefix: `TS_` → `TS_` (`core/config.py::env_prefix`), plus `.env.example`,
  `docker-compose.yml`, and every doc that mentions a `TS_*` variable.
- Default service credentials/names in `.env.example` and `docker-compose.yml`:
  `tracesignal` Postgres user/db → `tracesignal`, ClickHouse database
  `tracesignal` → `tracesignal`, Qdrant collection prefix `tracesignal` → `tracesignal`.

**Frontend**
- `frontend/package.json` name, `index.html` title, top-bar branding string,
  any UI copy mentioning TraceSignal.

**Docs & meta**
- `README.md`, `CLAUDE.md`, `docs/CONCEPT.md`, `docs/TECH_STACK.md`,
  `docs/MODEL_REFINEMENT.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md`.
- `docs/archive/*` stays untouched — historical record.
- GitHub repo rename `ScalarForensic/TraceSignal` → `ScalarForensic/TraceSignal`
  is a **manual owner action** (GitHub redirects the old URL automatically).
  Local remotes keep working; update them at leisure.

**Migration note** — add a short `docs/MIGRATION_RENAME.md`:
- Existing deployments must rename `TS_*` env vars to `TS_*`.
- Existing databases do **not** need renaming: set `TS_POSTGRES_URL`,
  `TS_CLICKHOUSE_DATABASE`, `TS_QDRANT_COLLECTION_PREFIX` explicitly to the old
  values. Only the *defaults* change.
- `uv run tsig ...` becomes `uv run tsig ...`.

**Verification**: `uv run pytest`, `uv run ruff check .`, `npm run build`,
`npm run test`, and a manual `tsig-web` smoke run against the dev compose stack.

Estimated effort: ~half a day, one PR, no functional change.

---

## Issue #10 — Timeline creation wizard with field aggregation

### Problem

Two badly normalized sources may carry the same kind of data under different
attribute keys (`src_ip` vs `ip_addr`). Analysts need to merge them into one
canonical field (`ip_address`) when composing a timeline.

### Chosen architecture: query-time mapping (Phase 1), materialization later (Phase 2)

The issue text proposes copying the ClickHouse data per timeline. The current
architecture deliberately stores each event exactly once and resolves timelines via
`source_id IN (...)` (see `db/clickhouse.py` module docstring). Copying would break
that, multiply storage per timeline, freeze mappings at creation time, and mutate a
copy of evidence — bad for the forensic-reproducibility requirement.

Instead, Phase 1 stores the mapping as **timeline metadata** and applies it at query
time. Original events are never touched; the mapping is explainable, auditable, and
editable. Phase 2 (optional, only if query overhead ever hurts) adds a user-triggered
"materialize timeline" job that bakes the mapping into a physical per-timeline table —
recorded here as a deliberate later step, not part of this implementation.

### Phase 1 — data model

- `Timeline.field_mappings` — new nullable JSON column in Postgres:

  ```json
  {
    "ip_address": ["attributes.src_ip", "attributes.ip_addr"],
    "user_name":  ["attributes.user", "attributes.username"]
  }
  ```

  Keys are canonical field names; values are ordered lists of raw field tokens
  (same token grammar `resolve_column_token` already understands). Order defines
  coalesce precedence when one event carries several of the raw keys.
- Add via the existing ad-hoc migration list in `PostgresStore` init
  (`postgres.py` ~line 649 pattern: `ALTER TABLE timelines ADD COLUMN ...`).
- Constraints enforced at the API layer:
  - canonical names must not collide with core event columns
    (`message`, `timestamp`, `artifact`, ...) or with an unmapped raw attribute key
    present in the timeline's sources;
  - each raw field may appear in at most one mapping;
  - mapping raw fields that don't exist in any member source is rejected with a hint.

### Phase 1 — query layer

Single integration point: `db/queries.py::_field_column_expr` /
`resolve_column_token`, which every filter, histogram, export and detector path
already goes through.

- Thread the timeline's `field_mappings` into `QueryFilters` (or a small
  `FieldMapping` resolver object next to it).
- A canonical field token resolves to
  `coalesce(nullif(attributes['src_ip'], ''), nullif(attributes['ip_addr'], ''), '')`.
- Reverse direction: field discovery endpoints (`list_fields`,
  `list_embedding_fields`, `list_anomaly_fields`, `field_recommend`) hide mapped raw
  keys and surface the canonical name instead, annotated so the UI can show the
  merge provenance (`ip_address ← src_ip, ip_addr`).
- Affected consumers to verify one by one (all funnel through the same resolver, but
  each needs a test): event grid filters/exclusions, free-text search column list,
  histogram group-by, `anomaly_stats.value_novelty` + `frequency` field selection,
  embedding wizard field lists, CSV/JSONL export (export writes canonical column and
  records the mapping in the export manifest).
- `db/anomaly_stats.py` bucket math is untouched — only the field-expression
  resolution changes (per its docstring warning).

### Phase 1 — API

- `POST /cases/{id}/timelines` gains optional `field_mappings`.
- `PATCH /cases/{id}/timelines/{tid}` allows editing mappings later; every change is
  written to the existing audit trail (mappings are metadata, evidence is untouched —
  that is the forensic argument for allowing edits).
- New helper endpoint for the wizard:
  `GET /cases/{id}/fields/coverage?source_ids=...` — returns, per raw field, which of
  the selected sources contain it and a sample value, so the wizard can show
  merge candidates. (Reuses `list_fields`/`field_recommend` internals.)

### Phase 1 — frontend wizard

Replace `CreateTimelineDialog.tsx` body with a stepped wizard (pattern already
exists in `EmbedWizard.tsx`):

1. **Name & description** (current form).
2. **Source selection** (current checkbox list).
3. **Field aggregation** — table of raw fields across selected sources with
   per-source presence badges and sample values; auto-grouped identical names;
   drag/select fields into named canonical groups; suggestions for likely merges
   (same `field_recommend` heuristics, e.g. near-identical names / value shapes).
   Skippable — mappings are optional.
4. **Review** — summary of sources + mappings, create.

Plus an "Edit field mappings" entry on the timeline (reopens step 3/4 against the
PATCH endpoint). Timeline list shows a small badge when mappings are active.

### Phase 2 — materialization (recorded, not implemented now)

User-triggered `JobStore` job that writes a physical
`events_tl_{timeline_id}` table with mappings applied, then flips a
`materialized` flag the query layer checks. Storage/duplication tradeoffs get their
own design discussion when (if) needed.

### Tests

- Unit: mapping resolver (coalesce expression, precedence, collision validation).
- Query: filters/histogram/novelty/frequency against a two-source fixture with
  `src_ip`/`ip_addr` merged.
- API: create/patch validation errors, audit entries.
- Frontend: wizard step flow, mapping table interactions (vitest).

Estimated effort: the biggest of the three — roughly 3–5 focused days.

---

## Issue #11 — Subtle guidance + vendored converters + AI prompt

### 1. Vendored normalization scripts

Upstream: `/home/user01/Projekte/2timesketch` (= `overcuriousity/2timesketch`),
stdlib-only converters, but the CLI entry scripts import a shared
`timesketch_converters` package — they are **not** single-file today.

- Add `scripts/vendor_converters.py`: reads the upstream checkout, inlines the
  shared modules (`common.py`, `terminal.py`, plus the per-source module) into one
  self-contained `.py` per converter, prepends a header with upstream repo URL,
  commit hash, and license, and writes the results to
  `src/tracesignal/assets/converters/` (shipped as package data). Committed outputs;
  re-run the script to re-sync with upstream.
- "Slightly adjusted to be reasonable downloads": the vendoring script strips the
  fancy terminal styling down to plain stderr logging where inlining it is
  disproportionate, keeps the uniform CLI (`-i/-o/-f`) intact.
- New API routes (no auth-sensitive data, read-only, works airgapped):
  - `GET /api/converters` — list with name, description, supported inputs, size,
    upstream commit;
  - `GET /api/converters/{name}` — file download.
- Tests: listing matches the vendored directory; downloaded file is byte-identical
  to the committed asset; each vendored script survives `python -m py_compile`.

### 2. Upload view: downloads panel + AI prompt

In the source-upload view (`UploadDialog.tsx` / `SourceList.tsx` area), add a right
side panel:

- **Converter downloads** — list from `GET /api/converters` with download buttons.
- Below it, a hint: *"Generative LLMs are good at writing normalization scripts for
  formats not covered here"* plus a **copy-prompt** field. The prompt is a static
  string committed to the frontend that instructs an LLM to produce a converter
  conforming to the expected standard: required columns
  (`datetime`, `timestamp_desc`, `message`, `data_type`, `timestamp`, plus the
  shared semantic columns like `src_ip`), CSV/JSONL output, stdlib-only, the
  `-i/-o/-f` CLI convention — i.e. the 2timesketch contract. Copy-to-clipboard
  button, no network access needed.

### 3. Subtle guidance text

- New `frontend/src/components/ui/GuidancePanel.tsx` — muted, low-contrast side
  panel; collapsible, collapsed-state persisted in `localStorage`
  (subtle, never modal, never blocking).
- **Case list page** (empty flanking space): what a case is — collection of sources,
  multi-investigator, timelines — and "create a case to begin".
- **Inside a case**: numbered walkthrough matching the real pipeline:
  1. Normalize input data (link to the converter downloads panel, why Timesketch
     format is required);
  2. Upload & ingest (note that large sources take time; job tray shows progress);
  3. Analyze immediately via the default "all sources" timeline; create custom
     timelines to recombine sources (once #10 lands: "…and merge equivalent fields");
  4. Optionally create embeddings for semantic search / similarity (points to the
     embed wizard).
- Copy lives in one `guidance.ts` strings module so wording is reviewable in one
  place.

### Tests

- API tests for the converter endpoints; vitest snapshot for GuidancePanel
  collapse/persist behavior.

Estimated effort: ~1–1.5 days.

---

## Cross-cutting constraints honored

- **Airgapped/offline**: converters vendored, AI prompt is static text, no new
  network paths.
- **Forensic reproducibility**: #10 mappings are auditable timeline metadata;
  evidence rows are never rewritten; exports record the mapping. Vendored converters
  carry upstream commit hashes.
- **Ephemeral JobStore** untouched; Phase 2 materialization (if ever built) will use
  it as-is.
