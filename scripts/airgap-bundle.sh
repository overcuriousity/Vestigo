#!/usr/bin/env bash
# Build a self-contained Vestigo deployment bundle on a machine WITH network
# access. The resulting tarball installs and upgrades an airgapped host with no
# registry, no npm, and no build step on the far side.
#
#     scripts/airgap-bundle.sh                    # app + backing services
#     scripts/airgap-bundle.sh --app-only         # app image only (services already there)
#     scripts/airgap-bundle.sh --no-embeddings    # skip the ~2 GB torch install
#     scripts/airgap-bundle.sh -o /media/usb      # write the tarball straight to the drive
#
# What it does: builds the frontend, builds the app image from the prebuilt
# frontend (so the far side never needs node), saves every image the stack
# runs, and packs them with the compose file and `install.sh`.
#
# --app-only produces a much smaller tarball but only installs on a host that
# already has the three backing-service images loaded — install.sh checks for
# them and refuses rather than letting compose try to pull. Use it for repeat
# upgrades of a host a full bundle already reached; use the full bundle when in
# doubt, or when you cannot inspect the target before travelling.
#
# Verified end to end by `tests/test_airgap_bundle.py`, which asserts the
# bundle's contents rather than its size.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$REPO/scripts/$(basename "${BASH_SOURCE[0]}")"   # absolute: --help reads it after the cd
cd "$REPO"

OUT_DIR="$REPO"
APP_ONLY=0
INSTALL_EMBEDDINGS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --app-only) APP_ONLY=1 ;;
    --no-embeddings) INSTALL_EMBEDDINGS=0 ;;
    -o) shift; OUT_DIR="${1:?-o needs a directory}" ;;
    -o*) OUT_DIR="${1#-o}" ;;
    # Print the header block itself, so help can never drift from it.
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Present is not the same as usable: a docker binary with no reachable daemon
# (rootless hosts, permission-denied on the socket) must not win over a working
# podman, or the build dies three steps later with a confusing error.
ENGINE=""
for candidate in docker podman; do
  if command -v "$candidate" >/dev/null && "$candidate" info >/dev/null 2>&1; then
    ENGINE="$candidate"
    break
  fi
done
[ -n "$ENGINE" ] || die "no usable container engine (tried docker, podman)"
command -v npm >/dev/null || die "npm not found — the frontend is built here, not on the target"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
[ -n "$VERSION" ] || die "could not read version from pyproject.toml"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
TAG="${VERSION}-${COMMIT}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
BUNDLE="$STAGE/vestigo-airgap-$TAG"
mkdir -p "$BUNDLE/images" "$BUNDLE/clickhouse"

say "version $VERSION, commit $COMMIT -> image tag vestigo-app:$TAG"

# ── 1. frontend, built here so the target needs no node ─────────────────────
say "building frontend"
(cd frontend && npm ci --no-audit --no-fund && npm run build)
[ -f frontend/dist/index.html ] || die "frontend build produced no dist/index.html"

# ── 2. app image, from the prebuilt dist ────────────────────────────────────
say "building app image (embeddings=$INSTALL_EMBEDDINGS)"
"$ENGINE" build \
  --build-arg FRONTEND_STAGE=frontend-prebuilt \
  --build-arg "INSTALL_EMBEDDINGS=$INSTALL_EMBEDDINGS" \
  --target app -t "vestigo-app:$TAG" -f Dockerfile .

# ── 3. every image the stack runs ───────────────────────────────────────────
IMAGES=("vestigo-app:$TAG")
if [ "$APP_ONLY" = 0 ]; then
  # Read the tags out of the compose file rather than repeating them here:
  # one place to bump a backing service.
  while read -r img; do
    if [ -n "$img" ]; then IMAGES+=("$img"); fi
  done < <(sed -n 's/^ *image: \(docker\.io[^$]*\)$/\1/p' deploy/airgap/docker-compose.airgap.yml)
fi
for img in "${IMAGES[@]}"; do
  case "$img" in
    vestigo-app:*) ;;
    *) say "pulling $img"; "$ENGINE" pull "$img" ;;
  esac
done

