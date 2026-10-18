#!/bin/bash
# Run PrintStash on Apple's `container` instead of Docker Compose.
#
# The default single-container stack only: web UI and API in one container, SQLite
# and local storage on one named volume. The advanced and manual-testing compose
# stacks stay on Docker. Setup, builder notes and what differs from compose are in
# deploy/apple-container/APP.md.
#
#   printstash.sh up [--image REF] [--port N] [--env-file FILE] [--recreate] [--pull]
#   printstash.sh restart
#   printstash.sh down
#   printstash.sh logs [-f]
#   printstash.sh status
#   printstash.sh build [--variant full|lite]
#
# Only tools in a stock macOS install are used, and bash 3.2 is enough.
set -euo pipefail

# The caller's stderr, kept so a message still reaches it from inside a command whose
# own output is redirected (a wedge detected under `>/dev/null 2>&1`).
exec 3>&2

# Resolve the script's real location, following symlinks, so the repository is
# found when the script is run through a link on PATH. CDPATH would make `cd`
# print the directory it chose, so it is cleared for every `cd` here.
resolve_script() {
  local path=$1 dir link
  while [ -L "$path" ]; do
    dir=$(CDPATH= cd -P -- "$(dirname -- "$path")" >/dev/null && pwd -P)
    link=$(readlink -- "$path")
    case "$link" in
      /*) path=$link ;;
      *) path=$dir/$link ;;
    esac
  done
  CDPATH= cd -P -- "$(dirname -- "$path")" >/dev/null && pwd -P
}
SCRIPT_DIR=$(resolve_script "$0")
REPO_ROOT=$(CDPATH= cd -P -- "$SCRIPT_DIR/../.." >/dev/null && pwd -P)

# docker-compose.yml reads these three from .env; read them the same way when the
# environment does not already set them.
read_dotenv() {
  local file=$REPO_ROOT/.env key value line
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      PRINTSTASH_IMAGE=* | PRINTSTASH_VERSION=* | PRINTSTASH_HTTP_PORT=*) ;;
      *) continue ;;
    esac
    key=${line%%=*}
    value=${line#*=}
    value=${value%\"}
    value=${value#\"}
    value=${value%\'}
    value=${value#\'}
    if [ -z "${!key+set}" ]; then
      export "$key=$value"
    fi
  done <"$file"
}
read_dotenv

NAME=${PRINTSTASH_CONTAINER_NAME:-printstash}
VOLUME=${PRINTSTASH_VOLUME:-printstash}
VOLUME_SIZE=${PRINTSTASH_VOLUME_SIZE:-}
# The same default as docker-compose.yml.
DEFAULT_IMAGE="${PRINTSTASH_IMAGE:-ghcr.io/xiao-villamor/printstash}:${PRINTSTASH_VERSION:-latest}"
DEFAULT_PORT=${PRINTSTASH_HTTP_PORT:-3000}
MEMORY=${PRINTSTASH_MEMORY:-4g}
GRACE_SECONDS=60
HEALTH_TIMEOUT=${PRINTSTASH_HEALTH_TIMEOUT:-180}
POLL_SECONDS=${PRINTSTASH_POLL_SECONDS:-1}
TIMEOUT_BIN=${PRINTSTASH_TIMEOUT_BIN:-/usr/bin/timeout}
BUILD_CPUS=${PRINTSTASH_BUILD_CPUS:-8}
BUILD_MEMORY=${PRINTSTASH_BUILD_MEMORY:-16g}
LOCAL_IMAGE=printstash:local
BUILD_LOCK=${PRINTSTASH_BUILD_LOCK:-$HOME/Library/Caches/printstash-apple-container-build.lock}

# Upper bounds for `container` commands. A command that outlives one is apple/container#2275.
SHORT_S=60
STOP_S=$((GRACE_SECONDS + 60))
RUN_S=600
PULL_S=1800
BUILD_S=7200

usage() {
  cat >&2 <<'EOF'
usage: printstash.sh <command> [options]

  up [--image REF] [--port N] [--env-file FILE] [--recreate] [--pull]
        Start PrintStash (creating it if needed) and wait until it answers.
        REF defaults to $PRINTSTASH_IMAGE:$PRINTSTASH_VERSION as in docker-compose.yml;
        use --image printstash:local for an image built with `build`.
        --env-file passes VAULT_* settings (one KEY=value per line).
        --pull fetches REF again first. A container whose image, port, memory,
        volume or settings differ is replaced only with --recreate (data kept).
  restart
        Stop and start the container, keeping it as it is.
  down
        Stop and remove the container. The data volume is kept.
  logs [-f]
        Print the container's log, optionally following it.
  status
        Say whether it is running and with what image, port, memory and volume.
  build [--variant full|lite]
        Build the image on Apple container as printstash:local.
EOF
  exit 2
}

die() {
  echo "printstash.sh: $*" >&3
  exit 1
}

bad_usage() {
  echo "printstash.sh: $*" >&2
  usage
}

wedge() {
  die "a container command did not answer within its limit. On macOS 27 this is \
apple/container#2275: com.apple.pfd stopped answering. Check \
\`launchctl print system/com.apple.pfd\`; a climbing \`runs\` with exit code 3 \
confirms it, and only a reboot clears it."
}

valid_port() {
  case "$1" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

require_cli() {
  command -v container >/dev/null 2>&1 ||
    die "the \`container\` CLI is not installed or not on PATH (it installs to /usr/local/bin)"
  [ -x "$TIMEOUT_BIN" ] || die "$TIMEOUT_BIN is missing; set PRINTSTASH_TIMEOUT_BIN"
}

# Every `container` call goes through here, bounded, except `logs --follow`. The
# output and status are left to the caller; a timeout ends the script naming #2275.
ctr() {
  local limit=$1 rc=0
  shift
  "$TIMEOUT_BIN" "$limit" container "$@" || rc=$?
  if [ "$rc" -eq 124 ]; then
    wedge
  fi
  return "$rc"
}

# --- system ----------------------------------------------------------------

system_running() {
  local rc=0
  ctr "$SHORT_S" system status >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) die "container system status failed (exit $rc)" ;;
  esac
}

ensure_system() {
  if system_running; then
    return 0
  fi
  echo "starting the container apiserver (up to 10 minutes the first time)..." >&2
  local rc=0
  ctr "$RUN_S" system start --enable-kernel-install </dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "container system start failed (exit $rc)"
  fi
}

# --- container state ---------------------------------------------------------

INSPECT=''

load_inspect() {
  local err rc=0
  err=$(mktemp "${TMPDIR:-/tmp}/printstash.XXXXXX")
  INSPECT=$(ctr "$SHORT_S" inspect "$NAME" 2>"$err") || rc=$?
  if [ "$rc" -ne 0 ]; then
    if grep -q "not found" "$err"; then
      INSPECT=''
    else
      cat "$err" >&2
      /bin/rm -f "$err"
      die "container inspect $NAME failed (exit $rc)"
    fi
  fi
  /bin/rm -f "$err"
}

container_state() {
  if [ -z "$INSPECT" ]; then
    echo absent
    return 0
  fi
  local state
  state=$(printf '%s\n' "$INSPECT" | sed -n 's/^ *"state" *: *"\([a-z]*\)".*/\1/p' | head -n 1)
  [ -n "$state" ] || die "cannot read the state of $NAME from container inspect"
  echo "$state"
}

