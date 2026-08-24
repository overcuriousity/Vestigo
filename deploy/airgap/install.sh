#!/usr/bin/env bash
# Install or upgrade Vestigo on an airgapped host from this bundle.
#
#     ./install.sh                  # install, or upgrade an existing install
#     ./install.sh --check          # verify the bundle and report, change nothing
#     ./install.sh --dir /srv/vestigo   # install somewhere other than the default
#
# The bundle directory is throwaway: everything the running stack needs is
# copied into an **install directory** that stays put across upgrades
# (/opt/vestigo when writable, else ~/vestigo; --dir or VESTIGO_INSTALL_DIR
# override it). That is what makes "extract a newer bundle and run this again"
# an upgrade rather than a second, empty installation — the .env, the compose
# project name and therefore the named volumes all live there and are reused.
#
# VESTIGO_HEALTH_TIMEOUT_SECONDS (default 120) sets how long the final health
# wait watches for /api/health before reporting that it gave up.
#
# Safe to re-run: loading images and `compose up -d` are both idempotent, and an
# existing .env is never overwritten. Data lives in named volumes, which are
# never touched here — the only way this removes case data is if you delete
# those volumes yourself.
set -euo pipefail

BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$BUNDLE/$(basename "${BASH_SOURCE[0]}")"   # absolute: --help reads it after the cd
cd "$BUNDLE"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# ── arguments ───────────────────────────────────────────────────────────────
# Unknown arguments are fatal: an installer that treats `--dry-run` as "install
# everything" is worse than one that has no such flag.
CHECK_ONLY=0
INSTALL_DIR="${VESTIGO_INSTALL_DIR:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --dir) shift; INSTALL_DIR="${1:?--dir needs a path}" ;;
    --dir=*) INSTALL_DIR="${1#--dir=}" ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

# ── integrity, before anything else and without needing an engine ───────────
if command -v sha256sum >/dev/null; then
  say "verifying bundle checksums"
  sha256sum -c --quiet MANIFEST.sha256 || die "bundle is damaged — re-copy it from the drive"
else
  warn "sha256sum not found; skipping integrity check"
fi

[ -f bundle.env ] || die "bundle.env missing — this is not a complete bundle"
[ -f images.list ] || die "images.list missing — this is not a complete bundle"
# shellcheck disable=SC1091
. ./bundle.env
say "Vestigo $VESTIGO_VERSION ($VESTIGO_COMMIT), image tag $VESTIGO_IMAGE_TAG, ${VESTIGO_BUNDLE_SCOPE:-full} bundle"

# The image archive carries its own inventory. Counting it here catches a short
# save (see the -m note in scripts/airgap-bundle.sh) on the side that can still
# do something about it — before any image is loaded or the stack is restarted.
ARCHIVE_COUNT="$(tar xOf images/vestigo-stack.tar manifest.json 2>/dev/null \
  | grep -o '"RepoTags"' | wc -l)"
EXPECTED_COUNT="${VESTIGO_IMAGE_COUNT:-0}"
[ "$ARCHIVE_COUNT" = "$EXPECTED_COUNT" ] || die \
  "image archive holds $ARCHIVE_COUNT image(s), bundle.env expects $EXPECTED_COUNT — rebuild the bundle"
say "image archive holds $ARCHIVE_COUNT image(s), as declared"

# ── engine ──────────────────────────────────────────────────────────────────
# `info`, not `--version`: a docker binary whose daemon is unreachable (no
# permission on the socket, daemon not running) would otherwise be selected and
# fail several GB into loading images.
ENGINE=""; COMPOSE=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1 \
   && docker compose version >/dev/null 2>&1; then
  ENGINE=docker; COMPOSE="docker compose"
elif command -v podman >/dev/null && podman info >/dev/null 2>&1 \
     && podman compose version >/dev/null 2>&1; then
  ENGINE=podman; COMPOSE="podman compose"
fi

if [ "$CHECK_ONLY" = 1 ]; then
  if [ -n "$ENGINE" ]; then
    say "container engine: $COMPOSE"
  else
    warn "no usable container engine found — install would fail here"
  fi
  say "bundle is complete and consistent — nothing changed (--check)"
  exit 0
