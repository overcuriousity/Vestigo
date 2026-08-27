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

**A local-first, large-scale log investigation platform for forensic investigators.**

Ingest Timesketch-compatible timelines and explore them through a web interface that
inherits its UX from ELK-style tools. Surface anomalies with explainable statistical
detectors and Sigma rules — every method documented, and explained in the interface itself
for peer review. Every mutating action is audit-trailed and the whole application can be
pinned offline, so chain of custody survives the investigation.

<p align="center">
  <img width="2866" height="1589" alt="Vestigo Explorer" src="https://github.com/user-attachments/assets/d505af86-9ba2-4fe1-b448-10b18ae2d409">
</p>

## Quick start

Run the three backing services (natively, or via the reference compose file — it binds to
`127.0.0.1` only), then install and start the app:

```bash
docker compose up -d      # or: podman compose up -d
uv sync
uv run vestigo-web
```

The app is at `http://localhost:8080`, OpenAPI docs at `/api/docs`; the frontend is
auto-built on first run. Log in with the one-time bootstrap admin credentials
(`VESTIGO_ADMIN_PASSWORD`, rotated on first login). Configuration is env-driven
(`VESTIGO_*`) — see `.env.example`.

Local embeddings are not in the base install (~2 GB of torch + sentence-transformers). Add
them with `uv sync --extra embeddings`, or point `VESTIGO_EMBEDDING_API_BASE_URL` at a
remote OpenAI-compatible endpoint. Without either, embedding features report unavailable
and everything else works normally.

For production hardening, containerized deployment, airgapped installation, TLS and upgrade
guarantees, see [Deployment](docs/DEPLOYMENT.md).

**Sizing a box first?** The [sizing calculator](https://overcuriousity.github.io/Vestigo/sizing/)
turns an expected dataset size and analyst count into recommended RAM, cores and `VESTIGO_*`
values. It is a starting point — `/api/health`'s `scan_budget` block reports what actually
resolved on the machine you deploy to.

## Capabilities

- **Ingestion at scale** — streaming parsers for Plaso and generic CSV/JSONL take tens of
  gigabytes without loading them into memory, via the UI or `vestigo ingest` (no upload
  cap). Downloadable converters normalize vendor logs — nginx, suricata, cloudtrail, evtx,
  zeek and more — client-side into typed Parquet, bulk-inserted via Arrow with per-row
  provenance. [Input Formats →](docs/INPUT_FORMATS.md)
- **Explorer** — virtualized event grid, full-text and structured filters pushed down into
  ClickHouse, time histogram with anomaly overlays, keyset pagination with jump-to-time,
  tag/comment annotations with bulk apply, saved views, and streaming CSV/JSONL export that
  keeps the forensic columns.
- **Anomaly detection** — fourteen analysis tools: twelve statistical detectors over
  ClickHouse needing no embeddings, a Sigma rule runner, and semantic similarity search
  over local embeddings. Each is documented method by method, scores against explicit
  baseline-vs-suspect windows, and yields findings whose confirm/dismiss disposition
  survives re-scans. [Anomaly Detection →](docs/ANOMALY_DETECTION.md)
- **Stories** — the write-up lives next to the evidence. View, chart and event blocks stay
  live while the analyst writes, then freeze to a hashed, server-resolved snapshot on
  export. [Stories →](docs/STORIES.md)
- **AI investigation agent** (optional, off by default) — searches, aggregates and runs
  detectors through read-only case-scoped tools, handing back findings the analyst applies
  with one click; writes need an explicit propose→confirm. Any OpenAI- or
  Anthropic-compatible endpoint works, including local ones (ollama, vllm, llama.cpp).
  [Agent & MCP →](docs/AGENT.md)
- **Teams, access control, audit** — session-cookie auth with optional OIDC SSO, case-level
  RBAC with teams, an append-only audit trail over every mutating action, and live
  collaboration over Server-Sent Events.
- **Enrichment** — post-ingest enrichers (GeoIP and ASN via local MaxMind databases)
  amend event attributes without touching the provenance columns.
- **Forensic rigor by construction** — sources are SHA-256 hashed, immutable and retained
  content-addressed; every event carries a content hash and byte offset back into its raw
  file; parser and embedding configs are hashed into the identity of what they produce. No
  code path reaches the network unconditionally.

## Architecture

- **Backend** — Python 3.13+, FastAPI/Uvicorn, managed with `uv`. Talks to three external
  services: PostgreSQL (metadata), ClickHouse (events, the primary log store), and Qdrant
  (vectors). None run inside the app.
- **Frontend** — React 19 + Vite + TypeScript, served as a static build directly from
  Uvicorn.
- **CLI** — a Typer-based `vestigo` command mirrors the API/UI for scriptable, offline use.

## How it compares

[Timesketch](https://github.com/google/timesketch) is the main inspiration and the tool
Vestigo shares a category with. It defined what collaborative timeline investigation should
feel like, and the Case/Timeline model here is descended from it. That is the comparison we
invite, and three axes are where we think we are already the better place to run an
investigation:

- **Detection is the workflow, not an add-on** — fourteen analysis tools in the box, each
  scoring against an analyst-declared baseline and carrying a verdict that survives
  re-scans, so triage accumulates instead of being redone.
- **Provenance goes all the way down** — not just "this file was imported": a finding is
  traceable to a byte range in an immutable, hashed original, months later.
- **One process, three services, no cluster** — no search cluster, broker or worker fleet,
  comfortable on 300M-row cases, and offline by default rather than as a hardening
  exercise.

Timesketch is also a mature project with years of production use, a larger analyzer
ecosystem and a community we have yet to earn.

[logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner) is a **method
source, not a competitor.** Its catalogue of detection methods, and the principle that a
detector must explain itself, are where ours come from — re-derived as batch SQL over an
already-ingested corpus, deliberately narrower, with several of its detectors not
implemented at all. AMiner solves a different problem: online detection over live log
streams. Anyone who needs that should run AMiner.

The full comparison, including what we hold narrower on purpose, is in
[Concept §8](docs/CONCEPT.md#8-differentiation).

## Documentation

**Running it**

- [Deployment](docs/DEPLOYMENT.md) — configuration, sizing, compose stack, airgapped install, TLS, upgrades
- [Sizing calculator](https://overcuriousity.github.io/Vestigo/sizing/) — hardware and `VESTIGO_*` values from your expected dataset size
- [Input Formats](docs/INPUT_FORMATS.md) — CSV/JSONL/Parquet field-level normalization spec

**Using it**

- [Anomaly Detection](docs/ANOMALY_DETECTION.md) — every detector explained, plain language
- [Stories](docs/STORIES.md) — the living report: blocks, collaboration, hashed export
- [AI Agent](docs/AGENT.md) — the optional investigation agent and the external MCP endpoint

**Why it is built this way**

- [Concept](docs/CONCEPT.md) — vision, target user, data model summary
- [Model Refinement](docs/MODEL_REFINEMENT.md) — the Case / Source / Timeline / Event / Artifact model
- [Tech Stack](docs/TECH_STACK.md) — why each backing service was chosen
- [Roadmap](docs/ROADMAP.md) — the open backlog · [Changelog](CHANGELOG.md)

## License

GPL-3.0 — see [LICENSE](LICENSE).
