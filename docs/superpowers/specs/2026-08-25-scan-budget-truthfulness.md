# Scan-budget truthfulness — design round (issues #301, #302, #303)

**Status:** accepted, 2026-08-25. Implemented by
`docs/superpowers/plans/2026-08-25-scan-budget-truthfulness.md`.

Batch A of the four scan-budget issues opened against 1.15.0. Issue #300 (splitting the
admission gate so foreground histograms do not queue behind detector sweeps) is
deliberately **not** in this round: it changes query behaviour on the hot path and needs
its own regression evidence. This round is about the numbers the app reports about
itself, and it lands first because it is also the diagnosis surface #300 will be
debugged against.

## What the issues asked for, and where they were wrong

Both #301 and #302 were written from the repo, not from a running server. Probing
ClickHouse 26.6.1 (the version the reference stack pins) corrected two premises. The
corrections are the reason this spec exists rather than the issues being implemented
as filed.

### #301 — "`system.asynchronous_metrics` exposes CPU counts"

It does not. On 26.6.1 the table carries `CGroupMemoryTotal` and `OSMemoryTotal` (which
is why the *memory* probe reads it) but no core count: no `CGroupMaxCPU`, no
`OSNProcessors`, no `NumberOfPhysicalCPUCores`. The per-core series
(`OSUserTimeCPU0..N`) counts *host* cores and is not cgroup-aware, so counting its rows
would reproduce the bug on a CPU-limited container.

The authoritative reading is `system.settings`:

```
SELECT value FROM system.settings WHERE name = 'max_threads'   -- 'auto(12)'
```

`auto(N)` is ClickHouse's own resolved core count, and it **is** cgroup-quota aware —
verified directly: the same image under `--cpus=2` reports `'auto(2)'`. That is a
strictly better source than the one the issue proposed, because it is the number
ClickHouse would pick for itself on this host.

An operator who pins `max_threads` server-side gets a plain integer instead of
`auto(N)`; that is equally usable — it is still what the server resolves to.

### #302 — the cache accounting is worse than the issue's table

The issue counted `mark_cache_size` (2 GiB) against the 9.5 GiB ceiling and found the
reference stack 0.1 GiB over. The real figure at 26.6.1 defaults:

| cache | bytes | GiB |
| --- | --- | --- |
| `mark_cache_size` (pinned by `memory.xml`) | 2147483648 | 2.0 |
| `index_mark_cache_size` (**default, not pinned**) | 5368709120 | 5.0 |
| `primary_index_cache_size` (**default, not pinned**) | 5368709120 | 5.0 |
| `uncompressed_cache_size` (pinned to 0) | 0 | 0 |
| `index_uncompressed_cache_size` (default 0) | 0 | 0 |
| **sum** | **12884901888** | **12.0** |

Twelve GiB of cache maxima under a 9.5 GiB ceiling, before a single scan is admitted.
`memory.xml` pins the two caches it names and leaves the two largest at their defaults.
The server ships many more cache settings (`text_index_*`, `vector_similarity_*`,
`parquet_metadata_*`, `iceberg_*`, `unique_key_*`); those are for table engines and
index types Vestigo does not use, and summing them would produce a permanently alarmist
`over_budget`. The five above are the ones a MergeTree scan workload actually fills.

## Decisions

### D1 — the ratio applies to the ceiling *after* caches

`stat_scan_memory_ratio`'s own help text already claims the remainder is "headroom for
what a per-query cap cannot bound: background merges, the mark and uncompressed caches".
That was never true: the ratio was taken of the whole ceiling and the caches were
counted nowhere. Rather than lower the default ratio (which silently shrinks every
existing deployment's budget), subtract the caches first:

```
budget_total = ratio x max(ceiling - cache_bytes, 0)
```

This makes the existing sentence true, leaves the ratio's meaning intact, and is a
no-op wherever the probe has not run (CLI, pre-probe fallback), where `cache_bytes` is
zero. It is also what makes the reference stack report `ok` honestly once D3 lands.

### D2 — `risk` counts caches, and shows its arithmetic

`over_budget` becomes `total_scans + cache_bytes > ceiling`. The report gains
`cache_bytes` and a `cache_breakdown` mapping so the comparison is inspectable rather
than implied — the same reason `budget_ceiling_bytes` is already reported separately
from `clickhouse_ceiling_bytes`.

Merge headroom stays *documented*, not reserved as a fraction. A fraction would be a
second guess stacked on the first, and the remainder after D1 already is the merge
headroom — naming it in `DEPLOYMENT.md` is what the issue's fallback position asked for.

### D3 — `memory.xml` pins the two caches it currently misses

`index_mark_cache_size` 512 MiB, `primary_index_cache_size` 1 GiB, keeping
`mark_cache_size` at 2 GiB. Caches then total 3.5 GiB; the auto budget is
0.8 x (9.5 - 3.5) = 4.8 GiB total, 2.4 GiB per query at concurrency 2, leaving ~1.2 GiB
under the ceiling for merges and allocator slack. The reference stack fits under its own
ceiling for the first time.

### D4 — thread width derives from cores, divided by the gate

`stat_scan_max_threads` default becomes `0` = auto, matching how
`stat_scan_max_memory_bytes` already spells "auto". Auto resolves to

```
max(2, resolved_cores // _GATE_CONCURRENCY)
```

capped at `resolved_cores`. The gate admits `_GATE_CONCURRENCY` scans at once, so an
even share is the width at which a full gate exactly saturates the box rather than
oversubscribing it: 20 cores / 2 = 10 (against today's 8), 4 cores / 2 = 2 (against
today's 8, i.e. 4x oversubscription). Detection failure falls back to the current
constant 8 — same posture as the memory probe, which never refuses to start.

An explicit `VESTIGO_STAT_SCAN_MAX_THREADS` still wins, unchanged.

### D5 — #303's root cause is a missing `cp`, not a path ambiguity

The issue diagnosed "two paths for one file". The actual defect is narrower and total:
`scripts/airgap-bundle.sh:140` copies `deploy/clickhouse/memory.xml` into the bundle,
and `deploy/airgap/install.sh` copies `clickhouse/allow-default-network.xml` into the
install directory **and never copies `clickhouse/memory.xml`**. The compose file's
`./clickhouse/memory.xml` mount source therefore never exists, Docker materialises an
empty directory at the target, and ClickHouse merges no ceiling — exactly the reported
production state. Every airgap install since the file shipped has run unbounded.

Fixes: copy the file; assert it is a regular file before `compose up` and fail loudly if
it is missing or a directory; cover both in `--check`. A parity test asserts that every
file the bundle script stages into `clickhouse/` is one the installer copies, so the
next file added cannot repeat this.

### D6 — `risk` becomes visible without reading logs

A `ScanBudgetCard` above the "scans" group on the admin settings page. That is where an
operator is already standing when they change the numbers the card describes, and it
needs no new route or nav entry. It renders for `over_budget`/`unbounded` as a warning
and for `ok` as a quiet one-line summary.

## Out of scope

- Issue #300 (gate class separation, single-pass histogram) — next round.
- `finalize_enrichment_apply` needing more than `budget / concurrency`.
- The `background_pool_size` `config.d` rejection on 26.6 — not a Vestigo defect; a
  sizing-docs note at most.
