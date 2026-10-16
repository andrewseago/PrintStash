#!/bin/bash
# Run PrintStash on Apple's `container` instead of Docker Compose.
#
# The default single-container stack only: web UI and API in one container, SQLite
# and local storage on one named volume. The advanced and manual-testing compose
# stacks stay on Docker. Setup, builder notes and what differs from compose are in
# deploy/apple-container/APP.md.
#
#   printstash.sh up [--image REF] [--port N] [--recreate] [--pull]
#   printstash.sh down
#   printstash.sh logs [-f]
#   printstash.sh status
#   printstash.sh build [--variant full|lite]
#
# Only tools in a stock macOS install are used, and bash 3.2 is enough.
set -euo pipefail

NAME=${PRINTSTASH_CONTAINER_NAME:-printstash}
VOLUME=${PRINTSTASH_VOLUME:-printstash}
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

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)

usage() {
  cat >&2 <<'EOF'
usage: printstash.sh <command> [options]

  up [--image REF] [--port N] [--recreate] [--pull]
        Start PrintStash (creating it if needed) and wait until it answers.
        REF defaults to $PRINTSTASH_IMAGE:$PRINTSTASH_VERSION as in docker-compose.yml;
        use --image printstash:local for an image built with `build`.
  down
        Stop and remove the container. The data volume is kept.
  logs [-f]
        Print the container's log, optionally following it.
  status
        Say whether it is running and with what image, port and memory.
  build [--variant full|lite]
        Build the image on Apple container as printstash:local.
EOF
  exit 2
}

die() {
  echo "printstash.sh: $*" >&2
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

# --- system ----------------------------------------------------------------

system_running() {
  local rc=0
  "$TIMEOUT_BIN" 30 container system status >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 124 ]; then
    wedge
  fi
  [ "$rc" -eq 0 ]
}

ensure_system() {
  if system_running; then
    return 0
  fi
  echo "starting the container apiserver (up to 10 minutes the first time)..." >&2
  local rc=0
  "$TIMEOUT_BIN" 600 container system start --enable-kernel-install </dev/null || rc=$?
  if [ "$rc" -eq 124 ]; then
    wedge
  fi
  if [ "$rc" -ne 0 ]; then
    die "container system start failed (exit $rc)"
  fi
}

# --- container state ---------------------------------------------------------

INSPECT=''

load_inspect() {
  INSPECT=$(container inspect "$NAME" 2>/dev/null) || INSPECT=''
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

same_spec() {
  [ "$(label_value printstash.image)" = "$1" ] &&
    [ "$(label_value printstash.port)" = "$2" ] &&
    [ "$(label_value printstash.memory)" = "$3" ]
}

# --- commands ----------------------------------------------------------------

port_taken() {
  nc -z 127.0.0.1 "$1" >/dev/null 2>&1 || nc -z ::1 "$1" >/dev/null 2>&1
}

wait_ready() {
  local port=$1 waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    load_inspect
    if [ "$(container_state)" != running ]; then
      echo "the container exited before it answered. Last log lines:" >&2
      container logs -n 40 "$NAME" >&2 || true
      exit 1
    fi
    if curl -fsS -o /dev/null -m 3 "http://127.0.0.1:$port/api/v1/health" 2>/dev/null &&
      curl -fsS -o /dev/null -m 3 "http://127.0.0.1:$port/" 2>/dev/null; then
      echo "PrintStash is running at http://127.0.0.1:$port"
      return 0
    fi
    sleep "$POLL_SECONDS"
    waited=$((waited + 1))
  done
  echo "PrintStash did not answer within ${HEALTH_TIMEOUT}s. Last log lines:" >&2
  container logs -n 40 "$NAME" >&2 || true
  exit 1
}

run_container() {
  local image=$1 port=$2
  if port_taken "$port"; then
    die "port $port is already in use on 127.0.0.1 or ::1; pick another with --port"
  fi
  # No VAULT_RESTART_ENABLED: nothing relaunches this container, so a Settings
  # restart would stop it for good.
  container run --detach --progress none \
    --name "$NAME" --memory "$MEMORY" \
    -p "127.0.0.1:$port:3000" \
    -e VAULT_SETUP_MODE=trusted_network \
    -v "$VOLUME:/data" \
    --label "printstash.image=$image" \
    --label "printstash.port=$port" \
    --label "printstash.memory=$MEMORY" \
    "$image" >/dev/null
  wait_ready "$port"
}

remove_container() {
  load_inspect
  case "$(container_state)" in
    absent) return 0 ;;
    running) container stop --time "$GRACE_SECONDS" "$NAME" >/dev/null ;;
  esac
  container delete "$NAME" >/dev/null
}

