#!/usr/bin/env bash
#
# setup_interactiveai.sh — bring up the InteractiveAI stack the HAI session
# service drives (spec FR-22 to FR-25).
#
# Run once per machine (and again after recreating any InteractiveAI container).
# It follows the steps of InteractiveAI's reference local_setup.sh, without
# calling or copying it, against the unmodified submodule:
#
#   1. start the backend from config/dev/cab-standalone, with cab_recommendation's
#      agent pointed at hai-session-runner (RL_AGENT_API_URL). Images are built
#      from the submodule's source only when missing (or with --rebuild);
#   2. set the Keycloak realm's Frontend URL, restart the frontend, wait for auth;
#   3. load the OperatorFabric resources (resources/loadTestConf.sh);
#   4. start the PowerGrid simulator with simulator.override.yml, which mounts
#      the shared CONFIG.toml and the scenarios folder;
#   5. connect cab_recommendation, frontend and the simulator to ai-effect-services;
#   6. reload the frontend's nginx and verify the paths a session depends on.
#
# Why existing images are not rebuilt by default: the backend services
# bind-mount their source (e.g. backend/recommendation-service:/my_app), so they
# already run the submodule's current code. The running containers also write
# __pycache__ into those folders, which changes the build context and makes
# every rebuild miss the cache: cab_recommendation would re-download PyTorch and
# CUDA, several GB each time. Use --rebuild only after a submodule update that
# changes a Dockerfile or requirements.
#
# The backend is started with `docker compose --env-file runtime/interactiveai.env`
# rather than the submodule's docker-compose.sh: that script sources a local
# .secrets file after the caller's environment, so a developer's .secrets would
# silently point the agent back at Deep Expert. Only COGNITIVE_TOKEN is taken
# from .secrets.
#
# Usage:
#   scripts/setup_interactiveai.sh            # set up (keeps running containers and images)
#   scripts/setup_interactiveai.sh --rebuild  # also rebuild frontend + cab_recommendation images
#   scripts/setup_interactiveai.sh --clean    # tear the stack down first
#   scripts/setup_interactiveai.sh --wipe     # tear down AND drop its volumes
#   scripts/setup_interactiveai.sh -h
#
# Environment (all optional):
#   HAI_FRONTEND_PUBLIC_URL  Frontend URL as browsers reach it (http://localhost:3200).
#                            Also the Keycloak Frontend URL: tokens fail with 401 if
#                            this differs from the address participants use.
#   HAI_AGENT_TOKEN          Bearer token cab_recommendation sends to the agent
#                            endpoint; must match hai-session-runner's. Unset = open.
#   KC_ADMIN / KC_PW         Keycloak admin (admin / admin).

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
USE_CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INTERACTIVEAI_DIR="$USE_CASE_DIR/services/human_ai_interaction_testing/InteractiveAI"
BACKEND_DIR="$INTERACTIVEAI_DIR/config/dev/cab-standalone"
RESOURCES_DIR="$INTERACTIVEAI_DIR/resources"
SIMULATOR_DIR="$INTERACTIVEAI_DIR/usecases_examples/PowerGrid"
SIMULATOR_COMPOSE="$SIMULATOR_DIR/docker-compose.local.yml"   # the local config, port 5122
SIMULATOR_OVERRIDE="$USE_CASE_DIR/simulator.override.yml"

RUNTIME_DIR="$USE_CASE_DIR/runtime"
SIMULATOR_CONFIG="$RUNTIME_DIR/simulator/CONFIG.toml"
SHIPPED_SIMULATOR_CONFIG="$SIMULATOR_DIR/config/CONFIG.toml"
BACKEND_ENV_FILE="$RUNTIME_DIR/interactiveai.env"

SERVICES_NETWORK="ai-effect-services"
# Containers that must share the network with hai-session-runner (FR-24).
NETWORKED_CONTAINERS=(cab_recommendation frontend PowerGrid-simulator-app)

# The agent endpoint as cab_recommendation reaches it over ai-effect-services.
HAI_AGENT_URL="http://hai-session-runner:8080/api/v1/recommendation"
HAI_AGENT_NAME="HAI algorithm"

FRONTEND_URL="${HAI_FRONTEND_PUBLIC_URL:-http://localhost:3200}"
SIMULATOR_URL="http://localhost:5122"
KC_BASE="http://localhost:89/auth"      # Keycloak 16.x, legacy /auth base path
KC_REALM="dev"
KC_CLIENT="opfab-client"
KC_ADMIN="${KC_ADMIN:-admin}"
KC_PW="${KC_PW:-admin}"
TEST_ACCOUNT="powergrid_user"
TEST_PASSWORD="test"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$*" >&2; exit 1; }

require() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."; }

