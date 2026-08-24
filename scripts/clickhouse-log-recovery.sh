#!/usr/bin/env bash
# Cap ClickHouse's own logging and reclaim the space it has already taken.
#
#     scripts/clickhouse-log-recovery.sh              # inspect, confirm, apply
#     scripts/clickhouse-log-recovery.sh --dry-run    # report only, change nothing
#     scripts/clickhouse-log-recovery.sh --yes        # no prompts (for a runbook)
#     scripts/clickhouse-log-recovery.sh --truncate-system-logs   # also clear existing telemetry rows
#
# Why: the stock ClickHouse image logs at `trace` with <size>1000M</size> and
# <count>10</count>, and our compose file mounts a volume for /var/lib/clickhouse
# but not for /var/log/clickhouse-server. Logs therefore accumulate — up to ~11 GB
# — inside the container's writable layer. On a host with a storage quota that is
# enough to exhaust it, and when a log write fails ClickHouse does not degrade
# gracefully: the ofstream latches its failbit, every subsequent rotation check
# throws "File access error", and the server livelocks trying to log that it
# cannot log. It looks like a crash. It is a full outage from a debug log.
#
# This script installs a logger drop-in (information level, 100M x 3, telemetry
# tables off) and recreates the container. Recreating discards the writable layer
# — which is exactly where the logs are — while /var/lib/clickhouse is a named
# volume and survives untouched. It refuses to run if that is not true.
#
# Airgap-safe: never contacts a registry. It runs `up` with --pull never and
# verifies the image is already in the local store before touching anything.
#
# Works with Docker or Podman; the engine and compose implementation are detected.
#
# NOTE: this treats the symptom. If ClickHouse is failing with "Disk quota
# exceeded" rather than plain "No space left on device", the limit is a quota
# enforced outside the container and `df` inside it cannot see it. Only whoever
# administers that layer can clear it. See the closing summary.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

SERVICE="clickhouse"
DROPIN="deploy/clickhouse/logger.xml"
MOUNT_LINE="      - ./${DROPIN}:/etc/clickhouse-server/config.d/zz-logger.xml:ro,z"
ANCHOR="allow-default-network.xml"
DATA_DIR="/var/lib/clickhouse"
LOG_DIR="/var/log/clickhouse-server"

DRY_RUN=0
ASSUME_YES=0
TRUNCATE_LOGS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)              DRY_RUN=1 ;;
    -y|--yes)               ASSUME_YES=1 ;;
    --truncate-system-logs) TRUNCATE_LOGS=1 ;;
    -h|--help)              sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//; $d'; exit 0 ;;
    *)                      echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }

# Docker and Podman are both first-class here: the reference stack in
# docker-compose.yml is run with `docker compose` in deployments and
# `podman compose` in local development.
ENGINE=""
for candidate in docker podman; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" info >/dev/null 2>&1; then
    ENGINE="$candidate"
    break
  fi
done
[[ -n "$ENGINE" ]] || die "no usable container engine found (tried docker, podman)"

engine() { "$ENGINE" "$@"; }

compose() {
  if "$ENGINE" compose version >/dev/null 2>&1; then "$ENGINE" compose "$@"
  elif command -v "${ENGINE}-compose" >/dev/null 2>&1; then "${ENGINE}-compose" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"
  else die "no compose implementation found for '$ENGINE'"; fi
}

# ---------------------------------------------------------------- preflight

say "Preflight"

[[ -f docker-compose.yml ]] || die "no docker-compose.yml in $REPO"
info "engine:    $ENGINE"

PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$REPO" | tr '[:upper:]' '[:lower:]')}"

# `compose ps -q <service>` is the direct route, but not every Compose
# implementation accepts a service filter. Fall back to the standard labels,
# which every one of them sets.
resolve_cid() {
  local cid
  cid="$(compose ps -q "$SERVICE" 2>/dev/null | head -n1 || true)"
  if [[ -z "$cid" ]]; then
    cid="$(engine ps -q \
      --filter "label=com.docker.compose.project=$PROJECT" \
      --filter "label=com.docker.compose.service=$SERVICE" | head -n1 || true)"
  fi
  printf '%s' "$cid"
}

CID="$(resolve_cid)"
[[ -n "$CID" ]] || die "service '$SERVICE' is not running in compose project '$PROJECT' (cd to the deployment directory first, or set COMPOSE_PROJECT_NAME)"
info "project:   $PROJECT"
info "container: $CID"

IMAGE="$(engine inspect "$CID" --format '{{.Config.Image}}')"
engine image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "image '$IMAGE' is not in the local store — on an airgapped host a recreate could not recover it"
info "image:     $IMAGE (present locally)"

