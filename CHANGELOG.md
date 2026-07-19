# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] — 2026-07-19

### Changed

- **Dependency roundup** — all 20 open Dependabot PRs merged and lockfiles fully
  refreshed. Backend: fastapi 0.139.2, clickhouse-connect 1.5.0, typer 0.27.0,
  geoip2 5.3.0, ruff 0.15.22, plus all transitive updates via `uv lock --upgrade`.
  Frontend: vite 8.1.5, tailwindcss 4.3.3, oxlint 1.74.0, @types/node 26,
  Radix UI patch releases, @tanstack/react-virtual 3.14.6, lucide-react 1.25.0,
  @fontsource/inter + jetbrains-mono 5.3.0. CI: docker/* actions and
  actions/setup-node major bumps. Full backend + frontend suites green on the
  upgraded set.
- Frontend `package.json` version now tracks the app version (was stale at 1.1.2).

## [1.2.0] — 2026-07-19

### Added

- **Sigma rule runner** (`docs/ANOMALY_DETECTION.md` §13) — deterministic signature
  matching of community-standard [Sigma](https://github.com/SigmaHQ/sigma) YAML rules
  over ClickHouse, deliberately separate from the statistical detectors. Rules come
  from an admin-managed offline directory (`VESTIGO_SIGMA_RULES_PATH`, a file drop —
  no restart needed, unchanged files reuse a per-file parse cache) and per-case
  uploads. Every hit is written as `Annotation(origin=system, annotation_type="sigma")`
  whose `sigma: <rule title>` label joins the unified tag filter panel.
- **Custom pySigma → ClickHouse backend**: one boolean SQL expression per rule.
  Sigma-spec case-insensitive matching (`ILIKE` with `*`/`?` wildcards), `|cased`,
  `|re` (RE2), `|cidr` (guarded `isIPAddressInRange`), numeric comparisons, null/missing
  semantics, field-less keywords over `search_blob`. Field names resolve through
  ruleset `vestigo-fieldmap.yml` → timeline canonical mappings → raw-attribute
  fallback (tracked and flagged in the UI). All values pass through an audited,
  adversarially-tested literal-quoting boundary.
- **Streamed, reproducible runs**: background job per timeline; per rule, hits stream
  under the shared heavy-scan gate through a bounded queue (no hit cap, no in-memory
  hit list) into batched annotation writes; re-runs are idempotent per rule and
  preserve confirmed findings. Persistent `sigma_runs` records (Alembic `0006`)
  snapshot each rule's YAML content hash, exact compiled SQL, match count, and status.
- **Sigma tab** in the Investigate panel: rule picker with level/logsource badges,
  YAML upload, run launch into the job tray, run history with per-rule status,
  compiled-SQL view, fallback-field warnings, and filter-grid-by-rule.
- Config: `VESTIGO_SIGMA_RULES_PATH`, `VESTIGO_SIGMA_ANNOTATION_BATCH_SIZE`.
  Deps: `pysigma`, explicit `pyyaml` (offline — no Sigma code path touches the network).

## [1.1.0] — 2026-07-13

### Added

- **Repeating-sequence (motif) mining** — new `sequence_motif` detector
  (`docs/ANOMALY_DETECTION.md` §12): per source, time-ordered n-grams of one field's
  values that *recur* are ranked by support × cadence regularity (median gap, CV,
  Greenwood spacing test). Mode-less — needs no baseline, runs right after ingestion;
  optional `start`/`end` scope. Tunables: `VESTIGO_STAT_MOTIF_MIN_SUPPORT`,
  `VESTIGO_STAT_MOTIF_MAX_CANDIDATES`, `VESTIGO_STAT_MOTIF_CADENCE_TOP_K`.
- **Routine suppression** — new disposition `kind="routine"`: a motif marked routine has
  its occurrences materialized (ClickHouse `motif_occurrences` table, auto-created) so the
  event grid, histogram, and export can collapse them via `collapse_routine`. The response
  always reports `routine_collapsed_count` — collapse is explicit, never silent. Routine is
  presentation-only: detectors keep scoring and it never enters the reproducibility hash.
- **Patterns tab** in the Investigate panel: motif list with support, period, regularity
  bar and per-source cadence; Mark routine / unmark; Explorer collapse toggle with an
  always-visible collapsed-count banner.
- **Unified findings feed** — the Anomalies tab now opens with one cross-detector ranked
  inbox (per-detector rank interleave, raw score with its unit per row, detector chips as
  filters), built from the detector sweep the count badges already paid for.

### Changed

- The 11 per-detector views moved under a collapsed **Advanced** expander, grouped
  Values / Volume & timing / Sequences. The dense baseline/suspect-window builder moved
  from the inline flow into an overlay drawer (FrameBar → *Manage baselines*; histogram
  mark-mode opens it automatically).

## [1.0.0] — 2026-07-12

First stable release. Everything below is new in 1.0.0.

### Renamed

The project was renamed **TraceSignal → Vestigo** ahead of 1.0 (*vestigo*, Latin:
"I follow the tracks"). For anyone upgrading a pre-release deployment:

- CLI entry points: `tsig` → `vestigo`, `tsig-web` → `vestigo-web`.
- Environment variables: `TS_*` → `VESTIGO_*` (e.g. `TS_POSTGRES_URL` →
  `VESTIGO_POSTGRES_URL`).
- Default backing-store names changed to `vestigo` (PostgreSQL database/user, ClickHouse
  database, Qdrant collection prefix). Existing deployments keep their data by pinning the
  old names via `VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_DATABASE`, and
  `VESTIGO_QDRANT_COLLECTION_PREFIX`.
- Converter scripts: `*2tracesignal.py` → `*2vestigo.py`. Parquet footer metadata keys
  moved from `tracesignal.*` to `vestigo.*`; the server still reads files produced by
  pre-rename converters.

### Ingestion

- Streaming parsers for Plaso CSV/JSONL and generic Timesketch-compatible CSV/JSONL —
  constant-memory, tens-of-GB capable, with per-record byte offsets and content hashes.
- Every ingested file (Source) is SHA-256 hashed and retained content-addressed.
- Vestigo Parquet interchange format v1: downloadable client-side converter scripts
  (nginx, filterlog, suricata, cloudtrail, pcap — plus vendored stdlib-only Timesketch
  converters for apache, browser, cowrie, evtx, journal, syslog) emit typed columnar
  Parquet that the server bulk-inserts via Arrow record batches, with forensic provenance
  anchored to the original raw evidence file.
- CLI ingestion (`vestigo ingest`) streams straight from disk with progress/ETA and
  per-user attribution; upload size cap (`VESTIGO_MAX_UPLOAD_BYTES`) with mid-stream 413.
- Optional per-source enrichers with recorded provenance, force re-run recovery, and
  upgrade guards.

### Explorer

- Virtualized ELK-like event grid over ClickHouse: resizable/pickable columns, density
  modes, light/dark themes, keyset pagination.
- Full filter model (field, value, time range, tags, annotations), saved Views per
  timeline, indexed full-text search, time histogram with brush zoom and event markers.
- Context query around any event; per-source clock-skew correction; column stats and
  field inventory backed by a per-source field-stats cache.

### Anomaly detection

- Statistical detectors run directly against ClickHouse, all SQL-explainable, each with
  self-baseline and temporal (baseline/suspect window) modes where applicable:
  value novelty, frequency (z-score spikes/silences), value combinations,
  timestamp order, charset, numeric range, entropy, interval periodicity
  (cadence breaks + beaconing), sequence novelty (n-grams), proportion shift
  (G-test with BH-FDR), and value distribution drift (KS / G-test).
- Embedding pipeline: user-triggered jobs embed events into Qdrant (local models,
  offline-capable); semantic search and nearest-neighbor similarity; embedding wizard
  with content-aware field recommendation.
- Triage workflow: unified disposition taxonomy, dismissals, Investigate panel bundling
  detectors with shared baseline configuration.

### Visualization

- Visualize page: time histogram, comparison histogram, punch card, pivot, Sankey and
  scatter charts, click-to-filter, saved charts — with scan guardrails at 300M-row scale.

### Platform

- Session-cookie auth with optional OIDC SSO, case-level RBAC, teams, audit trail.
- Alembic-managed PostgreSQL schema with automatic migration on startup (pre-Alembic
  databases are auto-adopted).
- Airgapped/offline by default (`VESTIGO_ALLOW_ONLINE` gates all network paths except
  the deliberately independent OIDC).
- Typer CLI mirroring the API for scriptable/offline use; reference `docker-compose.yml`
  for the three backing services (PostgreSQL, ClickHouse, Qdrant).
- Container images published to `ghcr.io/overcuriousity/vestigo`.

[1.0.0]: https://github.com/overcuriousity/Vestigo/releases/tag/v1.0.0