label_value() {
  printf '%s\n' "$INSPECT" | sed -n "s/^ *\"$1\" *: *\"\\([^\"]*\\)\".*/\\1/p" | head -n 1
}

# Only a container this script created is ever stopped or deleted.
require_ours() {
  [ "$(label_value printstash.managed)" = 1 ] ||
    die "a container named $NAME exists that printstash.sh did not create; \
remove it yourself or set PRINTSTASH_CONTAINER_NAME"
}

# The fields a running container must match to be left alone, and how a mismatch
# is described. The digest catches an image rebuilt or pulled under the same name.
SPEC_FIELDS="image digest port memory volume env"

spec_difference() {
  local field
  for field in $SPEC_FIELDS; do
    if [ "$(label_value "printstash.$field")" != "$(wanted "$field")" ]; then
      echo "$field"
      return 0
    fi
  done
  return 0
}

WANT_IMAGE=''
WANT_DIGEST=''
WANT_PORT=''
WANT_ENV_FILE=''
WANT_ENV=none

wanted() {
  case "$1" in
    image) echo "$WANT_IMAGE" ;;
    digest) echo "$WANT_DIGEST" ;;
    port) echo "$WANT_PORT" ;;
    memory) echo "$MEMORY" ;;
    volume) echo "$VOLUME" ;;
    env) echo "$WANT_ENV" ;;
  esac
}