# The whole plan rests on this: the data must live OUTSIDE the container layer,
# or --force-recreate destroys the cases. Refuse rather than guess.
engine inspect "$CID" --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' \
  | grep -qx "$DATA_DIR" \
  || die "$DATA_DIR is not a volume or bind mount — recreating this container would DESTROY the event data. Fix the compose file first."
MOUNT_DESC="$(engine inspect "$CID" --format \
  "{{range .Mounts}}{{if eq .Destination \"$DATA_DIR\"}}{{.Type}} {{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}")"
info "data:      $DATA_DIR <- $MOUNT_DESC (survives recreate)"

# ------------------------------------------------------------ current state

say "Current state"

info "log files in the container layer (discarded on recreate):"
engine exec "$CID" sh -c "ls -la $LOG_DIR 2>/dev/null | tail -n +2" | sed 's/^/      /' || true
LOG_BYTES="$(engine exec "$CID" sh -c "du -sb $LOG_DIR 2>/dev/null | cut -f1" || echo 0)"
info "total: $(numfmt --to=iec "${LOG_BYTES:-0}" 2>/dev/null || echo "${LOG_BYTES} bytes")"

info ""
info "system telemetry tables on the data volume (NOT discarded by a recreate):"
engine exec "$CID" clickhouse-client --query "
  SELECT table, formatReadableSize(sum(bytes_on_disk))
  FROM system.parts WHERE active AND database='system'
  GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 12
  FORMAT PrettyCompactMonoBlock" 2>/dev/null | sed 's/^/      /' || info "(query failed — server may be unhealthy)"

info ""
info "free space as ClickHouse itself sees it (reflects quotas, unlike df):"
engine exec "$CID" clickhouse-client --query "
  SELECT name, formatReadableSize(free_space), formatReadableSize(total_space)
  FROM system.disks FORMAT PrettyCompactMonoBlock" 2>/dev/null | sed 's/^/      /' || true

if [[ $DRY_RUN -eq 1 ]]; then
  say "Dry run — nothing changed"
  info "would write:    $DROPIN"
  grep -qF "$DROPIN" docker-compose.yml \
    && info "compose mount:  already present" \
    || info "would add:      $MOUNT_LINE"
  info "would recreate: service '$SERVICE' from $IMAGE (--pull never)"
  exit 0
fi

# ------------------------------------------------------------------ confirm

say "About to change this deployment"
info "1. write $DROPIN (information level, 100M x 3, telemetry tables off)"
info "2. add the drop-in mount to docker-compose.yml (backup kept)"
info "3. recreate '$SERVICE' — roughly a minute of downtime"
[[ $TRUNCATE_LOGS -eq 1 ]] && info "4. TRUNCATE the system.*_log tables"
info ""
info "Ingestion and embedding jobs are in-memory and will NOT survive this."
info "Check the Vestigo job tray is idle before continuing."

if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "    Proceed? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || die "cancelled by operator"
fi

# ------------------------------------------------------------------- drop-in

say "Writing $DROPIN"

mkdir -p "$(dirname "$DROPIN")"
if [[ -f "$DROPIN" ]]; then
  cp -a "$DROPIN" "$DROPIN.bak.$(date +%Y%m%d-%H%M%S)"
  info "existing file backed up"
fi

cat > "$DROPIN" <<'EOF'
<clickhouse>
    <!-- The stock image logs at `trace` with a 1000M x 10 rotation, i.e. up to
         ~11 GB of debug text nobody reads, written into the container's
         writable layer. On a quota-limited host that is enough to exhaust the
         allowance, and a failed log write livelocks the server (the ofstream
         latches its failbit and every rotation check then throws). Cap it. -->
    <logger>
        <level>information</level>
        <size>100M</size>
        <count>3</count>
    </logger>

    <!-- ClickHouse's internal telemetry, written continuously and unbounded.
         Nothing in Vestigo reads these, and they are the largest consumers on
         the data volume — trace_log and text_log reach gigabytes within weeks. -->
    <trace_log remove="1"/>
    <metric_log remove="1"/>
    <query_metric_log remove="1"/>
    <asynchronous_metric_log remove="1"/>
    <text_log remove="1"/>
    <background_schedule_pool_log remove="1"/>
    <processors_profile_log remove="1"/>

    <!-- These two stay: query_log and part_log are what you actually want when
         diagnosing a slow ingest or a merge problem, and dropping them would
         cost real troubleshooting ability. Bound them by time instead. -->
    <query_log>
        <ttl>event_date + INTERVAL 14 DAY DELETE</ttl>
    </query_log>
    <part_log>
        <ttl>event_date + INTERVAL 14 DAY DELETE</ttl>
    </part_log>
</clickhouse>
EOF
info "written"

# ------------------------------------------------------------------- compose

say "Wiring the drop-in into docker-compose.yml"

if grep -qF "$DROPIN" docker-compose.yml; then
  info "mount already present — leaving compose file alone"
else
  BACKUP="docker-compose.yml.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a docker-compose.yml "$BACKUP"
  info "backup: $BACKUP"

  grep -qF "$ANCHOR" docker-compose.yml \
    || { info "anchor '$ANCHOR' not found"; die "add this line to the $SERVICE service's volumes by hand, then re-run:
$MOUNT_LINE"; }

  awk -v line="$MOUNT_LINE" -v anchor="$ANCHOR" '
    { print }
    !done && index($0, anchor) { print line; done = 1 }
  ' "$BACKUP" > docker-compose.yml

  # Validating the YAML is local — it does not reach a registry.
  if ! compose config >/dev/null 2>&1; then
    cp -a "$BACKUP" docker-compose.yml
    die "the edited docker-compose.yml does not validate; restored from $BACKUP. Add this line by hand:
$MOUNT_LINE"
  fi
  info "mount added and compose file validates"
fi

# ------------------------------------------------------------------ recreate

say "Recreating '$SERVICE'"

# --pull never keeps an airgapped host from stalling on a registry it cannot
# reach. Older Compose builds lack the flag; the image is verified present above.
if ! compose up -d --force-recreate --pull never "$SERVICE" 2>/dev/null; then
  info "--pull never unsupported by this Compose; retrying without it (image is local)"
  compose up -d --force-recreate "$SERVICE"
fi

CID="$(resolve_cid)"
[[ -n "$CID" ]] || die "container did not come back — inspect with: compose logs $SERVICE"
info "new container: $CID"

# ------------------------------------------------------------------- verify

say "Waiting for ClickHouse to answer"

for i in $(seq 1 60); do
  if engine exec "$CID" clickhouse-client --query "SELECT 1" >/dev/null 2>&1; then
    info "responding after ${i}s"
    break
  fi
  [[ $i -eq 60 ]] && die "no response after 60s — check: compose logs $SERVICE"
  sleep 1
done

TABLES="$(engine exec "$CID" clickhouse-client --query \
  "SELECT count() FROM system.tables WHERE database='vestigo'" 2>/dev/null || echo 0)"
[[ "${TABLES:-0}" -gt 0 ]] \
  || die "the 'vestigo' database reports $TABLES tables — data may not have remounted. Do NOT run 'compose down -v'. Inspect the volume."
info "vestigo database: $TABLES tables present"

if [[ $TRUNCATE_LOGS -eq 1 ]]; then
  say "Truncating existing telemetry rows"
  for t in trace_log metric_log query_metric_log text_log asynchronous_metric_log \
           asynchronous_insert_log background_schedule_pool_log processors_profile_log; do
    engine exec "$CID" clickhouse-client --query "TRUNCATE TABLE IF EXISTS system.$t" 2>/dev/null \
      && info "truncated system.$t" || true
  done
fi

# ------------------------------------------------------------------ summary

say "Done"

NEW_BYTES="$(engine exec "$CID" sh -c "du -sb $LOG_DIR 2>/dev/null | cut -f1" || echo 0)"
info "log directory: $(numfmt --to=iec "${LOG_BYTES:-0}" 2>/dev/null || echo "$LOG_BYTES") -> $(numfmt --to=iec "${NEW_BYTES:-0}" 2>/dev/null || echo "$NEW_BYTES")"
info "logging now capped at ~400 MB (information level, 100M x 3)"
if [[ $TRUNCATE_LOGS -ne 1 ]]; then
  info ""
  info "NOTE: the telemetry tables are now switched off, but the rows they already"
  info "      wrote are still on the DATA volume and were not removed by the"
  info "      recreate. If the table listing above showed gigabytes, reclaim them:"
  info "          $0 --truncate-system-logs"
fi
info ""
info "Still outstanding — this script cannot reach it:"
info "  If ClickHouse was failing with 'Disk quota exceeded' (EDQUOT) rather than"
info "  'No space left on device' (ENOSPC), the limit is enforced outside this"
info "  container and df inside it cannot see it. Capping the logs lowers how fast"
info "  you reach that ceiling; it does not raise the ceiling. Check whichever"
info "  applies to your storage:"
info "    ZFS   zfs get quota,refquota,used,usedbysnapshots <dataset>"
info "          (quota counts snapshots, refquota does not — df shows refquota,"
info "           so a snapshot backlog is invisible from inside the guest)"
info "    XFS   xfs_quota -x -c 'report -h' <mountpoint>"
info "    ext4  repquota -s <mountpoint>"
info "    LVM   lvs --units g   (thin pool exhaustion surfaces the same way)"
info ""
info "Monitor from inside the guest with (df will not show the quota):"
info "  $ENGINE exec $CID clickhouse-client --query \\"
info "    \"SELECT name, formatReadableSize(free_space) FROM system.disks\""