# Windows ships a python3 stub that only advertises the Store, so each
# candidate has to actually run.
find_python() {
  local candidate
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" >/dev/null 2>&1; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}

# Block until an HTTP endpoint answers with the wanted status, or time out.
wait_for_http() {
  local url="$1" want="${2:-200}" tries="${3:-90}" attempt=1 code
  while (( attempt <= tries )); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)"
    [[ "$code" == "$want" ]] && { printf '\n'; return 0; }
    printf '    waiting for %s (%s/%s, last=%s)\r' "$url" "$attempt" "$tries" "$code"
    sleep 2; (( attempt++ ))
  done
  printf '\n'; return 1
}

container_running() { docker ps --format '{{.Names}}' | grep -qx "$1"; }

backend_compose() {
  docker compose --project-directory "$BACKEND_DIR" -f "$BACKEND_DIR/docker-compose.yml" \
    --env-file "$BACKEND_ENV_FILE" "$@"
}

simulator_compose() {
  docker compose -f "$SIMULATOR_COMPOSE" -f "$SIMULATOR_OVERRIDE" "$@"
}

# The docker bridge gateway, as docker-compose.sh computes HOST_IP.
docker_host_ip() {
  if [[ -n "${HOST_IP:-}" ]]; then echo "$HOST_IP"; return; fi
  if [[ "$OSTYPE" == "linux-gnu"* ]] && command -v ip >/dev/null 2>&1; then
    ip -4 addr show scope global dev docker0 | grep inet | awk '{print $2}' | cut -d / -f 1
  else
    docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}'
  fi
}

# Read one variable from the submodule's .secrets without letting it set anything else.
read_secret() {
  local name="$1" secrets_file="$BACKEND_DIR/.secrets"
  [[ -f "$secrets_file" ]] || return 0
  ( set +u; source "$secrets_file" >/dev/null 2>&1; printf '%s' "${!name:-}" )
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

check_prerequisites() {
  log "Checking prerequisites"
  require docker; require curl
  docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is required."
  docker info >/dev/null 2>&1 || die "Docker is not running."
  PYTHON_BIN="$(find_python)" || die "A working Python 3 is required (for JSON handling)."
  [[ -f "$BACKEND_DIR/docker-compose.yml" ]] || die "InteractiveAI submodule missing. Run: git submodule update --init --recursive"
  ok "docker, docker compose, curl, $PYTHON_BIN present; submodule checked out"
}

prepare_runtime_files() {
  log "Preparing runtime files"
  docker network create "$SERVICES_NETWORK" >/dev/null 2>&1 || true
  ok "network $SERVICES_NETWORK present"

  # FR-23a: a single-file bind mount of a missing path makes Docker create a
  # directory there, which the simulator cannot read as its config.
  mkdir -p "$(dirname "$SIMULATOR_CONFIG")"
  if [[ -d "$SIMULATOR_CONFIG" ]]; then
    die "$SIMULATOR_CONFIG is a directory (left by an earlier mount). Remove it and re-run."
  fi
  if [[ ! -f "$SIMULATOR_CONFIG" ]]; then
    cp "$SHIPPED_SIMULATOR_CONFIG" "$SIMULATOR_CONFIG"
    ok "created $SIMULATOR_CONFIG from the simulator's shipped config"
  else
    ok "$SIMULATOR_CONFIG exists"
  fi

  # The same keys docker-compose.sh writes to its .env, with the agent fixed to
  # hai-session-runner. Kept in runtime/ so nothing is written into the submodule.
  local cognitive_token
  cognitive_token="$(read_secret COGNITIVE_TOKEN)"
  cat > "$BACKEND_ENV_FILE" <<EOF
USER_ID=$(id -u)
USER_GID=$(id -g)
CONFIG_PATH=./
SPRING_PROFILES_ACTIVE=docker
HOST_IP=$(docker_host_ip)
RL_AGENT_API_URL=$HAI_AGENT_URL
RL_AGENT_API_TOKEN=${HAI_AGENT_TOKEN:-}
RL_AGENT_NAME=$HAI_AGENT_NAME
RL_AGENT_NUM_RECO=
RL_AGENT_NUM_RECO_SHARED=
RL_AGENT_2_API_URL=
RL_AGENT_2_API_TOKEN=
RL_AGENT_2_NAME=
RL_AGENT_3_API_URL=
RL_AGENT_3_API_TOKEN=
RL_AGENT_3_NAME=
RL_AGENT_3_TIMEOUT=
VITE_POWERGRID_SIMU=/powergrid-simu
POWERGRID_SIMU_UPSTREAM=http://host.docker.internal:5122/
COGNITIVE_TOKEN=$cognitive_token
EOF
  ok "wrote $BACKEND_ENV_FILE (agent: $HAI_AGENT_URL)"
  [[ -n "${HAI_AGENT_TOKEN:-}" ]] || warn "HAI_AGENT_TOKEN unset: the agent endpoint is open on the Docker network"
}

teardown() {
  local volume_flag="${1:-}"
  log "Tearing down the InteractiveAI stack${volume_flag:+ and its volumes}"
  simulator_compose down $volume_flag 2>/dev/null || true
  backend_compose down $volume_flag 2>/dev/null || true
  ok "stack removed"
}

start_backend() {
  local rebuild="${1:-}"
  log "Step 1/6 — Starting the InteractiveAI backend"
  # docker-compose.sh creates this bundle store before starting; so do we.
  mkdir -p "$BACKEND_DIR/businessconfig-storage"
  # Builds only images that do not exist yet (see the header for why).
  backend_compose up -d
  ok "backend compose up"

  if [[ -n "$rebuild" ]]; then
    log "Rebuilding frontend and cab_recommendation images (--rebuild; may re-download several GB)"
    backend_compose up -d --build --force-recreate frontend cabrecommendation
    ok "frontend, cab_recommendation rebuilt"
  fi
}

keycloak_admin_token() {
  curl -s --max-time 10 -X POST "$KC_BASE/realms/master/protocol/openid-connect/token" \
    -d client_id=admin-cli -d "username=$KC_ADMIN" -d "password=$KC_PW" -d grant_type=password \
    | "$PYTHON_BIN" -c 'import sys, json; print(json.load(sys.stdin).get("access_token", ""))' 2>/dev/null
}

# Without a matching Frontend URL, token issuers mismatch and auth returns 401.
configure_keycloak() {
  local admin_token realm updated_realm code
  admin_token="$(keycloak_admin_token)"
  [[ -n "$admin_token" ]] || { warn "could not obtain a Keycloak admin token"; return 1; }
  realm="$(curl -s --max-time 10 -H "Authorization: Bearer $admin_token" "$KC_BASE/admin/realms/$KC_REALM")"
  updated_realm="$(printf '%s' "$realm" | FRONTEND_URL="$FRONTEND_URL" "$PYTHON_BIN" -c '
import json, os, sys
realm = json.load(sys.stdin)
attributes = realm.get("attributes") or {}
attributes["frontendUrl"] = os.environ["FRONTEND_URL"]
realm["attributes"] = attributes
print(json.dumps(realm))' 2>/dev/null)"
  [[ -n "$updated_realm" ]] || { warn "could not read the '$KC_REALM' realm"; return 1; }
  code="$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$KC_BASE/admin/realms/$KC_REALM" \
    -H "Authorization: Bearer $admin_token" -H "Content-Type: application/json" -d "$updated_realm")"
  [[ "$code" == 2* ]] || { warn "realm update returned HTTP $code"; return 1; }
  ok "realm '$KC_REALM' Frontend URL set to $FRONTEND_URL"
}