say "saving ${#IMAGES[@]} image(s) — this is the slow part"
# podman needs -m for more than one image. WITHOUT it, podman does not fail: it
# reads the extra arguments as additional *tags for the first image* and writes
# a single-image archive carrying all four names. `podman load` on the far side
# then produces a `postgres:17-alpine` that is actually qdrant — an airgapped
# host, a wrong image, and no error anywhere along the way.
SAVE_OPTS=()
if [ "$ENGINE" = podman ] && [ "${#IMAGES[@]}" -gt 1 ]; then
  SAVE_OPTS+=(-m)
fi
"$ENGINE" save "${SAVE_OPTS[@]+"${SAVE_OPTS[@]}"}" \
  -o "$BUNDLE/images/vestigo-stack.tar" "${IMAGES[@]}"

# Verify what landed in the archive rather than trusting the exit status: this
# is the last moment the connected side can catch a short save, and `install.sh`
# repeats the same count check before it touches anything.
SAVED="$(tar xOf "$BUNDLE/images/vestigo-stack.tar" manifest.json 2>/dev/null \
  | grep -o '"RepoTags"' | wc -l)"
[ "$SAVED" = "${#IMAGES[@]}" ] || die \
  "archive holds $SAVED image(s), expected ${#IMAGES[@]} — refusing to ship a short bundle"

# ── 4. everything else the target needs ─────────────────────────────────────
# Deliberately *not* one of the four names compose auto-discovers
# (compose.yaml/yml, docker-compose.yaml/yml): install.sh renames it on the way
# into the install directory, so the extracted bundle has no stack an operator
# can accidentally drive from the wrong directory. See install.sh's copy step.
cp deploy/airgap/docker-compose.airgap.yml "$BUNDLE/compose.airgap.yml"
cp deploy/airgap/install.sh "$BUNDLE/install.sh"
cp deploy/clickhouse/allow-default-network.xml "$BUNDLE/clickhouse/"
cp .env.example "$BUNDLE/.env.example"
# Not optional: docs/DEPLOYMENT.md tells the operator this file is in the bundle,
# and TLS is the last step of a first install.
cp docs/nginx-tls.conf "$BUNDLE/nginx-tls.conf"
chmod +x "$BUNDLE/install.sh"

# Exactly what the archive holds, so install.sh can verify the load rather than
# discovering a missing backing service when compose tries to pull it.
printf '%s\n' "${IMAGES[@]}" > "$BUNDLE/images.list"

# The tag the compose file resolves; install.sh seeds .env from this.
cat > "$BUNDLE/bundle.env" <<EOF
VESTIGO_IMAGE_TAG=$TAG
VESTIGO_VERSION=$VERSION
VESTIGO_COMMIT=$COMMIT
VESTIGO_IMAGE_COUNT=${#IMAGES[@]}
VESTIGO_BUNDLE_SCOPE=$([ "$APP_ONLY" = 1 ] && echo app-only || echo full)
EOF

printf 'Vestigo %s (%s)\nBuilt %s\nImages: %s\n' \
  "$VERSION" "$COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${IMAGES[*]}" \
  > "$BUNDLE/BUNDLE-INFO"

(cd "$BUNDLE" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z \
  | xargs -0 sha256sum > MANIFEST.sha256)

# ── 5. pack ─────────────────────────────────────────────────────────────────
mkdir -p "$OUT_DIR"
TARBALL="$OUT_DIR/vestigo-airgap-$TAG.tar.gz"
say "packing $TARBALL"
tar czf "$TARBALL" -C "$STAGE" "$(basename "$BUNDLE")"

SUM="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
printf '%s  %s\n' "$SUM" "$(basename "$TARBALL")" > "$TARBALL.sha256"

say "done"
cat <<EOF

  $TARBALL
  $TARBALL.sha256
  $SUM

Carry both files to the airgapped host (they must travel together — the .sha256
is how the far side knows the copy survived the drive), then:

  sha256sum -c $(basename "$TARBALL").sha256
  tar xzf $(basename "$TARBALL")
  cd $(basename "$BUNDLE")
  ./install.sh --check     # verifies everything, changes nothing
  ./install.sh             # installs, or upgrades an existing install

An upgrade needs no other step: install.sh finds the existing install directory,
keeps its .env, and reuses the same named volumes. docs/DEPLOYMENT.md §"Route A"
has the full runbook, including backup and rollback.
EOF
