# TraceVector

A local-first, forensic-grade log investigation platform for small security teams.

TraceVector ingests Timesketch-compatible timelines at scale, lets analysts explore events through an ELK-like web interface, and detects anomalies by embedding every log line into a vector database.

## Quick start

TraceVector is a native Python application. It connects to external services for metadata (PostgreSQL), events (ClickHouse), and vectors (Qdrant).

### 1. Provide backing services

You can run them natively or use the optional reference Docker Compose:

```bash
docker compose up -d
```

### 2. Install and run the application

```bash
uv sync
uv run tv-web
```

The API is available at `http://localhost:8080` (OpenAPI docs at `/api/docs`), serving the
built frontend from `frontend/dist` (auto-built on first run).

For active frontend development, run `npm install && npm run dev` in `frontend/` alongside
`uv run tv-web` — see `frontend/README.md`.

## Documentation

- [Concept](docs/CONCEPT.md)
- [Tech Stack](docs/TECH_STACK.md)
- [Model Refinement](docs/MODEL_REFINEMENT.md) — approved Case / Source / Timeline / Artifact redesign
- [Roadmap](docs/ROADMAP.md)
- [Progress](docs/PROGRESS.md)

## License

MIT — see [LICENSE](LICENSE).
