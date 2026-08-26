# Scan admission classes — design round (issue #300)

**Status:** accepted, 2026-08-26. Implemented by
`docs/superpowers/plans/2026-08-26-scan-admission-classes.md`.

The second half of the 1.15.0 scan-budget work. Session 188 (`2026-08-25-scan-budget-
truthfulness.md`) made the numbers the app reports about itself true; this round changes
what those numbers *admit*. It was parked then because it touches the hot query path, and
it is picked up now on production evidence: a 700M-event timeline on a correctly sized
host (8 cores, `VESTIGO_STAT_SCAN_CONCURRENCY=2`, later 4) where an analyst opening a
per-value histogram during an Investigate sweep gets a spinner that never resolves, and a
page reload turns every chart on the page into the same spinner.

## The defect, precisely

Three things, all in code we own. None is a tuning problem, and raising the gate width
only dilutes them.

1. **Interactive aggregations share the detector lane.** `db/queries.py` decorates
   `histogram`, `field_terms`, `field_numeric_stats` and every other chart aggregation
   with the same `_gated_scan` that `db/anomaly_stats.py` applies to fourteen whole-corpus
   detectors. One `threading.BoundedSemaphore(N)` in `db/_scan.py`. An Investigate sweep
   is 4 cheap plus 9 heavy requests fired in parallel (`useMethodFindings.ts`), each
   parked in the threadpool on `acquire()`; a histogram request joins the back of that
   queue. A 60-bucket `GROUP BY` needs a fraction of a detector's memory share and should
   never wait for one.
2. **A queued request can never tell the client anything.** Every `with
   HEAVY_SCAN_GATE:` is an unbounded blocking acquire, and `heavy_scan_settings()` sets
   no `max_execution_time`. The export gate got a bounded acquire in session 187
   (`_acquire_export_slot`); the main gate never did.
3. **A disconnect orphans the work and the reload doubles it.** Nothing outside
   `stream.py` checks `request.is_disconnected()`; threadpool work is not cancellable and
   ClickHouse keeps executing after the HTTP client is gone. The nine heavy scans from
   before the reload keep their slots while the reloaded page queues nine more (cache
   misses — the originals have not finished writing `analysis_cache`).

Not a defect any more: the ROADMAP entry's "the default histogram scans the corpus twice"
shipped in `6c69855` — `histogram` folds the min/max range into the bucket query. The
entry is rewritten to the admission split alone.

## Approaches considered

- **A. Two admission classes plus tagged cancellation** — chosen. A second, small
  semaphore for foreground aggregations with its own share of the budget; a scan-context
  contextvar that tags every query and lets a disconnect kill it. Reuses the two choke
  points that already exist (`heavy_scan_settings()` on every scan, `run_in_threadpool`
  on every endpoint).
- **B. A priority queue in front of one gate.** Charts jump the queue but still wait for
  a *running* heavy scan to finish — minutes on 700M rows. Does not fix the modal case.
- **C. Cancellation only.** Fixes the reload doubling; a chart still waits behind a full
  sweep. Half the bug.

## Design

### 1. `db/_scan.py` — the two classes and the memory split

- `HEAVY_SCAN_GATE` keeps `_GATE_CONCURRENCY = N` slots, frozen at import as today.
- `FOREGROUND_SCAN_GATE = threading.BoundedSemaphore(_FOREGROUND_CONCURRENCY)`,
  `_FOREGROUND_CONCURRENCY = 4`. A module constant, not a setting: the foreground share
  is sized as one heavy slot, and how finely that slot is sliced is not something an
  operator needs to reason about. (`settings_registry` coverage is unaffected because no
  `Settings` field is added.)
- **Divisor becomes `N + 1`.** Heavy per-query cap = `budget / (N + 1)`; the reclaimed
  slot is the foreground share, and each foreground query's cap is that share `/ 4`.
  Heavy running (`N × budget/(N+1)`) plus foreground running (`4 × budget/(N+1)/4`)
  equals `budget`: the "total holds" invariant of the module docstring is preserved.
  `_resolve_scan_memory_budget` gains the extra divisor at its one caller;
  `detect_scan_memory_budget()` keeps its name and meaning (the heavy cap).
