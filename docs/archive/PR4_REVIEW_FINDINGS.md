# Code Review Findings — PR #4 (feat/statistical-anomaly-engine)

- **PR:** #4 "Feat/statistical anomaly engine" — `feat/statistical-anomaly-engine` → `main`, 69 files, +7458/−1892
- **Review date:** 2026-07-02
- **Method:** 8 independent finder passes (line-by-line diff scan, removed-behavior audit, cross-file tracing, reuse/simplification/efficiency/altitude/conventions), followed by one verifier pass per deduplicated correctness candidate. Verdicts: **CONFIRMED** (trigger + wrong output pinned to quoted code), **PLAUSIBLE** (mechanism real, trigger uncertain or intent ambiguous), **UNVERIFIED** (finder candidate, no dedicated verification pass — re-check before acting).
- **Line numbers** refer to this branch's working tree at review time. Verify with surrounding context before editing; the branch may have moved.
- No CLAUDE.md exists in the repo or user config, so there were no convention findings.
- **Resolution pass:** 2026-07-02, same day. All 30 items closed — 29 fixed, 1 (C13) deliberately descoped with a documented reason. See per-item ✅/⏭ status below and the summary table immediately following this header. Backend: 197 tests passing (up from 180 at review time), full `ruff check`/`ruff format --check` clean. Frontend: 23 tests passing (up from 16), `tsc`/`oxlint`/`vite build` all clean.
- **Follow-up pass:** 2026-07-02, same day. The two previously-deferred items, C13 and C18, were implemented in a dedicated pass after re-scoping was judged worth the risk. Both are now ✅ Fixed — see "Follow-up: C13 and C18 implemented" below. Backend: 215 tests passing (up from 197), `ruff check`/`ruff format --check` clean. Frontend: 23 tests passing, `tsc`/`oxlint`/`vitest`/`vite build` clean. This pass also fixed a pre-existing orphan-cleanup gap found along the way (`delete_case` wasn't removing `View`/`Annotation` rows).

## Resolution summary

| # | Status | What happened |
|---|--------|----------------|
| F1 | ✅ Fixed | str-wrapped at the source (`_normalize_event_row` in `queries.py`), not just at the two call sites — fixes every consumer of that row shape, not only export. |
| F2 | ✅ Fixed | Dropped eager `.load()` in `_get_embedding_model`. Also fixed the *same* bug independently present in `_get_field_encoder` (not originally called out as its own finding). |
| F3 | ✅ Fixed | NULL timestamps now sentinel-mapped (`2299-12-31 23:59:59.999`) in both cursor construction and the keyset predicate (`coalesce`). Verified empirically against a live ClickHouse instance — NULLS sort last in both ASC/DESC as assumed. |
| F4 | ✅ Fixed | Predicate now compares native `UUID` instead of `toString()`. This broke the jump-to-time "no anchor event" synthetic cursor, which relied on empty-string sorting before any real UUID string — fixed by mapping the empty sentinel to the all-zero UUID (`00000000-...-000000000000`), the native minimum, instead. |
| F5 | ✅ Fixed | Both `semantic_search_events` and `find_similar_events` now wrapped in `run_in_threadpool`. |
| F6 | ✅ Fixed | `getNextPageParam` synthesizes an `after` cursor from a before-seeded anchor page's own `next_cursor`, keyed off a `before`-flagged pageParam. Verified against the real 3.4M-event sample dataset end-to-end. |
| F7 | ✅ Fixed | `queryClient.cancelQueries` added before `setQueryData`; pending-jump effect now gated on a jump-sequence counter so a stray automatic fetch can't be mistaken for "ready". |
| F8 | ✅ Fixed | New `to_clickhouse_utc()` in `db/_dt.py`, used by both `anomaly_stats.py` and `queries.py`. Also fixed a **second, previously-uncalled-out instance** of the same bug in `queries.py`'s `start`/`end` range-filter formatting. |
| F9 | ⏭ No-op, documented | No migration added — no releases exist, pre-branch DBs already documented as deprecated. A code comment now says so explicitly in `init_schema`, so it isn't "rediscovered" as an oversight. |
| F10 | ✅ Fixed | New shared `db/_columns.py` (`TOP_LEVEL_EVENT_COLUMNS` + `resolve_column_token`), used by both `anomaly_stats._col_expr` and `queries._column_expr`. Widens anomaly_stats' allowlist to match queries.py's (adds `timestamp`, `parser_version`, `content_hash`, `file_hash`) rather than adding a reject-unknown-token path — see "Descoped/adjusted" below for why. |
| F11 | ⏭ Reviewed, no action | Confirmed deliberate per commit `2012579`; no code change needed. |
| U1 | ✅ Fixed (reverted) | Upload reverted to ingest-only; auto-embed-on-upload removed, `embed_job_id` field/plumbing removed end-to-end (backend response, frontend type, `UploadDialog.tsx`). |
| U2 | ✅ Fixed | `update_source_embedding_config` deleted from `postgres.py` — confirmed zero remaining callers. |
| U3 | ✅ Fixed | `artifact`/`artifacts` now merge into one effective list in `_build_where` instead of two ANDed predicates. `tag`/`tags_include` left as-is (see "Descoped/adjusted" — they don't collide the same way) with a deprecation note added to the query params. |
| C1 | ✅ Fixed | Folded into F8's fix (same `to_clickhouse_utc` helper). |
| C2 | ✅ Fixed | Shared `EVENT_SELECT_COLUMNS` tuple in `db/_columns.py`, used by both modules. |
| C3 | ✅ Fixed | Shared `db/_buckets.py` (`query_timestamp_range` + `bucket_interval_seconds`), used by `queries.py` histogram and both `anomaly_stats.py` range queries — WHERE-clause scope intentionally still differs per module (see module docstrings), only the query shape/formula is shared. |
| C4 | ✅ Fixed | New `fmtTimestampCompactUtc` in `lib/time.ts`; both `FrequencyView`/`ValueNoveltyView` now use it. **Decision changed from the original finding's suggestion**: uses UTC (matching the detail panel), not the grid's local time — user's explicit call, for forensic-reproducibility consistency across analyst timezones. |
| C5 | ✅ Fixed | `fieldLabel`/`tokenLabel` merged into one `anomalyFieldLabel` in `lib/format.ts` (kept `AnomalyFieldPicker`'s title-case labels as canonical). `mapAnomalyField` in `ExplorerPage.tsx` left separate — it does filter-key remapping, not label display, a different job. |
| C6 | ✅ Fixed | `_parse_id_list` deleted, its 4 call sites repointed to `_parse_str_list`. |
| C7 | ✅ Fixed | `SimilaritySearchResult` now uses `field(default_factory=list)`. |
| C8 | ✅ Fixed | Subsumed by F10 as predicted — the shared `resolve_column_token` helper needs only a fixed `"fk"` param name, no counter. |
| C9 | ✅ Fixed | Shared `tagResultLabel()` in `lib/format.ts`; all 6 cast sites across both views removed. |
| C10 | ✅ Fixed | Header comment rewritten to describe the actual findings-list rendering. |
| C11 | ✅ Fixed | Added `_MAX_AUTO_SCAN_FIELDS = 15` cap in auto-select mode, applied after `recommend_novelty_fields`'s coverage-descending sort so the highest-value fields are kept. UNION ALL/ARRAY JOIN batching not attempted (see "Descoped/adjusted"). |
| C12 | ✅ Fixed | `find_value_novelty` computes `total_events` once and passes it into `recommend_novelty_fields(total=...)`, which now skips its own identical `count()` query when given one. |
| C13 | ✅ Fixed (follow-up pass) | Originally descoped (see "Why C13 was descoped" below), later implemented — see "Follow-up: C13 and C18 implemented". |
| C14 | ✅ Fixed | New `useDebouncedValue` hook (`frontend/src/hooks/`), 400ms debounce on the z-threshold input before it feeds the query key. |
| C15 | ✅ Fixed | Scroll position moved out of `ExplorerPage` state into a new dedicated `useScrollPositionStore` (zustand), subscribed only by `TimelineHistogram` — `ExplorerPage` itself no longer re-renders on scroll. |
| C16 | ✅ Fixed | New shared `_run_stat_detector()` helper used by both `list_anomalies` and `tag_anomalies`. Also renamed `baseline_start` → `baseline_end` (GET query param, POST body field, frontend type) since it was always treated/passed internally as an end-of-baseline timestamp — the old name was actively misleading. |
| C17 | ✅ Fixed | Backend: new `_resolve_event_id_filters()` helper used by all 4 endpoints. Frontend: new `serializeEventFilterFields()` in `lib/queryParams.ts`, used by `eventsApi.list`, `eventsApi.histogram`, `annotationsApi.bulkByFilter`, and `downloadExport` — each still handles its own `filters`/`exclusions` object serialization since that genuinely differs by transport (stringified for query-param-shaped requests, raw objects for export's structured JSON body). |
| C18 | ✅ Fixed (follow-up pass) | Originally left as designed (documented deferral), later implemented — see "Follow-up: C13 and C18 implemented". |

### Why C13 was descoped

**C13** asked to push the tags-include/exclude predicate into ClickHouse's `WHERE` clause (`hasAny(tags, ...)`) instead of resolving matching event IDs into a Python list and re-injecting them as a query parameter on the next round trip.

The blocker: `_resolve_tags_event_ids` unions matches from **two different databases** — Postgres (user annotation tags) and ClickHouse (parser-derived `tags` array). A true push-down needs a single compound predicate, ANDed with every other active filter, that itself OR-combines the two systems: `hasAny(tags, :values) OR has(:postgres_ids, toString(event_id))`. That predicate can't be expressed through `EventQuery`'s existing `event_ids`/`exclude_event_ids` fields, which model "AND of independent restriction lists" (Python set intersection), not "one field that internally OR-combines two sources." Building it correctly means:
1. New `EventQuery` fields carrying `(tag_values, postgres_ids)` instead of a flat ID list for tags specifically,
2. A new WHERE-builder method emitting the compound `hasAny(...) OR has(...)` clause,
3. Changing `_resolve_event_id_filters` — the shared helper this session added for **C17**, touching the same four endpoints — to return this new shape instead of a plain ID-list tuple.

That's real surgery across the exact code path C17 already restructured in this same pass, for a win that only matters when a single tag value matches a very large fraction of a case's events (most tags in practice match a bounded set — this is a scale edge case, not a routine cost). Doing it as a narrow patch on top of a freshly-changed shared helper, without dedicated test coverage for the compound-predicate SQL, was judged a worse risk/benefit trade than the plan's other 29 items — most of which were either bug fixes or single-file mechanical extractions. It's documented (not silently dropped) with the exact blocker and next steps inline at `_resolve_tags_event_ids`'s docstring in `events.py`, so a future pass has the reasoning already worked out instead of re-deriving it.

### Follow-up: C13 and C18 implemented (2026-07-02, later same day)

Both items above were re-scoped and implemented in a dedicated follow-up pass, after the blockers documented above were judged worth the risk with focused test coverage.

**C13 — tag filtering pushed into ClickHouse.** Added `TagFilter` (`db/queries.py`): `tag_values` (matched via `hasAny(tags, ...)`) + `postgres_event_ids` (pre-resolved via one Postgres round trip, matched via `has(:ids, toString(event_id))`), OR-combined into a single compound predicate by a new `add_tag_filter()` builder method, ANDed alongside every other filter exactly as the original finding specified. `EventQuery` gained `tags_include`/`tags_exclude: TagFilter | None` fields, distinct from `event_ids`/`exclude_event_ids` since they carry OR-between-two-systems semantics. `_resolve_tags_event_ids` (renamed `_resolve_tags_filter`) now does only the Postgres lookup — the second ClickHouse round trip (`list_event_ids_by_parser_tags`) is deleted, its job subsumed by the native `hasAny(...)` half of the compound predicate. All 4 call sites (`list_events`, `bulk_annotate_by_filter`, `get_histogram`, `export_events`) updated to pass `tags_include`/`tags_exclude` filters instead of folding tag-derived IDs into `event_ids`. Filtering semantics are unchanged (a tag matches if either system has it) — this was a performance/architecture change only. Tests: `test_queries.py` asserts the compound SQL shape and negation; `test_events_router.py` covers `_resolve_tags_filter` and the separated-not-folded return shape.

**C18 — persisted detector runs replace `live_event_ids`.** New `DetectorRun` Postgres model (`db/postgres.py`): case/timeline-scoped, stores the scan's request params and its serialized `StatAnomalyResult` (JSON), created via `PostgresStore.create_detector_run`/looked up via `get_detector_run`. Rows accumulate rather than being overwritten (matches the forensic-reproducibility posture of `Annotation`/`View` — a case's scan history stays auditable). `list_anomalies` gained a `persist: bool = True` query param — on every successful (`status == "ok"`) scan it writes a `DetectorRun` and returns `run_id` in the response; `tag_anomalies` always persists one. New `GET /cases/{case_id}/detector-runs/{run_id}` endpoint returns a run's params/findings without re-running the detector. `live_event_ids` (comma-separated event IDs re-uploaded on every request, the original URL-length problem) is fully replaced — no back-compat path — by a single short `run_id` string threaded through `_resolve_annotated_event_ids`/`_resolve_event_id_filters` and the same 4 endpoints C13 touched; an unknown/foreign-case `run_id` now 404s rather than silently matching nothing. Frontend: `FrequencyView`/`ValueNoveltyView` gained an `onRunIdChange` callback (parallel to the existing `onFindingsChange`) plumbed through `AnalysisPanel` to `ExplorerPage`, which now tracks `anomalyRunId` state instead of deriving a live event-ID array from `anomalyMarkers`; `EventFilters.liveAnomalyEventIds` renamed to `anomalyRunId: string`, serialized as `run_id` in `lib/queryParams.ts`.

**Design decisions made without user sign-off in time** (defaulted to the recommended option in each case, per plan): `run_id` fully replaces `live_event_ids` (no fallback path); every `list_anomalies` scan persists a run by default (no explicit "pin" step); `detector_runs` rows accumulate unboundedly (no expiry/cap). Flagged here in case any should be revisited.

**Bug found and fixed along the way:** `PostgresStore.delete_case` deleted `Timeline`/`Source` rows but not `View`/`Annotation` — both are case-scoped by a plain `case_id` column with no FK/cascade, so they silently orphaned on every case delete. Fixed alongside adding the same cleanup for the new `DetectorRun` table.

Files touched: `src/tracevector/db/queries.py`, `src/tracevector/db/postgres.py`, `src/tracevector/api/routers/events.py`, `tests/test_queries.py`, `tests/test_events_router.py`, `tests/test_postgres_store.py` (new); `frontend/src/api/types.ts`, `frontend/src/api/anomalies.ts`, `frontend/src/lib/queryParams.ts`, `frontend/src/components/analysis/{AnalysisPanel,FrequencyView,ValueNoveltyView}.tsx`, `frontend/src/pages/ExplorerPage.tsx`, `frontend/src/test/queryParams.test.ts`.

### Other deviations from the original findings text

- **F1**: the "two call sites" framing in the original finding was inaccurate — `_index_annotations_by_event` has exactly one call site (`export_events`); lines 722/732/761 cited in the original were internal to the function/its consumers, not separate calls. The underlying UUID-vs-str bug was real; only the call-site count was wrong. Fixed at the row-normalization source instead of patching two spots, which also covers non-export consumers of the same row shape.
- **U1**: fixed by *reverting* the new auto-embed-on-upload behavior rather than keeping it — this was a judgement call the user made explicitly (not the plan's suggested default), given the failure-mode risk in deployments without a reachable embedding model.
- **C11**: implemented as a cap (`_MAX_AUTO_SCAN_FIELDS = 15`), not the UNION ALL/ARRAY JOIN batching the finding also mentioned as an option — the plan itself called out "simplest first pass: cap" as the intended first move.

---

## Part 1 — Confirmed correctness bugs (fix these)

### F1. Export annotation join never matches (UUID vs str keys) — CONFIRMED
**Where:** `src/tracevector/api/routers/events.py:732` (JSONL) and `:761` (CSV); dict built at `:722`.

`_index_annotations_by_event` keys the dict with `Annotation.event_id`, a Postgres `String(64)` (`src/tracevector/db/postgres.py:268`). But `iter_events` (`src/tracevector/db/queries.py:513`) yields raw clickhouse-connect rows where `event_id` is a `uuid.UUID` (schema `event_id UUID`, `src/tracevector/db/clickhouse.py:70`; no uuid-to-string query setting anywhere; `_normalize_event_datetimes` only touches datetimes). `uuid.UUID` and `str` never compare/hash equal, so `annotations_by_event.get(row["event_id"], [])` always returns the default.

**Impact:** every export has `annotations: []` (JSONL) / empty `user_tags`, `comments`, `anomaly_findings` (CSV) — the "self-contained record" claim in the endpoint docstring (events.py:787-791) silently never holds.
**Fix:** `annotations_by_event.get(str(row["event_id"]), [])` at both call sites (or `toString(event_id)` in the export SELECT). The rest of the codebase already str-wraps, e.g. `queries.py:468`, `clickhouse.py:273`.
**Test gap:** add an export test with an annotated event asserting non-empty annotations in output.

### F2. Semantic search 500s in remote-embedding mode — CONFIRMED
**Where:** `src/tracevector/db/similarity.py:155-159` (`_get_embedding_model`), called from `find_similar_by_text` at `:209`.

`_get_embedding_model()` unconditionally calls `EmbeddingModel.load()`, but this same PR makes `load()` raise `RuntimeError("load() is not available when using a remote embedding endpoint")` when `embedding_api_base_url` is set (`src/tracevector/models/embeddings.py:62-65`). `encode()` does NOT need `load()` in remote mode (`embeddings.py:103-105`). The endpoint `semantic_search_events` (`src/tracevector/api/routers/events.py:914-929`) has no try/except → HTTP 500 on every free-text search whenever remote embeddings are configured.

**Fix:** drop the eager `load()` — just construct `EmbeddingModel()` and let `encode()` route (it lazy-loads locally, calls remote otherwise). Compare `_get_field_encoder` in events.py:50-56, which guards the same call.

### F3. NULL-timestamp rows poison the pagination cursor — CONFIRMED
**Where:** `src/tracevector/db/queries.py:465-469` (cursor construction); `src/tracevector/api/routers/events.py:107-112` (`_parse_cursor` 400); `frontend/src/pages/ExplorerPage.tsx:57-59` (`cursorParam`).

`timestamp` is `Nullable(DateTime64(3))` (`clickhouse.py:82`) and NULL timestamps genuinely get ingested (`models/event.py:224` — unparsable/missing datetimes become `None`, pipeline inserts unfiltered). `next_cursor = (events[-1]["timestamp"], …)` has no None guard → API serializes `[null, "<uuid>"]` → frontend template literal produces `after="null,<uuid>"` (the `lastPage.next_cursor` truthiness check doesn't catch a `[null, id]` array) → `_parse_cursor`'s `datetime.fromisoformat("null")` raises → HTTP 400 → infinite query enters permanent error state. ClickHouse sorts NULLs last in both directions, exactly where infinite scroll arrives; trigger is a full page whose last row has NULL timestamp with more rows behind it.

**Secondary (same root cause):** even with the 400 fixed, the keyset predicate `(timestamp, toString(event_id)) {op} (…)` (`queries.py:288-291`) evaluates to NULL for NULL-ts rows, so those rows are unreachable in cursor mode. A complete fix must handle NULL ordering in the predicate (e.g. sentinel timestamp or `ORDER BY`/predicate on `coalesce`), not just guard cursor construction.

### F4. Keyset cursor order mismatch: native UUID vs toString — CONFIRMED
**Where:** `src/tracevector/db/queries.py:288-291` (predicate) vs `:433` (ORDER BY; also `:504` export path).

Pages are cut by `ORDER BY timestamp {dir}, event_id {dir}` (native UUID order — ClickHouse compares the two internal UInt64 halves; docs explicitly warn `ORDER BY uuid` ≠ `ORDER BY toString(uuid)`), but the seek predicate compares `(timestamp, toString(event_id))` lexicographically. The docstring at `:281-284` claims these match "exactly" — they don't. For events sharing one `DateTime64(3)` timestamp across a page boundary, rows are duplicated or permanently skipped during infinite scroll.

**Fix:** compare natively: `(timestamp, event_id) {op} ({ts:DateTime64(3)}, {id:UUID})`. This also lets the predicate use the `(case_id, source_id, timestamp, event_id)` primary index, which `toString()` defeats.

### F5. Semantic search blocks the FastAPI event loop — CONFIRMED
**Where:** `src/tracevector/api/routers/events.py:914-929` (`semantic_search_events`, new in this PR).

`async def` handler calls the fully synchronous `svc.find_similar_by_text(...)` directly: lazy SentenceTransformer load on first query (multi-second), CPU-bound `encode()` (or blocking `httpx.Client` in remote mode), then sync Qdrant + ClickHouse I/O. All concurrent requests freeze for the duration. The anomaly endpoints in the same file already use `await run_in_threadpool(...)` (lines 986, 1059, 1070, 1083, 1165, 1176, 1189; import at line 13).

**Fix:** `result = await run_in_threadpool(svc.find_similar_by_text, case_id, source_ids, q, limit=limit)`.
**Note:** neighboring `find_similar_events` (~line 904) has the same defect but predates this PR.

### F6. Jump-to-time (no eventId): forward pagination permanently dead — CONFIRMED
**Where:** `frontend/src/pages/ExplorerPage.tsx:541-553` (no-eventId branch of `handleJumpToTime`); backend `src/tracevector/db/queries.py:443-451`; consumer `ExplorerPage.tsx:340-343`.

The branch seeds the query cache with a single page fetched via a `before` cursor. Backend before-mode pages only ever set `has_more_before`; `has_more_after` stays `False`. `getNextPageParam` therefore returns `undefined`, `hasNextPage` is false, footer shows "all loaded", and scrolling down never fetches — yet the events at/below the jump target are exactly in that dead direction (a before-page contains only rows on the near side of the cursor). No recovery: `staleTime: 10_000` + `refetchOnWindowFocus: false` (App.tsx). The eventId branch is fine (takes `has_more_after` from its after-cursor fetch, line ~536).

**Fix options:** have the backend probe/set `has_more_after` for before-pages, or seed the cache so the anchor page's pageParam is a before-param and `getNextPageParam` can synthesize an after-cursor from the page's last row.

### F7. Jump-to-time races the automatic React Query fetch — CONFIRMED
**Where:** `frontend/src/pages/ExplorerPage.tsx:501` (`setFilters({})`) → `:550` (`setQueryData`); query key at `:317`.

`setFilters({})` changes the query key to `["events", caseId, timelineId, {}, sortDir]`; that key has no cached data so `useInfiniteQuery` immediately fires an offset-0 fetch (newest page, includes the expensive COUNT). The handler then awaits 1-3 of its own requests and calls `setQueryData`. Nothing calls `queryClient.cancelQueries` (zero hits in `frontend/src`), so if the automatic fetch resolves after the seeding it overwrites the anchor page and the jump silently lands at the top of the timeline. Additionally, in the reverse ordering the offset-0 page can satisfy the pending-jump effect's `ready = events.length > 0` check (~line 620) against the wrong page.

**Fix:** `await queryClient.cancelQueries({ queryKey })` before `setQueryData` (standard optimistic-update pattern), and make the pending-jump effect validate it is looking at the seeded page.

### F8. value_novelty temporal split drops timezone offsets — CONFIRMED
**Where:** `src/tracevector/db/anomaly_stats.py:210-212` (`_fmt_dt`), used at `:521` (baseline_size) and `:555` (main query); router pass-through `src/tracevector/api/routers/events.py:1028/1057/1078/1090` (GET) and `:1125/:1163` (POST).

`_fmt_dt` is a bare `strftime` — for an aware datetime it emits wall-clock digits and discards the offset. FastAPI parses `baseline_start=2026-06-01T14:00:00+02:00` as an aware datetime and the router passes it verbatim; ClickHouse timestamps are stored naive-UTC (`_dt.py` docstring). Result: split lands at 14:00 UTC instead of 12:00 UTC — first-seen values in the gap are silently suppressed by `HAVING baseline_cnt = 0` (or falsely flagged with negative offsets), and `baseline_size` is wrong. `find_frequency_anomalies` handles the same input correctly (`:698-699`, `:776-780`, aware-vs-aware Python comparisons), so the two detectors disagree about the same `baseline_start`.

**Fix:** convert before formatting: `_fmt_dt(ensure_utc(baseline_end).astimezone(timezone.utc))`. Note `ensure_utc` alone is NOT sufficient — it passes already-aware datetimes through unchanged (`_dt.py:17-21`); the `.astimezone` is required. Cleanest: normalize once in the router for both detectors.

---

## Part 2 — Verified but downgraded (judgement calls, not clear-cut bugs)

### F9. `outlier` → `anomaly` annotation_type rename has no data migration — CONFIRMED mechanism, dev-only impact
**Where:** `src/tracevector/api/routers/events.py:1217`; `postgres.py:972-1004`, `:324-346`.

Main wrote system annotations with `annotation_type="outlier"`; this branch only creates/deletes/filters `"anomaly"` (further scoped by `detector`). No `UPDATE annotations SET annotation_type='anomaly' WHERE annotation_type='outlier'` exists, yet `init_schema` DOES ship in-place column ALTERs (`pinned`, `detector`) for main-era DBs. On such a DB, stale `outlier` rows surface read-only in `EventDetailPanel` (system annotations have no delete button), are invisible to the `annotated=anomaly` filter, and no re-tag run ever clears them. Mitigation: no releases exist (`git tag` empty) and commit `2012579` declares old databases deprecated — impact limited to dev instances upgraded in place. If in-place upgrade from main is meant to work (the pinned/detector ALTERs suggest yes), add the one-line UPDATE next to them.

### F10. Field-token allowlist drift between anomaly engine and query layer — PLAUSIBLE (API-reachable only)
**Where:** `src/tracevector/db/anomaly_stats.py:55-64` (`_TOP_LEVEL_COLUMNS`) + `:188-207` (`_col_expr`) vs `src/tracevector/db/queries.py:87-102` (`_TOP_LEVEL_FILTER_COLUMNS`) + `:295-302` (`_column_expr`).

Duplicated routing logic; the sets have already drifted — queries.py includes `timestamp`, `parser_version`, `content_hash`, `file_hash`, which anomaly_stats lacks (also: queries.py normalizes `strip().lower()`, anomaly_stats matches exactly). `_col_expr` has no error path: an unknown bare token silently becomes `attributes['<col>']` (always empty) and the detectors' `!= ''` predicates yield zero findings with `status: "ok"`. The shipped UI only emits safe tokens (picker options come from `_NOVELTY_CANDIDATE_TOP_LEVEL` + `attr:`-prefixed keys; FrequencyView uses a static safe list), so this is reachable only via direct API use (`?fields=parser_version`, `?series_field=content_hash` — both unvalidated free-form params). **Fix:** share one routing helper between the two modules, or validate tokens at the API boundary and 400 on unknown names.

### F11. init_schema migration rewrite drops legacy ALTERs — PLAUSIBLE, appears deliberate
**Where:** `src/tracevector/db/postgres.py:334-346`.

The old per-startup ALTERs (timelines embedding_* columns; annotations.source_id ADD + backfill) are gone; only inspector-checked `pinned`/`detector` ALTERs remain. The Timeline ORM still maps the embedding columns, so a pre-embedding-columns DB would raise UndefinedColumn — but commit `2012579` ("Older databases are deprecated") documents this as intentional, there are no releases, and main's own migration block was itself broken for fresh installs. **No action needed unless in-place upgrades from pre-`4c6ab62` databases must be supported.** Listed so the next agent doesn't "rediscover" it as a bug.

---

## Part 3 — Unverified finder candidates (correctness-flavored; re-verify before fixing)

These came out of the finder passes with concrete failure scenarios but did not get a dedicated verification pass.

- **U1. `src/tracevector/api/routers/cases.py:~271` — upload now auto-schedules an embedding job.** Old behavior documented "Embeddings are *not* generated here"; new code unconditionally spawns an all-fields embed job per upload. In deployments that cannot embed (no cached model, unreachable remote endpoint) every upload produces a failed job in the job tray; the auto job may also repopulate the default all-fields collection after a curated re-embed, and `find_collection_for_sources` (most-points heuristic) could flip similarity search back to the un-curated collection. Possibly intentional product behavior — check with the author before "fixing".
- **U2. `src/tracevector/api/routers/cases.py:~590` — per-source embed endpoint deleted.** `POST /cases/{id}/sources/{sid}/embed` was documented as "remains for advanced per-source use and backwards compatibility" and is now gone; `update_source_embedding_config` appears to be dead code with no remaining caller. If deliberate, delete the dead code too.
- **U3. `src/tracevector/db/queries.py:~28 + _build_where` — plural filters bolted beside singular ones.** `artifacts` next to `artifact`, `tags_include`/`tags_exclude` next to `tag`/`exclude_tag`, including a `len==1` special case. A caller setting both `artifact='a'` and `artifacts=['b']` gets silently-ANDed empty results. Consider folding singular into the list form with a deprecated alias at the API boundary.

---

## Part 4 — Cleanup findings (reuse / simplification / efficiency / altitude; UNVERIFIED unless noted)

### Reuse (duplication of existing helpers)
- **C1.** `anomaly_stats.py:210` `_fmt_dt` re-implements `queries.py`'s `_format_clickhouse_datetime`. queries.py already needed a `_precise` variant because second-truncation skipped boundary events — anomaly baselines won't get that fix. Move one helper to `db/_dt.py`. (Interacts with F8 — fix together.)
- **C2.** `anomaly_stats.py:91` `_EVENT_COLUMNS` duplicates `queries.py` `_EVENT_SELECT_COLUMNS` (22-column event projection). New schema columns will silently be missing from anomaly-hydrated events only.
- **C3.** `anomaly_stats.py:730` (and `get_timeline_midpoint` ~`:441`) copies the histogram's min/max range query and `interval = max(1, int(duration / bucket_count))` formula from `queries.py` — the module docstring admits it. Frequency-anomaly window markers are overlaid on the frontend histogram; formula drift misaligns every marker. Share a bucketing helper.
- **C4.** `frontend/src/components/analysis/FrequencyView.tsx:50` and `ValueNoveltyView.tsx:56` — private `fmtTs` copies using local-timezone `toLocaleString()`, while `frontend/src/lib/time.ts` is the codebase's single point for the UTC-display policy (the deleted AnomaliesList.tsx imported it correctly). Anomaly panels show times shifted from the event grid for non-UTC analysts. Import from `lib/time.ts`.
- **C5.** The `attr:`-token→label mapping exists three times with diverging behavior: `fieldLabel` (`ValueNoveltyView.tsx:41`), `tokenLabel` (`AnomalyFieldPicker.tsx:37`), `mapAnomalyField` (`ExplorerPage.tsx:193`). They already disagree (`timestamp_desc`→'desc' in one, `tags`→'tag' in another). One shared helper in `frontend/src/lib/format.ts`.

### Simplification
- **C6.** `events.py:212` `_parse_str_list` is byte-identical to `_parse_id_list` (`:205`). Keep one.
- **C7.** `similarity.py:43` — hand-written `__init__` inside a `@dataclass` just to default `results` to `[]`; use `field(default_factory=list)` (the exact form the diff deleted). Future fields won't appear in the constructor otherwise.
- **C8.** `anomaly_stats.py:204` — `_col_expr`'s mutable `ctr=[0]` unique-param-name machinery is never exercised (every call site uses a fresh counter once); a fixed param name does the same job. (Subsumed if F10's shared-helper fix lands.)
- **C9.** `ValueNoveltyView.tsx:411` / `FrequencyView.tsx:388` — tag-mutation success JSX including three `as { tagged?; skipped_unresolved? }` casts is copy-pasted; the casts erase existing typing (`anomaliesApi.tag` already returns `TagAnomaliesResponse`). Extract a `tagResultLabel(data)` helper, drop the casts.
- **C10.** `FrequencyView.tsx:2` — header comment describes a "time-series bar chart" with "hand-rolled div bars" and an `onRangeSelect` brush that don't exist; component renders a findings list with `onDrillField`/`onJumpToTime`. Rewrite the comment.

### Efficiency
- **C11.** `anomaly_stats.py:527` — `find_value_novelty` runs one sequential full-scan GROUP BY per field token; auto mode can select ~54 fields → ~54 serial ClickHouse round-trips per panel open, occupying a threadpool worker throughout. Combine into UNION ALL / ARRAY JOIN, or cap auto-selected fields.
- **C12.** `anomaly_stats.py:497` — auto-field mode calls `recommend_novelty_fields` (which already counts the same predicate) then immediately re-runs the identical `count()`. Reuse the total.
- **C13.** `events.py:186` — `_resolve_tags_event_ids` materializes every matching event_id into Python and ships the list back into ClickHouse as a `has(...)` parameter, with a blocking sync query inside an async handler. For a tag on millions of events this balloons memory/latency per page fetch. Push the predicate into the events WHERE clause (`hasAny(tags, {vals})` / subquery).
- **C14.** `FrequencyView.tsx:203` — z-threshold number input feeds the react-query key with no debounce; each keystroke (including transient `''`) triggers a full detector scan. Debounce/commit-on-blur, or filter client-side on the already-returned `z_score`.
- **C15.** `ExplorerPage.tsx:799` — `onVisibleTimestampChange={setScrollPositionTs}` re-renders the whole page tree (non-memoized EventGrid + FilterRail + AnalysisPanel) on nearly every row crossed while scrolling, even with the histogram closed. Route through a ref/zustand store subscribed only by TimelineHistogram, or `React.memo` EventGrid.

### Altitude (right-depth concerns)
- **C16.** `events.py:~1175` — the detector-dispatch pipeline (temporal-midpoint resolution, suppression fetch, config-default fallbacks, detector branching) is duplicated between `list_anomalies` and `tag_anomalies`. Drift here means "Tag N anomalies" persists a different set than the preview the analyst approved. Extract `_run_stat_detector(...)`.
- **C17.** `events.py` (×4: list_events ~356, bulk_annotate ~465, histogram ~630, export ~794) — the annotated/tags_include/tags_exclude/ids resolution-and-intersect block is copy-pasted with ~10 identical query params each, mirrored by four hand-rolled serializers in `frontend/src/api/` (events.ts ×2, annotations.ts, export.ts). A shared FastAPI dependency + one `serializeEventFilters()` on the client. Any new filter param currently needs ~8 edits; a miss makes grid/histogram/bulk/export disagree.
- **C18.** `events.py:~313` — `live_event_ids` uploads the client's unpersisted finding IDs on every request; at `limit=500` that's ~18KB of UUIDs in a GET query string, past common proxy URL limits — the filter breaks exactly when findings are numerous. Deeper fix: persist detector runs server-side under a run ID, or filter live findings purely client-side.

---

## Suggested order of attack

1. F1, F2, F5 — small, isolated, high-impact one-to-few-line fixes.
2. F8 + C1 together (timezone + shared datetime formatter).
3. F3 + F4 together (both live in the keyset cursor path in `queries.py`; fix predicate to native UUID and handle NULL timestamps in one pass; then add pagination tests for tied timestamps and NULL-ts tails).
4. F6 + F7 together (both in `handleJumpToTime`; cancelQueries + before-page has_more semantics).
5. F9/U1/U2/U3 — confirm intent with the author before changing behavior.
6. Cleanup C1-C18 opportunistically, ideally after the correctness fixes so they don't tangle diffs. C16/C17 are the highest-leverage structural ones.