image_digest() {
  local out
  out=$(ctr "$SHORT_S" image inspect "$1" 2>/dev/null) || return 1
  printf '%s\n' "$out" | sed -n 's/^ *"digest" *: *"\(sha256:[0-9a-f]*\)".*/\1/p' | head -n 1
}

# --- commands ----------------------------------------------------------------

port_taken() {
  nc -z 127.0.0.1 "$1" >/dev/null 2>&1 || nc -z ::1 "$1" >/dev/null 2>&1
}

wait_ready() {
  local port=$1 deadline=$((SECONDS + HEALTH_TIMEOUT)) rc=0 last=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    load_inspect
    if [ "$(container_state)" != running ]; then
      echo "the container exited before it answered. Last log lines:" >&2
      ctr "$SHORT_S" logs -n 40 "$NAME" >&2 || true
      exit 1
    fi
    rc=0
    curl --noproxy '*' -fsS -o /dev/null -m 3 "http://127.0.0.1:$port/api/v1/health" 2>/dev/null || rc=$?
    if [ "$rc" -eq 0 ]; then
      curl --noproxy '*' -fsS -o /dev/null -m 3 "http://127.0.0.1:$port/" 2>/dev/null || rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
      echo "PrintStash is running at http://127.0.0.1:$port"
      return 0
    fi
    last=$rc
    sleep "$POLL_SECONDS"
  done
  echo "PrintStash did not answer within ${HEALTH_TIMEOUT}s. Last log lines:" >&2
  ctr "$SHORT_S" logs -n 40 "$NAME" >&2 || true
  case "$last" in
    52 | 56)
      echo "The port accepted connections and then dropped them. If the log looks \
healthy, container-runtime-linux probably lacks Local Network permission: enable it \
under System Settings > Privacy & Security > Local Network." >&2
      ;;
  esac
  exit 1
}

ensure_volume() {
  if ctr "$SHORT_S" volume inspect "$VOLUME" >/dev/null 2>&1; then
    return 0
  fi
  echo "creating a new, empty data volume $VOLUME (see APP.md to bring data from Docker)" >&2
  if [ -n "$VOLUME_SIZE" ]; then
    ctr "$SHORT_S" volume create -s "$VOLUME_SIZE" "$VOLUME" >/dev/null
  else
    ctr "$SHORT_S" volume create "$VOLUME" >/dev/null
  fi
}

run_container() {
  local port=$1
  local env_args=()
  if [ -n "$WANT_ENV_FILE" ]; then
    env_args=(--env-file "$WANT_ENV_FILE")
  fi
  ensure_volume
  # No VAULT_RESTART_ENABLED: nothing relaunches this container, so a Settings
  # restart would leave it stopped until `up` runs; `restart` is the restart.
  ctr "$RUN_S" run --detach --progress none \
    --name "$NAME" --memory "$MEMORY" \
    -p "127.0.0.1:$port:3000" \
    -e VAULT_SETUP_MODE=trusted_network \
    ${env_args[@]+"${env_args[@]}"} \
    -v "$VOLUME:/data" \
    --label printstash.managed=1 \
    --label "printstash.image=$WANT_IMAGE" \
    --label "printstash.digest=$WANT_DIGEST" \
    --label "printstash.port=$port" \
    --label "printstash.memory=$MEMORY" \
    --label "printstash.volume=$VOLUME" \
    --label "printstash.env=$WANT_ENV" \
    "$WANT_IMAGE" >/dev/null
  wait_ready "$port"
}

remove_container() {
  case "$(container_state)" in
    absent) return 0 ;;
    running) ctr "$STOP_S" stop --time "$GRACE_SECONDS" "$NAME" >/dev/null ;;
  esac
  ctr "$SHORT_S" delete "$NAME" >/dev/null
}

