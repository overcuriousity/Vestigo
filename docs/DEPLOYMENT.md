# Deployment

How to run Vestigo beyond a laptop evaluation: the reference compose stack, the
containerized app image, fully airgapped installation, TLS termination, and what the
1.x line guarantees across upgrades.

The application itself is a native Python app (`uv run vestigo-web`) talking to three
**external** backing services — PostgreSQL (metadata), ClickHouse (events), Qdrant
(vectors). Provide those however you prefer: official images, native packages, or
existing infrastructure. Vestigo only needs connection strings (`VESTIGO_*` env vars,
see `.env.example` and `src/vestigo/core/config.py`).

## Configuration: environment vs. the admin console

Every setting has two possible layers, resolved **per field**:

1. **Environment** (`VESTIGO_*`, optionally via `.env`) — the deploy-time layer.
2. **Database** (`app_settings` table) — the runtime layer, edited by an administrator
   under **Administration → Settings** and applied without a restart.

**The environment always wins.** A field pinned in the environment is shown read-only in
the console with its variable name, and any override stored before the pin appeared is
ignored — so a locked-down deployment stays locked down no matter who has an admin
account. Clearing an override in the console deletes its row; the field then falls back
to the environment value, then the built-in default.

Three things the console handles specially:

- **Environment-only fields** are never stored in the database: `VESTIGO_POSTGRES_URL`
  (it is the database these settings live in), `VESTIGO_ENVIRONMENT`,
  `VESTIGO_LOG_LEVEL`, the `VESTIGO_ADMIN_*` bootstrap seed, the data directories
  (`SOURCE_RETENTION_PATH`, `TRANSFER_TEMP_PATH`, `ENRICHER_DATA_PATH`,
  `QDRANT_PATH`), and `VESTIGO_SECRETS_MODE` itself. They are displayed for reference.
- **Restart-required fields** (the ClickHouse and Qdrant connection settings) are stored
  and shown as pending, but the running process keeps the client it built at startup.
- **Secrets** (passwords, API keys) are stored in plaintext and never returned by the
  API — the console shows only whether one is set. Set `VESTIGO_SECRETS_MODE=env-only`
  to refuse database storage of secrets entirely, in which case environment variables
  are the only way to supply them. (The LLM key has its own equivalent switch,
  `VESTIGO_AGENT_SECRET_MODE`, and is edited on the Agent tab.)
- **Optional (nullable) fields** distinguish "unset" from an empty value. Emptying an
  optional field's box clears it; emptying a plain string field stores the empty string,
  which for `VESTIGO_SIGMA_RULES_PATH` is a meaningful value (it disables the global
  ruleset). One asymmetry: `VESTIGO_QDRANT_URL` cannot be unset from the console —
  clearing it restores the default endpoint. Select an embedded on-disk Qdrant with
  `VESTIGO_QDRANT_PATH` (environment-only), which takes precedence over the URL.

**Back up accordingly.** Any secret an admin stores through the console lives in the
`app_settings` table in plaintext, so every Postgres dump, replica and filesystem
snapshot of the metadata store now carries the ClickHouse password, the Qdrant API key,
the embedding API key and the OIDC client secret. Treat those backups as secret
material, or set `VESTIGO_SECRETS_MODE=env-only` and keep the credentials in the
deployment environment.

**What an administrator can now change at runtime.** The console reaches the security
knobs too — `VESTIGO_AUDIT_ENABLED`, the login-backoff thresholds, the session TTL and
the OIDC registration. An admin can therefore disable the audit trail without shell
access (the PUT that does so is itself audited, so the change leaves a final record).
Pin these in the environment on deployments where an admin account must not be able to
weaken them.

Settings are cached per process and reloaded on save, matching the single-process
deployment model the job store already assumes. If you run more than one app process
against one database, restart the others after a settings change. The CLI
(`vestigo ingest`, `vestigo embed`) reads the same layer at startup, so console-tuned
values apply to scripted runs too.

**Optional subsystems are hidden when unconfigured.** `/api/health` reports a
`capabilities` map (embeddings, agent, MCP, OIDC, enrichers, Sigma, case transfer);
a subsystem that is off or unconfigured renders no entry point in the UI and — for the
AI agent — its tools are not advertised to the model at all. The endpoints refuse
independently, so hiding is never the only enforcement.

The map requires a session: an anonymous `GET /api/health` answers with liveness,
version and `oidc_enabled` only, since the login page needs those and an inventory of
which subsystems an instance runs is otherwise not public. One exception to "refuses
independently": with `VESTIGO_TRANSFER_ENABLED=false`, starting an export or import is
refused with 503, but an archive an earlier export already produced can still be
downloaded — it is single-use and swept from disk shortly after, so refusing it would
only strand a legitimate export.

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