- `foreground_scan_settings()` beside `heavy_scan_settings()`: the same clause shape
  (`max_threads`, external-aggregation/sort thresholds clamped to half the cap,
  `max_memory_usage`), with the foreground cap. Foreground `max_threads` is the heavy
  width — a chart is latency-bound and short; the gate width, not the thread width, is
  what bounds its CPU.
- `scan_budget_report()` gains a `foreground` block: `{"concurrency": 4, "per_query":
  bytes}`, and the existing `per_query`/`total` keep describing the heavy class. Rendered
  on the admin Settings page under the existing block.

### 2. `db/_scan.py` — scan context and cancellable admission

```python
@dataclass
class ScanContext:
    token: str                      # uuid4 hex
    cancelled: threading.Event

_scan_context: ContextVar[ScanContext | None]

class ScanBusy(Exception):     # wait expired; carries ahead: int
class ScanCancelled(Exception) # cancelled while queued or running

def acquire_scan_slot(gate, *, wait: float | None) -> ContextManager
```

- `acquire_scan_slot` loops on `gate.acquire(timeout=1.0)`, checking the active
  context's `cancelled` event between attempts; `wait=None` is unbounded (heavy),
  `wait=30.0` is the foreground bound. It raises `ScanCancelled` on cancel and
  `ScanBusy(ahead=…)` on expiry. Each gate carries a waiter counter so `ahead` is the
  number of callers parked before this one — the figure the client is shown.
- Both settings functions append `, log_comment = 'vestigo-scan/<token>'` when a
  context is active. `log_comment` is a query-level setting, so it rides the existing
  `SETTINGS` clause, appears in `system.processes.Settings`, and is the key `KILL QUERY
  WHERE Settings['log_comment'] = … ASYNC` kills by. Verified on the reference
  ClickHouse: the thread receives `QUERY_WAS_CANCELLED` (code 394).
- `_gated_scan` in `anomaly_stats.py` and the enrichment/Sigma/inventory holders switch
  from `with HEAVY_SCAN_GATE:` to `with acquire_scan_slot(HEAVY_SCAN_GATE, wait=None):`.
  Without a context (background jobs, CLI) the loop never sees a cancel and behaves
  exactly as the bare acquire did.

### 3. `db/queries.py` — which aggregations are foreground

Every `_gated_scan` in `EventQueryService` becomes `_foreground_scan`
(`acquire_scan_slot(FOREGROUND_SCAN_GATE, wait=30.0)`) and its `SETTINGS` clause becomes
`foreground_scan_settings()`: `histogram`, `field_terms`, `field_numeric_stats`,
`field_correlation`, `field_numeric_grouped`, `field_value_timeseries`,
`compare_time_histogram`, `compare_field_terms`, `compare_field_numeric`,
`time_punchcard`, `field_pivot`, `field_scatter`. The `_compare` internals and
`_field_terms_impl` run under the caller's slot as today.

`count_field_inventory` and `iter_field_inventory` stay heavy: a whole-corpus distinct
inventory is exactly the shape the heavy class exists for, and the export already holds
its own drain slot.

The agent's chart tool (`agent/chart_exec.py`) calls the same service methods, so it
inherits the foreground class. `ScanBusy` there surfaces as a tool error naming the
queue, which is the honest answer to give the model.

### 4. `api/scan_exec.py` (new) and the request contextvar

- A pure ASGI middleware (`api/request_context.py`) stores the current `Request` in a
  contextvar for HTTP scopes. Pure ASGI, not `BaseHTTPMiddleware`, so the endpoint runs
  in the same task and sees the value. Verified.
- `run_scan(fn, *args, **kwargs)`:
  1. Creates a `ScanContext`, sets the contextvar (it propagates into the threadpool —
     verified with `starlette.concurrency.run_in_threadpool`).
  2. Runs `fn` via `run_in_threadpool` as a task and, concurrently, polls
     `request.is_disconnected()` once a second while the task is pending. No request in
     context (tests calling the helper directly, jobs) means no polling.
  3. On disconnect: sets `cancelled`, runs `KILL QUERY WHERE Settings['log_comment'] =
     {token} ASYNC` in the threadpool, awaits the task (which ends with `ScanCancelled`
     from the acquire loop or `DatabaseError` 394 from ClickHouse), and raises
     `ScanCancelled`. The client is gone, so the response is irrelevant; what matters is
     that the slot and the ClickHouse process are released within about a second.
  4. Maps `ScanBusy` to `HTTPException(503, detail=…, headers={"Retry-After": "5"})`
     with the body `{"detail": "…", "queued_ahead": n}`.
