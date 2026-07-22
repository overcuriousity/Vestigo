# Deployment

How to run Vestigo beyond a laptop evaluation: the reference compose stack, the
containerized app image, fully airgapped installation, TLS termination, and what the
1.x line guarantees across upgrades.

The application itself is a native Python app (`uv run vestigo-web`) talking to three
**external** backing services — PostgreSQL (metadata), ClickHouse (events), Qdrant
(vectors). Provide those however you prefer: official images, native packages, or
existing infrastructure. Vestigo only needs connection strings (`VESTIGO_*` env vars,
see `.env.example` and `src/vestigo/core/config.py`).

## Reference compose stack

`docker-compose.yml` starts the three backing services for local/dev use:

```bash
docker compose up -d   # or: podman compose up -d
```

It publishes all three services on `127.0.0.1` only — they run with default or no
credentials, so they are deliberately unreachable from the LAN. The app's defaults
(`.env.example`) connect via these localhost ports.

**This compose file is a reference/evaluation deployment, not a production hardening
guide.** It ships with fixed, well-known defaults so it works out of the box:
`postgres`/`vestigo` DB credentials, no ClickHouse/Qdrant auth, and a one-time
`VESTIGO_ADMIN_PASSWORD` bootstrap secret (forced to rotate on first login). For any
deployment reachable by more than you, prefer the native `uv run vestigo-web` install
against properly credentialed, network-restricted backing services, and set your own
`VESTIGO_ADMIN_PASSWORD` / `VESTIGO_*_PASSWORD` / `VESTIGO_QDRANT_API_KEY` values
rather than the compose defaults.

## Containerized app (optional)

Released application images are published to GitHub Container Registry:

```bash
docker pull ghcr.io/overcuriousity/vestigo:latest
```

`docker-compose.yml` ships with a **commented-out** `app` service that builds the
image from the local checkout (`Dockerfile`) and reaches the backing services over the
compose-internal network. Uncomment it, then `docker compose up -d` brings up the full
stack in one command.

## Airgapped installation

Vestigo's application layer (backend + frontend) can be installed fully offline.
**The three backing services are out of scope for this procedure**: provision them on
the airgapped network however you normally handle offline service deployment (e.g.
`podman load` of pre-pulled images, or native packages).

On a machine **with internet access**:

1. Clone or copy the repository.
2. Install and build everything, so all dependencies are resolved and cached locally:
   ```bash
   uv sync --extra embeddings
   cd frontend && npm install && npm run build && cd ..
   ```
   This populates `.venv/` (all Python dependencies, including the CPU PyTorch wheels
   for local embeddings — drop `--extra embeddings` if the deployment won't embed
   locally) and `frontend/dist/` (the built static frontend).
3. Copy the whole repository — including `.venv/`, `uv.lock`, and `frontend/dist/` —
   to a portable drive.

On the **airgapped machine**:

1. Copy the repository from the portable drive.
2. Point `VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_URL`, and `VESTIGO_QDRANT_URL`
   (in `.env`, copied from `.env.example`) at the already-running backing services on
   the isolated network.
3. Run the app directly from the carried-over virtualenv — no `uv sync` or
   `npm install` needed, since both were already resolved on the online machine:
   ```bash
   .venv/bin/vestigo-web
   ```
   Because `frontend/dist/` was carried over and the app is started via the `.venv`
   entry point directly (not `uv run`, which would try to re-resolve the environment),
   no network access is required at any point on the airgapped machine.
   `VESTIGO_ALLOW_ONLINE=false` (the default) additionally keeps the embedding
   pipeline from reaching any remote endpoint.
4. Same binary compatibility requirements apply as any offline Python deployment:
   build and run on matching OS/architecture (e.g. build on the same Linux
   distribution/glibc version you'll run on), since the `.venv/` carries compiled
   wheels (PyTorch, onnxruntime, etc.).

## TLS reverse proxy (nginx)

Vestigo (`vestigo-web`) listens on plain HTTP, `0.0.0.0:8080`
(`src/vestigo/web/app.py`). It has no TLS support of its own — put nginx in front of
it to terminate HTTPS for LAN/production use. Config: `docs/nginx-tls.conf`.

Certbot/Let's Encrypt is out of scope here (this host is airgapped/LAN-only per
`docs/TECH_STACK.md` §6). Use a self-signed cert instead.

### 1. Generate a self-signed certificate