fi

[ -n "$ENGINE" ] || die \
  "need docker with the compose plugin, or podman with podman-compose (and a reachable daemon)"
say "using $COMPOSE"

# ── install directory ───────────────────────────────────────────────────────
# An upgrade extracts a new bundle directory. Running the stack out of *that*
# would give compose a new project name, hence new empty volumes, hence an
# install that looks fine and has no cases in it. So the stack always runs from
# a stable directory instead, and this bundle is only its source.
if [ -z "$INSTALL_DIR" ]; then
  if [ -d /opt/vestigo ] || mkdir -p /opt/vestigo 2>/dev/null; then
    INSTALL_DIR=/opt/vestigo
  else
    INSTALL_DIR="$HOME/vestigo"
  fi
fi
mkdir -p "$INSTALL_DIR" || die "cannot create $INSTALL_DIR (try --dir, or run as root)"
[ -w "$INSTALL_DIR" ] || die "$INSTALL_DIR is not writable"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"

if [ -f "$INSTALL_DIR/.env" ]; then
  PREVIOUS_TAG="$(sed -n 's/^VESTIGO_IMAGE_TAG=//p' "$INSTALL_DIR/.env" | head -1)"
  say "upgrading the existing install in $INSTALL_DIR"
  say "  ${PREVIOUS_TAG:-<unknown>}  ->  $VESTIGO_IMAGE_TAG"
  echo ""
  say "case data lives in this stack's named volumes and is not touched here."
  say "to snapshot it first, stop the stack and copy the volumes — see"
  say "docs/DEPLOYMENT.md §\"Backup before an upgrade\"."
  echo ""
else
  PREVIOUS_TAG=""
  say "fresh install into $INSTALL_DIR"
fi

# ── images ──────────────────────────────────────────────────────────────────
# Loaded and checked *before* the install directory is touched, so a bundle that
# cannot produce a working stack leaves the existing install exactly as it was —
# same .env, same image tag, same running containers.
# `load` reports per-image failures on stderr and still exits 0. Observed on a
# fresh Docker 29 in an unprivileged LXC: every layer failed to extract
# ("apply layer error … err: permission denied"), four "Error unpacking image"
# lines went by, the exit status was 0, and `image inspect` was satisfied
# afterwards because `load` registers an image's *metadata* before unpacking
# its layers. The installer therefore believed all four images were present,
# copied the payload, and started a stack in which nothing could run. Trusting
# an exit status is the same mistake `podman save` taught us one layer up, so:
# capture the output and treat any error line as fatal.
say "loading images (slow — several GB)"
LOAD_LOG="$(mktemp)"
trap 'rm -f "$LOAD_LOG"' EXIT
if ! $ENGINE load -i "$BUNDLE/images/vestigo-stack.tar" 2>&1 | tee "$LOAD_LOG"; then
  die "loading images failed — nothing was changed or started."
fi
LOAD_ERRORS="$(grep -iE 'error unpacking|apply layer error|failed to extract|^error' "$LOAD_LOG" || true)"
if [ -n "$LOAD_ERRORS" ]; then
  printf 'error: the container engine could not unpack the images it just registered:\n' >&2
  printf '%s\n' "$LOAD_ERRORS" | sed 's/^/  /' >&2
  die "images loaded but did not unpack — nothing was changed or started. This is a host storage/permission problem, not a damaged bundle; see docs/DEPLOYMENT.md §Troubleshooting."
fi

# Every image compose is about to reference must now exist locally *and be
# usable*. Without the first, a missing image sends compose to a registry that
# is not there — the exact failure this bundle exists to prevent, surfacing as
# a confusing DNS timeout instead of "this --app-only bundle needs a host that
# already has the services". Without the second, a half-loaded image passes and
# fails at `up` instead.
image_usable() {
  # `image inspect` reads metadata, which survives a failed unpack. Creating a
  # container additionally makes the engine prepare a snapshot from the image's
  # layers, which is the part that was actually missing. The command is never
  # run — `create` only records it — so a bogus one is fine and avoids the
  # "no command specified" error on an image without a CMD.
  "$ENGINE" image inspect "$1" >/dev/null 2>&1 || return 1
  local probe
  probe="$("$ENGINE" create "$1" /nonexistent-vestigo-probe 2>/dev/null)" || return 1
  [ -z "$probe" ] || "$ENGINE" rm -f "$probe" >/dev/null 2>&1 || true
  return 0
}