keycloak_manual_fallback() {
  cat <<EOF

  Automatic Keycloak configuration did not complete. Configure it by hand:
    1. Open $KC_BASE/admin  (login: $KC_ADMIN / $KC_PW)
    2. Realm '$KC_REALM' -> Realm Settings -> General -> Frontend URL = $FRONTEND_URL -> Save
    3. Clients -> '$KC_CLIENT' -> Valid Redirect URIs must include ${FRONTEND_URL%/}/* -> Save
EOF
  if [[ -t 0 ]]; then
    read -r -p "  Press ENTER once Keycloak is configured... " _
  else
    warn "non-interactive shell: continuing; auth may fail until Keycloak is configured"
  fi
}

setup_authentication() {
  log "Step 2/6 — Configuring Keycloak"
  wait_for_http "$KC_BASE/realms/master" 200 || die "Keycloak did not come up on :89"
  configure_keycloak || keycloak_manual_fallback
  docker restart frontend >/dev/null && ok "frontend restarted to pick up the Keycloak change"
  wait_for_http "$FRONTEND_URL/" 200 || warn "frontend not answering 200 yet (continuing)"

  local attempt=1 token_response
  while true; do
    token_response="$(curl -s --max-time 5 -X POST \
      -d "username=$TEST_ACCOUNT&password=$TEST_PASSWORD&grant_type=password&client_id=$KC_CLIENT" \
      "$FRONTEND_URL/auth/token" || true)"
    [[ "$token_response" == *access_token* ]] && break
    (( attempt > 24 )) && die "the test account cannot log in at $FRONTEND_URL/auth/token"
    printf '    waiting for auth to be ready (%s/24)\r' "$attempt"; sleep 5; (( attempt++ ))
  done
  printf '\n'; ok "test account $TEST_ACCOUNT can log in"
}

load_resources() {
  log "Step 3/6 — Loading OperatorFabric resources and registering use cases"
  ( cd "$RESOURCES_DIR" && ./loadTestConf.sh )
  ok "resources loaded"
}

start_simulator() {
  local rebuild="${1:-}"
  log "Step 4/6 — Starting the PowerGrid simulator (simulator.override.yml, simulator.Dockerfile)"
  # Built only when missing or with --rebuild, like the backend: the container
  # runs its bind-mounted source, and the image holds several GB of PyTorch.
  # Recreated every time so the override's mounts are always applied.
  if [[ -n "$rebuild" ]]; then
    simulator_compose up -d --build --force-recreate app
  else
    simulator_compose up -d --force-recreate app
  fi
  ok "simulator started; CONFIG.toml and scenarios mounted"
}

connect_network() {
  log "Step 5/6 — Connecting InteractiveAI containers to $SERVICES_NETWORK"
  local container output
  for container in "${NETWORKED_CONTAINERS[@]}"; do
    container_running "$container" || die "$container is not running"
    if output="$(docker network connect "$SERVICES_NETWORK" "$container" 2>&1)"; then
      ok "$container connected"
    elif [[ "$output" == *"already exists"* || "$output" == *"already connected"* ]]; then
      ok "$container already connected"
    else
      die "could not connect $container: $output"
    fi
  done
}

# nginx caches upstream IPs at config load; a recreated simulator would 502
# behind /powergrid-simu/ until it reloads.
reload_frontend_nginx() {
  if docker exec frontend nginx -c /personal-conf/nginx.conf -s reload >/dev/null 2>&1 \
     || docker exec frontend nginx -s reload >/dev/null 2>&1; then
    ok "frontend nginx reloaded"
  else
    warn "nginx reload failed; restarting the frontend container"
    docker restart frontend >/dev/null && wait_for_http "$FRONTEND_URL/" 200 >/dev/null || true
  fi
}

verify() {
  log "Step 6/6 — Reloading the gateway and verifying"
  reload_frontend_nginx

  if wait_for_http "$FRONTEND_URL/cabrecommendation/api/v1/health" 200 15 >/dev/null; then
    ok "gateway reaches cab_recommendation"
  else
    warn "gateway route $FRONTEND_URL/cabrecommendation/ is not answering 200"
  fi

  if docker exec cab_recommendation python -c "
import urllib.request
urllib.request.urlopen('http://hai-session-runner:8080/api/v1/health', timeout=5)
" >/dev/null 2>&1; then
    ok "cab_recommendation reaches hai-session-runner"
  else
    warn "cab_recommendation cannot reach hai-session-runner yet."
    warn "  Start it:  cd $USE_CASE_DIR && docker compose up -d --build"
  fi

  if wait_for_http "$SIMULATOR_URL/" 200 15 >/dev/null; then
    ok "simulator answers on $SIMULATOR_URL"
  else
    warn "simulator not answering on $SIMULATOR_URL"
  fi
}

print_summary() {
  local commit
  commit="$(git -C "$INTERACTIVEAI_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '\n\033[1;32mInteractiveAI is set up for HAI sessions.\033[0m\n\n'
  printf '  InteractiveAI UI     %s   (%s / %s)\n' "$FRONTEND_URL" "$TEST_ACCOUNT" "$TEST_PASSWORD"
  printf '  PowerGrid simulator  %s   (%s / %s, server http://host.docker.internal:3200/)\n' \
    "$SIMULATOR_URL" "$TEST_ACCOUNT" "$TEST_PASSWORD"
  printf '  Keycloak admin       %s/admin   (%s / %s)\n' "$KC_BASE" "$KC_ADMIN" "$KC_PW"
  printf '\n  Record the InteractiveAI version in session results:\n'
  printf '    export HAI_INTERACTIVEAI_COMMIT=%s\n' "$commit"
  printf '  Then start the session service:\n'
  printf '    cd %s && docker compose up -d --build\n' "$USE_CASE_DIR"
  printf '\n  Re-run this script after recreating frontend, cab_recommendation or the\n'
  printf '  simulator: their %s connection does not survive a recreate.\n\n' "$SERVICES_NETWORK"
}

main() {
  local clean_mode="" rebuild_mode=""
  while (( $# )); do
    case "$1" in
      --rebuild) rebuild_mode="rebuild" ;;
      --clean)   clean_mode="clean" ;;
      --wipe)    clean_mode="wipe" ;;
      -h|--help) sed -n '3,48p' "${BASH_SOURCE[0]}"; exit 0 ;;
      *)         die "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done

  check_prerequisites
  prepare_runtime_files
  case "$clean_mode" in
    clean) teardown "" ;;
    wipe)  teardown "-v" ;;
  esac
  start_backend "$rebuild_mode"
  setup_authentication
  load_resources
  start_simulator "$rebuild_mode"
  connect_network
  verify
  print_summary
}

main "$@"
