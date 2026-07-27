#!/usr/bin/env bash
# Install or upgrade Vestigo on an airgapped host from this bundle.
#
#     ./install.sh              # install, or upgrade an existing install
#     ./install.sh --check      # verify the bundle and report, change nothing
#
# Safe to re-run: loading images and `compose up -d` are both idempotent, and
# an existing .env is never overwritten. Data lives in named volumes, which are
# never touched here — the only way this removes case data is if you delete
# those volumes yourself.
set -euo pipefail

BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BUNDLE"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# ── engine ──────────────────────────────────────────────────────────────────
# `info`, not `--version`: a docker binary whose daemon is unreachable (no
# permission on the socket, daemon not running) would otherwise be selected and
# fail several GB into loading images.
if command -v docker >/dev/null && docker info >/dev/null 2>&1 \
   && docker compose version >/dev/null 2>&1; then
  ENGINE=docker; COMPOSE="docker compose"
elif command -v podman >/dev/null && podman info >/dev/null 2>&1 \
     && podman compose version >/dev/null 2>&1; then
  ENGINE=podman; COMPOSE="podman compose"
else
  die "need docker with the compose plugin, or podman with podman-compose (and a usable daemon)"
fi
say "using $COMPOSE"

# ── integrity ───────────────────────────────────────────────────────────────
if command -v sha256sum >/dev/null; then
  say "verifying bundle checksums"
  sha256sum -c --quiet MANIFEST.sha256 || die "bundle is damaged — re-copy it"
else
  warn "sha256sum not found; skipping integrity check"
fi

[ -f bundle.env ] || die "bundle.env missing — this is not a complete bundle"
# shellcheck disable=SC1091
. ./bundle.env
say "Vestigo $VESTIGO_VERSION ($VESTIGO_COMMIT), image tag $VESTIGO_IMAGE_TAG"

if [ "$CHECK_ONLY" = 1 ]; then
  say "bundle is complete and consistent — nothing changed (--check)"
  exit 0
fi

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

# ── images ──────────────────────────────────────────────────────────────────
say "loading images (slow — several GB)"
$ENGINE load -i images/vestigo-stack.tar

# ── start ───────────────────────────────────────────────────────────────────
say "starting the stack"
$COMPOSE up -d --no-build

# ── wait for health, and say so honestly if it never comes ──────────────────
PORT="$(sed -n 's/^VESTIGO_PORT=\(.*\)/\1/p' .env | head -1)"
PORT="${PORT:-8080}"
say "waiting for the app to answer on :$PORT"
ready=0
for _ in $(seq 1 60); do
  if command -v curl >/dev/null; then
    curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && { ready=1; break; }
  else
    # No curl on minimal hosts; bash's /dev/tcp is always there.
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { ready=1; break; }
  fi
  sleep 2
done

echo ""
if [ "$ready" = 1 ]; then
  say "Vestigo is up: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
else
  warn "the app did not answer within 120s. It is not necessarily broken —"
  warn "first start runs database migrations. Check: $COMPOSE logs -f app"
fi

if [ "${NEW_ENV:-0}" = 1 ]; then
  echo ""
  say "FIRST INSTALL — do these two things now:"
  echo "  1. Set VESTIGO_ADMIN_PASSWORD in .env, then: $COMPOSE up -d app"
  echo "     (the default bootstrap credential must be rotated on first login)"
  echo "  2. Put TLS in front of it before analysts use it — nginx-tls.conf is"
  echo "     in this bundle, and docs/DEPLOYMENT.md walks through it."
fi
