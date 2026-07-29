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

A local-first, large-scale log investigation platform for forensic investigators.

Vestigo ingests Timesketch-compatible timelines.
Analysts can inspect them throug a web interface, which inherits established UX from ELK-Style interfaces.
It surfaces anomalies with explainable statistical detectors and/or Sigma rules.
The methods are documented, and explained right in the web interface for peer review.
The complete application writes a granular audit trail and can enforce offline deployment, for an optimal chain of custody preservation.

Two projects inspired the creation of this:
[logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner) is where the statistical
detection methods come from. Aminer is an online detection engine
for live log streams. We try to adapt the premise that a detector must be able to be reproducible, and we implement multiple
methods which are executed as batch SQL over an already-ingested corpus. Several of its
detectors we deliberately do not implement.

[Timesketch](https://github.com/google/timesketch) is the main inspiration.
Timesketch defined what collaborative timeline investigation should feel like, and the
Case/Timeline model here is descended from it. That is the comparison we invite, and on
three axes we think we are already the better place to run an investigation:

- **Detection is a first-class part of the investigation, not an add-on.** Fourteen
  analysis tools ship in the box, every one of them explainable down to the SQL it ran.
  Each temporal detector scores against an analyst-declared baseline window, and each
  finding carries a verdict that survives re-scans — so "why is this flagged?" always has
  an answer, and answering it once is not wasted work.
- **Provenance goes all the way down.** Not just "this file was imported": every single
  event carries a SHA-256 of its own raw content and the byte offset it came from, and
  parser and embedding configurations are hashed into the identity of what they produce.
  A finding is traceable to a byte range in an immutable, hashed original, months later.
- **One process, three services, no cluster.** No search cluster, no message broker, no
  worker fleet — the whole application is a single native process against PostgreSQL,
  ClickHouse and Qdrant, comfortable on 300M-row cases and 80 GiB+ timelines. Offline by
  default, so it runs in an airgapped lab without special handling.

We are not finished, and Timesketch is a mature project with years of production use, a
larger analyzer ecosystem and a community we have yet to earn.

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
