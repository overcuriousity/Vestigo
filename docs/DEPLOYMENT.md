# Deployment

How to run Vestigo beyond a laptop evaluation: configuration layers, resource sizing, the
reference compose stack, the containerized app image, airgapped installation, TLS, and what
the 1.x line guarantees across upgrades.

The application is a native Python app (`uv run vestigo-web`) talking to three **external**
backing services — PostgreSQL (metadata), ClickHouse (events), Qdrant (vectors). Provide
those however you prefer: official images, native packages, or existing infrastructure.
Vestigo only needs connection strings (`VESTIGO_*`, see `.env.example` and
`src/vestigo/core/config.py`).

## Configuration: environment vs. the admin console

Every setting has two layers, resolved **per field**:

1. **Environment** (`VESTIGO_*`, optionally via `.env`) — the deploy-time layer.
2. **Database** (`app_settings`) — the runtime layer, edited under **Administration →
   Settings** and applied without a restart.

**The environment always wins.** A field pinned in the environment is shown read-only in the
console with its variable name, and any override stored before the pin appeared is ignored —
so a locked-down deployment stays locked down no matter who has an admin account. Clearing
an override deletes its row; the field falls back to the environment value, then the default.

Special cases:

- **Environment-only fields** are never stored in the database: `VESTIGO_POSTGRES_URL` (it is
  the database these settings live in), `VESTIGO_ENVIRONMENT`, `VESTIGO_LOG_LEVEL`, the
  `VESTIGO_ADMIN_*` bootstrap seed, the data directories (`SOURCE_RETENTION_PATH`,
  `TRANSFER_TEMP_PATH`, `ENRICHER_DATA_PATH`, `QDRANT_PATH`) and `VESTIGO_SECRETS_MODE`
  itself. They are displayed for reference.
- **Restart-required fields** (ClickHouse and Qdrant connection settings) are stored and
  shown as pending; the running process keeps the client it built at startup.
- **Secrets** are stored in plaintext and never returned by the API — the console shows only
  whether one is set. `VESTIGO_SECRETS_MODE=env-only` refuses database storage of secrets
  entirely. (The LLM key has its own switch, `VESTIGO_AGENT_SECRET_MODE`, on the Agent tab.)
- **Optional (nullable) fields** distinguish "unset" from empty. Emptying an optional field
  clears it; emptying a plain string field stores the empty string, which for
  `VESTIGO_SIGMA_RULES_PATH` is meaningful (it disables the global ruleset). One asymmetry:
  `VESTIGO_QDRANT_URL` cannot be unset from the console — clearing it restores the default
  endpoint. Select an embedded on-disk Qdrant with `VESTIGO_QDRANT_PATH` (environment-only),
  which takes precedence over the URL.

**Back up accordingly.** Any secret an admin stores through the console lives in
`app_settings` in plaintext, so every Postgres dump, replica and snapshot of the metadata
store carries the ClickHouse password, Qdrant API key, embedding API key and OIDC client
secret. Treat those backups as secret material, or set `VESTIGO_SECRETS_MODE=env-only`.

**The console reaches the security knobs too** — `VESTIGO_AUDIT_ENABLED`, login-backoff
thresholds, session TTL, OIDC registration. An admin can disable the audit trail without
shell access (the PUT that does so is itself audited, so the change leaves a final record).
Pin these in the environment where an admin account must not be able to weaken them.

