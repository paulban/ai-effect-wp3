#!/usr/bin/env bash
#
# cleanup_interactiveai.sh — stop and remove everything the HAI setup starts,
# including every image it built or pulled.
#
# Removes, using the same compose files and overrides as setup_interactiveai.sh:
#   1. hai-session-runner          (this use case's docker-compose.yml)
#   2. the PowerGrid simulator     (docker-compose.local.yml + simulator.override.yml)
#   3. the InteractiveAI backend   (cab-standalone/docker-compose.yml + keycloak.override.yml):
#      cab_* services, frontend, Keycloak, OperatorFabric, Postgres, MongoDB, RabbitMQ
#
# For each stack: `docker compose down --rmi all --remove-orphans`, so containers
# and the stack's networks go, and so does every image its services use, built
# (cab/*, powergrid-simulator-app, the hai-session-runner image) or pulled
# (postgres, mongo, rabbitmq, keycloak, OperatorFabric). Docker keeps an image
# that a container outside these stacks still uses, and compose reports it.
#
# Not touched:
#   - containers and images of anything else on this Docker host;
#   - the ai-effect-services network (the orchestrator is attached to it too);
#   - named volumes (the Postgres databases, pgadmin, and hai_artifacts with
#     session results) unless --volumes is given;
#   - runtime/ files and the Docker build cache (`docker builder prune` clears it,
#     but that is host-wide).
#
# The next setup_interactiveai.sh run rebuilds and re-pulls everything; the
# cab_recommendation image alone re-downloads PyTorch and CUDA (several GB).
#
# Usage:
#   scripts/cleanup_interactiveai.sh             # asks for confirmation
#   scripts/cleanup_interactiveai.sh --volumes   # also drop the stacks' named volumes
#   scripts/cleanup_interactiveai.sh -y          # no confirmation (non-interactive use)
#   scripts/cleanup_interactiveai.sh -h

set -euo pipefail

USE_CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INTERACTIVEAI_DIR="$USE_CASE_DIR/services/human_ai_interaction_testing/InteractiveAI"
BACKEND_DIR="$INTERACTIVEAI_DIR/config/dev/cab-standalone"
SIMULATOR_COMPOSE="$INTERACTIVEAI_DIR/usecases_examples/PowerGrid/docker-compose.local.yml"
SIMULATOR_OVERRIDE="$USE_CASE_DIR/simulator.override.yml"
KEYCLOAK_OVERRIDE="$USE_CASE_DIR/keycloak.override.yml"
BACKEND_ENV_FILE="$USE_CASE_DIR/runtime/interactiveai.env"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$*" >&2; exit 1; }

failed_stacks=()

# Runs `down` for one stack; a failure is reported at the end, not fatal, so
# one broken stack does not leave the others running.
down_stack() {
  local name="$1"; shift
  log "Removing $name"
  if docker compose "$@" down --rmi all --remove-orphans $volume_flag; then
    ok "$name removed"
  else
    warn "$name: docker compose down failed (see above)"
    failed_stacks+=("$name")
  fi
}

main() {
  local assume_yes="" answer backend_env
  volume_flag=""
  while (( $# )); do
    case "$1" in
      --volumes) volume_flag="--volumes" ;;
      -y|--yes)  assume_yes="yes" ;;
      -h|--help) sed -n '3,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
      *)         die "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done

  docker compose version >/dev/null 2>&1 || die "'docker compose' (v2 or later) is required."
  docker info >/dev/null 2>&1 || die "Docker is not running."
  [[ -f "$BACKEND_DIR/docker-compose.yml" ]] \
    || die "InteractiveAI submodule missing. Run: git submodule update --init --recursive"

  if [[ -z "$assume_yes" ]]; then
    [[ -t 0 ]] || die "non-interactive shell: pass -y to confirm"
    printf 'This stops the HAI / InteractiveAI containers and deletes all their images%s.\n' \
      "${volume_flag:+ AND volumes (databases, session results)}"
    read -r -p 'Continue? [y/N] ' answer
    [[ "$answer" == [yY]* ]] || { echo "Aborted."; exit 1; }
  fi

  # The backend file interpolates ${CONFIG_PATH} into a volume spec, which compose
  # rejects when empty, even for `down`. Without the env file setup writes, use a
  # minimal one.
  if [[ -f "$BACKEND_ENV_FILE" ]]; then
    backend_env="$BACKEND_ENV_FILE"
  else
    backend_env="$(mktemp)"
    trap 'rm -f "$backend_env"' EXIT
    printf 'CONFIG_PATH=./\nUSER_ID=%s\nUSER_GID=%s\n' "$(id -u)" "$(id -g)" > "$backend_env"
  fi

  down_stack "hai-session-runner" -f "$USE_CASE_DIR/docker-compose.yml"
  down_stack "PowerGrid simulator" -f "$SIMULATOR_COMPOSE" -f "$SIMULATOR_OVERRIDE"
  down_stack "InteractiveAI backend" --project-directory "$BACKEND_DIR" \
    -f "$BACKEND_DIR/docker-compose.yml" -f "$KEYCLOAK_OVERRIDE" --env-file "$backend_env"

  if (( ${#failed_stacks[@]} )); then
    die "not fully removed: ${failed_stacks[*]}"
  fi
  printf '\n\033[1;32mHAI / InteractiveAI containers and images removed.\033[0m\n'
  printf '  Set up again with:  scripts/setup_interactiveai.sh\n\n'
}

main "$@"