# The image to run, present locally, with its digest. Pulled first, so a failed
# pull leaves a running container untouched.
prepare_image() {
  local pull=$1
  if [ "$pull" = true ] || ! image_digest "$WANT_IMAGE" >/dev/null; then
    ctr "$PULL_S" image pull --progress none "$WANT_IMAGE" ||
      die "pulling $WANT_IMAGE failed; nothing was changed"
  fi
  WANT_DIGEST=$(image_digest "$WANT_IMAGE") || die "image $WANT_IMAGE is not available locally"
  [ -n "$WANT_DIGEST" ] || die "cannot read the digest of $WANT_IMAGE"
}

cmd_up() {
  local recreate=false pull=false
  WANT_IMAGE=$DEFAULT_IMAGE
  WANT_PORT=$DEFAULT_PORT
  while [ $# -gt 0 ]; do
    case "$1" in
      --image)
        [ $# -ge 2 ] || bad_usage "--image needs a value"
        WANT_IMAGE=$2
        shift 2
        ;;
      --port)
        [ $# -ge 2 ] || bad_usage "--port needs a value"
        valid_port "$2" || bad_usage "invalid port: $2"
        WANT_PORT=$2
        shift 2
        ;;
      --env-file)
        [ $# -ge 2 ] || bad_usage "--env-file needs a value"
        [ -f "$2" ] && [ -r "$2" ] || bad_usage "cannot read env file: $2"
        WANT_ENV_FILE=$2
        shift 2
        ;;
      --recreate)
        recreate=true
        shift
        ;;
      --pull)
        pull=true
        shift
        ;;
      *) bad_usage "unknown option for up: $1" ;;
    esac
  done
  case "$WANT_IMAGE" in
    '' | -*) bad_usage "invalid image reference: '$WANT_IMAGE'" ;;
  esac
  valid_port "$WANT_PORT" || bad_usage "invalid port: $WANT_PORT"
  if [ -n "$WANT_ENV_FILE" ]; then
    WANT_ENV=$(shasum -a 256 <"$WANT_ENV_FILE" | cut -c1-16)
  fi

  require_cli
  ensure_system
  prepare_image "$pull"
  load_inspect
  local state difference
  state=$(container_state)
  case "$state" in
    absent | running | stopped) ;;
    *) die "$NAME is $state; wait for it to settle and run up again" ;;
  esac
  if [ "$state" = absent ]; then
    if port_taken "$WANT_PORT"; then
      die "port $WANT_PORT is already in use on 127.0.0.1 or ::1; pick another with --port"
    fi
    run_container "$WANT_PORT"
    return 0
  fi
  require_ours
  difference=$(spec_difference)
  if [ -z "$difference" ] && [ "$recreate" != true ]; then
    if [ "$state" = stopped ]; then
      ctr "$RUN_S" start "$NAME" >/dev/null || die "container start $NAME failed"
    fi
    wait_ready "$WANT_PORT"
    return 0
  fi
  if [ "$recreate" != true ]; then
    case "$difference" in
      digest) die "the image $WANT_IMAGE has changed (rebuilt or pulled) since $NAME \
was created. Run again with --recreate to replace the container (the data volume is kept)." ;;
      *) die "$NAME has a different $difference. Run again with --recreate to replace \
the container (the data volume is kept)." ;;
    esac
  fi
  if [ "$WANT_PORT" != "$(label_value printstash.port)" ] && port_taken "$WANT_PORT"; then
    die "port $WANT_PORT is already in use on 127.0.0.1 or ::1; nothing was changed"
  fi
  if [ "$state" = stopped ]; then
    echo "replacing the stopped container; its last log lines:" >&2
    ctr "$SHORT_S" logs -n 20 "$NAME" >&2 || true
  fi
  remove_container
  run_container "$WANT_PORT"
}

