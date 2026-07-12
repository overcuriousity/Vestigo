# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