Settings are cached per process and reloaded on save, matching the single-process deployment
model (see [Operational scale](#operational-scale)). The CLI reads the same layer at startup,
so console-tuned values apply to scripted runs.

**Optional subsystems are hidden when unconfigured.** `/api/health` reports a `capabilities`
map (embeddings, agent, MCP, OIDC, enrichers, Sigma, case transfer, demo case, converter
generation); a subsystem that is off renders no UI entry point and — for the AI agent — its
tools are not advertised to the model at all. The endpoints refuse independently, so hiding
is never the only enforcement.

Two of those capabilities are *probed* rather than read off configuration, because a
configured-looking subsystem that does not answer is the same thing to an analyst as an
unconfigured one. The agent's LLM endpoint must serve its model listing, and embeddings need
both arms live: the vector store answering `get_collections()`, and — when
`VESTIGO_EMBEDDING_API_BASE_URL` is set — that endpoint answering a one-token embed. So
removing Qdrant from the stack removes "Improve search quality", semantic search and the
agent's embedding tools, rather than leaving buttons whose jobs fail at the store. Results
are cached (`VESTIGO_AGENT_PROBE_TTL_SECONDS`, `VESTIGO_EMBEDDING_PROBE_TTL_SECONDS`, both
60s by default) and revalidated in the background, so a poll never waits on a hung service
and a service coming back restores its UI within a TTL — no restart. To declare a deployment
embedding-free outright, clear `VESTIGO_QDRANT_URL` and `VESTIGO_QDRANT_PATH`; nothing is
probed then.

The map requires a session: an anonymous `GET /api/health` answers with liveness, version and
`oidc_enabled` only. One exception to "refuses independently": with
`VESTIGO_TRANSFER_ENABLED=false`, starting an export or import is refused with 503, but an
archive an earlier export already produced can still be downloaded — it is single-use and
swept from disk shortly after, so refusing it would only strand a legitimate export.

## Generated converters (optional; executes model-written code)

`converter_generation_enabled` (default **off**) lets an analyst upload a plain-text log and
have the configured AI model write the converter server-side (`INPUT_FORMATS.md` §"Generated
converters"). It needs the agent endpoint configured and reachable. Companion tunables:
`converter_max_attempts` (4), `converter_sample_bytes` (4 KiB shown to the model — whole
records, long values shortened; small on purpose, see `INPUT_FORMATS.md`),
`converter_run_timeout_seconds` (600), `converter_run_memory_mb` (2048; pyarrow does not
import below that), `converter_run_output_mb` (4096).

**What the run isolates — and what it does not.** The script runs as a child of the app
process with `python -I`, in a private temporary working directory holding only the script
and a read-only copy of the input, with an environment reduced to `PATH`/`HOME`/`TMPDIR`/
`LANG` (no `VESTIGO_*`, no proxies, no credentials), under `RLIMIT_AS`, `RLIMIT_CPU`,
`RLIMIT_FSIZE` and `RLIMIT_NOFILE`, in its own session so a timeout kills the whole group.
Before it runs, an AST scan allow-lists imports (standard library plus `pyarrow`/`numpy`,
minus network, subprocess, threading, `ctypes`, `importlib`, `pickle`, `inspect` and
friends), resolves aliases, allows an imported module only as `module.<attribute>`, and
rejects `exec`/`eval`/`globals`/`vars`, `sys.modules`/`sys.path`, `getattr` on a module or
with a dunder string, the object-graph dunders and destructive `os.*`/`Path.*` attributes.

This is **best-effort static analysis, not a sandbox** — deliberately no bwrap and no
container-in-container, so the reference uv and image deployments keep working. It does not
stop a script from writing anywhere the app user can write, nor from reaching the network if
it finds a path the scan does not cover. Run the app as a dedicated unprivileged user (the
container image already does), keep the switch off where model-written code must not run,
and remember that a log line is untrusted input to the model: the harness validates the
*output* against the data contract and records every attempt.

## OIDC single sign-on (optional)

Off by default. Enabling it adds an SSO button next to local username/password login — it
does not replace local accounts, and the bootstrap admin stays local so a broken IdP can
never lock you out.

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
[Required Vestigo-side settings](#required-vestigo-side-settings)).

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
(`.env.example`) connect via these localhost ports. PostgreSQL is initialized with
`trust` authentication for the same reason ClickHouse and Qdrant carry no auth here;
set `VESTIGO_POSTGRES_HOST_AUTH=scram-sha-256` before the first `up` to keep password
auth. It applies at initdb only, so on a stack that has already run, changing it means
recreating the volume (`docker compose down -v`, which destroys the local data).

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

**Start here:** the [sizing calculator](https://overcuriousity.github.io/Vestigo/sizing/)
turns an expected dataset size, analyst count and deployment shape into the numbers this
section explains, using the same arithmetic `db/_scan.py` uses. It cannot see your host, so
read `/api/health`'s `scan_budget` block once the stack is up.

Scans run in two lanes. `VESTIGO_STAT_SCAN_CONCURRENCY` (N) is the number of heavy scans —
detectors, Sigma, inventory exports, the enrichment rewrite — admitted at once; interactive
charts (histogram, top terms, numeric stats) have their own four-slot lane and never queue
behind a sweep. The memory budget is divided by N + 2: N heavy caps plus two slots the four
chart queries share, so a chart is capped at half a detector's cap rather than a quarter of it
— charts over high-cardinality fields are the ordinary workload, and they spill at that size
rather than dying. Reported as `scan_budget.foreground` on `/api/health` and on the admin
Settings page. Raising N widens the heavy lane and shrinks every cap; it does not change how
charts are admitted. A chart that cannot get a slot within 5 s answers 503 with the queue
depth and the UI keeps waiting visibly, re-asking at the server's pace for about four minutes
before it calls the lane wedged; a request that disconnects (a reload mid-sweep) frees its slot
and kills its ClickHouse query within about a second.

Threads are bounded but **not** reserved the way memory is. A chart runs at `max_threads × 2 ÷ 4`
(floor 2), not the heavy width, so a full chart lane costs about two heavy slots' worth of CPU
rather than four times a detector's. Reported as `scan_budget.foreground.max_threads`. Charts are
short and latency-bound; the width they lose costs them little, and the sweep they would otherwise
have slowed down is usually the thing the analyst is actually waiting for.

The heavy width still divides the cores by N alone, though, so those two slots are *added* to a
box the heavy divisor has already sized to be exactly saturated by a full heavy gate — unlike
`per_query_bytes`, which divides by N + 2 and therefore takes the chart lane's share out of the
detectors' own. Every slot busy at once is up to twice the core count in threads: on a 20-core
host at the default N, `2 × 10 + 4 × 5 = 40`. That is the deliberate trade — it is a quarter of
the 8× oversubscription it replaced, it needs all six slots full to appear at all, and closing it
would mean halving every detector sweep's width on a box where nobody has opened a chart. Size for
it if you run the Visualize tab continuously alongside sweeps: pin `VESTIGO_STAT_SCAN_MAX_THREADS`
to `cores ÷ (N + 2)` and both lanes fit inside the core count.

> **Upgrading across the chart-lane change (#300):** the heavy per-query cap changed divisor from
> N to N + 2, so at the default `VESTIGO_STAT_SCAN_CONCURRENCY=2` **every detector query's
> `max_memory_usage` halved** — and the spill thresholds with it. The reservation is permanent,
> not demand-driven: an install that never opens the Visualize tab still pays it. On a corpus
> where a whole-corpus `value_combo` or `distribution_drift` sweep was already close to its cap,
> that is the difference between spilling and `MEMORY_LIMIT_EXCEEDED`. If a sweep that used to
> complete now fails, check `scan_budget.per_query_bytes` on `/api/health` against the sizing
> calculator and raise `max_server_memory_usage` (and the container limit with it) rather than
> lowering N — N is what bounds how many sweeps stack.

Both compose files ship **with memory limits set**, sized for a 32 GiB host and overridable
per service (`VESTIGO_CLICKHOUSE_MEM_LIMIT`, `VESTIGO_POSTGRES_MEM_LIMIT`,
`VESTIGO_QDRANT_MEM_LIMIT`, `VESTIGO_APP_MEM_LIMIT`). Raise them to fit your box. **Do not
remove them.** Four processes share one host's RAM, none knows about the others, and with no
limits the kernel resolves the shortfall by killing whichever is largest — almost always
ClickHouse.

**Recognizing an OOM kill.** `clickhouse-1 exited with code 137 (restarting)`, plus a burst
of `Connection refused` against `clickhouse:8123` in the app log. Exit 137 is SIGKILL from
outside — **not** a ClickHouse memory error; its own limit produces a
`MEMORY_LIMIT_EXCEEDED` query failure with the server still alive. Confirm with `dmesg -T |
grep -i -A5 'killed process'`, which names the cgroup and the RSS at kill time. A subtler
tell, visible without reading any log: `docker compose ps` showing ClickHouse with an uptime
far shorter than the rest of the stack — `restart: unless-stopped` makes a nightly kill look
like a healthy service.

### The three ceilings, and how they must relate

1. **The container limit** (`mem_limit`) — the hard stop. Above it the kernel kills, without
   warning or a log line from the victim. It decides *who dies*, never whether anyone
   throttles first.
2. **ClickHouse's own `max_server_memory_usage`** (`deploy/clickhouse/memory.xml`, mounted by
   both compose files) — must sit *below* the container limit so the server refuses a query
   rather than being killed. By default it is derived as 0.9 × detected RAM, and a
   containerized server can misdetect that badly (503 GiB observed on a 128 GiB VM); in a
   container with *no* memory limit "detected RAM" is the whole host. Either way it never
   self-throttles. Pinning does not disable the ratio — the effective ceiling is the *lower*
   of the two, and ClickHouse logs a "lowered to" line when it clamps. Vestigo applies the
   same clamp when reading the settings back, so `scan_budget` reports the effective ceiling
   rather than the pinned one; keep the pinned value under (ratio × `mem_limit`) anyway.

   This is also **the only ceiling that bounds background merges**, which no per-query cap
   can reach — the layer that matters after an enrichment partition rewrite.

   **In an airgap install this file is at `./clickhouse/memory.xml`, relative to the install
   directory** — not `deploy/clickhouse/memory.xml`. They are the same file with two
   locations (`scripts/airgap-bundle.sh` copies one to the other), and only the
   bundle-relative one is mounted. Editing the repo path on an airgap host has no effect.

   **The file on disk is not proof of the file in effect.** A missing bind-mount source
   becomes an empty *directory*, which ClickHouse skips without complaint. Verify
   server-side, never with `grep`:

   ```sql
   SELECT name, value FROM system.server_settings
   WHERE name LIKE 'max_server_memory%' OR name LIKE '%cache_size';
   ```

   A `max_server_memory_usage` of 0 and a ratio of 0.9 mean nothing was merged.
3. **Vestigo's scan budget** (`VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`) — a *total across
   concurrent heavy scans*; each query is granted budget ÷ `VESTIGO_STAT_SCAN_CONCURRENCY`
   as its `max_memory_usage`. Must sit below (2).

Left at its `0` (auto) default, (3) is **derived from (2)**: at startup the app asks
ClickHouse what ceiling it runs under (`system.server_settings`, falling back to
`system.asynchronous_metrics`) and takes `VESTIGO_STAT_SCAN_MEMORY_RATIO` of it, reserving
the remainder for merges and caches. One number to set, read from the service it protects.

Only when that probe finds no ceiling does the app fall back to measuring its own container —
and that fallback is the trap: in a full-docker stack it reads the whole host and sizes a
budget as though ClickHouse owned the machine. On a 64 GiB host that is 64 × 0.8 ÷ 2 = **25.6
GiB granted to a single query** while three other services are resident. The app says so,
loudly, at startup and in `/api/health`.

### Checking what actually resolved

`GET /api/health` (authenticated) carries a `scan_budget` block:

```json
{"risk": "ok", "per_query_bytes": 2576980377, "total_bytes": 5153960754,
 "cache_bytes": 3758096384, "cache_breakdown": {"mark_cache_size": 2147483648,
 "index_mark_cache_size": 536870912, "primary_index_cache_size": 1073741824},
 "headroom_bytes": 1288490190, "clickhouse_ceiling_bytes": 10200547328,
 "clickhouse_ceiling_is_explicit": true, "budget_ceiling_bytes": 10200547328,
 "local_detected_bytes": 34359738368, "source": "clickhouse", "concurrency": 2,
 "pending_concurrency": null, "max_threads": 6, "max_threads_source": "clickhouse",
 "detected_cores": 12}
```

`risk` is what to act on, and it is also rendered on the admin **Settings** page above the
"Scans" group:

- `ok` — scans *and* ClickHouse's caches fit under its ceiling, with `headroom_bytes` left
  for background merges.
- `over_budget` — `total_bytes + cache_bytes` exceeds the ceiling. Lower
  `VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES`, shrink the caches in `memory.xml`, or raise
  `max_server_memory_usage`. The caches count because they are configured maxima under the
  same ceiling: at 26.6 defaults `index_mark_cache_size` and `primary_index_cache_size` are
  5 GiB *each*, which alone exceeds the ceiling the reference stack pins.
- `unbounded` — ClickHouse reports no ceiling an operator set. Nothing bounds its merges,
  caches or allocator slack, and the kernel is the only backstop. Mount `memory.xml` and set
  a container limit. The budget still uses the derived ceiling, capped by what the *app's*
  container can see — two guesses, so the lower one — which is what `budget_ceiling_bytes`
  reports when it differs from `clickhouse_ceiling_bytes`.

`max_threads` is per-*heavy*-scan thread width; the chart lane derives its own from it (above,
and `foreground.max_threads` in the same block). At `VESTIGO_STAT_SCAN_MAX_THREADS=0` (default)
it is `detected_cores ÷ concurrency`, floor 2, where `detected_cores` is what ClickHouse resolves
for itself — cgroup-CPU-quota aware, so a `--cpus=2` container reports 2.
`max_threads_source` is `pinned`, `clickhouse_pinned` (an operator pinned `max_threads` in a
ClickHouse profile — a thread limit, not a core count, so it is honoured as written and *not*
divided by `concurrency`, and `detected_cores` is `null`), `clickhouse`, or `fallback` (the
probe failed; width is the former constant 8).

`concurrency` is the size the admission gate was built with at startup, which is also the
divisor. Change `VESTIGO_STAT_SCAN_CONCURRENCY` and `pending_concurrency` names the value
waiting for a restart; until then both halves keep using the old one, so the total never
exceeds what the gate admits.

Heavy scans are admission-controlled (default 2), and the enrichment partition rewrite takes
a slot too — so the worst case is `concurrency × per-query cap`, not one query's cap. That
rewrite also holds its slot through the merges its `REPLACE PARTITION` queues
(`VESTIGO_ENRICHMENT_APPLY_MERGE_WAIT_SECONDS`, default 300; set it to 0 when ClickHouse has
a `max_server_memory_usage`, which bounds merges directly).

### Worked example: 32 GiB host, full-docker (the shipped defaults)

| Setting | Value | Where |
| --- | --- | --- |
| ClickHouse container limit | 12 GiB | `mem_limit: 12g` |
| ClickHouse server limit | 9.5 GiB | `deploy/clickhouse/memory.xml` (under 0.8 × 12 GiB) |
| ClickHouse caches (mark + index-mark + primary-index) | 3.5 GiB | `memory.xml` |
| Vestigo scan budget (total) | 4.8 GiB | auto: 0.8 × (9.5 − 3.5) GiB |
| → per-query cap | 2.4 GiB | budget ÷ concurrency (2) |
| → merge headroom | 1.2 GiB | 9.5 − 4.8 − 3.5, reported as `headroom_bytes` |
| Postgres / Qdrant / App | 4 / 4 / 4 GiB | `mem_limit` (Qdrant only with embeddings) |

That leaves roughly 8 GiB for the OS page cache. Do not spend it: ClickHouse reads compressed
parts through it, and on a large timeline it is the difference between scanning from RAM and
scanning from disk.

Scaling up follows the same shape — on a 64 GiB host, a 34 GiB ClickHouse container with a
27 GiB server limit leaves an auto budget of 0.8 × (27 − 3.5) = 18.8 GiB total, 9.4 GiB per
query, and ~12 GiB for the OS. A pinned `VESTIGO_STAT_SCAN_MAX_MEMORY_BYTES` is honoured
verbatim and **bypasses the cache subtraction** — it is a decision, not a derivation. Keep
concurrency at 2 even with a single analyst: opening the Investigate surface fires several
detectors at once, and the gate is what makes them queue instead of stack. Raising it adds no
throughput; it divides the same total into smaller, more spill-bound slices.

## Airgapped installation

Two supported routes. Pick by how the host runs Vestigo:

- **Containers → the bundle.** One tarball carries every image, the compose file and an
  installer. Nothing is built, pulled or resolved on the isolated host. Take this route
  unless you have a reason not to.
- **Native (`uv run vestigo-web`) → the carried checkout**, below.

### Route A: the deployment bundle (containers)

**Step 1 — build the bundle (connected machine),** from a checkout of the version to ship:

```bash
scripts/airgap-bundle.sh                    # app + all three backing services
scripts/airgap-bundle.sh -o /media/usb      # write straight to the drive
scripts/airgap-bundle.sh --no-embeddings    # skip the ~2 GB local embedding stack
scripts/airgap-bundle.sh --app-only         # app image only — see the upgrade caveat
```

It builds the frontend, builds the app image from that prebuilt frontend (so the isolated
host never resolves the `node:24-alpine` build image), saves every image the stack runs,
verifies the archive really holds that many, and packs everything with the compose file,
`.env.example`, `nginx-tls.conf` and `install.sh`. The compose file travels as
`compose.airgap.yml` — not a name `docker compose` auto-discovers — and only `install.sh`
renames it in the install directory, so a compose command run from the extracted bundle
cannot reach the live stack from the wrong directory. Output is
`vestigo-airgap-<version>-<commit>.tar.gz` **and** a matching `.sha256`; copy **both** — the
`.sha256` is how the far side distinguishes a bad copy from a bad bundle.

**Step 2 — carry it.** Nothing on the isolated host needs a registry, npm, a build or name
resolution. It must already have a **container engine**: Docker with the compose plugin, or
podman with podman-compose. Full bundle size is roughly 2.5–5 GB (`--no-embeddings` roughly
halves it).

**Step 3 — install or upgrade (airgapped host):**

```bash
sha256sum -c vestigo-airgap-<version>-<commit>.tar.gz.sha256
tar xzf vestigo-airgap-<version>-<commit>.tar.gz
cd vestigo-airgap-<version>-<commit>
./install.sh --check      # verify everything, change nothing, touch nothing
./install.sh
```

`install.sh` verifies its own checksums and the image count (a truncated copy fails before
anything is loaded or the running stack disturbed); picks the container engine by probing
`info`, not by finding a binary; copies the payload into the **install directory** and runs
from there; creates `.env` from the example only when there is none, rewriting exactly one
line of an existing one (`VESTIGO_IMAGE_TAG`); loads the images and then checks every image
the compose file references is present — a missing one would send compose to an unreachable
registry whose DNS timeout names nothing useful; then `compose up -d --no-build`, waits for
`/api/health`, and says plainly when the wait times out rather than reporting a success it
did not observe.

**The install directory.** The extracted bundle is throwaway; the stack runs from
`/opt/vestigo` (or `~/vestigo` when `/opt` is not writable; override with `--dir` or
`VESTIGO_INSTALL_DIR`). It holds `.env`, the compose file and the compose project — and
therefore the named volumes with all case data. That is what makes an upgrade an upgrade: a
newer bundle unpacks into a *different* directory but installs into the *same* one. Run all
later `compose` commands from there.

The compose file pins `name: vestigo` for the same reason. If a host ran Vestigo under a
different compose project name, `install.sh` notices the foreign volumes and tells you to set
`COMPOSE_PROJECT_NAME=<that prefix>` before continuing — otherwise the upgraded stack comes
up healthy, empty, and next to your data.

First install prints the two things left to do: set `VESTIGO_ADMIN_PASSWORD` in `.env` (then
`docker compose up -d app`), and put TLS in front of the app before analysts use it.

**Upgrading.** Same command, from a newer bundle. It reports `<old tag>  ->  <new tag>`,
keeps `.env`, reuses the volumes, and the app migrates on startup. `--app-only` bundles are
much smaller and are the normal choice — but only for a host a **full** bundle has already
reached, since they carry no backing-service images; `install.sh` checks and refuses rather
than letting compose try to pull. When you cannot verify the target's state before
travelling, carry the full bundle.

**Back up before an upgrade.** Nothing here deletes data, but a schema migration is not
reversible, so snapshot the volumes while stopped:

```bash
cd /opt/vestigo && docker compose down
for v in vestigo_postgres_data vestigo_clickhouse_data vestigo_qdrant_data vestigo_app_data; do
  docker run --rm -v "$v":/from -v "$PWD/backup":/to alpine \
    tar czf "/to/$v.tar.gz" -C /from .
done
```

(Restore by reversing the mounts into a freshly created volume of the same name. The `alpine`
image must already be on the host — add it to the bundle yourself, or use `docker compose cp`
from a running service.)

**Rollback.** The previous image stays loaded, so reverting the *code* is one line —
`install.sh` prints it after an upgrade:

```bash
cd /opt/vestigo
sed -i 's|^VESTIGO_IMAGE_TAG=.*|VESTIGO_IMAGE_TAG=<previous tag>|' .env
docker compose up -d app
```

If the upgrade migrated the schema, restore the backup instead.

**When something is wrong:**

- *The bundle is reported damaged* → the copy did not survive the drive. Re-copy; the
  `.sha256` tells you whether the tarball itself arrived intact.
- *The health wait times out* → probably still migrating. Raise it with
  `VESTIGO_HEALTH_TIMEOUT_SECONDS=600 ./install.sh`, or follow `docker compose logs -f app`.
- *Anything tries to reach a registry* → a bug in this path, not an operator error. Nothing
  in the bundle should resolve a name; report it.

### Route B: native install from a carried checkout

Use this when the host runs the app directly rather than in a container. **The three backing
services are out of scope for this procedure** — provision them on the airgapped network
however you normally handle offline service deployment.

On a machine **with internet access**: clone the repository, then resolve and build
everything so it is all cached locally.

```bash
uv sync --extra embeddings      # drop the extra if the deployment won't embed locally
cd frontend && npm install && npm run build && cd ..
```

Copy the whole repository — including `.venv/`, `uv.lock` and `frontend/dist/` — to a
portable drive.

On the **airgapped machine**: copy the repository over, point `VESTIGO_POSTGRES_URL`,
`VESTIGO_CLICKHOUSE_URL` and `VESTIGO_QDRANT_URL` at the running backing services, and start
the app from the carried virtualenv directly:

```bash
.venv/bin/vestigo-web
```

Not `uv run`, which would try to re-resolve the environment. Because `frontend/dist/` came
along, no network access is required at any point; `VESTIGO_ALLOW_ONLINE=false` (the default)
additionally keeps the embedding pipeline from reaching any remote endpoint. Normal offline
binary-compatibility rules apply: build and run on matching OS/architecture and glibc, since
`.venv/` carries compiled wheels.

### Patching an airgapped host in place

Carry the `git format-patch` output plus a prebuilt `frontend/dist`, `git am` the patches,
replace `frontend/dist`, restart.

For a **container** deployment this is only a stopgap — `docker cp` into a running container
survives `restart` but not `up --force-recreate` or a rebuild, so the image still holds the
old code. Follow it with a proper bundle at the next opportunity, or the fix silently
disappears the next time the stack is recreated. A rebuilt frontend is not optional for any
change under `frontend/`: the app serves `frontend/dist`, and `docker compose up -d` after a
**failed** build happily keeps serving the previous image.

### Troubleshooting a container install

Both of these were hit installing into a fresh, unprivileged **LXC** guest, and neither is a
bundle problem — the host cannot run containers yet. Fix the host and re-run the installer,
which is idempotent.

**`Error unpacking image … err: permission denied`, on every image.** The
`/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/` path in the error is the tell:
this is Docker's **containerd image store**, the default since Docker 28 and therefore what
any fresh install gets. It mounts overlay with `userxattr`, which an unprivileged LXC guest
refuses — which is why an older Docker on the same host works, having used the classic
`overlay2` graphdriver. Confirm with `docker info | grep 'Storage Driver'` (`overlayfs` is
the snapshotter, `overlay2` the graphdriver), then go back to the graphdriver:

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

**`error mounting "proc" to rootfs at "/proc": … permission denied`.** Images unpack,
containers are created, every one fails to start. runc mounts a fresh procfs, which an
unprivileged LXC guest blocks unless allowed to nest. `sudo` inside the guest changes nothing
— root there is not root on the LXC host — so fix it **on the host**, then restart the guest:

```bash
pct set <vmid> --features nesting=1 && pct reboot <vmid>   # Proxmox
lxc config set <name> security.nesting true                # LXD/Incus
```

Plain LXC: `lxc.apparmor.profile = generated` plus `lxc.apparmor.allow_nesting = 1`.
`systemd-detect-virt` confirms you are in an LXC guest; `cat /proc/self/attr/current` shows
the AppArmor profile in force.

## TLS reverse proxy (nginx)

`vestigo-web` listens on plain HTTP, `0.0.0.0:8080`, and has no TLS of its own — put nginx in
front of it for LAN/production use. Config: [`nginx-tls.conf`](nginx-tls.conf). Certbot is
out of scope (this host is airgapped/LAN-only per `TECH_STACK.md` §6); use a self-signed cert.

```bash
sudo mkdir -p /etc/nginx/tls
sudo openssl req -x509 -nodes -newkey rsa:4096 \
  -keyout /etc/nginx/tls/vestigo.key \
  -out    /etc/nginx/tls/vestigo.crt \
  -days 825 \
  -subj "/CN=vestigo.example.internal" \
  -addext "subjectAltName=DNS:vestigo.example.internal,IP:192.168.18.125"
sudo chmod 600 /etc/nginx/tls/vestigo.key

sudo cp docs/nginx-tls.conf /etc/nginx/sites-available/vestigo.conf
sudo ln -s /etc/nginx/sites-available/vestigo.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Adjust `-subj`/`subjectAltName` and the config's `server_name` and cert paths to your
hostname/IP — browsers enforce SAN matching, a bare CN is not enough. Analysts get a
self-signed warning on first visit; there is no CA issuing it.

### Required Vestigo-side settings

Once TLS terminates at nginx the app still thinks it is plain HTTP:

- `VESTIGO_AUTH_COOKIE_SECURE=1` — the session cookie's `Secure` flag defaults to `false`
  (a dev default). Behind TLS this **must** be `1`, or the cookie is sent unflagged and a
  browser would still attach it over a stray HTTP request.
- `VESTIGO_ENVIRONMENT=production` — disables uvicorn's auto-reload watcher.
- With OIDC enabled, update `VESTIGO_OIDC_REDIRECT_URL` to the `https://` form — the IdP
  redirect target must match what nginx exposes, not `http://localhost:8080`.
- With the external `/mcp` endpoint enabled, set `VESTIGO_PUBLIC_BASE_URL` to the same
  outside-facing origin (`https://vestigo.example.org`). Links Vestigo hands an MCP client —
  the Visualize deep link a chart comes back with — are relative paths otherwise, and a
  client that is not the browser has no origin to complete one against. Include the scheme:
  a bare `vestigo.example.org` *is* a relative URL, so it is rejected at set-time rather than
  producing links that look absolute and resolve as paths. Vestigo does not infer the value
  from `Host`/`X-Forwarded-*`: those are whatever the proxy sent, and a confidently wrong
  link is worse than a relative one.

### Notes on the proxy config

- `client_max_body_size 200G` — raised from nginx's 1 MiB default; source uploads can be
  large. Lower it to whatever your largest expected source needs.
- The SSE live-update stream gets a dedicated regex location with `proxy_buffering off` —
  buffering would batch events until nginx's buffer fills, defeating a live feed. It is
  scoped to the exact `/stream` path (not all of `/api/cases/`) so the large source-upload
  endpoint keeps the 300s timeouts from `location /` instead of nginx's 60s defaults. A 20s
  server-side keepalive is built in, so `proxy_read_timeout 3600s` need only outlive several
  keepalives, not be infinite.
- `X-Forwarded-For` is forwarded for logging only — Vestigo deliberately does **not** trust
  it for access-control decisions, since this runs on a LAN where the header would otherwise
  be attacker-controlled.
- No websocket handling is configured — the app has no WebSocket routes (SSE only).

## Operational scale

Vestigo does not care how many analysts share an instance — case-level RBAC, teams and the
audit trail work the same at any headcount. What *is* bounded is the process topology: **run
exactly one app process per instance.**

Five subsystems keep state in that process's memory, so a second worker would not share it:

| State | Module | What a second process breaks |
|---|---|---|
| Background jobs (ingest, embed, transfer) | `core/jobs.py` | A job started on worker A is invisible to a status poll landing on worker B |
| Live-collaboration pub/sub (SSE) | `core/events_bus.py` | Subscribers only see changes made by their own worker |
| Failed-login backoff | `core/login_backoff.py` | Effective lockout threshold multiplies by the worker count |
| Visualization baseline cache | `db/viz_cache.py` | Correct, just colder — each worker warms its own |
| Merged settings cache | `core/config.py` | An admin-console change reaches only the worker that served the save |

The transfer temp-path startup sweep likewise assumes one process per configured path.

So do not pass `--workers`/`--reload` to uvicorn, and do not run two app containers against
one database. To serve more concurrent analysts, scale the box (the API is async and
I/O-bound; heavy scans are admission-controlled and transfers capped by
`VESTIGO_TRANSFER_MAX_CONCURRENT`) and scale ClickHouse, which is where query cost lives.
Nothing here caps **data** volume — the reference case is 300M rows and single timelines run
to 80 GiB+. Scaling the box without raising the memory ceilings is how you get an OOM-killed
ClickHouse; see [Resource sizing](#resource-sizing).

Multi-process scale-out is possible but unbuilt: it means moving those state holders to a
shared backend and re-opening the standing decisions priced against a single trusted process
— CSRF tokens and the full-user-directory listing (`ROADMAP.md`). A milestone, not a flag.

## The demo case

New users are seeded a fabricated example investigation on first login — 251k events across
four sources, with notes, tags, saved views, a baseline definition, four Sigma rules and a
story already in place. `ANOMALY_DETECTION.md` describes what it contains and why.

- **It is generated, not shipped.** The generator (`src/vestigo/demo/`) runs per user as a
  background job: ~2.5s to fabricate the source files and a few seconds more to ingest them
  through the normal pipeline. Deterministic, so every copy is identical down to the sources'
  SHA-256 hashes.
- **It costs CPU while it runs** — a quarter of a million events is CPU-bound Python, so a
  build competes with the rest of the app. One runs at a time by default
  (`VESTIGO_DEMO_MAX_CONCURRENT=1`); raise it on a box with cores to spare, or set 0 to
  remove the cap. A first login that finds the cap full is not seeded and not marked as
  seeded — it tries again at the next login. An explicit restore gets a 429 instead.
- **It costs storage per user** — ~251k events each, about 12.5M rows across 50 users. Turn
  seeding off on large instances, or where fabricated data in a case list is a policy
  problem: `VESTIGO_DEMO_CASE_ENABLED=false`. Demo cases are flagged (`cases.is_demo`), and
  administrators do not see other users' copies in their case list.
- **Seeding happens once per user, ever.** The stamp (`users.demo_case_seeded_at`) survives
  the user deleting the case, so a deleted demo stays deleted. Users can request a fresh copy
  from the case list — the only way it comes back, and only while they do not already have
  one (409 otherwise). Turning the setting off stops future seeding and leaves existing
  copies alone; upgrading an instance backfills existing users at their next login.

## ClickHouse log growth

The stock ClickHouse image logs at `trace` with a `1000M` × `10` rotation policy, and the
reference stack mounts a volume for `/var/lib/clickhouse` but not for
`/var/log/clickhouse-server`. Debug logs therefore accumulate — up to ~11 GB — inside the
container's **writable layer**, alongside several system telemetry tables (`trace_log`,
`text_log`, `metric_log`) that grow without bound on the data volume. Nothing in Vestigo
reads any of it.

On a host with room to spare this is waste. On a host with a storage quota it is an outage,
because ClickHouse does not degrade gracefully when a log write fails: the write fails once
(`ENOSPC`, or `EDQUOT` under a quota), the `ofstream` latches its failbit and C++ streams do
not recover, the pre-message size check for rotation then throws `Poco::Exception … File
access error`, and trying to log *that* requires the same check, which throws again. The
logging thread spins and the server stops answering. The give-away is the nesting: `Cannot
log message in OwnAsyncSplitChannel channel: Cannot log message in …`, repeating one stack
trace through `RotateBySizeStrategy::mustRotate`. Nothing has crashed — a restart clears it.

`scripts/clickhouse-log-recovery.sh` caps the logger (`information`, `100M` × `3`), turns off
the unbounded telemetry tables, puts a 14-day TTL on `query_log` and `part_log` (worth
keeping when an ingest misbehaves) and recreates the container. The recreate is what reclaims
the space: it discards the writable layer where the logs live, while `/var/lib/clickhouse` is
a named volume and survives. The script refuses to run if that is not true of your deployment.

```bash
./scripts/clickhouse-log-recovery.sh --dry-run              # report sizes, change nothing
./scripts/clickhouse-log-recovery.sh                        # apply, ~1 minute of downtime
./scripts/clickhouse-log-recovery.sh --truncate-system-logs # also drop rows already written
```

It works with Docker or Podman, never contacts a registry, backs up and validates its compose
edit, and confirms the `vestigo` database is intact afterwards. Ingestion and embedding jobs
are in-memory and will not survive the restart — check the job tray is idle first.

### When the limit is a quota, not the disk

`Disk quota exceeded` (`EDQUOT`) is not `No space left on device` (`ENOSPC`), and the
difference decides whether capping the logs is a fix or only a delay. A quota enforced
outside the container is invisible to `df` inside it — most sharply on ZFS, where `quota`
counts snapshots and `refquota` does not, so `df` reports the refquota view while a snapshot
backlog exhausts the real ceiling. Check from the storage layer (`zfs get
quota,refquota,usedbysnapshots <dataset>`, `xfs_quota -x -c 'report -h'`, `repquota -s`,
`lvs` for thin pools), not from the guest.

One check does work from inside, because ClickHouse asks the filesystem the same way its
writes do:

```bash
docker exec <clickhouse> clickhouse-client --query \
  "SELECT name, formatReadableSize(free_space) FROM system.disks"
```

Monitor that rather than `df`, which reports healthy throughout this failure.

## On-disk state outside the databases

Two directories hold case data on the app host itself, both `VESTIGO_*`-configurable:

- `VESTIGO_SOURCE_RETENTION_PATH` (default `data/sources`) — content-addressed copies of
  every uploaded source file, shared instance-wide and sharded by hash prefix.
- `VESTIGO_TRANSFER_TEMP_PATH` (default `data/transfer`) — in-flight case export archives.
  An archive is a **complete** case (optionally including the original source blobs), so
  treat this directory as exactly as sensitive as the retention path. Vestigo creates it
  `0700`, forces that mode on an existing directory, and refuses to export if the path is
  owned by another user or is not a real directory — do not point it at a shared `/tmp`.
  Size it for the largest single case you expect to export, times
  `VESTIGO_TRANSFER_MAX_CONCURRENT`.

  Archives are removed as soon as they are downloaded; anything left by an interrupted
  download is expired 24 hours later by the next export, and leftovers are cleared at
  startup. That sweep assumes **one app process per configured path**. It removes only what
  a transfer job writes and logs a warning about anything else it finds, so pointing the
  setting at a populated directory costs that directory nothing. Give it its own directory
  regardless.

Import is open to any authenticated user and restores as a new case owned by them. Because an
uploaded archive is untrusted input, two size caps apply, both checked against the manifest
before a single member is read:

- `VESTIGO_TRANSFER_MAX_EXPANDED_BYTES` (default 200 GiB, `0` disables) caps the archive's
  **total** uncompressed size. Events and blobs travel uncompressed, so a real export expands
  by roughly 1x; far above that is a decompression bomb rather than a big case.
- `VESTIGO_TRANSFER_MAX_METADATA_BYTES` (default 2 GiB, `0` disables) caps any **single**
  `postgres/*` member — a lone 100 GiB metadata member would fit comfortably under a 200 GiB
  total. These hold case metadata (rows, not events), so a genuine export stays far below it.

`VESTIGO_TRANSFER_MAX_CONCURRENT` (default 2, `0` disables) caps in-flight export and import
jobs across the instance. Both directions reserve real disk for the whole job and any
authenticated user can start either, so this is admission control rather than a throughput
knob; an import over the cap is rejected with 429 *before* its upload is accepted. Transfers
hash, zip and verify in worker threads, so a multi-GiB job does not stall the API — but they
remain CPU- and disk-bound, which is what makes the cap matter.

Restored `audit_log` rows keep the actor, action and timestamp the archive asserted — that is
the point of exporting them — but nothing on the importing instance vouches for any of it.
Every imported row carries `detail.imported` (import job id, importing user, source case id)
and is badged **imported** in the admin audit view. When reviewing a user's activity, treat
badged rows as claims made by whoever uploaded the archive, not as locally recorded events.

## Stability & upgrades

What the 1.x line guarantees, and what it does not:

- **PostgreSQL metadata schema** is Alembic-managed; the app migrates to head automatically
  on startup. Upgrading is: stop, update code/image, start.
- **Parquet interchange format v1** (converter output) is stable: files produced by any 1.x
  converter remain ingestible by any 1.x server. Pre-rename (`*2tracesignal.py`) converter
  output is still accepted.
- **Forensic identity is append-only**: parser/embedding `config_hash()` values identify
  processing configurations, and existing hashes never change meaning within 1.x.
- **ClickHouse and Qdrant schemas** have no in-place migration story yet: within 1.x they
  will not change; a future change would come with an explicit re-ingest/re-embed procedure
  in the release notes, never a silent one.
- The REST API is versioned by the app itself (`/api/health` reports the version); breaking
  API changes are reserved for 2.0.

**Event ids for sources containing invalid UTF-8 changed.** `byte_offset` used to be measured
over `errors="replace"`-decoded text, which over-counts by two bytes per undecodable byte, so
every offset after the first bad byte was wrong — and `byte_offset` feeds `derive_event_id`.
Offsets are now measured over the file's real bytes. **Already-ingested data is unaffected**:
ids are derived once at ingest and nothing recomputes them. The change is visible only if you
*re-ingest* a file containing invalid UTF-8, where the new ids will not match the old ones, so
annotations recorded against the previous ingest will not carry over. Sources are immutable,
so the safe procedure is to treat a re-ingest as a new source — which is what the model
already does.

**Upgrading from a pre-1.0 (TraceSignal) deployment:** the project was renamed for 1.0 — CLI
`tsig` → `vestigo`, env vars `TS_*` → `VESTIGO_*`, and default backing-store names are now
`vestigo`. Existing data stays where it is: rename your env vars and pin the old names via
`VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_DATABASE` and
`VESTIGO_QDRANT_COLLECTION_PREFIX`. See [CHANGELOG.md](../CHANGELOG.md).
