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
deployment model the job store already assumes — see
[Operational scale](#operational-scale) for why that model is a constraint rather than a
default. The CLI (`vestigo ingest`, `vestigo embed`) reads the same layer at startup, so
console-tuned values apply to scripted runs too.

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

## OIDC single sign-on (optional)

Off by default. Enabling it adds an SSO button to the login page next to local
username/password login — it does not replace local accounts, and the bootstrap admin
stays a local account so a broken IdP can never lock you out of the instance.

```bash
VESTIGO_OIDC_ENABLED=true
VESTIGO_OIDC_ISSUER=https://idp.example.org/application/o/vestigo/
VESTIGO_OIDC_CLIENT_ID=...
VESTIGO_OIDC_CLIENT_SECRET=...
VESTIGO_OIDC_REDIRECT_URL=https://vestigo.example.org/api/auth/oidc/callback
VESTIGO_OIDC_SCOPES="openid email profile"   # default; rarely needs changing
```

This is the one optional subsystem deliberately independent of
`VESTIGO_ALLOW_ONLINE` — see [TECH_STACK.md](TECH_STACK.md) §6. An airgapped
instance with an IdP on the same isolated network can use SSO without opening
general outbound access.

### Getting the issuer right

`VESTIGO_OIDC_ISSUER` is the base URL Vestigo appends
`/.well-known/openid-configuration` to. Everything else (authorization, token and
userinfo endpoints) is read from that discovery document, so the issuer is the only
IdP URL you configure. Vestigo follows redirects on that fetch, so the tidy form
works even where the provider serves the document elsewhere.

| IdP | Issuer |
|---|---|
| Authentik | `https://auth.example.org/application/o/<app-slug>/` |
| Nextcloud | `https://cloud.example.org` (301s to `/index.php/.well-known/...`; followed automatically) |
| Keycloak | `https://sso.example.org/realms/<realm>` |
| Okta | `https://<tenant>.okta.com/oauth2/default` |
| Google | `https://accounts.google.com` |

Verify before touching Vestigo — this is the exact request the app makes:

```bash
curl -sL https://idp.example.org/application/o/vestigo/.well-known/openid-configuration | jq .authorization_endpoint
```

If that returns JSON, the issuer is correct. If it 404s, you have the wrong base URL;
if it needs `-L` to succeed, that is fine — Vestigo follows the same redirect. A
discovery fetch that fails at runtime surfaces as `502` with the attempted URL in the
response and a `WARNING` in the log, not as a generic 500.

### Redirect URL and the IdP side

`VESTIGO_OIDC_REDIRECT_URL` must match a redirect URI registered with the IdP
*character for character*, and must be the URL the browser reaches — behind the TLS
proxy that is the `https://` form, not `http://localhost:8080` (see
[Required Vestigo-side settings](#3-required-vestigo-side-settings)).

New subjects are auto-provisioned into a team-less "default pool": they can log in
and see only their own work until an admin assigns them a team in the admin console.
No case access is granted implicitly.

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

The build takes `FRONTEND_STAGE`: the default (`frontend-build`) builds the frontend
in a node stage, while `--build-arg FRONTEND_STAGE=frontend-prebuilt` copies an
already-built `frontend/dist` out of the build context instead. The prebuilt stage is
`FROM scratch`, so selecting it means the node base image is never resolved — which is
what makes an offline build possible at all. `scripts/airgap-bundle.sh` uses it.

The selection is an alias stage (`FROM ${FRONTEND_STAGE} AS frontend`) rather than a
`COPY --from=${FRONTEND_STAGE}`, because Docker refuses variable expansion in `--from`
while buildah/podman performs it — the terser form builds locally under podman and
fails every Docker build. Keep the alias if you edit this.

## Resource sizing

The reference stack ships **no memory limits**. That is fine for evaluation on a
roomy box and wrong for anything real: four processes (app, ClickHouse, Postgres,
Qdrant) share one host's RAM, none of them knows about the others, and the kernel
resolves the shortfall by killing whichever is largest. That is almost always
ClickHouse.

**Recognizing it.** A kernel/cgroup kill shows up as

```
clickhouse-1 exited with code 137 (restarting)
```

and, in the app's log, a burst of `Connection refused` against `clickhouse:8123`.
Exit 137 is SIGKILL from outside — **not** a ClickHouse memory error. If ClickHouse
had hit its own limit you would instead see a `MEMORY_LIMIT_EXCEEDED` query failure
and the server would still be alive. Confirm with `dmesg -T | grep -i -A5 'killed
process'`, which names the cgroup and the RSS at kill time.

### The three ceilings, and how they must relate

1. **The container limit** (`mem_limit` in `docker-compose.yml`) — the hard stop.
   Above it the kernel kills, without warning or a log line from the victim.
2. **ClickHouse's own `max_server_memory_usage`** — must sit *below* the container
   limit so the server refuses a query rather than being killed. By default it is
   derived as 0.9 × detected RAM, and a containerized server can misdetect that
   badly (503 GiB observed on a 128 GiB VM), in which case it never self-throttles.
   Pin an absolute value: see `deploy/clickhouse/memory.xml.example`. Pinning does
   not disable the ratio — the effective ceiling is the *lower* of the two, and
   ClickHouse logs a "lowered to" line when it clamps the pinned value down, so
   confirm the value it actually adopted in the server log.
3. **Vestigo's scan budget** (`VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`) — a *total across
   concurrent heavy scans*; each query is granted budget ÷
   `VESTIGO_STAT_SCAN_CONCURRENCY` as its `max_memory_usage`. Must sit below (2).

The trap is (3) when left at its `0` (auto) default. Detection runs **in the app
process**, from its cgroup limit or, failing that, the host's `MemTotal`. In a
full-docker stack with no limits set, the app reads the whole host and sizes a
budget as though ClickHouse owned the machine. On a 32 GiB host that is 32 × 0.8 ÷ 2
= 12.8 GiB granted to a single query, while Postgres, Qdrant and the app are also
resident. Pin it explicitly whenever the app and ClickHouse are in separate
containers — the automatic value is only correct when they genuinely share one
memory limit.

Heavy scans are admission-controlled (`VESTIGO_STAT_SCAN_CONCURRENCY`, default 2),
and the enrichment partition rewrite takes a slot too — so the worst case is
`concurrency × per-query cap`, not one query's cap.

### Worked example: 32 GiB host, full-docker

| Setting | Value | Where |
| --- | --- | --- |
| ClickHouse container limit | 12 GiB | `mem_limit: 12g` |
| ClickHouse server limit | 10 GiB | `deploy/clickhouse/memory.xml` |
| Vestigo scan budget (total) | 8 GiB | `VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES=8000000000` |
| → per-query cap | 4 GiB | budget ÷ concurrency (2) |
| Postgres | 4 GiB | `mem_limit: 4g` |
| Qdrant | 4 GiB | `mem_limit: 4g` (only with embeddings) |

That leaves roughly 12 GiB for the app process and the OS page cache. Scale the
ClickHouse numbers first when you have more RAM; it is where query cost lives.

Every `VESTIGO_STAT_SCAN_*` value is editable in the admin console, but all of them
are **restart-required** — the SETTINGS clause is built once at import and the
admission semaphore is shared by value across the scan modules, so a saved edit does
nothing until the app restarts. The console labels them accordingly.

## Airgapped installation

Two supported routes. Pick by how the host runs Vestigo:

- **Containers → the bundle.** One tarball carries every image, the compose file and
  an installer. Nothing is built, pulled or resolved on the isolated host. This is the
  route to take unless you have a reason not to.
- **Native (`uv run vestigo-web`) → the carried checkout**, further below.

### Route A: the deployment bundle (containers)

#### Step 1 — build the bundle (connected machine)

From a checkout of the version you want to ship:

```bash
scripts/airgap-bundle.sh                    # app + all three backing services
scripts/airgap-bundle.sh -o /media/usb      # write straight to the drive
scripts/airgap-bundle.sh --no-embeddings    # skip the ~2 GB local embedding stack
scripts/airgap-bundle.sh --app-only         # app image only — see the caveat below
```

It builds the frontend, builds the app image **from that prebuilt frontend** (the
`frontend-prebuilt` Dockerfile stage, so the isolated host never resolves
the `node:24-alpine` build image), saves every image the stack runs, verifies the resulting archive
really holds that many images, and packs everything with the compose file,
`.env.example`, `nginx-tls.conf` and `install.sh`. The compose file travels as
`compose.airgap.yml` — not one of the names `docker compose` auto-discovers — and only
`install.sh` renames it to `docker-compose.yml` in the install directory, so a compose
command run from the extracted bundle cannot reach the live stack from the wrong
directory. Output is
`vestigo-airgap-<version>-<commit>.tar.gz` **and** a matching `.sha256`.

Copy **both files** to the drive. The `.sha256` is how the far side distinguishes a
bad copy from a bad bundle.

#### Step 2 — carry it

Nothing on the isolated host needs a registry, npm, a build, or any name resolution.
What the host must already have is a **container engine**: Docker with the compose
plugin, or podman with podman-compose. The bundle installs Vestigo, not Docker.

Full bundle size is roughly 2.5–5 GB (`--no-embeddings` roughly halves it), so size
the drive before you leave.

#### Step 3 — install or upgrade (airgapped host)

```bash
sha256sum -c vestigo-airgap-<version>-<commit>.tar.gz.sha256
tar xzf vestigo-airgap-<version>-<commit>.tar.gz
cd vestigo-airgap-<version>-<commit>
./install.sh --check      # verify everything, change nothing, touch nothing
./install.sh
```

`install.sh`:

1. verifies its own checksums and that the image archive holds the number of images
   the bundle declares (a truncated copy or a short `save` fails here, before
   anything is loaded or the running stack is disturbed);
2. picks the container engine by probing `info`, not by finding a binary — a docker
   binary whose daemon is unreachable does not beat a working podman;
3. copies the bundle payload into the **install directory** and runs everything from
   there (see below);
4. creates `.env` from the example **only when there is none**, and rewrites exactly
   one line of an existing `.env`: `VESTIGO_IMAGE_TAG`;
5. loads the images, then checks every image the compose file references is actually
   present — a missing one would otherwise send compose to a registry that is not
   reachable, and the resulting DNS timeout names nothing useful;
6. `compose up -d --no-build`, waits for `/api/health`, and says plainly when the
   wait times out rather than reporting a success it did not observe.

**The install directory.** The extracted bundle is throwaway; the stack runs from
`/opt/vestigo` (or `~/vestigo` when `/opt` is not writable; override with `--dir` or
`VESTIGO_INSTALL_DIR`). That directory holds `.env`, the compose file and the compose
project — and therefore the named volumes with all case data. This is what makes an
upgrade an upgrade: a newer bundle unpacks into a *different* directory but installs
into the *same* one, so the operator's configuration and the existing data are picked
up rather than replaced by an empty second stack. Run all later `compose` commands
from there.

The compose file also pins `name: vestigo` for the same reason. If a host ran Vestigo
under a different compose project name (for example from a repository checkout named
something else), `install.sh` notices the foreign volumes and tells you to set
`COMPOSE_PROJECT_NAME=<that prefix>` in the install directory's `.env` before
continuing — otherwise the upgraded stack comes up healthy, empty, and next to your
data.

First install prints the two things left to do: set `VESTIGO_ADMIN_PASSWORD` in `.env`
(then `docker compose up -d app`), and put TLS in front of the app before analysts use
it (§TLS reverse proxy below).

#### Upgrading an existing airgapped install

Same command. Build a bundle from the newer checkout, carry it over, run
`./install.sh`. It reports `<old tag>  ->  <new tag>`, keeps the `.env`, reuses the
volumes, and the app runs its schema migrations on startup. `--app-only` bundles are
much smaller and are the normal choice here — but only for a host a **full** bundle
has already reached, since they carry no backing-service images. `install.sh` checks
for them and refuses (having started nothing) rather than letting compose try to pull.
When you cannot verify the target's state before travelling, carry the full bundle.

**Backup before an upgrade.** Nothing in this path deletes data, but a schema
migration is not reversible, so snapshot the volumes while the stack is stopped:

```bash
cd /opt/vestigo && docker compose down
for v in vestigo_postgres_data vestigo_clickhouse_data vestigo_qdrant_data vestigo_app_data; do
  docker run --rm -v "$v":/from -v "$PWD/backup":/to alpine \
    tar czf "/to/$v.tar.gz" -C /from .
done
```

(Restore by reversing the mounts into a freshly created volume of the same name.)
The `alpine` image must already be on the host — add it to the bundle yourself if you
want this available offline, or use `docker compose cp` from a running service.

**Rollback.** The previous image stays loaded on the host, so reverting the code is
one line — `install.sh` prints the exact command after an upgrade:

```bash
cd /opt/vestigo
sed -i 's|^VESTIGO_IMAGE_TAG=.*|VESTIGO_IMAGE_TAG=<previous tag>|' .env
docker compose up -d app
```

This reverts the *code* only. If the upgrade migrated the schema, restore the backup
instead.

#### When something is wrong

- `install.sh` says the bundle is damaged → the copy did not survive the drive.
  Re-copy; the `.sha256` next to the tarball tells you whether the tarball itself
  arrived intact.
- The health wait times out → the stack is probably still migrating. Raise the watch
  with `VESTIGO_HEALTH_TIMEOUT_SECONDS=600 ./install.sh`, or just follow
  `cd /opt/vestigo && docker compose logs -f app`.
- Anything tries to reach a registry → that is a bug in this path, not an operator
  error. Nothing in the bundle should resolve a name; report it.

### Route B: native install from a carried checkout

Use this when the host runs the app directly rather than in a container.
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

### Patching an airgapped host in place

Applying a fix without a full bundle: carry the `git format-patch` output plus a
prebuilt `frontend/dist`, `git am` the patches, replace `frontend/dist`, restart.

For a **container** deployment that is only a stopgap — `docker cp` into a running
container survives `restart` but not `up --force-recreate` or a rebuild, so the image
still holds the old code. Follow it with a proper bundle (Route A) at the next
opportunity, or the fix silently disappears the next time the stack is recreated.
A rebuilt frontend is not optional for any change under `frontend/`: the app serves
`frontend/dist`, and `docker compose up -d` after a **failed** build happily keeps
serving the previous image.

### Troubleshooting a container install

The first two were hit on a first install into a fresh, unprivileged **LXC** guest and
neither is a bundle problem — the bundle is fine, the host cannot run containers yet.
`install.sh` refuses on the first and compose fails on the second; in both cases fix
the host and re-run the installer, which is idempotent. The third is a packaging bug in
bundles built before 1.8.5.

**`Error unpacking image … err: permission denied`, on every image.**

```
apply layer error for "docker.io/library/postgres:17-alpine": failed to extract layer
sha256:…: failed to mount /var/lib/containerd/tmpmounts/containerd-mount…:
mount source: "overlay", … userxattr,index=off, err: permission denied
```

The `/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/` path is the tell:
this is Docker's **containerd image store**, the default since Docker 28 and therefore
what any *fresh* install gets. It mounts overlay with `userxattr`, which an
unprivileged LXC guest refuses. An older Docker on the same kind of host works
because it used the classic `overlay2` graphdriver — so "Docker has always worked in
my LXC containers" and this failing are consistent.

Confirm with `docker info | grep 'Storage Driver'`: `overlayfs` is the containerd
snapshotter, `overlay2` is the graphdriver. To go back to the graphdriver:

```json
/* /etc/docker/daemon.json */
{ "features": { "containerd-snapshotter": false } }
```

```bash
sudo systemctl restart docker
docker info | grep 'Storage Driver'   # want: overlay2
```

Images already registered live in the containerd store and do not carry over; re-run
`install.sh` and it reloads them.

**`error mounting "proc" to rootfs at "/proc": … permission denied`.**

Images unpack, containers are created, and every one fails to start. runc mounts a
fresh procfs into the container, which an unprivileged LXC guest blocks unless the
guest is allowed to nest. `sudo` inside the guest changes nothing — root there is not
root on the LXC host — so this is fixed **on the host**, followed by a guest restart:

```bash
pct set <vmid> --features nesting=1 && pct reboot <vmid>   # Proxmox
lxc config set <name> security.nesting true                # LXD/Incus
```

Plain LXC: `lxc.apparmor.profile = generated` plus `lxc.apparmor.allow_nesting = 1`.
`systemd-detect-virt` confirms you are in an LXC guest; `cat /proc/self/attr/current`
shows which AppArmor profile is in force.

**`missing image(s): vestigo-app:<tag>`, right after the log said it loaded it.**

Bundles built **before 1.8.5** on a podman host and installed on a **docker** host.
Podman stores a locally built, unqualified image as `localhost/vestigo-app:<tag>` and
saves it under that name; docker `load` keeps the name verbatim, but resolves the bare
`vestigo-app:<tag>` the old compose file asked for to `docker.io/library/vestigo-app` —
a different image, absent, so the installer correctly refused. Podman on the far side
resolved the short name to `localhost/` and never saw it.

The bundle is intact; verify its checksum if you like, it will match. Either build a
current bundle, or retag once on the target and re-run the (idempotent) installer:

```bash
docker tag localhost/vestigo-app:<tag> vestigo-app:<tag>
./install.sh --dir <install dir>
```

From 1.8.5 the app image is `localhost/vestigo-app:<tag>` in the builder, the compose
file, and the installer's check alike, which both engines read the same way.

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

## Operational scale

Vestigo does not care how many analysts share an instance — case-level RBAC, teams and the
audit trail all work the same at any headcount, and nothing in the data path is sized to a
team. What *is* bounded is the process topology: **run exactly one app process per
instance.**

Five subsystems keep state in that process's memory, so a second worker would not share it:

| State | Module | What a second process breaks |
|---|---|---|
| Background jobs (ingest, embed, transfer) | `core/jobs.py` | A job started on worker A is invisible to a status poll that lands on worker B |
| Live-collaboration pub/sub (SSE) | `core/events_bus.py` | Subscribers only see changes made by their own worker |
| Failed-login backoff | `core/login_backoff.py` | Effective lockout threshold multiplies by the worker count |
| Visualization baseline cache | `db/viz_cache.py` | Correct, just colder — each worker warms its own |
| Merged settings cache | `core/config.py` (`get_settings`, `lru_cache`) | An admin-console change reaches only the worker that served the save |

The transfer temp-path startup sweep likewise assumes one process per configured path.

So do not pass `--workers`/`--reload` to uvicorn, and do not run two app containers
against one database. To serve more concurrent analysts, scale the box (the API is async
and I/O-bound; heavy scans are already admission-controlled by `HEAVY_SCAN_GATE`, and
transfers by `VESTIGO_TRANSFER_MAX_CONCURRENT`) and scale ClickHouse, which is where query
cost actually lives. Nothing here caps **data** volume — the reference case is 300M rows
and single timelines run to 80 GiB+. Scaling the box without also raising the memory
ceilings is how you get an OOM-killed ClickHouse — see [Resource
sizing](#resource-sizing).

Multi-process scale-out is possible but unbuilt: it means moving those state holders
to a shared backend (Postgres or Redis for jobs and backoff, a real pub/sub for the event
bus) and re-opening the standing decisions priced against a single trusted process — CSRF
tokens and the full-user-directory listing (`docs/ROADMAP.md`). Treat it as a milestone,
not a config flag.

## The demo case

New users are seeded a fabricated example investigation the first time they log
in — 251k events across four sources, with the analyst's notes, tags, saved
views, a baseline definition, four Sigma rules and a story already in place.
`docs/ANOMALY_DETECTION.md` describes what it contains and why.

Operationally there are four things worth knowing:

- **It is generated, not shipped.** The generator (`src/vestigo/demo/`) runs
  per user as a background job: roughly 2.5s to fabricate the four source files
  and a few seconds more to ingest them through the normal pipeline. It is
  deterministic, so every user's copy is identical down to the source files'
  SHA-256 hashes.
- **It costs CPU while it runs.** Generating and ingesting a quarter of a
  million events is CPU-bound Python, so a build competes with the rest of the
  app on a single-process deployment. One runs at a time by default
  (`VESTIGO_DEMO_MAX_CONCURRENT=1`); raise it on a box with cores to spare, or
  set it to 0 to remove the cap. A first login that finds the cap full is not
  seeded and is not marked as seeded — it simply tries again at the next login.
  An explicit restore gets a 429 instead.
- **It costs storage per user.** ~251k events per seeded user in ClickHouse —
  about 12.5M rows across 50 users. Turn seeding off on large instances, or on
  any deployment where fabricated data sitting in a case list is a policy
  problem: `VESTIGO_DEMO_CASE_ENABLED=false` (also editable in the admin
  console, under Onboarding). Demo cases are flagged as such (`cases.is_demo`),
  and administrators do not see other users' copies in their case list — with
  one per account they would otherwise bury the real work.
- **Seeding happens once per user, ever.** The stamp (`users.demo_case_seeded_at`)
  survives the user deleting the case, so a deleted demo stays deleted. Users
  can ask for a fresh copy from the case list, which is the only way it comes
  back, and only while they do not already have one — the endpoint answers 409
  otherwise. Turning the setting off stops future seeding and leaves existing
  copies alone; upgrading an instance backfills every existing user at their
  next login, since their stamp is null.

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