- Wired at the three places every request-driven scan passes through:
  `events.py::_run_regex_guarded` (19 endpoints across `events.py` and `viz.py`),
  `events.py::_run_stat_detector` (all fourteen detector dispatches) and
  `analysis.py::_run_log_templates`. `get_analysis_findings` does not write
  `analysis_cache` on `ScanCancelled` — a killed scan has no answer to cache.

### 5. Frontend

`TimelineHistogram`, `FieldHistogramModal` and `ChartCanvas` treat a 503 whose body
carries `queued_ahead` as *busy, not failed*: the query keeps retrying with the
`Retry-After` delay, and the spinner is replaced by "waiting behind N scans". One helper
in `lib/queryClient.ts` (`busyRetry`) so the three surfaces cannot drift. `ApiError`
gains an optional `queuedAhead` parsed from the body. The sweep hook is unchanged —
detectors do not get a bounded wait (decided in the round: a long-queued but healthy
sweep is not an error state; its fix is cancellation).

### 6. Sizing calculator and docs

- `scripts/gen_sizing_constants.py` emits `foreground_concurrency`; `docs/sizing/
  index.html` divides by `concurrency + 1`, adds a "→ foreground per-chart cap" row and
  amends the per-query "why" text. `tests/test_sizing_constants.py` enforces parity.
- `docs/ANOMALY_DETECTION.md` scan-cost section: the two classes, the `N+1` divisor,
  cancellation. `docs/DEPLOYMENT.md` §Resource sizing: the same from the operator's side,
  and that `scan_budget.foreground` on `/api/health` is where to read the chart cap.
- `docs/ROADMAP.md` #300 entry deleted once shipped; `docs/PROGRESS.md` session entry.

## Error handling

| Situation | Behaviour |
|---|---|
| Foreground gate full for 30 s | 503, `Retry-After: 5`, `queued_ahead`; UI keeps waiting visibly |
| Heavy gate full | Queues, as today |
| Client disconnects while queued | Slot never taken; thread exits with `ScanCancelled` within 1 s |
| Client disconnects while running | `KILL QUERY … ASYNC`; thread exits with 394; slot released |
| No request in context (job, CLI, agent tool) | No polling, no cancel — identical to today |
| `KILL QUERY` itself fails | Logged at warning; the scan finishes on its own, as today |

## Testing

- `tests/test_scan_budget.py`: `N+1` divisor; foreground cap = heavy cap / 4; the
  invariant `N·heavy + 4·foreground ≤ budget`; `scan_budget_report()["foreground"]`.
- `tests/test_scan_admission.py` (new, pure): `acquire_scan_slot` busy after `wait`,
  `ahead` counts, cancel while queued releases nothing and raises, `log_comment` present
  only under a context.
- `tests/test_scan_exec_clickhouse.py` (new, real ClickHouse): a fake request that reports
  disconnected after 2 s around a `sleepEachRow` query — the row disappears from
  `system.processes` and the helper raises `ScanCancelled`.
- Endpoint tests: with `HEAVY_SCAN_GATE` held to exhaustion, `GET …/histogram` still
  returns 200; with `FOREGROUND_SCAN_GATE` held and the wait patched to 0.1 s, it returns
  503 with `queued_ahead`.
- `tests/test_demo_detector_coverage_clickhouse.py` and the viz/agent suites unchanged
  and green (the service methods keep their signatures).
- Frontend: `busyRetry` unit test; `TimelineHistogram` renders the waiting copy on a
  busy `ApiError`.

## Effect on the reporting host

8 cores, `N=4`, ~15 GiB budget: heavy scans go from 3.8 GiB to 3 GiB each; charts get
~0.77 GiB each in their own lane and never queue behind a sweep; a reload frees its
slots within a second instead of leaving nine ghosts holding them.
