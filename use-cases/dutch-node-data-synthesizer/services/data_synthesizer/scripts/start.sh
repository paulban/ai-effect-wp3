#!/usr/bin/env bash
#
# Start the synthetic grid service.
#
# The compose file lives at the use case's root, not beside this script: the
# build context is use-cases/ so the shared control-plane package can be copied
# into the image. This script used to name a docker-compose-all.yml in the
# service directory, which the split into per-use-case packages removed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USE_CASE_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# The service's result references are built from this. An internal Docker name
# here produces URLs the party that submitted the workflow cannot fetch, so the
# compose file refuses to start without it.
export NODE_PUBLIC_BASE_URL="${NODE_PUBLIC_BASE_URL:-http://localhost:8444}"

docker network create ai-effect-services >/dev/null 2>&1 || true

cd "$USE_CASE_DIR"
docker compose up -d --build

echo
echo "synthetic-data is up on the ai-effect-services network. It publishes no"
echo "host port; the orchestrator's workers reach it at synthetic-data:8080."
echo "Submit a workflow with services/data_synthesizer/run_workflow.sh."