cmd_restart() {
  [ $# -eq 0 ] || bad_usage "restart takes no arguments"
  require_cli
  ensure_system
  load_inspect
  [ "$(container_state)" != absent ] || die "$NAME does not exist; run up first"
  require_ours
  if [ "$(container_state)" = running ]; then
    ctr "$STOP_S" stop --time "$GRACE_SECONDS" "$NAME" >/dev/null
  fi
  ctr "$RUN_S" start "$NAME" >/dev/null || die "container start $NAME failed"
  wait_ready "$(label_value printstash.port)"
}

cmd_down() {
  [ $# -eq 0 ] || bad_usage "down takes no arguments"
  require_cli
  if ! system_running; then
    echo "the container apiserver is not running; nothing to stop"
    return 0
  fi
  load_inspect
  if [ "$(container_state)" = absent ]; then
    echo "$NAME is not running"
    return 0
  fi
  require_ours
  local volume
  volume=$(label_value printstash.volume)
  remove_container
  echo "$NAME removed; the ${volume:-$VOLUME} volume is kept"
}

cmd_logs() {
  local follow=false
  case "${1:-}" in
    '') ;;
    -f)
      follow=true
      shift
      ;;
    *) bad_usage "unknown option for logs: $1" ;;
  esac
  [ $# -eq 0 ] || bad_usage "unexpected argument: $1"
  require_cli
  system_running || die "the container apiserver is not running"
  if [ "$follow" = true ]; then
    container logs --follow "$NAME"
  else
    ctr "$SHORT_S" logs "$NAME"
  fi
}

cmd_status() {
  [ $# -eq 0 ] || bad_usage "status takes no arguments"
  require_cli
  if ! system_running; then
    echo "apiserver: stopped"
    return 0
  fi
  load_inspect
  local state
  state=$(container_state)
  echo "$NAME: $state"
  if [ "$state" != absent ]; then
    echo "image:  $(label_value printstash.image)"
    echo "port:   $(label_value printstash.port)"
    echo "memory: $(label_value printstash.memory)"
    echo "volume: $(label_value printstash.volume)"
  fi
}

build_one() {
  local tag=$1
  shift
  echo "building $tag..." >&2
  if ! ctr "$BUILD_S" build --cpus "$BUILD_CPUS" --memory "$BUILD_MEMORY" -t "$tag" "$@"; then
    echo "printstash.sh: building $tag failed on this machine's builder." >&2
    echo "Nothing was substituted. To run a published image instead:" >&2
    echo "  printstash.sh up --image $DEFAULT_IMAGE" >&2
    exit 1
  fi
}

release_build_lock() {
  /bin/rm -rf "$BUILD_LOCK"
}

cmd_build() {
  local variant=full
  while [ $# -gt 0 ]; do
    case "$1" in
      --variant)
        [ $# -ge 2 ] || bad_usage "--variant needs a value"
        case "$2" in
          full | lite) variant=$2 ;;
          *) bad_usage "invalid variant: $2 (full or lite)" ;;
        esac
        shift 2
        ;;
      *) bad_usage "unknown option for build: $1" ;;
    esac
  done
  [ -f "$REPO_ROOT/backend/unified/Dockerfile" ] ||
    die "cannot find backend/unified/Dockerfile under $REPO_ROOT"
  require_cli
  # The intermediate tags are fixed names the unified Dockerfile refers to, so two
  # builds at once would mix each other's images.
  mkdir -p "$(dirname "$BUILD_LOCK")"
  mkdir "$BUILD_LOCK" 2>/dev/null ||
    die "another build holds $BUILD_LOCK; wait for it, or remove the directory if none is running"
  trap release_build_lock EXIT
  ensure_system
  # Every `container build` resizes the builder to its own --cpus/--memory, so the
  # same values go on all three, and the builder is not started separately.
  build_one api-image --build-arg "PRINTSTASH_VARIANT=$variant" "$REPO_ROOT/backend"
  build_one frontend-image "$REPO_ROOT/frontend"
  build_one "$LOCAL_IMAGE" "$REPO_ROOT/backend/unified"
  echo "built $LOCAL_IMAGE ($variant); run it with: printstash.sh up --image $LOCAL_IMAGE --recreate"
}

[ $# -ge 1 ] || usage
command=$1
shift
case "$command" in
  up) cmd_up "$@" ;;
  restart) cmd_restart "$@" ;;
  down) cmd_down "$@" ;;
  logs) cmd_logs "$@" ;;
  status) cmd_status "$@" ;;
  build) cmd_build "$@" ;;
  -h | --help | help) usage ;;
  *) bad_usage "unknown command: $command" ;;
esac