## On-disk state outside the databases

Two directories hold case data on the app host itself, both `VESTIGO_*`-configurable
(`src/vestigo/core/config.py`):

- `VESTIGO_SOURCE_RETENTION_PATH` (default `data/sources`) — content-addressed copies
  of every uploaded source file, shared instance-wide and sharded by hash prefix.
- `VESTIGO_TRANSFER_TEMP_PATH` (default `data/transfer`) — in-flight case export
  archives. An archive is a **complete** case (optionally including the original
  source blobs), so treat this directory as exactly as sensitive as the retention
  path. Vestigo creates it `0700`, forces that mode on an existing directory, and
  refuses to export if the path turns out to be owned by another user or not to be
  a real directory — do not point it at a shared `/tmp`. Size it for the largest
  single case you expect to export, times `VESTIGO_TRANSFER_MAX_CONCURRENT` (below).

  Archives are removed as soon as they are downloaded; anything left by an
  interrupted download is expired 24 hours later by the next export, and everything
  left over is cleared at startup (the job store is in-memory, so nothing there
  survives a restart anyway). That startup sweep assumes **one app process per
  configured path**. It removes only what a transfer job writes — `*.vestigo`
  archives and job-id working directories — and logs a warning about anything else
  it finds, so pointing the setting at a populated directory costs that directory
  nothing. Give it a directory of its own regardless.

Import is open to any authenticated user and restores as a new case owned by them.
Because an uploaded archive is untrusted input, two size caps apply, both checked
against the manifest before a single member is read:

- `VESTIGO_TRANSFER_MAX_EXPANDED_BYTES` (default 200 GiB, `0` disables) caps the
  archive's **total** uncompressed size. Events and blobs travel uncompressed, so a
  real export expands by roughly 1x; a ratio far above that is a decompression bomb
  rather than a big case.
- `VESTIGO_TRANSFER_MAX_METADATA_BYTES` (default 2 GiB, `0` disables) caps any
  **single** `postgres/*` member. A total says nothing about one member — a lone
  100 GiB metadata member fits comfortably under a 200 GiB total. These members
  hold case metadata (rows, not events), so a genuine export stays far below the
  default.

Raise either only if a genuine export trips the check.

`VESTIGO_TRANSFER_MAX_CONCURRENT` (default 2, `0` disables) caps how many export and
import jobs may be in flight at once, across the instance. Both directions reserve
real disk for the whole job and any authenticated user can start either, so this is
admission control rather than a throughput knob; an import over the cap is rejected
with 429 *before* its upload is accepted. Raise it only alongside the temp path's
capacity.

Transfers do their hashing, zipping and verification in worker threads, so a
multi-GiB export or import does not stall the rest of the API while it runs. They
are still CPU- and disk-bound, so the concurrency cap remains the thing that keeps
them from crowding out ordinary queries.

Restored `audit_log` rows keep the actor, action and timestamp the archive asserted —
that is the point of exporting them — but nothing on the importing instance vouches
for any of it. Every imported row therefore carries `detail.imported` (import job id,
importing user, source case id) and is badged **imported** in the admin audit view.
When reviewing a user's activity, treat badged rows as claims made by whoever
uploaded the archive, not as locally recorded events.

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

**Event ids for sources containing invalid UTF-8 changed.** `byte_offset` used to be
measured over `errors="replace"`-decoded text, which over-counts by two bytes per
undecodable byte, so every offset after the first bad byte was wrong — and
`byte_offset` feeds `derive_event_id`. Offsets are now measured over the file's real
bytes (`docs/INPUT_FORMATS.md`). **Already-ingested data is unaffected**: ids are
derived once at ingest and nothing recomputes them. The change is only visible if you
*re-ingest* a file that contains invalid UTF-8 — the new ids won't match the old ones,
so annotations recorded against the previous ingest of that same file will not carry
over. Sources are immutable, so the safe procedure is to treat a re-ingest as a new
source, which is what the model already does.

**Upgrading from a pre-1.0 (TraceSignal) deployment:** the project was renamed for
1.0 — CLI `tsig` → `vestigo`, env vars `TS_*` → `VESTIGO_*`, and default
backing-store names are now `vestigo`. Existing data stays where it is: rename your
env vars and pin the old names via `VESTIGO_POSTGRES_URL`,
`VESTIGO_CLICKHOUSE_DATABASE`, and `VESTIGO_QDRANT_COLLECTION_PREFIX`. See
[CHANGELOG.md](../CHANGELOG.md).