say "checking every image the stack references is present and usable"
MISSING=""
while read -r img; do
  [ -n "$img" ] || continue
  image_usable "$img" || MISSING="$MISSING $img"
done < <(sed -n 's/^ *image: \(docker\.io[^$]*\)$/\1/p' "$BUNDLE/compose.airgap.yml")
# Must match `image:` for the app service in the compose file byte for byte,
# including the `localhost/` registry component — see the comment there.
image_usable "localhost/vestigo-app:$VESTIGO_IMAGE_TAG" \
  || MISSING="$MISSING localhost/vestigo-app:$VESTIGO_IMAGE_TAG"

if [ -n "$MISSING" ]; then
  printf 'error: missing image(s) after load:\n' >&2
  printf '  %s\n' $MISSING >&2
  if [ "${VESTIGO_BUNDLE_SCOPE:-full}" = "app-only" ]; then
    die "this is an --app-only bundle and this host does not have the backing-service images. Bring a full bundle (scripts/airgap-bundle.sh with no --app-only); nothing was changed or started."
  fi
  die "the bundle did not load completely — re-copy it and re-run; nothing was changed or started."
fi

# Bundle-owned files: refreshed on every run, because they are what carries the
# new version. The operator's .env is the one thing that is not.
say "copying bundle payload into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/clickhouse"
# Named `compose.airgap.yml` in the bundle and `docker-compose.yml` only in the
# install directory. Compose auto-discovers the four canonical names, so a copy
# called `docker-compose.yml` sitting in the bundle means `docker compose ps`
# run from the extracted bundle silently targets the real project (`name:
# vestigo` is pinned in the file) with no `.env` loaded beside it — an operator
# gets "required variable VESTIGO_IMAGE_TAG is missing" at best, and acts on
# the live stack from the wrong directory at worst. This name is not one compose
# looks for, so the bundle directory has no runnable stack in it.
cp compose.airgap.yml "$INSTALL_DIR/docker-compose.yml"
cp clickhouse/allow-default-network.xml "$INSTALL_DIR/clickhouse/"
cp .env.example "$INSTALL_DIR/.env.example"
cp bundle.env BUNDLE-INFO images.list "$INSTALL_DIR/"
if [ -f nginx-tls.conf ]; then cp nginx-tls.conf "$INSTALL_DIR/"; fi
cd "$INSTALL_DIR"

# ── configuration ───────────────────────────────────────────────────────────
# An existing .env is authoritative: this is an upgrade, and silently rewriting
# an operator's configuration is the last thing an installer should do. Only
# the image tag is updated, because it is what selects the code being run.
if [ ! -f .env ]; then
  say "creating .env from .env.example"
  cp .env.example .env
  {
    echo ""
    echo "# --- written by install.sh ---"
    echo "VESTIGO_ENVIRONMENT=production"
    echo "VESTIGO_ALLOW_ONLINE=false"
  } >> .env
  NEW_ENV=1
else
  say "keeping existing .env"
  NEW_ENV=0
fi

# VESTIGO_IMAGE_TAG selects which loaded image compose runs. Rewritten on every
# run so an upgrade takes effect; everything else in .env is left alone.
if grep -q '^VESTIGO_IMAGE_TAG=' .env; then
  sed -i "s|^VESTIGO_IMAGE_TAG=.*|VESTIGO_IMAGE_TAG=$VESTIGO_IMAGE_TAG|" .env
else
  echo "VESTIGO_IMAGE_TAG=$VESTIGO_IMAGE_TAG" >> .env
fi

