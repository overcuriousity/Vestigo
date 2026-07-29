<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo.svg" alt="Vestigo" width="320">
  </picture>
</p>

<p align="center"><em>vestigo</em> (Latin) — <em>I follow the tracks; I investigate.</em></p>

<p align="center">
  <a href="https://github.com/overcuriousity/Vestigo/actions/workflows/ci.yml"><img src="https://github.com/overcuriousity/Vestigo/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/overcuriousity/Vestigo/actions/workflows/codeql.yml"><img src="https://github.com/overcuriousity/Vestigo/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/overcuriousity/Vestigo/releases/latest"><img src="https://img.shields.io/github/v/release/overcuriousity/Vestigo?logo=github" alt="Latest release"></a>
  <a href="https://github.com/overcuriousity/Vestigo/pkgs/container/vestigo"><img src="https://img.shields.io/badge/container-ghcr.io-blue?logo=docker&logoColor=white" alt="Container image"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/overcuriousity/Vestigo" alt="License: GPL-3.0"></a>
  <img src="https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
  <img src="https://img.shields.io/badge/react-19-61DAFB?logo=react&logoColor=black" alt="React 19">
</p>

A local-first, forensic-grade log investigation platform for security teams.

Vestigo ingests Timesketch-compatible timelines at scale, lets analysts explore them
through an ELK-like web interface, and surfaces anomalies with explainable statistical
detectors, Sigma rules, and locally-computed embeddings — reproducible and auditable at
every step, airgapped if needed. It sits between a heavyweight SIEM and one-off notebook
scripts: [Timesketch](https://github.com/google/timesketch)'s investigative UX combined
with [logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner)'s
lightweight, explainable detection, without needing a cluster to run it.

<img width="2866" height="1589" alt="Vestigo Explorer" src="https://github.com/user-attachments/assets/d505af86-9ba2-4fe1-b448-10b18ae2d409" />

## Capabilities

- **Ingestion at scale** — streaming parsers for Plaso CSV/JSONL and generic CSV/JSONL
  handle tens of gigabytes without loading everything into memory; also scriptable via
  `vestigo ingest` for huge files (no upload cap, live progress). Downloadable converter
  scripts parse vendor logs (nginx, suricata, cloudtrail, pcap, evtx, apache, cowrie,
  zeek, and more) client-side; the native converters emit typed Parquet that the server
  bulk-inserts via Arrow — an order of magnitude faster than row-by-row CSV, with
  per-row raw-file provenance — and stdlib-only Timesketch-format variants exist for
  every format.
- **Explorer** — virtualized event grid with full-text and structured filtering pushed
  down into ClickHouse, time histogram with anomaly overlays, keyset pagination with
  jump-to-time, tag/comment annotations with bulk apply, saved views, and streaming
  CSV/JSONL export that includes the forensic columns.
- **Anomaly detection** — twelve statistical detectors run directly over ClickHouse (no
  embeddings required): value novelty, value combinations, frequency spikes/silences,
  timestamp order, numeric range, charset novelty, entropy outliers, proportion shift,
  interval cadence, sequence novelty, recurring-sequence motifs, and value-distribution
  drift — plus log-template clustering, a Sigma rule runner, and semantic similarity
  search over locally-computed embeddings (Qdrant). Every detector supports explicit
  baseline-vs-suspect windows, and findings carry a confirm/dismiss disposition workflow
  that survives re-scans. See [Anomaly Detection](docs/ANOMALY_DETECTION.md).
- **Enrichment** — post-ingest enrichers (currently GeoIP via a local MaxMind database)
  amend event attributes without ever touching the provenance columns.
- **AI investigation agent (optional, off by default)** — an assistant embedded in the
  Explorer that searches, aggregates, and runs detectors through read-only, case-scoped
  tools, handing results back as findings the analyst applies with one click; writes
  happen only through an explicit propose→confirm flow. Works with any OpenAI- or
  Anthropic-compatible endpoint, including fully local ones (ollama, vllm, llama.cpp).
  The same tools are exposable as an audited [MCP endpoint](docs/AGENT.md) for external
  agents. Every conversation and tool call is persisted and audit-trailed.
- **Teams, access control, audit** — session-cookie auth (optional OIDC SSO), case-level
  RBAC with teams, an append-only audit trail over every mutating action, and live
  collaboration via Server-Sent Events.
- **Forensic rigor by construction** — every ingested file is SHA-256 hashed, immutable,
  and retained content-addressed; every event carries a content hash and byte offset back
  into the raw file; parser and embedding configs are hashed into the identity of what
  they produce. Airgapped/offline-by-default: no code path reaches the network
  unconditionally.

## Architecture

- **Backend** — Python 3.13+, FastAPI/Uvicorn, managed with `uv`. Talks to three external
  services: PostgreSQL (metadata), ClickHouse (events, the primary log store), and Qdrant
  (vectors). None run inside the app.
- **Frontend** — React 19 + Vite + TypeScript, served as a static build directly from
  Uvicorn.
- **CLI** — a Typer-based `vestigo` command mirrors the API/UI for scriptable, offline
  use.

## Quick start

Run the three backing services (natively, or via the reference compose file — it binds
to `127.0.0.1` only), then install and start the app:

```bash
docker compose up -d      # or: podman compose up -d
uv sync
uv run vestigo-web
```

The app is at `http://localhost:8080` (OpenAPI docs at `/api/docs`); the frontend is
auto-built on first run. Log in with the one-time bootstrap admin credentials
(`VESTIGO_ADMIN_PASSWORD`, rotated on first login).

The base install ships without the local embedding stack (~2 GB of torch +
sentence-transformers). For local embeddings run `uv sync --extra embeddings`, or point
`VESTIGO_EMBEDDING_API_BASE_URL` at a remote OpenAI-compatible endpoint. Without either,
embedding features report unavailable and everything else works normally.

Configuration is env-driven (`VESTIGO_*`); see `.env.example` for the full list. For
production hardening, containerized deployment, fully airgapped installation, TLS, and
upgrade guarantees, see [Deployment](docs/DEPLOYMENT.md).

## Documentation

- [Concept](docs/CONCEPT.md) — vision, target user, data model summary
- [Deployment](docs/DEPLOYMENT.md) — compose stack, airgapped install, TLS, upgrades
- [Input Formats](docs/INPUT_FORMATS.md) — CSV/JSONL/Parquet field-level normalization spec
- [Anomaly Detection](docs/ANOMALY_DETECTION.md) — every detector explained, plain language
- [AI Agent](docs/AGENT.md) — the optional investigation agent and the external MCP endpoint
- [Tech Stack](docs/TECH_STACK.md) — why each backing service was chosen
- [Model Refinement](docs/MODEL_REFINEMENT.md) — the Case / Source / Timeline / Event / Artifact model
- [Roadmap](docs/ROADMAP.md) — the open backlog, prioritized
- [Changelog](CHANGELOG.md)

## License

GPL-3.0 — see [LICENSE](LICENSE).
