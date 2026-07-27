#!/usr/bin/env bash
# Build a self-contained Vestigo deployment bundle on a machine WITH network
# access. The resulting tarball installs and upgrades an airgapped host with no
# registry, no npm, and no build step on the far side.
#
#     scripts/airgap-bundle.sh                    # app + backing services
#     scripts/airgap-bundle.sh --app-only         # app image only (services already there)
#     scripts/airgap-bundle.sh --no-embeddings    # skip the ~2 GB torch install
#     scripts/airgap-bundle.sh -o /media/usb      # write the tarball elsewhere
#
# What it does: builds the frontend, builds the app image from the prebuilt
# frontend (so the far side never needs node), saves every image the stack
# runs, and packs them with the compose file and `install.sh`.
#
# Verified end to end by `tests/test_airgap_bundle.py`, which asserts the
# bundle's contents rather than its size.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
    [ -n "$img" ] && IMAGES+=("$img")
  done < <(sed -n 's/^ *image: \(docker\.io[^$]*\)$/\1/p' deploy/airgap/docker-compose.airgap.yml)
fi
for img in "${IMAGES[@]}"; do
  case "$img" in
    vestigo-app:*) ;;
    *) say "pulling $img"; "$ENGINE" pull "$img" ;;
  esac
done
say "saving ${#IMAGES[@]} image(s) — this is the slow part"
"$ENGINE" save -o "$BUNDLE/images/vestigo-stack.tar" "${IMAGES[@]}"

# ── 4. everything else the target needs ─────────────────────────────────────
cp deploy/airgap/docker-compose.airgap.yml "$BUNDLE/docker-compose.yml"
cp deploy/airgap/install.sh "$BUNDLE/install.sh"
cp deploy/clickhouse/allow-default-network.xml "$BUNDLE/clickhouse/"
cp .env.example "$BUNDLE/.env.example"
cp docs/nginx-tls.conf "$BUNDLE/nginx-tls.conf" 2>/dev/null || true
chmod +x "$BUNDLE/install.sh"

# The tag the compose file resolves; install.sh seeds .env from this.
cat > "$BUNDLE/bundle.env" <<EOF
VESTIGO_IMAGE_TAG=$TAG
VESTIGO_VERSION=$VERSION
VESTIGO_COMMIT=$COMMIT
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

say "done"
printf '\n  %s\n  %s\n\n' "$TARBALL" "$(sha256sum "$TARBALL" | cut -d' ' -f1)"
printf 'Copy it to the airgapped host, then:\n'
printf '  tar xzf %s\n  cd %s\n  ./install.sh\n' \
  "$(basename "$TARBALL")" "$(basename "$BUNDLE")"