# ── volume continuity ───────────────────────────────────────────────────────
# The compose file pins `name: vestigo`; COMPOSE_PROJECT_NAME in .env overrides
# it, which is how a host that once ran the stack under another project name
# adopts its existing data. Detect that case rather than starting empty.
PROJECT="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env | head -1)"
PROJECT="${PROJECT:-vestigo}"
if ! "$ENGINE" volume inspect "${PROJECT}_postgres_data" >/dev/null 2>&1; then
  FOREIGN="$("$ENGINE" volume ls --format '{{.Name}}' 2>/dev/null \
    | grep -E '_postgres_data$' || true)"
  if [ -n "$FOREIGN" ]; then
    warn "this host already has Postgres volume(s) from another compose project:"
    printf '  %s\n' $FOREIGN >&2
    warn "starting now would create empty '${PROJECT}_*' volumes beside them."
    warn "if one of those holds your Vestigo data, stop here and put"
    warn "COMPOSE_PROJECT_NAME=<its prefix> in $INSTALL_DIR/.env, then re-run."
    echo ""
  fi
fi

# ── start ───────────────────────────────────────────────────────────────────
say "starting the stack"
$COMPOSE up -d --no-build

# ── wait for health, and say so honestly if it never comes ──────────────────
# Strip quotes and any trailing comment: this value came out of a file an
# operator edits by hand.
PORT="$(sed -n 's/^VESTIGO_PORT=//p' .env | head -1 | sed 's/#.*//; s/["'"'"']//g; s/[[:space:]]//g')"
PORT="${PORT:-8080}"
# Slow hardware plus a large schema migration can outlast the default. Raising
# this changes only how long we watch — the stack starts either way.
HEALTH_TIMEOUT="${VESTIGO_HEALTH_TIMEOUT_SECONDS:-120}"
ATTEMPTS=$((HEALTH_TIMEOUT / 2))
[ "$ATTEMPTS" -ge 1 ] || ATTEMPTS=1
say "waiting up to ${HEALTH_TIMEOUT}s for the app to answer on :$PORT"
ready=0
for _ in $(seq 1 "$ATTEMPTS"); do
  if command -v curl >/dev/null; then
    curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && { ready=1; break; }
  else
    # No curl on minimal hosts; bash's /dev/tcp is always there. Weaker signal —
    # the port listens before migrations finish.
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { ready=1; break; }
  fi
  sleep 2
done

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-localhost}"

echo ""
if [ "$ready" = 1 ]; then
  say "Vestigo $VESTIGO_VERSION is up: http://$HOST_IP:$PORT"
  say "installed in $INSTALL_DIR — run compose commands from there"
else
  warn "the app did not answer within ${HEALTH_TIMEOUT}s. It is not necessarily broken —"
  warn "first start runs database migrations. Check:"
  warn "  cd $INSTALL_DIR && $COMPOSE logs -f app"
fi

if [ -n "$PREVIOUS_TAG" ] && [ "$PREVIOUS_TAG" != "$VESTIGO_IMAGE_TAG" ]; then
  echo ""
  say "ROLLBACK, if this version misbehaves:"
  echo "  cd $INSTALL_DIR"
  echo "  sed -i 's|^VESTIGO_IMAGE_TAG=.*|VESTIGO_IMAGE_TAG=$PREVIOUS_TAG|' .env"
  echo "  $COMPOSE up -d app"
  echo "  (the previous image is still loaded on this host. Schema migrations are"
  echo "   not reversed — restore a volume backup if the upgrade migrated data.)"
fi

if [ "${NEW_ENV:-0}" = 1 ]; then
  echo ""
  say "FIRST INSTALL — do these two things now:"
  echo "  1. Set VESTIGO_ADMIN_PASSWORD in $INSTALL_DIR/.env, then:"
  echo "     cd $INSTALL_DIR && $COMPOSE up -d app"
  echo "     (the default bootstrap credential must be rotated on first login)"
  echo "  2. Put TLS in front of it before analysts use it — nginx-tls.conf is"
  echo "     in this bundle, and docs/DEPLOYMENT.md walks through it."
fi