```bash
sudo mkdir -p /etc/nginx/tls
sudo openssl req -x509 -nodes -newkey rsa:4096 \
  -keyout /etc/nginx/tls/vestigo.key \
  -out    /etc/nginx/tls/vestigo.crt \
  -days 825 \
  -subj "/CN=vestigo.example.internal" \
  -addext "subjectAltName=DNS:vestigo.example.internal,IP:192.168.18.125"
sudo chmod 600 /etc/nginx/tls/vestigo.key
```

Adjust `-subj`/`subjectAltName` to your actual hostname/IP — browsers and most HTTP
clients enforce SAN matching, a bare CN is not enough anymore. Analysts' browsers will
show a self-signed warning on first visit (expected, click through / pin the cert);
there's no CA issuing it.

### 2. Install the nginx config

```bash
sudo cp docs/nginx-tls.conf /etc/nginx/sites-available/vestigo.conf
sudo ln -s /etc/nginx/sites-available/vestigo.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Edit `server_name` and the cert paths in the copied file to match your environment
first.

### 3. Required Vestigo-side settings

Once TLS terminates at nginx, the app itself still thinks it's plain HTTP — two
settings need to change (`VESTIGO_*` env vars, see `src/vestigo/core/config.py`):

- `VESTIGO_AUTH_COOKIE_SECURE=1` — the session cookie's `Secure` flag defaults to
  `false` (`auth_cookie_secure`, dev default). Behind TLS this **must** be `1`,
  otherwise the session cookie is sent unflagged and a browser would still attach it
  over a stray HTTP request.
- `VESTIGO_ENVIRONMENT=production` — disables uvicorn's auto-reload watcher, which
  isn't wanted once nginx is fronting things for real use.

If OIDC SSO is enabled (`VESTIGO_OIDC_ENABLED=1`), also update
`VESTIGO_OIDC_REDIRECT_URL` to the `https://` form of your callback URL — the IdP
redirect target must match what nginx exposes, not `http://localhost:8080`.

### Notes on the proxy config

- `client_max_body_size 200G` — raised from nginx's 1 MiB default; Plaso CSV/JSONL
  source uploads can be large. Lower it to whatever ceiling your largest expected
  source needs.
- The SSE live-update stream (`api/routers/stream.py`, `GET /api/cases/{id}/stream`)
  gets a dedicated regex location with `proxy_buffering off` — buffering would
  delay/batch events until nginx's buffer fills, defeating the point of a live feed.
  The location is scoped to the exact `/stream` path (not all of `/api/cases/`) so the
  large source-upload endpoint keeps the 300s body/send timeouts from `location /`
  instead of nginx's 60s defaults. A 20s server-side keepalive is already built in
  (`_KEEPALIVE_SECONDS`), so `proxy_read_timeout 3600s` just needs to outlive several
  keepalives, not be infinite.
- `X-Forwarded-For` is forwarded for logging only — Vestigo deliberately does **not**
  trust it for access-control decisions (see comment in `api/routers/auth.py`), since
  this is meant to run on a LAN where the header would otherwise be
  attacker-controlled.
- No upstream `Connection: upgrade`/websocket handling is configured — the app has no
  WebSocket routes today (SSE only), so ordinary HTTP/1.1 keepalive is sufficient.

## Stability & upgrades

What the 1.x line guarantees, and what it doesn't:

- **PostgreSQL metadata schema** is Alembic-managed; the app migrates to the current
  head automatically on startup. Upgrading a deployment is: stop, update code/image,
  start.
- **Parquet interchange format v1** (converter output) is stable: files produced by
  any 1.x converter script remain ingestible by any 1.x server. Files written by
  pre-rename (`*2tracesignal.py`) converters are still accepted.
- **Forensic identity is append-only**: parser/embedding config hashes
  (`config_hash()`) identify processing configurations; existing hashes never change
  meaning within 1.x.
- **ClickHouse and Qdrant schemas** have no in-place migration story yet: within 1.x
  they won't change; a future change would come with an explicit re-ingest/re-embed
  procedure in the release notes, never a silent one.
- The REST API is versioned by the app itself (`/api/health` reports the version);
  breaking API changes are reserved for 2.0.

**Upgrading from a pre-1.0 (TraceSignal) deployment:** the project was renamed for
1.0 — CLI `tsig` → `vestigo`, env vars `TS_*` → `VESTIGO_*`, and default
backing-store names are now `vestigo`. Existing data stays where it is: rename your
env vars and pin the old names via `VESTIGO_POSTGRES_URL`,
`VESTIGO_CLICKHOUSE_DATABASE`, and `VESTIGO_QDRANT_COLLECTION_PREFIX`. See
[CHANGELOG.md](../CHANGELOG.md).