cmd_up() {
  local image=$DEFAULT_IMAGE port=$DEFAULT_PORT recreate=false pull=false
  while [ $# -gt 0 ]; do
    case "$1" in
      --image)
        [ $# -ge 2 ] || bad_usage "--image needs a value"
        image=$2
        shift 2
        ;;
      --port)
        [ $# -ge 2 ] || bad_usage "--port needs a value"
        valid_port "$2" || bad_usage "invalid port: $2"
        port=$2
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
  [ -n "$image" ] || bad_usage "empty image reference"
  valid_port "$port" || bad_usage "invalid port: $port"

  ensure_system
  load_inspect
  case "$(container_state)" in
    absent)
      if [ "$pull" = true ]; then container image pull --progress none "$image"; fi
      run_container "$image" "$port"
      ;;
    running)
      if same_spec "$image" "$port" "$MEMORY"; then
        echo "PrintStash is already running at http://127.0.0.1:$port"
        return 0
      fi
      if [ "$recreate" != true ]; then
        die "$NAME is running with a different image, port or memory size. \
Use --recreate to replace it (the data volume is kept)."
      fi
      remove_container
      if [ "$pull" = true ]; then container image pull --progress none "$image"; fi
      run_container "$image" "$port"
      ;;
    stopped)
      if same_spec "$image" "$port" "$MEMORY"; then
        container start "$NAME" >/dev/null
        wait_ready "$port"
        return 0
      fi
      echo "the stopped container has a different spec; its last log lines:" >&2
      container logs -n 20 "$NAME" >&2 || true
      remove_container
      if [ "$pull" = true ]; then container image pull --progress none "$image"; fi
      run_container "$image" "$port"
      ;;
    *) die "unexpected container state: $(container_state)" ;;
  esac
}

cmd_down() {
  [ $# -eq 0 ] || bad_usage "down takes no arguments"
  if ! system_running; then
    echo "the container apiserver is not running; nothing to stop"
    return 0
  fi
  load_inspect
  if [ "$(container_state)" = absent ]; then
    echo "$NAME is not running"
    return 0
  fi
  remove_container
  echo "$NAME removed; the $VOLUME volume is kept"
}

cmd_logs() {
  local follow=()
  case "${1:-}" in
    '') ;;
    -f)
      follow=(--follow)
      shift
      ;;
    *) bad_usage "unknown option for logs: $1" ;;
  esac
  [ $# -eq 0 ] || bad_usage "unexpected argument: $1"
  system_running || die "the container apiserver is not running"
  container logs ${follow[@]+"${follow[@]}"} "$NAME"
}

cmd_status() {
  [ $# -eq 0 ] || bad_usage "status takes no arguments"
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
  fi
}

build_one() {
  local tag=$1
  shift
  echo "building $tag..." >&2
  if ! container build --cpus "$BUILD_CPUS" --memory "$BUILD_MEMORY" -t "$tag" "$@"; then
    echo "printstash.sh: building $tag failed on this machine's builder." >&2
    echo "Nothing was substituted. To run a published image instead:" >&2
    echo "  printstash.sh up --image ghcr.io/xiao-villamor/printstash:latest" >&2
    exit 1
  fi
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
  ensure_system
  # Every `container build` resizes the builder to its own --cpus/--memory, so the
  # same values go on all three, and the builder is not started separately.
  build_one api-image --build-arg "PRINTSTASH_VARIANT=$variant" "$REPO_ROOT/backend"
  build_one frontend-image "$REPO_ROOT/frontend"
  build_one "$LOCAL_IMAGE" "$REPO_ROOT/backend/unified"
  echo "built $LOCAL_IMAGE ($variant); run it with: printstash.sh up --image $LOCAL_IMAGE"
}

[ $# -ge 1 ] || usage
command=$1
shift
case "$command" in
  up) cmd_up "$@" ;;
  down) cmd_down "$@" ;;
  logs) cmd_logs "$@" ;;
  status) cmd_status "$@" ;;
  build) cmd_build "$@" ;;
  -h | --help | help) usage ;;
  *) bad_usage "unknown command: $command" ;;
esac
